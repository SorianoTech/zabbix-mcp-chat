"""Pasarela HTTP entre el módulo de chat de Zabbix y el LLM + MCP.

El módulo PHP de Zabbix (../zabbix-module-mcpchat) llama a esta pasarela desde el servidor web;
el navegador nunca la ve. Reutiliza ZabbixChat de cliente.py (mismo .env, mismo logging y
mismas opciones de reducción de tokens) y mantiene una conversación por usuario de Zabbix.

Uso:
    python pasarela.py

Endpoints (todos exigen "Authorization: Bearer <GATEWAY_API_KEY>" si la clave está definida):
    GET  /health                                    -> estado y nº de conversaciones
    POST /chat   {"conversation", "user", "message", "zabbix_auth"}
                                                    -> {"answer", "tools_used", "usage"}
    POST /reset  {"conversation"}                    -> {"ok": true}

"zabbix_auth" es la credencial de Zabbix del usuario (su sesión del frontend). Se reenvía al
servidor MCP en el _meta de cada llamada a herramienta para que Zabbix aplique SUS permisos.
No se guarda (ni en memoria entre peticiones ni en los logs) y nunca llega al LLM.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import re
import sys
import time
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

import cliente

log = logging.getLogger("pasarela")

_CONVERSATION_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_CREDENTIAL_RE = re.compile(r"^[A-Za-z0-9]{16,128}$")   # sesión (32 hex) o token de API (64 hex)


@dataclass
class GatewaySettings:
    host: str
    port: int
    api_key: str
    idle_minutes: int
    max_conversations: int
    max_message_chars: int

    @classmethod
    def from_env(cls) -> "GatewaySettings":
        """Llamar después de cliente.Settings.from_env(), que carga el .env."""
        return cls(
            host=os.getenv("GATEWAY_HOST", "127.0.0.1"),
            port=int(os.getenv("GATEWAY_PORT", "8770")),
            api_key=os.getenv("GATEWAY_API_KEY", ""),
            idle_minutes=int(os.getenv("GATEWAY_IDLE_MINUTES", "60")),
            max_conversations=int(os.getenv("GATEWAY_MAX_CONVERSATIONS", "200")),
            max_message_chars=int(os.getenv("GATEWAY_MAX_MESSAGE_CHARS", "4000")),
        )


@dataclass
class Conversation:
    chat: cliente.ZabbixChat
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_used: float = field(default_factory=time.monotonic)


class Gateway:
    def __init__(self, settings: cliente.Settings, gw: GatewaySettings) -> None:
        self.s = settings
        self.gw = gw
        self.session = None
        self.tools: list[dict] = []
        self.instructions: str | None = None
        self.llm = cliente.build_chat_model(settings)   # un modelo compartido por todas las conversaciones
        self.conversations: dict[str, Conversation] = {}

    async def start(self, stack: AsyncExitStack) -> None:
        self.session, self.instructions = await cliente.connect_mcp(stack, self.s)
        mcp_tools = await cliente.list_all_tools(self.session)
        self.tools = cliente.select_tools(mcp_tools, self.s)
        log.info("Pasarela lista: %d herramientas enviadas al LLM (~%d tokens) de %d disponibles",
                 len(self.tools), cliente.est_tokens(self.tools) if self.tools else 0, len(mcp_tools))

    def _expire(self) -> None:
        limit = time.monotonic() - self.gw.idle_minutes * 60
        for cid in [c for c, conv in self.conversations.items()
                    if conv.last_used < limit and not conv.lock.locked()]:
            del self.conversations[cid]
            log.info("Conversación %s caducada por inactividad", cid)

    def get(self, conversation_id: str, user: str) -> Conversation:
        self._expire()
        conv = self.conversations.get(conversation_id)
        if conv is None:
            if len(self.conversations) >= self.gw.max_conversations:
                oldest = min(self.conversations, key=lambda c: self.conversations[c].last_used)
                del self.conversations[oldest]
            label = f"{re.sub(r'[^A-Za-z0-9_.-]', '_', user)[:40]}-{conversation_id[:8]}"
            chat = cliente.ZabbixChat(self.s, self.session, self.tools, self.instructions,
                                      llm=self.llm, session_label=label)
            conv = self.conversations[conversation_id] = Conversation(chat)
            log.info("Nueva conversación %s (usuario %s)", conversation_id[:8], user)
        conv.last_used = time.monotonic()
        return conv


def _authorized(request: Request, gw: GatewaySettings) -> bool:
    if not gw.api_key:
        return True
    header = request.headers.get("authorization", "")
    return hmac.compare_digest(header.encode(), f"Bearer {gw.api_key}".encode())


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


async def _json_body(request: Request) -> dict[str, Any] | None:
    try:
        body = await request.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None


def build_app(gateway: Gateway) -> Starlette:
    gw = gateway.gw

    async def health(request: Request) -> JSONResponse:
        if not _authorized(request, gw):
            return _error(401, "No autorizado")
        return JSONResponse({"status": "ok", "tools": len(gateway.tools),
                             "conversations": len(gateway.conversations)})

    async def chat(request: Request) -> JSONResponse:
        if not _authorized(request, gw):
            return _error(401, "No autorizado")
        body = await _json_body(request)
        if body is None:
            return _error(400, "Se esperaba un objeto JSON")
        conversation_id = str(body.get("conversation", ""))
        message = str(body.get("message", "")).strip()
        user = str(body.get("user", "desconocido"))
        zabbix_auth = body.get("zabbix_auth") or None
        if zabbix_auth is not None and (not isinstance(zabbix_auth, str) or not _CREDENTIAL_RE.match(zabbix_auth)):
            return _error(400, "Credencial de Zabbix no válida")
        if not _CONVERSATION_RE.match(conversation_id):
            return _error(400, "Identificador de conversación no válido")
        if not message:
            return _error(400, "Mensaje vacío")
        if len(message) > gw.max_message_chars:
            return _error(400, f"Mensaje demasiado largo (máx. {gw.max_message_chars} caracteres)")

        conv = gateway.get(conversation_id, user)
        if conv.lock.locked():
            return _error(409, "Ya hay una pregunta en curso en esta conversación")
        async with conv.lock:
            before = dict(conv.chat.totals)
            log.info("[%s] %s%s: %s", conversation_id[:8], user,
                     " (con sus permisos de Zabbix)" if zabbix_auth else " (credencial de servicio del MCP)",
                     message[:200])
            try:
                answer = await conv.chat.ask(message, zabbix_auth=zabbix_auth)
            except Exception as exc:
                log.exception("Error procesando la pregunta de %s", user)
                # Quita del historial la pregunta fallida para no arrastrar un estado roto.
                conv.chat.discard_last_turn()
                return _error(502, f"Error consultando el LLM o el MCP: {type(exc).__name__}")
            conv.last_used = time.monotonic()
            usage = {k: conv.chat.totals[k] - before[k] for k in conv.chat.totals}
        return JSONResponse({"answer": answer, "tools_used": conv.chat.last_tools_used, "usage": usage})

    async def reset(request: Request) -> JSONResponse:
        if not _authorized(request, gw):
            return _error(401, "No autorizado")
        body = await _json_body(request) or {}
        conversation_id = str(body.get("conversation", ""))
        if not _CONVERSATION_RE.match(conversation_id):
            return _error(400, "Identificador de conversación no válido")
        gateway.conversations.pop(conversation_id, None)
        return JSONResponse({"ok": True})

    @asynccontextmanager
    async def lifespan(app: Starlette):
        async with AsyncExitStack() as stack:
            await gateway.start(stack)
            yield

    return Starlette(routes=[
        Route("/health", health, methods=["GET"]),
        Route("/chat", chat, methods=["POST"]),
        Route("/reset", reset, methods=["POST"]),
    ], lifespan=lifespan)


def main() -> None:
    settings = cliente.Settings.from_env()   # carga .env (también las variables GATEWAY_*)
    cliente.setup_logging(settings)
    gw = GatewaySettings.from_env()
    if not gw.api_key:
        log.warning("GATEWAY_API_KEY vacía: cualquiera que alcance %s:%d puede usar la pasarela",
                    gw.host, gw.port)
    app = build_app(Gateway(settings, gw))
    uvicorn.run(app, host=gw.host, port=gw.port, log_level="warning")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    main()
