"""Tests de la observabilidad (sin LLM ni servidor MCP reales).

    cd mcp-client
    python -m unittest discover -s tests -p "test_*.py"
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langchain_core.messages import AIMessage  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

import cliente  # noqa: E402
import observabilidad  # noqa: E402
import pasarela  # noqa: E402

SECRET = "a" * 32   # credencial de Zabbix ficticia: nunca debe acabar en la BD
logging.disable(logging.CRITICAL)   # los errores simulados se registran con log.exception


def make_settings(tmp: Path) -> cliente.Settings:
    return cliente.Settings(
        provider="fake", model="fake-model", base_url="", api_key="", temperature=None, max_tokens=None,
        llm_kwargs={}, mcp_transport="http", mcp_url="", mcp_token="", mcp_stdio_command="",
        include_server_instructions=False, tool_result_max_chars=50, log_dir=tmp, log_llm_payloads=False)


class FakeLLM:
    """Primero pide una herramienta y luego responde (o falla si fail=True)."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def bind_tools(self, tools: list[dict]) -> "FakeLLM":
        return self

    async def ainvoke(self, messages: list[Any]) -> AIMessage:
        self.calls += 1
        if self.fail:
            raise RuntimeError("LLM caído")
        usage = {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110,
                 "input_token_details": {"cache_read": 40}}
        if self.calls % 2 == 1:
            return AIMessage("", tool_calls=[{"name": "host_get", "args": {"limit": 1}, "id": "c1"}],
                             usage_metadata=usage)
        return AIMessage("Hay 1 host.", usage_metadata=usage)


class FakeSession:
    def __init__(self, error: bool = False) -> None:
        self.error = error
        self.metas: list[Any] = []

    async def call_tool(self, name: str, args: dict, meta: dict | None = None) -> Any:
        self.metas.append(meta)
        text = SimpleNamespace(type="text", text='[{"hostid": "1"}]' + "x" * 100)
        return SimpleNamespace(content=[text], is_error=self.error)


class TempDirTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)


class EventStoreTest(TempDirTest):
    def test_record_turn_and_retention(self) -> None:
        store = observabilidad.EventStore(self.tmp / "o.db", tool_result_max_chars=10, retention_days=30)
        old = (datetime.now() - timedelta(days=40)).isoformat()
        events = [
            {"kind": "llm", "n": 1, "ts": old, "elapsed_s": 0.5, "prompt_tokens": 100,
             "completion_tokens": 5, "cached_tokens": 0, "est_total": 90, "est_tools": 50, "n_messages": 2},
            {"kind": "tool", "name": "host_get", "args": {"limit": 1}, "ts": old, "elapsed_s": 0.1,
             "result_chars": 500, "truncated": True, "is_error": False, "result": "y" * 500},
        ]
        common = dict(conversation="c1", user="Admin", delegated=True, elapsed_s=1.0, question="¿hosts?",
                      answer="1", status="ok", error=None, provider="p", model="m",
                      usage={"prompt_tokens": 100, "completion_tokens": 5, "cached_tokens": 0}, events=events)
        store.record_turn(ts_start=old, ts_end=old, **common)
        now = datetime.now().isoformat()
        turn_id = store.record_turn(ts_start=now, ts_end=now, **common)

        conn = sqlite3.connect(store.path)
        row = conn.execute("SELECT llm_requests, n_tools, delegated FROM turns WHERE id=?", (turn_id,)).fetchone()
        self.assertEqual(row, (1, 1, 1))
        result = conn.execute("SELECT result FROM tool_calls WHERE turn_id=?", (turn_id,)).fetchone()[0]
        self.assertTrue(result.startswith("y" * 10) and "recortado" in result)

        self.assertEqual(store.purge(), 1)
        self.assertEqual(conn.execute("SELECT count(*) FROM turns").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT count(*) FROM tool_calls").fetchone()[0], 1)   # cascada
        conn.close()
        store.close()


class ChatEventsTest(TempDirTest):
    def test_events_of_a_turn(self) -> None:
        session = FakeSession(error=True)
        chat = cliente.ZabbixChat(make_settings(self.tmp), session, [], None, llm=FakeLLM())
        answer = asyncio.run(chat.ask("¿hosts?", zabbix_auth=SECRET))
        self.assertEqual(answer, "Hay 1 host.")
        self.assertEqual(chat.last_status, "ok")
        kinds = [e["kind"] for e in chat.last_turn_events]
        self.assertEqual(kinds, ["llm", "tool", "llm"])
        tool = chat.last_turn_events[1]
        self.assertTrue(tool["is_error"])
        self.assertTrue(tool["truncated"])
        self.assertEqual(tool["result_chars"], len("ERROR de la herramienta: ") + 117)
        self.assertEqual(chat.last_turn_events[0]["cached_tokens"], 40)
        self.assertNotIn(SECRET, repr(chat.last_turn_events))
        self.assertEqual(session.metas[0], {cliente.DELEGATED_AUTH_META_KEY: SECRET})


class GatewayRecordTest(TempDirTest):
    def _client(self, llm: FakeLLM) -> tuple[TestClient, observabilidad.EventStore]:
        settings = make_settings(self.tmp)
        gw = pasarela.GatewaySettings(host="127.0.0.1", port=0, api_key="", idle_minutes=60,
                                      max_conversations=10, max_message_chars=1000)
        store = observabilidad.EventStore(self.tmp / "o.db")
        original = cliente.build_chat_model
        cliente.build_chat_model = lambda s: llm
        try:
            gateway = pasarela.Gateway(settings, gw, store)
        finally:
            cliente.build_chat_model = original
        gateway.session = FakeSession()
        # Sin "with TestClient(...)" no se ejecuta el lifespan: no hay conexión MCP real.
        return TestClient(pasarela.build_app(gateway)), store

    def _turns(self, store: observabilidad.EventStore) -> list[tuple]:
        conn = sqlite3.connect(store.path)
        try:
            return conn.execute("SELECT user, status, question, answer, error, prompt_tokens, delegated,"
                                " n_tools FROM turns").fetchall()
        finally:
            conn.close()

    def _dump(self, store: observabilidad.EventStore) -> str:
        conn = sqlite3.connect(store.path)
        try:
            return "\n".join(line for line in conn.iterdump())
        finally:
            conn.close()

    def test_chat_ok_is_recorded(self) -> None:
        client, store = self._client(FakeLLM())
        resp = client.post("/chat", json={"conversation": "conv1", "user": "Admin",
                                          "message": "¿hosts?", "zabbix_auth": SECRET})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self._turns(store), [("Admin", "ok", "¿hosts?", "Hay 1 host.", None, 200, 1, 1)])
        self.assertNotIn(SECRET, self._dump(store))
        store.close()

    def test_chat_error_is_recorded(self) -> None:
        client, store = self._client(FakeLLM(fail=True))
        resp = client.post("/chat", json={"conversation": "conv2", "user": "Admin", "message": "hola"})
        self.assertEqual(resp.status_code, 502)
        (row,) = self._turns(store)
        self.assertEqual(row[:3], ("Admin", "error", "hola"))
        self.assertIn("LLM caído", row[4])
        self.assertEqual(row[6], 0)
        store.close()


if __name__ == "__main__":
    unittest.main()
