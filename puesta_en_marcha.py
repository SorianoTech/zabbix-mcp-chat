#!/usr/bin/env python3
"""Puesta en marcha interactiva del Proyecto MCP.

Crea los ficheros de configuración a partir de sus ejemplos y pregunta los
valores que el usuario debe introducir:

  zabbix-mcp-server/config.toml   <- config.example.toml
  zabbix-mcp-server/.env          <- .env.example        (solo si el MCP corre en Docker)
  mcp-client/.env                 <- .env.example
  zabbix-module-mcpchat/manifest.json  (opcional: gateway_url y gateway_api_key)

No escribe nada hasta confirmar el resumen final. Si un fichero ya existe,
pregunta antes de sobrescribirlo y guarda una copia .bak.

Uso:  python puesta_en_marcha.py
Solo usa la librería estándar (Python >= 3.10).
"""

from __future__ import annotations

import getpass
import json
import re
import secrets
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SERVER_DIR = ROOT / "zabbix-mcp-server"
CLIENT_DIR = ROOT / "mcp-client"
MODULE_DIR = ROOT / "zabbix-module-mcpchat"

# Mismo formato que acepta delegation.py para la credencial delegada.
_CREDENTIAL_RE = re.compile(r"^[A-Za-z0-9]{16,128}$")

LLM_PROVIDERS = {
    # proveedor: (modelo por defecto, URL base por defecto, ¿necesita API key?)
    "anthropic": ("claude-sonnet-5-5", "", True),
    "openai": ("gpt-4o-mini", "", True),
    "ollama": ("qwen2.5:14b", "http://localhost:11434", False),
    "google_genai": ("", "", True),
    "azure_openai": ("", "", True),
    "mistralai": ("", "", True),
    "groq": ("", "", True),
}


# --------------------------------------------------------------------------
# Entrada interactiva
# --------------------------------------------------------------------------

def ask(prompt: str, default: str = "", *, required: bool = False,
        validate=None) -> str:
    """Pregunta un valor visible. Enter = valor por defecto."""
    suffix = f" [{default}]" if default else ""
    while True:
        value = input(f"  {prompt}{suffix}: ").strip() or default
        if required and not value:
            print("    ! Este valor es obligatorio.")
            continue
        if value and validate:
            error = validate(value)
            if error:
                print(f"    ! {error}")
                continue
        return value


def ask_secret(prompt: str, *, required: bool = False, generate: bool = False,
               validate=None) -> str:
    """Pregunta un secreto sin mostrarlo. Con generate, Enter = aleatorio."""
    hint = " (Enter = generar uno aleatorio)" if generate else (
        "" if required else " (Enter = dejar vacío)")
    while True:
        prompt_text = f"  {prompt}{hint}: "
        if sys.stdin.isatty():
            value = getpass.getpass(prompt_text).strip()
        else:  # entrada redirigida: getpass no puede leerla en Windows
            value = input(prompt_text).strip()
        if not value and generate:
            value = secrets.token_hex(32)
            print("    -> generado.")
        if required and not value:
            print("    ! Este valor es obligatorio.")
            continue
        if value and validate:
            error = validate(value)
            if error:
                print(f"    ! {error}")
                continue
        return value


def ask_choice(prompt: str, options: list[str], default: str) -> str:
    def check(value: str) -> str | None:
        return None if value in options else f"Elige una opción: {', '.join(options)}"
    return ask(f"{prompt} ({'/'.join(options)})", default, validate=check)


def ask_yes(prompt: str, default: bool = True) -> bool:
    options = "S/n" if default else "s/N"
    while True:
        value = input(f"  {prompt} [{options}]: ").strip().lower()
        if not value:
            return default
        if value in ("s", "si", "sí", "y", "yes"):
            return True
        if value in ("n", "no"):
            return False


def check_url(value: str) -> str | None:
    if not re.match(r"^https?://[^\s/]+", value):
        return "Debe empezar por http:// o https://"
    return None


def check_port(value: str) -> str | None:
    if not value.isdigit() or not 1 <= int(value) <= 65535:
        return "Puerto no válido (1-65535)."
    return None


def check_credential(value: str) -> str | None:
    if not _CREDENTIAL_RE.match(value):
        return "Formato no válido: 16-128 caracteres alfanuméricos (un token de API de Zabbix tiene 64)."
    return None


def mask(value: str) -> str:
    if not value:
        return "(vacío)"
    return f"{value[:4]}…({len(value)} caracteres)"


def title(text: str) -> None:
    print(f"\n=== {text} ===")


# --------------------------------------------------------------------------
# Edición de ficheros
# --------------------------------------------------------------------------

def read_text(path: Path) -> str:
    with path.open(encoding="utf-8", newline="") as fh:
        return fh.read()


def env_value(value: str) -> str:
    """Entrecomilla un valor de .env si contiene caracteres conflictivos."""
    if value and re.search(r"[\s#'\"\\]", value):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return value


def set_env(text: str, key: str, value: str, comment: str = "") -> str:
    """Fija KEY=valor (descomentando la línea si hace falta) o la añade al final."""
    pattern = re.compile(rf"^[ \t]*#?[ \t]*{re.escape(key)}=[^\r\n]*", re.MULTILINE)
    line = f"{key}={env_value(value)}"
    text, count = pattern.subn(lambda _m: line, text, count=1)
    if count == 0:
        newline = "\r\n" if "\r\n" in text else "\n"
        if not text.endswith(("\n", "\r")):
            text += newline
        if comment:
            text += f"{newline}# {comment}{newline}"
        text += line + newline
    return text


def replace_line(text: str, pattern: str, new_line: str | None, what: str, *, repl=None) -> str:
    """Sustituye la primera línea completa que coincide; avisa si el ejemplo cambió.

    `pattern` describe la línea entera sin anclas (los ficheros pueden ser CRLF).
    `repl` (función que recibe el match) sustituye a `new_line` si se indica.
    """
    regex = re.compile(rf"^{pattern}(?=\r?$)", re.MULTILINE)
    text, count = regex.subn(repl or (lambda _m: new_line), text, count=1)
    if count == 0:
        print(f"  ! No encontré '{what}' en el ejemplo: revísalo a mano.")
    return text


def toml_str(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)  # cadena básica TOML válida


# --------------------------------------------------------------------------
# Recogida de datos
# --------------------------------------------------------------------------

@dataclass
class Plan:
    writes: dict[Path, str] = field(default_factory=dict)
    summary: list[str] = field(default_factory=list)
    next_steps: list[str] = field(default_factory=list)


def may_write(paths: list[Path]) -> bool:
    existing = [p for p in paths if p.exists()]
    if not existing:
        return True
    names = ", ".join(str(p.relative_to(ROOT)) for p in existing)
    return ask_yes(f"Ya existe {names}. ¿Sobrescribir? (se guarda copia .bak)", default=False)


def server_section(plan: Plan) -> dict:
    title("1/3 · Servidor MCP (zabbix-mcp-server)")
    docker = ask_choice("¿Cómo lo arrancarás?", ["docker", "local"], "docker") == "docker"
    targets = [SERVER_DIR / "config.toml"] + ([SERVER_DIR / ".env"] if docker else [])
    if not may_write(targets):
        print("  Se conserva la configuración actual del servidor.")
        port = ask("Puerto del servidor MCP", "8080", validate=check_port)
        token = ask_secret("Token de acceso al MCP que ya tienes configurado (auth_token / MCP_AUTH_TOKEN)")
        delegated = ask_choice("delegated_auth configurado en el servidor", ["required", "allowed", "off"], "required")
        return {"port": port, "token": token, "delegated": delegated}

    if docker:
        print("  El MCP corre en un contenedor: 127.0.0.1 sería el propio contenedor.\n"
              "  Si Zabbix está en este equipo (o en otro contenedor con el puerto publicado),\n"
              "  usa host.docker.internal.")
        url_default = "http://host.docker.internal:8088"
    else:
        url_default = "http://127.0.0.1"
    zabbix_url = ask("URL del frontend de Zabbix vista desde el servidor MCP", url_default,
                     required=True, validate=check_url).rstrip("/")
    print("  Token de API de la cuenta de servicio (Zabbix: Configuración de usuario → Tokens de API).\n"
          "  Lo usa el MCP para detectar la versión y para health_check; las consultas del chat\n"
          "  usan la credencial de cada usuario si delegated_auth lo exige.")
    api_token = ask_secret("Token de API de Zabbix", required=True)
    verify_ssl = True
    if zabbix_url.startswith("https://"):
        verify_ssl = ask_yes("¿Verificar el certificado TLS de Zabbix?", default=True)
    print("  delegated_auth: required = solo responde con la credencial del usuario (recomendado para el chat);\n"
          "                  allowed = la del usuario si llega, si no la de servicio; off = solo la de servicio.")
    delegated = ask_choice("Autenticación delegada", ["required", "allowed", "off"], "required")
    port = ask("Puerto del servidor MCP", "8080", validate=check_port)
    mcp_token = ask_secret("Token de acceso al MCP (lo enviarán los clientes como Bearer)", generate=True)

    # --- config.toml ---
    text = read_text(SERVER_DIR / "config.example.toml")
    text = replace_line(text, r"port = 8080", f"port = {port}", "port")
    text = replace_line(text, r'url = "https://zabbix\.example\.com"', f"url = {toml_str(zabbix_url)}", "url")
    if docker:
        text = replace_line(text, r'# auth_token = "\$\{MCP_AUTH_TOKEN\}"',
                            'auth_token = "${MCP_AUTH_TOKEN}"', "auth_token")
        text = replace_line(text, r'api_token = "your-api-token-here"',
                            'api_token = "${ZABBIX_API_TOKEN}"', "api_token")
    else:
        text = replace_line(text, r'# auth_token = "your-secret-token-here"',
                            f"auth_token = {toml_str(mcp_token)}", "auth_token")
        text = replace_line(text, r'api_token = "your-api-token-here"',
                            f"api_token = {toml_str(api_token)}", "api_token")
        # /var/log/zabbix-mcp no existe fuera del contenedor o del instalador: log por consola.
        text = replace_line(text, r'log_file = "/var/log/zabbix-mcp/server\.log"',
                            '# log_file = "/var/log/zabbix-mcp/server.log"', "log_file")
    text = replace_line(text, r'# delegated_auth = "off"', f'delegated_auth = "{delegated}"', "delegated_auth")
    if not verify_ssl:
        text = replace_line(text, r"verify_ssl = true", "verify_ssl = false", "verify_ssl")
    plan.writes[SERVER_DIR / "config.toml"] = text

    # --- .env (Docker) ---
    if docker:
        env = read_text(SERVER_DIR / ".env.example")
        env = set_env(env, "MCP_PORT", port)
        env = set_env(env, "MCP_AUTH_TOKEN", mcp_token)
        env = set_env(env, "ZABBIX_API_TOKEN", api_token,
                      "ZABBIX_API_TOKEN - Zabbix API token of the service account (config.toml: api_token)")
        plan.writes[SERVER_DIR / ".env"] = env
        plan.next_steps.append("cd zabbix-mcp-server; docker compose up -d --build; docker compose logs -f"
                               "   # si ya estaba arrancado: docker compose up -d --force-recreate")
    else:
        plan.next_steps += [
            "cd zabbix-mcp-server; python -m venv .venv; .\\.venv\\Scripts\\Activate.ps1; pip install -e .",
            "cd zabbix-mcp-server; zabbix-mcp-server --config config.toml --check-config",
            "cd zabbix-mcp-server; zabbix-mcp-server --config config.toml --transport http",
        ]

    plan.summary += [
        f"Servidor MCP ({'Docker' if docker else 'local'}): Zabbix {zabbix_url}, api_token {mask(api_token)}, "
        f"delegated_auth={delegated}, puerto {port}, token MCP {mask(mcp_token)}"
        + ("" if verify_ssl else ", sin verificar TLS"),
    ]
    return {"port": port, "token": mcp_token, "delegated": delegated}


def client_section(plan: Plan, server: dict) -> dict:
    title("2/3 · Cliente y pasarela (mcp-client)")
    target = CLIENT_DIR / ".env"
    if not may_write([target]):
        print("  Se conserva mcp-client/.env.")
        port = ask("Puerto de la pasarela (GATEWAY_PORT)", "8770", validate=check_port)
        key = ask_secret("GATEWAY_API_KEY que ya tienes configurada")
        return {"gateway_host": "127.0.0.1", "gateway_port": port, "gateway_key": key}

    print(f"  Proveedores habituales: {', '.join(LLM_PROVIDERS)} (u otro soportado por LangChain).")
    provider = ask("Proveedor del LLM (LLM_PROVIDER)", "anthropic", required=True)
    model_default, base_default, needs_key = LLM_PROVIDERS.get(provider, ("", "", True))
    model = ask("Modelo (LLM_MODEL)", model_default, required=True)
    if provider == "openai":
        print("  Para un servidor compatible con OpenAI (vLLM, LM Studio, LiteLLM…) indica su URL /v1.")
    base_url = ask("URL base del LLM (LLM_BASE_URL, vacío = la del proveedor)", base_default,
                   validate=check_url)
    api_key = ""
    if needs_key:
        api_key = ask_secret("API key del LLM (LLM_API_KEY; vacío = variable propia del proveedor)")

    mcp_url = ask("URL del servidor MCP (MCP_URL)", f"http://127.0.0.1:{server['port']}/mcp",
                  required=True, validate=check_url)
    mcp_token = server["token"]
    if not mcp_token:
        mcp_token = ask_secret("Token del MCP (MCP_TOKEN)")

    user_token = ""
    if server["delegated"] != "off":
        note = ("obligatorio para usar cliente.py en consola" if server["delegated"] == "required"
                else "opcional: sin él, cliente.py usa la credencial de servicio")
        print(f"  ZABBIX_USER_TOKEN: tu token de API personal de Zabbix ({note}).\n"
              "  La pasarela no lo usa: allí viaja la sesión de cada usuario del frontend.")
        user_token = ask_secret("Tu token de API personal de Zabbix", validate=check_credential)

    print("  GATEWAY_HOST: 127.0.0.1 si el frontend de Zabbix está en este equipo; 0.0.0.0 si está en otra\n"
          "  máquina o en un contenedor que no llega a 127.0.0.1 del anfitrión (limítalo con el cortafuegos).")
    gateway_host = ask("Interfaz de la pasarela (GATEWAY_HOST)", "127.0.0.1", required=True)
    gateway_port = ask("Puerto de la pasarela (GATEWAY_PORT)", "8770", validate=check_port)
    gateway_key = ask_secret("Clave compartida pasarela ↔ módulo (GATEWAY_API_KEY)", generate=True)

    env = read_text(CLIENT_DIR / ".env.example")
    for key, value in {
        "LLM_PROVIDER": provider,
        "LLM_MODEL": model,
        "LLM_BASE_URL": base_url,
        "LLM_API_KEY": api_key,
        "ZABBIX_USER_TOKEN": user_token,
        "MCP_TRANSPORT": "http",
        "MCP_URL": mcp_url,
        "MCP_TOKEN": mcp_token,
        "GATEWAY_HOST": gateway_host,
        "GATEWAY_PORT": gateway_port,
        "GATEWAY_API_KEY": gateway_key,
    }.items():
        env = set_env(env, key, value)
    plan.writes[target] = env

    package = {"openai": "langchain-openai", "anthropic": "langchain-anthropic",
               "ollama": "langchain-ollama", "google_genai": "langchain-google-genai",
               "azure_openai": "langchain-openai", "mistralai": "langchain-mistralai",
               "groq": "langchain-groq"}.get(provider, f"langchain-{provider}")
    plan.next_steps += [
        f"cd mcp-client; python -m venv .venv; .\\.venv\\Scripts\\Activate.ps1; "
        f"pip install -r requirements.txt {package}",
        'cd mcp-client; python cliente.py -p "¿Qué problemas graves hay ahora?"   # prueba en consola',
        "cd mcp-client; python pasarela.py   # deja la pasarela arrancada para el globo",
    ]
    plan.summary.append(
        f"Cliente: {provider}/{model}" + (f" en {base_url}" if base_url else "")
        + f", API key {mask(api_key)}, MCP {mcp_url}, token MCP {mask(mcp_token)}, "
        f"token personal {mask(user_token)}, pasarela {gateway_host}:{gateway_port}, "
        f"clave {mask(gateway_key)}")
    return {"gateway_host": gateway_host, "gateway_port": gateway_port, "gateway_key": gateway_key}


def module_section(plan: Plan, gateway: dict) -> None:
    title("3/3 · Módulo de Zabbix (zabbix-module-mcpchat)")
    print("  gateway_url es la URL de la pasarela vista desde el SERVIDOR WEB de Zabbix\n"
          "  (si el frontend corre en Docker en este equipo: http://host.docker.internal:<puerto>).")
    host = gateway["gateway_host"]
    default_url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{gateway['gateway_port']}"
    gateway_url = ask("URL de la pasarela (gateway_url)", default_url, required=True, validate=check_url)
    key = gateway["gateway_key"]

    print("  El manifest solo se lee al REGISTRAR el módulo. Si ya está registrado, cambia la\n"
          "  configuración con la API (module.update) o desregístralo y vuelve a escanear.\n"
          "  Alternativa para la clave: variable de entorno MCPCHAT_GATEWAY_API_KEY en el PHP-FPM.")
    if ask_yes("¿Guardar gateway_url y gateway_api_key en manifest.json?", default=True):
        manifest_path = MODULE_DIR / "manifest.json"
        text = read_text(manifest_path)
        # Solo se sustituyen los dos valores, conservando el formato del fichero.
        for name, value in (("gateway_url", gateway_url), ("gateway_api_key", key)):
            text = replace_line(text, rf'(\s*)"{name}": "[^"\r\n]*"(,?)',
                                None, name, repl=lambda m, n=name, v=value:
                                f'{m.group(1)}"{n}": {json.dumps(v)}{m.group(2)}')
        config = json.loads(text)["config"]  # comprueba que sigue siendo JSON válido
        if config.get("gateway_url") != gateway_url or config.get("gateway_api_key") != key:
            raise SystemExit("No se pudo actualizar manifest.json: revísalo a mano.")
        plan.writes[manifest_path] = text
        plan.summary.append(f"Módulo: gateway_url {gateway_url}, gateway_api_key {mask(key)} (manifest.json)")
    else:
        plan.summary.append(f"Módulo: sin cambios (usa gateway_url {gateway_url} y la clave de la pasarela)")
    plan.next_steps.append("Copia zabbix-module-mcpchat a <frontend>/modules/mcpchat y en Zabbix: "
                           "Administración → General → Módulos → Escanear directorio → Habilitar")


# --------------------------------------------------------------------------
# Escritura
# --------------------------------------------------------------------------

def write_all(plan: Plan) -> None:
    for path, content in plan.writes.items():
        if path.exists():
            backup = path.with_name(path.name + ".bak")
            shutil.copy2(path, backup)
            print(f"  copia de seguridad: {backup.relative_to(ROOT)}")
        with path.open("w", encoding="utf-8", newline="") as fh:
            fh.write(content)
        print(f"  escrito: {path.relative_to(ROOT)}")


def main() -> int:
    # Consola de Windows o salida redirigida (cp1252): forzar UTF-8 para tildes y flechas.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    for required in (SERVER_DIR / "config.example.toml", SERVER_DIR / ".env.example",
                     CLIENT_DIR / ".env.example", MODULE_DIR / "manifest.json"):
        if not required.exists():
            print(f"No encuentro {required}. Ejecuta el script desde la raíz del proyecto.")
            return 1

    print("Puesta en marcha del Proyecto MCP. Enter acepta el valor entre corchetes.\n"
          "Los secretos no se muestran al escribirlos. No se escribe nada hasta el final.")
    plan = Plan()
    server = server_section(plan)
    gateway = client_section(plan, server)
    if ask_yes("\n¿Configurar también el módulo de Zabbix?", default=True):
        module_section(plan, gateway)

    title("Resumen")
    for line in plan.summary:
        print(f"  - {line}")
    if not plan.writes:
        print("  No hay ficheros que escribir.")
        return 0
    print("  Ficheros: " + ", ".join(str(p.relative_to(ROOT)) for p in plan.writes))
    if not ask_yes("¿Escribir la configuración?", default=True):
        print("Cancelado: no se ha modificado nada.")
        return 1
    write_all(plan)

    title("Siguientes pasos")
    for number, step in enumerate(plan.next_steps, 1):
        print(f"  {number}. {step}")
    print("\n  config.toml y los .env contienen secretos: no los subas a git.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        print("\nCancelado: no se ha modificado nada.")
        sys.exit(1)
