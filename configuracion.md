He implementado la opción A y el cliente ya usa LangChain. En el Zabbix 7.0.32 de Docker, con la misma pregunta («¿Qué hosts hay?»), `Admin` ve 3 hosts y `operador1`, que solo tiene permiso sobre un grupo, ve únicamente `web-a`. Las pruebas usaron un LLM simulado; con un modelo real no está probado.

**Servidor MCP (fork)**
- La lógica nueva está en [delegation.py](zabbix-mcp-server/src/zabbix_mcp/delegation.py). En el código original solo hay enganches pequeños marcados `Fork:`, listados en el registro de personalizaciones de [AGENTS.md](AGENTS.md).
- **Nueva opción `[server] delegated_auth`:**
  - `"off"`: valor por defecto. Rechaza las llamadas que traigan la credencial de un usuario, en lugar de ignorarla y responder con la cuenta de servicio.
  - `"allowed"`: usa la credencial del usuario si viene y, si no, la cuenta de servicio.
  - `"required"`: rechaza las llamadas sin credencial de usuario. Es lo que conviene para el chat.
- **Sin vuelta atrás:** si Zabbix rechaza la sesión del usuario (caducada o sin acceso a la API), se devuelve el error y no se reintenta con la cuenta de servicio.
- **`graph_render`:** ahora comprueba primero, con la credencial del usuario, que este puede ver la gráfica.
- **Tests:** 18 tests nuevos, todos pasan. En la batería completa fallan 18 tests (2 fallos y 16 errores), pero fallan igual en una copia limpia del original: dependen de funciones de Linux y no funcionan en Windows.

**Cliente y pasarela con LangChain**
- El modelo se elige con `LLM_PROVIDER` y `LLM_MODEL`: OpenAI o compatibles, Anthropic, Ollama, Gemini, Azure, Mistral, Bedrock… Los proveedores están en el [README del cliente](mcp-client/README.md).
- Logging, reducción de tokens y `analizar_contexto.py` funcionan igual que antes.
- La credencial del usuario va solo en los metadatos de cada llamada al MCP. Comprobé que no aparece en ningún log y nunca llega al LLM.
- En la consola también puedes consultar con tus permisos: pon tu token de API personal en `ZABBIX_USER_TOKEN`. Probado con el token de `operador1`: ve solo `web-a`.

**Módulo de Zabbix**
- Envía la sesión del usuario conectado a la pasarela, de servidor a servidor, siempre que la nueva opción `delegate_user_session` esté activada (lo está por defecto).

**Probado de punta a punta en Zabbix 7.0.32**

| Caso | Resultado |
|---|---|
| Admin pregunta por los hosts | `Zabbix server`, `db-b`, `web-a` |
| operador1, misma pregunta | solo `web-a` |
| Llamada sin credencial | rechazada antes de consultar Zabbix |
| Sesión inventada | «Session terminated» de Zabbix, sin reintento con la cuenta de servicio |

**Ten en cuenta**
- **El chat hereda lo que el usuario puede hacer en Zabbix.** Si un usuario tiene permisos de escritura y das al LLM herramientas de escritura en `TOOLS_ALLOW`, podría modificar Zabbix en su nombre.
- **El MCP sigue necesitando su `api_token`**, aunque solo para tareas internas: detectar la versión de Zabbix, la zona horaria y `health_check`.
- **Para activarlo en tu entorno:** pon `delegated_auth = "required"` en el `config.toml` del MCP. Si ya registraste el módulo en un Zabbix, añade `delegate_user_session` a su configuración con `module.update`. Si no lo haces, se aplica el valor por defecto (activado).
- **En `zabbix-mcp-server/.env.example` hay un carácter suelto** («º» tras `ADMIN_PORT=9090`) que no he puesto yo. Parece una pulsación accidental en el editor; no lo he tocado.

El Zabbix de prueba sigue en http://localhost:8088 con los datos de prueba (`operador1` / `Pruebas.Seguras.2026!`). Para borrarlo: `docker compose -p mcpchat-test down -v`.