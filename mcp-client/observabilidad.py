"""Observabilidad de la pasarela: registro de cada pregunta en SQLite y trazas opcionales en Phoenix.

- EventStore guarda por cada turno (pregunta del usuario) la respuesta, el consumo de tokens y la
  línea temporal de llamadas al LLM y a herramientas. Lo lee panel.py (Streamlit) en tiempo real.
- setup_phoenix() activa, si PHOENIX_ENABLED=true, la instrumentación OpenInference de LangChain y
  envía las trazas a un servidor Arize Phoenix (ver requirements-phoenix.txt).

La credencial de Zabbix del usuario (zabbix_auth) nunca llega aquí: solo se guarda si la pregunta
se hizo con ella (columna "delegated").
"""

from __future__ import annotations

import contextlib
import importlib
import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from collections.abc import Generator
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
log = logging.getLogger("observabilidad")

SCHEMA = """
CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation TEXT NOT NULL,
    user TEXT NOT NULL,
    delegated INTEGER NOT NULL DEFAULT 0,
    ts_start TEXT NOT NULL,
    ts_end TEXT NOT NULL,
    elapsed_s REAL,
    question TEXT,
    answer TEXT,
    status TEXT NOT NULL,              -- ok | max_rounds | error
    error TEXT,
    provider TEXT,
    model TEXT,
    llm_requests INTEGER DEFAULT 0,
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    cached_tokens INTEGER DEFAULT 0,
    n_tools INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_turns_ts ON turns(ts_start);
CREATE INDEX IF NOT EXISTS idx_turns_user ON turns(user);
CREATE INDEX IF NOT EXISTS idx_turns_conv ON turns(conversation);

CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_id INTEGER NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
    n INTEGER,
    ts TEXT,
    elapsed_s REAL,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    cached_tokens INTEGER,
    est_total INTEGER,
    est_tools INTEGER,
    n_messages INTEGER
);
CREATE INDEX IF NOT EXISTS idx_llm_turn ON llm_calls(turn_id);

CREATE TABLE IF NOT EXISTS tool_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_id INTEGER NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
    ts TEXT,
    name TEXT NOT NULL,
    args TEXT,
    elapsed_s REAL,
    result_chars INTEGER,
    truncated INTEGER,
    is_error INTEGER,
    result TEXT
);
CREATE INDEX IF NOT EXISTS idx_tool_turn ON tool_calls(turn_id);
CREATE INDEX IF NOT EXISTS idx_tool_name ON tool_calls(name);
"""


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name, "").strip().lower()
    return default if not value else value in ("1", "true", "yes", "si", "sí")


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    return int(value) if value else default


def resolve_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else BASE_DIR / path


@dataclass
class ObsSettings:
    enabled: bool = True
    db_path: Path = BASE_DIR / "logs" / "observabilidad.db"
    tool_result_max_chars: int = 20000
    retention_days: int = 90
    phoenix_enabled: bool = False
    phoenix_endpoint: str = "http://127.0.0.1:6006"
    phoenix_project: str = "zabbix-chat"

    @classmethod
    def from_env(cls) -> "ObsSettings":
        """Llamar después de cargar el .env (cliente.Settings.from_env())."""
        return cls(
            enabled=_env_bool("OBS_ENABLED", True),
            db_path=resolve_path(os.getenv("OBS_DB", "logs/observabilidad.db")),
            tool_result_max_chars=_env_int("OBS_TOOL_RESULT_MAX_CHARS", 20000),
            retention_days=_env_int("OBS_RETENTION_DAYS", 90),
            phoenix_enabled=_env_bool("PHOENIX_ENABLED", False),
            phoenix_endpoint=os.getenv("PHOENIX_COLLECTOR_ENDPOINT", "http://127.0.0.1:6006").rstrip("/"),
            phoenix_project=os.getenv("PHOENIX_PROJECT", "zabbix-chat"),
        )


def _cut(text: str | None, limit: int) -> str | None:
    if text is None or limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + f"\n…[recortado para la observabilidad: {len(text)} caracteres en total]"


class EventStore:
    """Almacén SQLite (modo WAL: el panel puede leer mientras la pasarela escribe)."""

    PURGE_EVERY_S = 24 * 3600

    def __init__(self, path: Path, *, tool_result_max_chars: int = 20000, retention_days: int = 90) -> None:
        self.path = Path(path)
        self.tool_result_max_chars = tool_result_max_chars
        self.retention_days = retention_days
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self._last_purge = 0.0
        self.purge()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def purge(self) -> int:
        """Borra los turnos más antiguos que la retención (0 = no borrar nunca)."""
        self._last_purge = time.monotonic()
        if self.retention_days <= 0:
            return 0
        limit = (datetime.now() - timedelta(days=self.retention_days)).isoformat()
        with self._lock:
            cur = self._conn.execute("DELETE FROM turns WHERE ts_start < ?", (limit,))
            self._conn.commit()
        if cur.rowcount:
            log.info("Observabilidad: %d turnos anteriores a %s purgados", cur.rowcount, limit[:10])
        return cur.rowcount

    def record_turn(self, *, conversation: str, user: str, delegated: bool, ts_start: str, ts_end: str,
                    elapsed_s: float, question: str, answer: str | None, status: str,
                    error: str | None, provider: str, model: str, usage: dict[str, int],
                    events: list[dict[str, Any]]) -> int:
        """Guarda un turno y sus eventos. Devuelve el id del turno."""
        if time.monotonic() - self._last_purge > self.PURGE_EVERY_S:
            self.purge()
        llm = [e for e in events if e.get("kind") == "llm"]
        tools = [e for e in events if e.get("kind") == "tool"]
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO turns (conversation, user, delegated, ts_start, ts_end, elapsed_s, question,"
                " answer, status, error, provider, model, llm_requests, prompt_tokens, completion_tokens,"
                " cached_tokens, n_tools) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (conversation, user, int(delegated), ts_start, ts_end, round(elapsed_s, 3), question,
                 answer, status, error, provider, model, len(llm),
                 usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0),
                 usage.get("cached_tokens", 0), len(tools)))
            if cur.lastrowid is None:   # no ocurre tras un INSERT, pero lastrowid es Optional
                raise RuntimeError("SQLite no devolvió el id del turno insertado")
            turn_id = cur.lastrowid
            self._conn.executemany(
                "INSERT INTO llm_calls (turn_id, n, ts, elapsed_s, prompt_tokens, completion_tokens,"
                " cached_tokens, est_total, est_tools, n_messages) VALUES (?,?,?,?,?,?,?,?,?,?)",
                [(turn_id, e.get("n"), e.get("ts"), e.get("elapsed_s"), e.get("prompt_tokens"),
                  e.get("completion_tokens"), e.get("cached_tokens"), e.get("est_total"),
                  e.get("est_tools"), e.get("n_messages")) for e in llm])
            self._conn.executemany(
                "INSERT INTO tool_calls (turn_id, ts, name, args, elapsed_s, result_chars, truncated,"
                " is_error, result) VALUES (?,?,?,?,?,?,?,?,?)",
                [(turn_id, e.get("ts"), e.get("name"),
                  json.dumps(e.get("args") or {}, ensure_ascii=False, default=str), e.get("elapsed_s"),
                  e.get("result_chars"), int(bool(e.get("truncated"))), int(bool(e.get("is_error"))),
                  _cut(e.get("result"), self.tool_result_max_chars)) for e in tools])
        return turn_id


def open_store(obs: ObsSettings) -> EventStore | None:
    if not obs.enabled:
        return None
    store = EventStore(obs.db_path, tool_result_max_chars=obs.tool_result_max_chars,
                       retention_days=obs.retention_days)
    log.info("Observabilidad: registrando preguntas en %s", obs.db_path)
    return store


# ---------------------------------------------------------------------------
# Phoenix (OpenTelemetry + OpenInference), opcional
# ---------------------------------------------------------------------------

_phoenix_active = False


def setup_phoenix(obs: ObsSettings) -> bool:
    """Activa el envío de trazas a Phoenix. Llamar antes de crear el modelo de LangChain.

    Solo se instrumenta LangChain (llamadas al LLM y mensajes, incluidos los resultados de
    herramientas). No se usa el instrumentador MCP para no tocar el _meta de las llamadas,
    donde viaja la credencial delegada del usuario."""
    global _phoenix_active
    if not obs.phoenix_enabled:
        return False
    try:
        # Dependencias opcionales (requirements-phoenix.txt): pueden no estar instaladas.
        register = importlib.import_module("phoenix.otel").register
        LangChainInstrumentor = importlib.import_module("openinference.instrumentation.langchain").LangChainInstrumentor
    except ImportError as exc:
        log.warning("PHOENIX_ENABLED=true pero faltan paquetes (%s): pip install -r requirements-phoenix.txt",
                    exc)
        return False
    provider = register(project_name=obs.phoenix_project, endpoint=f"{obs.phoenix_endpoint}/v1/traces",
                        batch=True, set_global_tracer_provider=False)
    LangChainInstrumentor().instrument(tracer_provider=provider)
    _phoenix_active = True
    log.info("Phoenix: trazas de LangChain → %s (proyecto %s)", obs.phoenix_endpoint, obs.phoenix_project)
    return True


@contextlib.contextmanager
def trace_attributes(conversation: str, user: str) -> Generator[None, None, None]:
    """Etiqueta las trazas de Phoenix con la conversación (sesión) y el usuario de Zabbix."""
    if not _phoenix_active:
        yield
        return
    using_attributes = importlib.import_module("openinference.instrumentation").using_attributes
    with using_attributes(session_id=conversation, user_id=user):
        yield
