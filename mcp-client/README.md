# Cliente LLM + MCP de Zabbix

Cliente de chat en Python que conecta **cualquier LLM soportado por LangChain** con el servidor MCP
de [../zabbix-mcp-server](../zabbix-mcp-server). Registra con detalle qué contexto se envía al LLM, para
depurar y reducir tokens.

## Elegir el modelo

El modelo se crea con `init_chat_model` de LangChain a partir de `LLM_PROVIDER` y `LLM_MODEL`. Las
herramientas MCP se pasan en formato OpenAI y LangChain las traduce al formato de cada proveedor.

| Proveedor | `LLM_PROVIDER` | Paquete extra | Credenciales / URL |
|---|---|---|---|
| OpenAI o compatible (vLLM, LM Studio, LiteLLM, Groq…) | `openai` | (incluido) | `LLM_API_KEY`, `LLM_BASE_URL` (p. ej. `http://host:8000/v1`) |
| Anthropic | `anthropic` | `langchain-anthropic` | `LLM_API_KEY` o `ANTHROPIC_API_KEY` |
| Ollama (API nativa) | `ollama` | `langchain-ollama` | `LLM_BASE_URL=http://localhost:11434` |
| Google Gemini | `google_genai` | `langchain-google-genai` | `LLM_API_KEY` o `GOOGLE_API_KEY` |
| Azure OpenAI | `azure_openai` | (incluido) | variables `AZURE_OPENAI_*`; despliegue en `LLM_KWARGS` |
| Mistral / Bedrock / otros | `mistralai`, `bedrock_converse`… | `langchain-<proveedor>` | las de cada proveedor |

`LLM_KWARGS` (JSON) pasa opciones propias de un proveedor, por ejemplo `{"num_ctx": 16384}` en Ollama.
El modelo debe admitir **llamadas a herramientas** (tool calling).
Los nombres antiguos `OPENAI_BASE_URL` y `OPENAI_API_KEY` siguen funcionando.

## Consultar Zabbix con los permisos del usuario (autenticación delegada)

Si el servidor MCP tiene `delegated_auth = "allowed"` o `"required"` en `[server]`, cada llamada a una
herramienta puede llevar la credencial de Zabbix de un usuario. Zabbix aplica entonces **sus** permisos
de hosts, su rol y sus restricciones de API.

- **Pasarela** (globo de chat de Zabbix): el módulo envía la sesión del usuario conectado y la pasarela
  la reenvía en cada llamada. No se guarda entre peticiones.
- **Consola** (`cliente.py`): pon tu token de API personal en `ZABBIX_USER_TOKEN`.
- La credencial viaja solo en el `_meta` de la llamada MCP (`zabbix-mcp/delegated-auth`). **Nunca** se
  envía al LLM ni se escribe en los logs.
- Si Zabbix rechaza la credencial (sesión caducada, rol sin acceso a la API), la herramienta devuelve el
  error. **No** se reintenta con la credencial de servicio del MCP.

| Fichero | Para qué |
|---|---|
| `cliente.py` | Chat interactivo o una sola pregunta; bucle LLM → herramientas MCP → LLM. |
| `analizar_contexto.py` | Analiza `logs/llm_requests.jsonl`: tamaño de cada parte y contexto completo de cada petición. |
| `pasarela.py` | API HTTP (`/chat`, `/reset`, `/health`) para el globo de chat de Zabbix ([../zabbix-module-mcpchat](../zabbix-module-mcpchat)). Usa el mismo `.env` (variables `GATEWAY_*`) y una conversación por usuario de Zabbix. |
| `.env.example` | Toda la configuración (LLM, MCP, reducción de tokens, logging). |
| `mcp-pasarela.service` | Unidad systemd para dejar `pasarela.py` como servicio en Linux (ver [Pasarela como servicio en Linux](#pasarela-como-servicio-en-linux)). |

## Instalación

```powershell
cd mcp-client
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env      # y edita los valores
```

Arranca el servidor MCP (en otra terminal) o usa `MCP_TRANSPORT=stdio` para que el cliente lo lance:

```powershell
..\zabbix-mcp-server\.venv\Scripts\zabbix-mcp-server.exe --config ..\zabbix-mcp-server\config.toml --transport http
```

## Uso

```powershell
python cliente.py                                  # chat (/tokens, /tools, /reset, /salir)
python cliente.py -p "¿Qué problemas graves hay ahora?"
python cliente.py --herramientas                   # tokens de cada herramienta: completa vs. reducida
python analizar_contexto.py                        # resumen de todas las peticiones registradas
python analizar_contexto.py --sesion ULTIMA        # solo la última sesión
python analizar_contexto.py --ver 2                # contexto COMPLETO de la petición nº 2 (última sesión)
```

## Pasarela como servicio en Linux

Para el módulo de chat de Zabbix, `pasarela.py` debe estar siempre arrancada en una máquina que el
**servidor web del frontend** alcance por HTTP (`gateway_url` del módulo). Con systemd arranca al
encender y se reinicia si se cae. Ejemplo instalando en `/opt/mcp-chat/mcp-client`:

```bash
# 1. Copiar e instalar dependencias
sudo mkdir -p /opt/mcp-chat
sudo cp -r mcp-client /opt/mcp-chat/
cd /opt/mcp-chat/mcp-client
sudo python3 -m venv .venv
sudo .venv/bin/pip install -r requirements.txt
sudo cp .env.example .env && sudo nano .env   # LLM_*, MCP_URL, MCP_TOKEN, GATEWAY_API_KEY...
sudo mkdir -p logs                            # debe existir: el servicio solo puede escribir ahí

# 2. Usuario sin privilegios y permisos
sudo useradd --system --no-create-home --shell /usr/sbin/nologin mcpchat
sudo chown -R mcpchat: /opt/mcp-chat/mcp-client
sudo chmod 600 .env                           # contiene claves

# 3. Probar a mano (Ctrl+C para salir) y comprobar desde otra terminal
sudo -u mcpchat .venv/bin/python pasarela.py
curl http://127.0.0.1:8770/health

# 4. Instalar el servicio
sudo cp mcp-pasarela.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mcp-pasarela
```

Operación habitual:

```bash
systemctl status mcp-pasarela
sudo systemctl restart mcp-pasarela   # tras cambiar el .env
journalctl -u mcp-pasarela -f         # salida del servicio; el detalle está en logs/cliente.log
```

Notas:

- Si instalas en otra ruta o con otro usuario, ajusta `User`, `Group`, `WorkingDirectory`, `ExecStart`
  y `ReadWritePaths` en [mcp-pasarela.service](mcp-pasarela.service). `WorkingDirectory` importa: los
  logs (`LOG_DIR=logs`) son relativos a él. Si cambias `LOG_DIR`, cambia también `ReadWritePaths`.
- Con `MCP_TRANSPORT=http` el servidor MCP debe estar arrancado aparte (en `MCP_URL`). Si corre en la
  misma máquina como servicio, descomenta las líneas `After=`/`Wants=` de la unidad.
- Si el frontend está en otra máquina: `GATEWAY_HOST=0.0.0.0` en el `.env` y cortafuegos que solo deje
  entrar al puerto `GATEWAY_PORT` desde la IP del frontend.

## Logging

| Salida | Contenido |
|---|---|
| Consola | Con `LOG_LEVEL=INFO`, por petición: tokens estimados por parte (herramientas, system, user, assistant, tool), `usage` real del proveedor, cada llamada MCP con sus argumentos y el tamaño de su resultado. Con `DEBUG`, además el desglose completo y los resultados. |
| `logs/cliente.log` | Lo mismo, siempre a nivel DEBUG y con fecha y hora. |
| `logs/llm_requests.jsonl` | Una línea por petición al LLM: proveedor, modelo, mensajes y herramientas enviados (en formato OpenAI, el mismo para cualquier proveedor), desglose, `usage` y respuesta. Desactívalo con `LOG_LLM_PAYLOADS=false`. |
| `logs/mcp_server_stderr.log` | Salida de errores del servidor MCP cuando se lanza por stdio. |

Las estimaciones usan ~4 caracteres/token. La cifra real es `prompt_tokens` de `usage` (lo que
LangChain llama `input_tokens`), si el proveedor la devuelve. La representación neutral muestra el
contenido enviado, pero cada proveedor lo empaqueta a su manera, así que el número real puede variar.
Los logs contienen datos de Zabbix y las preguntas: no los compartas sin revisarlos.

## Observabilidad (panel en tiempo real y Phoenix)

La pasarela registra cada pregunta en SQLite (`OBS_DB`, por defecto `logs/observabilidad.db`):
usuario de Zabbix, conversación, pregunta, respuesta o error, tokens (entrada, salida, cacheados),
cada llamada al LLM y cada llamada a herramienta con sus argumentos, duración, tamaño, si se recortó,
si falló y su resultado (recortado a `OBS_TOOL_RESULT_MAX_CHARS`). La credencial de Zabbix del
usuario **nunca** se guarda; solo si la pregunta se hizo con ella (columna «Credencial»).
Los datos se purgan pasados `OBS_RETENTION_DAYS` días. Desactívalo con `OBS_ENABLED=false`.

**Panel** (Streamlit, lee la BD en solo lectura y se refresca cada 5/15/60 s):

```powershell
pip install -r requirements.txt
streamlit run panel.py                 # http://localhost:8501
```

| Pestaña | Qué muestra |
|---|---|
| En vivo | KPIs del periodo (preguntas, usuarios, tokens, % errores, latencia p50/p95) y las últimas preguntas. |
| Consumo | Tokens y preguntas por hora/día, por usuario y por modelo; rondas LLM por pregunta; coste si defines `PANEL_PRICE_*`. |
| Herramientas | Llamadas, % error, latencia, tamaño medio y % recortado por herramienta (para ajustar `TOOLS_ALLOW` y `TOOL_RESULT_MAX_CHARS`). |
| Conversaciones | Búsqueda y detalle de cada pregunta: línea temporal LLM → herramienta con argumentos y resultados, y la respuesta. |
| Errores | Preguntas fallidas o sin respuesta final y herramientas con error. |

Streamlit no tiene usuarios: déjalo en `127.0.0.1` (o tras un proxy con autenticación) y define
`PANEL_PASSWORD` si lo expones. En Linux: [mcp-panel.service](mcp-panel.service), igual que la pasarela.

**Phoenix** (opcional, [Arize Phoenix](https://github.com/arize-ai/phoenix)): trazas OpenTelemetry de
cada llamada al LLM (mensajes completos, herramientas, tokens), agrupadas por conversación (sesión) y
usuario. Útil para depurar por qué el modelo eligió una herramienta; el panel sirve para el uso diario.

```powershell
pip install -r requirements-phoenix.txt      # en el entorno de la pasarela
pip install arize-phoenix; phoenix serve     # o: docker run -p 6006:6006 arizephoenix/phoenix
# .env: PHOENIX_ENABLED=true  PHOENIX_COLLECTOR_ENDPOINT=http://127.0.0.1:6006
```

Solo se instrumenta LangChain: el `_meta` de las llamadas MCP (donde va la credencial delegada) no se toca.

## Cómo reducir los tokens

Medido con el servidor 1.42.2 (estimación ~4 caracteres/token, coste fijo de **cada** petición al LLM):

| Escenario | Herramientas | ~Tokens por petición |
|---|---:|---:|
| Catálogo completo (sin filtrar) | 254 | 195.000 |
| `TOOLS_ALLOW` de `.env.example` | 9 | 10.300 |
| + recortes de descripciones y simplificación del esquema | 9 | 7.000 |
| + `schema_constraints = false` en el servidor | 9 | 4.500 |

Ojo: una pregunta que usa herramientas genera **varias** peticiones (una por ronda), y cada una reenvía
las herramientas, el system prompt y todo el historial.

Por orden de impacto:

1. **Envía solo las herramientas necesarias** (`TOOLS_ALLOW`, admite `*`, p. ej. `host_*,problem_*`).
   Es con diferencia lo que más ahorra. Prefiere las herramientas "vista" que resuelven una pregunta en
   una llamada: `problem_active_get`, `host_status_get`, `hostgroup_overview_get`,
   `infrastructure_summary_get`, `item_history_summary_get`. Ahorran tanto catálogo como rondas.
   También se puede filtrar en el servidor (`[server] tools = [...]` / `disabled_tools`) o con un token de
   scope reducido; así se aplica a cualquier cliente.
2. **Esquemas más ligeros**: el cliente ya quita `title`, los `anyOf` con `null` y los `default: null`.
   `TOOL_DESC_MAX_CHARS`, `PARAM_DESC_MAX_CHARS` y `DROP_PARAMS` recortan más. Si el modelo empieza a
   usar mal una herramienta, sube esos límites.
   En el servidor, `schema_constraints = false` quita las listas de valores permitidos de `output`/`filter`
   (−35 % adicional). El servidor sigue validando, y el LLM recibe un error descriptivo si se equivoca.
3. **Respuestas pequeñas de las herramientas**: deja `compact_output = true` en el servidor (devuelve
   solo campos clave), baja `response_max_chars` en el servidor o `TOOL_RESULT_MAX_CHARS` en el cliente,
   y pide en el system prompt que use `limit`, `output` con campos concretos y `count`.
4. **Historial acotado**: `HISTORY_MAX_TURNS` limita los turnos que se reenvían y
   `HISTORY_TOOL_RESULT_MAX_CHARS` recorta los resultados de preguntas anteriores (suelen ser lo más
   pesado). `/reset` empieza de cero al cambiar de tema.
5. **System prompt**: `INCLUDE_SERVER_INSTRUCTIONS=false` ahorra ~500 tokens por petición, aunque esas
   instrucciones ayudan al modelo a usar bien la API. Pruébalo antes de quitarlas.
6. **Caché de prompt del proveedor**: OpenAI y otros cachean automáticamente el prefijo repetido
   (herramientas + system); se ve en la columna `cache`. Mantener herramientas y system estables
   entre peticiones lo aprovecha (cuestan menos, aunque siguen ocupando contexto).
7. **Menos rondas**: `MAX_TOOL_ROUNDS` evita bucles largos, y un modelo con mejor uso de herramientas
   suele resolver la pregunta en menos rondas.

Flujo recomendado: ejecuta `python cliente.py --herramientas` para ver qué pesa, haz unas preguntas
reales y revisa `python analizar_contexto.py` para ver si el coste está en herramientas, historial o
resultados. Ajusta el `.env` según lo que veas.
