"""Tests del ciclo de vida de las conversaciones de la pasarela (sin LLM ni servidor MCP reales).

    cd mcp-client
    python -m unittest discover -s tests -p "test_*.py"
"""

from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path
from typing import Any, cast
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from starlette.testclient import TestClient  # noqa: E402

import cliente  # noqa: E402
import observabilidad  # noqa: E402
import pasarela  # noqa: E402
from test_observabilidad import FakeLLM, FakeSession, TempDirTest, make_settings  # noqa: E402


class GatewayConversationTest(TempDirTest):
    def setUp(self) -> None:
        super().setUp()
        gw = pasarela.GatewaySettings(host="127.0.0.1", port=0, api_key="", idle_minutes=60,
                                      max_conversations=10, max_message_chars=1000)
        self.store = observabilidad.EventStore(self.tmp / "o.db")
        with mock.patch.object(cliente, "build_chat_model", return_value=FakeLLM()):
            self.gateway = pasarela.Gateway(make_settings(self.tmp), gw, self.store)
        self.gateway.session = cast(Any, FakeSession())
        self.client = TestClient(pasarela.build_app(self.gateway))

    def tearDown(self) -> None:
        self.store.close()
        super().tearDown()

    def _ask(self, conversation: str = "conv1") -> dict:
        resp = self.client.post("/chat", json={"conversation": conversation, "user": "Admin",
                                               "message": "¿hosts?"})
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def _recorded_conversations(self) -> list[str]:
        conn = sqlite3.connect(self.store.path)
        try:
            return [row[0] for row in conn.execute("SELECT conversation FROM turns ORDER BY id")]
        finally:
            conn.close()

    def test_new_conversation_flag(self) -> None:
        self.assertTrue(self._ask()["new_conversation"])
        self.assertFalse(self._ask()["new_conversation"])

    def test_reset_starts_a_new_observable_conversation(self) -> None:
        self._ask()
        self._ask()
        self.assertEqual(self.client.post("/reset", json={"conversation": "conv1"}).json(), {"ok": True})
        self.assertTrue(self._ask()["new_conversation"])

        first, second, third = self._recorded_conversations()
        self.assertEqual(first, second)
        self.assertNotEqual(second, third)
        self.assertTrue(first.startswith("conv1#") and third.startswith("conv1#"))

    def test_expired_conversation_is_reported_as_new(self) -> None:
        self._ask()
        self.gateway.conversations["conv1"].last_used -= 61 * 60
        self.assertTrue(self._ask()["new_conversation"])
        self.assertEqual(len(set(self._recorded_conversations())), 2)

    def test_reset_refused_while_question_in_progress(self) -> None:
        conv = self.gateway.get("conv1", "Admin")
        with mock.patch.object(conv.lock, "locked", return_value=True):
            resp = self.client.post("/reset", json={"conversation": "conv1"})
        self.assertEqual(resp.status_code, 409)
        self.assertIs(self.gateway.conversations.get("conv1"), conv)

    def test_reset_unknown_conversation_is_ok(self) -> None:
        resp = self.client.post("/reset", json={"conversation": "nunca-usada"})
        self.assertEqual(resp.status_code, 200)


if __name__ == "__main__":
    unittest.main()
