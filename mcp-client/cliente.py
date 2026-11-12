"""Cliente de chat sencillo: cualquier LLM (vía LangChain) + servidor MCP de Zabbix.

El modelo se elige con LLM_PROVIDER + LLM_MODEL (OpenAI, Anthropic, Ollama, Gemini,
Azure OpenAI, Mistral, Groq, Bedrock... o cualquier endpoint compatible con OpenAI).

Uso:
    python cliente.py                         # chat interactivo
    python cliente.py -p "¿Qué problemas hay ahora?"   # una sola pregunta
    python cliente.py --herramientas          # mide el catálogo de herramientas y sale

Toda la configuración se lee de .env (ver .env.example).
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import fnmatch
import json
import logging
import os
import shlex
import sys
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
from mcp.types import PaginatedRequestParams
from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage, convert_to_openai_messages,
)

BASE_DIR = Path(__file__).resolve().parent
log = logging.getLogger("cliente")

# Clave de _meta con la que el servidor MCP (fork) recibe la credencial del usuario de
# Zabbix en cuyo nombre se hace la llamada. Ver zabbix-mcp-server/src/zabbix_mcp/delegation.py
DELEGATED_AUTH_META_KEY = "zabbix-mcp/delegated-auth"

SYSTEM_PROMPT = (
    "Eres un asistente de operaciones que consulta Zabbix mediante herramientas. "
    "Usa las herramientas para obtener datos reales; no inventes IDs ni valores. "
    "Responde en español, de forma breve y con los datos relevantes."
)


# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------

def _env_int(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    return int(value) if value else default


def _env_list(name: str) -> list[str]:
    return [x.strip() for x in os.getenv(name, "").split(",") if x.strip()]


@dataclass
class Settings:
    provider: str
    model: str
    base_url: str
    api_key: str
    temperature: float | None   # None = no se envía (valor por defecto del modelo)
    max_tokens: int | None
    llm_kwargs: dict[str, Any]
    mcp_transport: str
    mcp_url: str
    mcp_token: str
    mcp_stdio_command: str
    include_server_instructions: bool
    # Token de API de Zabbix del propio usuario para la CLI (credencial delegada). Vacío = el
    # servidor MCP usa su credencial de servicio (si su delegated_auth no es "required").
    zabbix_user_token: str = ""
    tools_allow: list[str] = field(default_factory=list)
    tools_deny: list[str] = field(default_factory=list)
    tool_desc_max_chars: int = 0
    param_desc_max_chars: int = 0
    drop_params: list[str] = field(default_factory=list)
    tool_result_max_chars: int = 0
    history_tool_result_max_chars: int = 0
    history_max_turns: int = 0
    max_tool_rounds: int = 8
    log_level: str = "INFO"
    log_dir: Path = BASE_DIR / "logs"
    log_llm_payloads: bool = True

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv(BASE_DIR / ".env")
        max_tokens = os.getenv("LLM_MAX_TOKENS", "").strip()
        log_dir = Path(os.getenv("LOG_DIR", "logs"))
        llm_kwargs_raw = os.getenv("LLM_KWARGS", "").strip()
        try:
            llm_kwargs = json.loads(llm_kwargs_raw) if llm_kwargs_raw else {}
        except json.JSONDecodeError as exc:
            raise SystemExit(f"LLM_KWARGS no es JSON válido: {exc}") from None
        if not isinstance(llm_kwargs, dict):
            raise SystemExit("LLM_KWARGS debe ser un objeto JSON, p. ej. {\"num_ctx\": 8192}")
        return cls(
            provider=os.getenv("LLM_PROVIDER", "openai").strip().lower(),
            model=os.getenv("LLM_MODEL", "gpt-4o-mini"),
            # LLM_BASE_URL / LLM_API_KEY; se aceptan los nombres antiguos OPENAI_* por compatibilidad.
            # Vacíos = cada proveedor usa sus variables propias (ANTHROPIC_API_KEY, OLLAMA_HOST...).
            base_url=os.getenv("LLM_BASE_URL") or os.getenv("OPENAI_BASE_URL", ""),
            api_key=os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY", ""),
            # Vacía = no se envía: algunos modelos (p. ej. claude-sonnet-5-5) rechazan valores no por defecto.
            temperature=float(os.getenv("LLM_TEMPERATURE")) if os.getenv("LLM_TEMPERATURE", "").strip() else None,
            max_tokens=int(max_tokens) if max_tokens else None,
            llm_kwargs=llm_kwargs,
            zabbix_user_token=os.getenv("ZABBIX_USER_TOKEN", "").strip(),
            mcp_transport=os.getenv("MCP_TRANSPORT", "http").lower(),
            mcp_url=os.getenv("MCP_URL", "http://127.0.0.1:8080/mcp"),
            mcp_token=os.getenv("MCP_TOKEN", ""),
            mcp_stdio_command=os.getenv("MCP_STDIO_COMMAND", ""),
            include_server_instructions=os.getenv("INCLUDE_SERVER_INSTRUCTIONS", "true").lower() == "true",
            tools_allow=_env_list("TOOLS_ALLOW"),
            tools_deny=_env_list("TOOLS_DENY"),
            tool_desc_max_chars=_env_int("TOOL_DESC_MAX_CHARS", 0),
            param_desc_max_chars=_env_int("PARAM_DESC_MAX_CHARS", 0),
            drop_params=_env_list("DROP_PARAMS"),
            tool_result_max_chars=_env_int("TOOL_RESULT_MAX_CHARS", 0),
            history_tool_result_max_chars=_env_int("HISTORY_TOOL_RESULT_MAX_CHARS", 0),
            history_max_turns=_env_int("HISTORY_MAX_TURNS", 0),
            max_tool_rounds=_env_int("MAX_TOOL_ROUNDS", 8),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            log_dir=log_dir if log_dir.is_absolute() else BASE_DIR / log_dir,
            log_llm_payloads=os.getenv("LOG_LLM_PAYLOADS", "true").lower() == "true",
        )


def build_chat_model(settings: Settings) -> BaseChatModel:
    """Crea el modelo de chat con LangChain para el proveedor configurado.

    Solo se pasan las opciones definidas: cada proveedor usa sus valores y variables de
    entorno por defecto para el resto. LLM_KWARGS añade opciones propias de un proveedor."""
    kwargs: dict[str, Any] = {}
    if settings.temperature is not None:
        kwargs["temperature"] = settings.temperature
    if settings.base_url:
        kwargs["base_url"] = settings.base_url
    if settings.api_key:
        kwargs["api_key"] = settings.api_key
    if settings.max_tokens:
        kwargs["max_tokens"] = settings.max_tokens
    kwargs.update(settings.llm_kwargs)
    try:
        return init_chat_model(settings.model, model_provider=settings.provider, **kwargs)
    except ImportError as exc:
        raise SystemExit(
            f"Falta el paquete de LangChain para el proveedor '{settings.provider}': {exc}. "
            f"Instálalo (p. ej. pip install langchain-{settings.provider.replace('_', '-')})."
        ) from None


# ---------------------------------------------------------------------------
# Logging y análisis del contexto
# ---------------------------------------------------------------------------

def setup_logging(settings: Settings) -> None:
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(settings.log_level)
    console.setFormatter(logging.Formatter("%(levelname)-7s %(name)s: %(message)s"))
    root.addHandler(console)

    # El fichero guarda siempre todo (DEBUG), aunque la consola muestre menos.
    file_handler = logging.FileHandler(settings.log_dir / "cliente.log", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root.addHandler(file_handler)

    # Las librerías HTTP son muy ruidosas en DEBUG.
    for noisy in ("httpx", "httpx2", "httpcore", "openai", "anthropic", "langchain", "mcp", "anyio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def est_tokens(obj: Any) -> int:
    """Estimación aproximada: ~4 caracteres por token (el dato real lo da `usage`)."""
    text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    return max(1, len(text) // 4)


def context_breakdown(messages: list[dict], tools: list[dict]) -> dict[str, Any]:
    """Desglose de qué ocupa cada parte del contexto enviado al LLM."""
    by_role: dict[str, int] = {}
    for m in messages:
        by_role[m["role"]] = by_role.get(m["role"], 0) + est_tokens(m)
    tools_tokens = est_tokens(tools) if tools else 0
    biggest_tools = sorted(((t["function"]["name"], est_tokens(t)) for t in tools),
                           key=lambda x: x[1], reverse=True)[:5]
    call_names = {tc["id"]: tc["function"]["name"]
                  for m in messages for tc in m.get("tool_calls") or []}
    biggest_results = sorted(
        ((call_names.get(m["tool_call_id"], m["tool_call_id"]), est_tokens(m.get("content") or ""))
         for m in messages if m["role"] == "tool"),
        key=lambda x: x[1], reverse=True)[:5]
    return {
        "est_total": sum(by_role.values()) + tools_tokens,
        "est_tools": tools_tokens,
        "n_tools": len(tools),
        "est_by_role": by_role,
        "n_messages": len(messages),
        "top_tools": biggest_tools,
        "top_tool_results": biggest_results,
    }


class PayloadLogger:
    """Guarda cada petición al LLM (y su respuesta) en JSONL para analizarla después."""

    def __init__(self, settings: Settings) -> None:
        self.enabled = settings.log_llm_payloads
        self.path = settings.log_dir / "llm_requests.jsonl"
        self.session_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.counter = 0

    def write(self, record: dict[str, Any]) -> None:
        if not self.enabled:
            return
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


# ---------------------------------------------------------------------------
# Herramientas MCP -> formato OpenAI (con reducción de tokens)
# ---------------------------------------------------------------------------

def _truncate(text: str, limit: int, note: str = "…") -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit].rstrip() + note


def _strip_titles(schema: Any) -> Any:
    """Quita las claves "title" que añade pydantic: no aportan nada al LLM.

    Respeta los parámetros que se llamen "title" (claves dentro de "properties")."""
    if isinstance(schema, list):
        return [_strip_titles(x) for x in schema]
    if not isinstance(schema, dict):
        return schema
    out = {}
    for key, value in schema.items():
        if key == "title" and isinstance(value, str):
            continue
        if key == "properties" and isinstance(value, dict):
            out[key] = {name: _strip_titles(sub) for name, sub in value.items()}
        else:
            out[key] = _strip_titles(value)
    return out


def _simplify_optional(prop: Any) -> Any:
    """`{"anyOf": [X, {"type": "null"}], "default": null}` -> `X`.

    Un parámetro opcional ya se reconoce porque no está en "required"; el envoltorio
    nullable y el default nulo solo gastan tokens."""
    if not isinstance(prop, dict):
        return prop
    prop = dict(prop)
    if prop.get("default", "") is None:
        del prop["default"]
    variants = prop.get("anyOf")
    if isinstance(variants, list):
        non_null = [v for v in variants if v != {"type": "null"}]
        if len(non_null) == 1 and len(non_null) < len(variants) and isinstance(non_null[0], dict):
            del prop["anyOf"]
            prop = {**non_null[0], **prop}
        elif len(non_null) < len(variants):
            prop["anyOf"] = non_null
    return prop


def _matches(name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, p) for p in patterns)


def select_tools(mcp_tools: list[Any], settings: Settings, *, reduce: bool = True) -> list[dict]:
    """Filtra las herramientas MCP y las convierte al formato `tools` de OpenAI."""
    result = []
    for tool in mcp_tools:
        if reduce and settings.tools_allow and not _matches(tool.name, settings.tools_allow):
            continue
        if reduce and _matches(tool.name, settings.tools_deny):
            continue
        params = copy.deepcopy(tool.input_schema or {"type": "object", "properties": {}})
        description = tool.description or ""
        if reduce:
            params = _strip_titles(params)
            required = set(params.get("required", []))
            params["properties"] = {
                name: prop if name in required else _simplify_optional(prop)
                for name, prop in params.get("properties", {}).items()
            }
            props = params["properties"]
            for name in settings.drop_params:
                if name in props and name not in required:
                    del props[name]
            if settings.param_desc_max_chars:
                for prop in props.values():
                    if isinstance(prop, dict) and isinstance(prop.get("description"), str):
                        prop["description"] = _truncate(prop["description"], settings.param_desc_max_chars)
            description = _truncate(description, settings.tool_desc_max_chars)
        result.append({
            "type": "function",
            "function": {"name": tool.name, "description": description, "parameters": params},
        })
    return result


def tool_result_text(result: Any) -> str:
    """Texto plano de un CallToolResult (las imágenes se sustituyen por un aviso)."""
    parts = []
    for item in getattr(result, "content", None) or []:
        if getattr(item, "type", "") == "text":
            parts.append(item.text)
        else:
            parts.append(f"[contenido {getattr(item, 'type', '?')} omitido]")
    text = "\n".join(parts)
    if getattr(result, "is_error", False):
        text = f"ERROR de la herramienta: {text}"
    return text


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------

def _message_text(message: BaseMessage) -> str:
    """Texto de un mensaje con independencia del proveedor (contenido str o lista de bloques)."""
    text = message.text
    if isinstance(text, str):        # langchain-core >= 1.0: propiedad (str)
        return str(text)
    return text() if callable(text) else ""   # versiones antiguas: método


class ZabbixChat:
    def __init__(self, settings: Settings, session: ClientSession, tools: list[dict],
                 server_instructions: str | None, *, llm: BaseChatModel | None = None,
                 session_label: str | None = None) -> None:
        self.s = settings
        self.session = session
        self.tools = tools
        base_llm = llm or build_chat_model(settings)
        # Las herramientas van en formato OpenAI; LangChain las traduce al de cada proveedor.
        self.llm = base_llm.bind_tools(tools) if tools else base_llm
        self.payloads = PayloadLogger(settings)
        if session_label:  # agrupa en los logs las peticiones de un mismo usuario/conversación
            self.payloads.session_id = session_label
        self.last_tools_used: list[str] = []
        system = SYSTEM_PROMPT
        if server_instructions and settings.include_server_instructions:
            system += "\n\nInstrucciones del servidor MCP:\n" + server_instructions
        self.system_message = SystemMessage(system)
        self.history: list[BaseMessage] = []
        self.totals = {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0}

    # --- gestión del historial -------------------------------------------------

    def _trimmed_history(self) -> list[BaseMessage]:
        """Historial que se envía: últimos N turnos y resultados antiguos recortados."""
        history = self.history
        if self.s.history_max_turns > 0:
            user_idx = [i for i, m in enumerate(history) if isinstance(m, HumanMessage)]
            if len(user_idx) > self.s.history_max_turns:
                history = history[user_idx[-self.s.history_max_turns]:]
        limit = self.s.history_tool_result_max_chars
        if limit > 0:
            last_user = max((i for i, m in enumerate(history) if isinstance(m, HumanMessage)), default=0)
            history = [
                ToolMessage(_truncate(_message_text(m), limit, " …[resultado antiguo recortado]"),
                            tool_call_id=m.tool_call_id)
                if isinstance(m, ToolMessage) and i < last_user else m
                for i, m in enumerate(history)
            ]
        return history

    def reset(self) -> None:
        self.history.clear()

    def discard_last_turn(self) -> None:
        """Quita la última pregunta y lo que vino después (p. ej. si falló a mitad)."""
        while self.history and not isinstance(self.history[-1], HumanMessage):
            self.history.pop()
        if self.history:
            self.history.pop()

    # --- llamadas -------------------------------------------------------------

    async def _call_llm(self, messages: list[BaseMessage]) -> AIMessage:
        self.payloads.counter += 1
        n = self.payloads.counter
        # Representación neutral (formato OpenAI) de lo que se envía, para medir y registrar.
        as_dicts = convert_to_openai_messages(messages)
        breakdown = context_breakdown(as_dicts, self.tools)
        log.info("→ LLM #%d (%s:%s): ~%d tokens estimados (herramientas: %d tok en %d tools, mensajes: %s)",
                 n, self.s.provider, self.s.model, breakdown["est_total"], breakdown["est_tools"],
                 breakdown["n_tools"], breakdown["est_by_role"])
        log.debug("Desglose contexto #%d: %s", n, json.dumps(breakdown, ensure_ascii=False))

        started = time.perf_counter()
        message = await self.llm.ainvoke(messages)
        elapsed = time.perf_counter() - started

        usage = message.usage_metadata or {}
        usage_log: dict[str, Any] = {}
        if usage:
            cached = (usage.get("input_token_details") or {}).get("cache_read", 0) or 0
            self.totals["requests"] += 1
            self.totals["prompt_tokens"] += usage.get("input_tokens", 0) or 0
            self.totals["completion_tokens"] += usage.get("output_tokens", 0) or 0
            self.totals["cached_tokens"] += cached
            log.info("← LLM #%d en %.1fs: prompt=%s completion=%s cacheados=%s",
                     n, elapsed, usage.get("input_tokens"), usage.get("output_tokens"), cached)
            # Mismas claves que la API de OpenAI, para analizar_contexto.py; "raw" = dato de LangChain.
            usage_log = {"prompt_tokens": usage.get("input_tokens"), "completion_tokens": usage.get("output_tokens"),
                         "total_tokens": usage.get("total_tokens"),
                         "prompt_tokens_details": {"cached_tokens": cached}, "raw": dict(usage)}
        else:
            log.info("← LLM #%d en %.1fs (el proveedor no devolvió usage)", n, elapsed)

        self.payloads.write({
            "session": self.payloads.session_id, "request": n, "time": datetime.now().isoformat(),
            "provider": self.s.provider, "model": self.s.model, "elapsed_s": round(elapsed, 2),
            "breakdown": breakdown, "usage": usage_log,
            "request_payload": {"model": self.s.model, "messages": as_dicts, "tools": self.tools},
            "response_message": convert_to_openai_messages([message])[0],
        })
        return message

    async def _run_tool(self, name: str, args: dict[str, Any], zabbix_auth: str | None = None) -> str:
        # La credencial del usuario solo viaja en el _meta de la llamada MCP: nunca se registra
        # ni entra en el contexto del LLM.
        log.info("⚙ MCP %s %s%s", name, json.dumps(args, ensure_ascii=False)[:300],
                 " [en nombre del usuario]" if zabbix_auth else "")
        started = time.perf_counter()
        try:
            meta = {DELEGATED_AUTH_META_KEY: zabbix_auth} if zabbix_auth else None
            result = await self.session.call_tool(name, args, meta=meta)
            text = tool_result_text(result)
        except Exception as exc:  # error de transporte o del servidor
            log.exception("Fallo llamando a la herramienta %s", name)
            text = f"ERROR llamando a {name}: {exc}"
        original = len(text)
        text = _truncate(text, self.s.tool_result_max_chars,
                         f"\n…[recortado: {original} caracteres en total; pide menos datos con filtros/limit]")
        log.info("  ↳ %d caracteres (~%d tok) en %.1fs%s", original, est_tokens(text),
                 time.perf_counter() - started, " → recortado" if len(text) < original else "")
        log.debug("  ↳ resultado %s: %s", name, text[:2000])
        return text

    async def ask(self, question: str, *, zabbix_auth: str | None = None) -> str:
        """Responde una pregunta. *zabbix_auth* = credencial de Zabbix del usuario final
        (sesión o token de API): las herramientas consultan Zabbix con sus permisos."""
        self.history.append(HumanMessage(question))
        self.last_tools_used = []
        for _ in range(self.s.max_tool_rounds):
            messages = [self.system_message, *self._trimmed_history()]
            message = await self._call_llm(messages)
            if not message.tool_calls and not message.invalid_tool_calls:
                self.history.append(message)
                return _message_text(message)
            self.history.append(message)
            for tc in message.tool_calls:
                self.last_tools_used.append(tc["name"])
                content = await self._run_tool(tc["name"], tc["args"] or {}, zabbix_auth)
                self.history.append(ToolMessage(content, tool_call_id=tc["id"]))
            for bad in message.invalid_tool_calls:  # argumentos que no eran JSON válido
                log.warning("Llamada a herramienta no válida de %s: %s", bad.get("name"), bad.get("error"))
                self.history.append(ToolMessage(
                    f"ERROR: los argumentos no son JSON válido ({bad.get('error')}). Corrige la llamada.",
                    tool_call_id=bad.get("id") or "invalid"))
        log.warning("Se alcanzó MAX_TOOL_ROUNDS=%d sin respuesta final", self.s.max_tool_rounds)
        answer = "(Se alcanzó el máximo de llamadas a herramientas sin una respuesta final.)"
        self.history.append(AIMessage(answer))
        return answer

    def usage_summary(self) -> str:
        t = self.totals
        return (f"Peticiones: {t['requests']} | prompt: {t['prompt_tokens']} | "
                f"respuesta: {t['completion_tokens']} | cacheados: {t['cached_tokens']}")


# ---------------------------------------------------------------------------
# Conexión MCP y bucle principal
# ---------------------------------------------------------------------------

async def connect_mcp(stack: AsyncExitStack, settings: Settings) -> tuple[ClientSession, str | None]:
    if settings.mcp_transport == "stdio":
        # En Windows shlex no-POSIX respeta las barras invertidas pero conserva las comillas.
        parts = [p[1:-1] if len(p) > 1 and p[0] == p[-1] == '"' else p
                 for p in shlex.split(settings.mcp_stdio_command, posix=os.name != "nt")]
        if not parts:
            raise SystemExit("MCP_STDIO_COMMAND está vacío")
        log.info("Lanzando servidor MCP por stdio: %s", parts)
        errlog = stack.enter_context(
            (settings.log_dir / "mcp_server_stderr.log").open("a", encoding="utf-8"))
        params = StdioServerParameters(command=parts[0], args=parts[1:], cwd=str(BASE_DIR))
        read, write = await stack.enter_async_context(stdio_client(params, errlog=errlog))
    else:
        headers = {"Authorization": f"Bearer {settings.mcp_token}"} if settings.mcp_token else None
        http_client = await stack.enter_async_context(create_mcp_http_client(headers=headers))
        log.info("Conectando al servidor MCP: %s", settings.mcp_url)
        read, write = await stack.enter_async_context(
            streamable_http_client(settings.mcp_url, http_client=http_client))
    session = await stack.enter_async_context(ClientSession(read, write))
    init = await session.initialize()
    info = init.server_info
    log.info("Servidor MCP: %s %s", info.name, info.version)
    return session, init.instructions


async def list_all_tools(session: ClientSession) -> list[Any]:
    tools, cursor = [], None
    while True:
        params = PaginatedRequestParams(cursor=cursor) if cursor else None
        page = await session.list_tools(params=params)
        tools.extend(page.tools)
        cursor = page.next_cursor
        if not cursor:
            return tools


def print_tool_report(mcp_tools: list[Any], settings: Settings) -> None:
    full = select_tools(mcp_tools, settings, reduce=False)
    reduced = select_tools(mcp_tools, settings, reduce=True)
    print(f"Catálogo completo : {len(full):4d} herramientas  ~{est_tokens(full):7d} tokens por petición")
    print(f"Con tu filtro .env: {len(reduced):4d} herramientas  ~{est_tokens(reduced):7d} tokens por petición\n")
    print(f"{'herramienta':40s} {'completa':>9s} {'reducida':>9s}")
    reduced_by_name = {t["function"]["name"]: t for t in reduced}
    for t in sorted(full, key=est_tokens, reverse=True):
        name = t["function"]["name"]
        red = reduced_by_name.get(name)
        print(f"{name:40s} {est_tokens(t):9d} {est_tokens(red) if red else '-':>9}")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Cliente LLM (OpenAI compatible) + MCP de Zabbix")
    parser.add_argument("-p", "--pregunta", help="Hace una sola pregunta y termina")
    parser.add_argument("--herramientas", action="store_true",
                        help="Muestra el tamaño en tokens de cada herramienta y termina")
    args = parser.parse_args()

    settings = Settings.from_env()
    setup_logging(settings)

    async with AsyncExitStack() as stack:
        session, instructions = await connect_mcp(stack, settings)
        mcp_tools = await list_all_tools(session)
        if args.herramientas:
            print_tool_report(mcp_tools, settings)
            return

        tools = select_tools(mcp_tools, settings)
        log.info("Herramientas MCP: %d disponibles, %d enviadas al LLM (~%d tokens)",
                 len(mcp_tools), len(tools), est_tokens(tools) if tools else 0)
        if not tools:
            log.warning("Ninguna herramienta coincide con TOOLS_ALLOW/TOOLS_DENY")
        chat = ZabbixChat(settings, session, tools, instructions)
        user_auth = settings.zabbix_user_token or None
        if user_auth:
            log.info("Las herramientas consultarán Zabbix con el token del usuario (ZABBIX_USER_TOKEN)")

        if args.pregunta:
            print(await chat.ask(args.pregunta, zabbix_auth=user_auth))
            log.info(chat.usage_summary())
            return

        print("Chat con Zabbix. Comandos: /tokens, /tools, /reset, /salir\n")
        while True:
            try:
                question = (await asyncio.to_thread(input, "Tú> ")).strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not question:
                continue
            if question in ("/salir", "/exit", "/quit"):
                break
            if question == "/tokens":
                print(chat.usage_summary())
                continue
            if question == "/tools":
                print(", ".join(t["function"]["name"] for t in tools))
                continue
            if question == "/reset":
                chat.reset()
                print("Historial borrado.")
                continue
            try:
                answer = await chat.ask(question, zabbix_auth=user_auth)
            except Exception as exc:
                log.exception("Error procesando la pregunta")
                chat.discard_last_turn()
                print(f"Error: {exc}")
                continue
            print(f"\nAsistente> {answer}\n")
        log.info(chat.usage_summary())


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
