"""Panel de observabilidad del chat de Zabbix (Streamlit).

Lee en solo lectura la base de datos que escribe la pasarela (OBS_DB, por defecto
logs/observabilidad.db) y se refresca solo: preguntas en tiempo real, consumo de tokens,
uso y errores de herramientas, y el detalle de cada conversación.

Uso:
    streamlit run panel.py                                   # http://localhost:8501
    streamlit run panel.py --server.address 0.0.0.0 --server.port 8501

Si PANEL_PASSWORD está definida en .env, el panel pide esa contraseña.

Las preguntas, respuestas y resultados vienen de usuarios, del LLM y de Zabbix: son datos no
confiables y se muestran como texto plano (nunca como Markdown/HTML salvo que se pida).
"""

from __future__ import annotations

import hmac
import json
import os
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

import observabilidad

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
OBS = observabilidad.ObsSettings.from_env()

REFRESH_OPTIONS = {"5 s": 5, "15 s": 15, "60 s": 60, "Pausado": None}
PERIODS = {"1 h": timedelta(hours=1), "24 h": timedelta(days=1), "7 días": timedelta(days=7),
           "30 días": timedelta(days=30), "Personalizado": None}
STATUS_TEXT = {"ok": "correcta", "max_rounds": "sin respuesta final", "error": "error"}
STATUS_ICON = {"ok": ":material/check_circle:", "max_rounds": ":material/warning:", "error": ":material/error:"}
FEED_ROWS = 100
SEARCH_ROWS = 200
ERROR_ROWS = 500

# Columnas ligeras: los textos largos (respuesta, resultados) solo se leen en el detalle.
TURN_COLUMNS = ("t.id, t.conversation, t.user, t.delegated, t.ts_start, t.elapsed_s, t.question, t.status,"
                " t.error, t.provider, t.model, t.llm_requests, t.prompt_tokens, t.completion_tokens,"
                " t.cached_tokens, t.n_tools")


def _price(name: str) -> float:
    value = os.getenv(name, "").strip()
    try:
        return float(value) if value else 0.0
    except ValueError:
        return 0.0


PRICE_IN = _price("PANEL_PRICE_INPUT_PER_MTOK")
PRICE_OUT = _price("PANEL_PRICE_OUTPUT_PER_MTOK")
PRICE_CACHED = _price("PANEL_PRICE_CACHED_PER_MTOK")
HAS_PRICES = any((PRICE_IN, PRICE_OUT, PRICE_CACHED))


# ---------------------------------------------------------------------------
# Acceso y datos
# ---------------------------------------------------------------------------

def check_password() -> None:
    expected = os.getenv("PANEL_PASSWORD", "")
    if not expected or st.session_state.get("auth_ok"):
        return
    st.title("Observabilidad del chat de Zabbix")
    given = st.text_input("Contraseña", type="password")
    if given and hmac.compare_digest(given.encode(), expected.encode()):
        st.session_state["auth_ok"] = True
        st.rerun()
    elif given:
        st.error("Contraseña incorrecta")
    st.stop()


def query(sql: str, params: list | tuple = ()) -> pd.DataFrame:
    # closing(): el "with" de sqlite3 solo cierra la transacción, no la conexión.
    with closing(sqlite3.connect(f"file:{OBS.db_path.as_posix()}?mode=ro", uri=True)) as conn:
        return pd.read_sql_query(sql, conn, params=list(params))


def period_bounds(period: str, custom: tuple | date | None) -> tuple[datetime, datetime | None]:
    """Inicio y fin del periodo. Fin None = sin límite (incluye lo que llegue después,
    también pasada la medianoche con el panel abierto)."""
    now = datetime.now()
    span = PERIODS.get(period)
    if span is not None:
        return now - span, None
    if isinstance(custom, tuple) and custom:
        start, end = custom[0], custom[-1]
    else:
        start = end = custom if isinstance(custom, date) else now.date()
    start_dt = datetime.combine(start, datetime.min.time())
    end_dt = None if end >= now.date() else datetime.combine(end + timedelta(days=1), datetime.min.time())
    return start_dt, end_dt


def build_where(start: datetime, end: datetime | None, users: list[str], models: list[str]) -> tuple[str, list]:
    clauses, params = ["t.ts_start >= ?"], [start.isoformat()]
    if end is not None:
        clauses.append("t.ts_start < ?")
        params.append(end.isoformat())
    if users:
        clauses.append(f"t.user IN ({','.join('?' * len(users))})")
        params += users
    if models:
        clauses.append(f"t.model IN ({','.join('?' * len(models))})")
        params += models
    return " AND ".join(clauses), params


def load_turns(where: str, params: list) -> pd.DataFrame:
    df = query(
        f"SELECT {TURN_COLUMNS}, (SELECT group_concat(name, ', ') FROM tool_calls WHERE turn_id = t.id)"
        f" AS tools FROM turns t WHERE {where} ORDER BY t.ts_start DESC", params)
    if not df.empty:
        df["ts"] = pd.to_datetime(df["ts_start"])
        df["cost"] = cost(df)
        df["estado"] = df["status"].map(STATUS_TEXT).fillna(df["status"])
    return df


def load_tool_stats(where: str, params: list) -> pd.DataFrame:
    return query(
        "SELECT tc.name, tc.elapsed_s, tc.result_chars, tc.truncated, tc.is_error, t.user"
        f" FROM tool_calls tc JOIN turns t ON t.id = tc.turn_id WHERE {where}", params)


def cost(df: pd.DataFrame) -> pd.Series:
    """Coste estimado (los tokens cacheados están incluidos en prompt_tokens)."""
    cached = df["cached_tokens"].fillna(0)
    return ((df["prompt_tokens"].fillna(0) - cached) * PRICE_IN + cached * PRICE_CACHED
            + df["completion_tokens"].fillna(0) * PRICE_OUT) / 1_000_000


def fmt_int(value: float | None) -> str:
    return f"{int(value or 0):,}".replace(",", ".")


def like_pattern(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


# ---------------------------------------------------------------------------
# Pestañas
# ---------------------------------------------------------------------------

def tab_live(turns: pd.DataFrame) -> None:
    if turns.empty:
        st.info("No hay preguntas en el periodo seleccionado.")
        return
    errors = (turns["status"] != "ok").mean() * 100
    with st.container(horizontal=True):
        st.metric("Preguntas", fmt_int(len(turns)), border=True)
        st.metric("Usuarios", fmt_int(turns["user"].nunique()), border=True)
        st.metric("Tokens entrada", fmt_int(turns["prompt_tokens"].sum()), border=True)
        st.metric("Tokens salida", fmt_int(turns["completion_tokens"].sum()), border=True)
        st.metric("Cacheados", fmt_int(turns["cached_tokens"].sum()), border=True)
        st.metric("Errores", f"{errors:.1f} %", border=True)
        st.metric("Latencia p50 / p95", f"{turns['elapsed_s'].median():.1f} / "
                                        f"{turns['elapsed_s'].quantile(0.95):.1f} s", border=True)
        if HAS_PRICES:
            st.metric("Coste estimado", f"{turns['cost'].sum():.4f}", border=True)

    st.subheader("Últimas preguntas")
    feed = turns.head(FEED_ROWS).assign(
        hora=lambda d: d["ts"].dt.strftime("%d/%m %H:%M:%S"),
        tokens=lambda d: d["prompt_tokens"] + d["completion_tokens"],
        permisos=lambda d: d["delegated"].map({1: "usuario", 0: "servicio"}),
    )
    st.dataframe(
        feed[["estado", "hora", "user", "question", "tools", "llm_requests", "tokens", "elapsed_s", "permisos"]],
        hide_index=True, alt="Últimas preguntas al chat",
        column_config={
            "estado": "Estado", "hora": "Hora", "user": "Usuario",
            "question": st.column_config.TextColumn("Pregunta", width="large"),
            "tools": "Herramientas", "llm_requests": "Rondas LLM", "tokens": "Tokens",
            "elapsed_s": st.column_config.NumberColumn("Duración (s)", format="%.1f"),
            "permisos": "Credencial",
        })


def tab_usage(turns: pd.DataFrame, hourly: bool) -> None:
    if turns.empty:
        st.info("No hay datos en el periodo seleccionado.")
        return
    unit = "por hora" if hourly else "por día"
    by_time = turns.set_index("ts").resample("h" if hourly else "D").agg(
        {"id": "count", "prompt_tokens": "sum", "completion_tokens": "sum", "cached_tokens": "sum"})
    left, right = st.columns(2)
    with left, st.container(border=True):
        st.subheader(f"Tokens {unit}")
        st.line_chart(by_time[["prompt_tokens", "completion_tokens", "cached_tokens"]].rename(columns={
            "prompt_tokens": "entrada", "completion_tokens": "salida", "cached_tokens": "cacheados"}),
            alt=f"Tokens de entrada, salida y cacheados {unit}")
    with right, st.container(border=True):
        st.subheader(f"Preguntas {unit}")
        st.bar_chart(by_time["id"].rename("preguntas"), alt=f"Número de preguntas {unit}")

    agg = {"preguntas": ("id", "count"), "tokens_entrada": ("prompt_tokens", "sum"),
           "tokens_salida": ("completion_tokens", "sum"), "cacheados": ("cached_tokens", "sum"),
           "rondas_medias": ("llm_requests", "mean"), "duracion_media_s": ("elapsed_s", "mean"),
           "errores": ("status", lambda s: int((s != "ok").sum()))}
    if HAS_PRICES:
        agg["coste"] = ("cost", "sum")
    with st.container(border=True):
        st.subheader("Por usuario")
        st.dataframe(turns.groupby("user").agg(**agg).sort_values("tokens_entrada", ascending=False),
                     alt="Consumo por usuario")
    with st.container(border=True):
        st.subheader("Por modelo")
        st.dataframe(turns.groupby(["provider", "model"]).agg(**agg), alt="Consumo por modelo")

    left, right = st.columns(2)
    with left, st.container(border=True):
        st.subheader("Rondas LLM por pregunta")
        st.bar_chart(turns["llm_requests"].value_counts().sort_index().rename("preguntas"),
                     alt="Distribución del número de llamadas al LLM por pregunta")
    with right, st.container(border=True):
        st.subheader("Tokens de entrada por pregunta")
        st.bar_chart(pd.cut(turns["prompt_tokens"], bins=10).value_counts().sort_index()
                     .rename("preguntas").rename(index=str),
                     alt="Distribución de tokens de entrada por pregunta")
    if not HAS_PRICES:
        st.caption("Define PANEL_PRICE_INPUT_PER_MTOK / PANEL_PRICE_OUTPUT_PER_MTOK / "
                   "PANEL_PRICE_CACHED_PER_MTOK en .env para ver costes estimados.")


def tab_tools(where: str, params: list) -> None:
    tools = load_tool_stats(where, params)
    if tools.empty:
        st.info("No hay llamadas a herramientas en el periodo seleccionado.")
        return
    stats = tools.groupby("name").agg(
        llamadas=("name", "count"), usuarios=("user", "nunique"),
        error_pct=("is_error", lambda s: s.mean() * 100),
        latencia_media_s=("elapsed_s", "mean"), latencia_p95_s=("elapsed_s", lambda s: s.quantile(0.95)),
        resultado_medio_chars=("result_chars", "mean"), recortado_pct=("truncated", lambda s: s.mean() * 100),
    ).sort_values("llamadas", ascending=False)
    with st.container(border=True):
        st.subheader("Llamadas por herramienta")
        st.bar_chart(stats["llamadas"], horizontal=True, alt="Número de llamadas por herramienta")
    st.dataframe(stats, alt="Estadísticas por herramienta", column_config={
        "llamadas": "Llamadas", "usuarios": "Usuarios",
        "error_pct": st.column_config.NumberColumn("% error", format="%.1f"),
        "recortado_pct": st.column_config.NumberColumn("% recortado", format="%.1f",
                                                       help="Resultado recortado por TOOL_RESULT_MAX_CHARS"),
        "latencia_media_s": st.column_config.NumberColumn("Latencia media (s)", format="%.2f"),
        "latencia_p95_s": st.column_config.NumberColumn("Latencia p95 (s)", format="%.2f"),
        "resultado_medio_chars": st.column_config.NumberColumn("Resultado medio (car.)", format="%d"),
    })


def show_turn(turn_id: int) -> None:
    turn = query("SELECT * FROM turns WHERE id = ?", [turn_id]).iloc[0]
    status = str(turn["status"])
    with st.container(horizontal=True):
        st.metric("Estado", STATUS_TEXT.get(status, status), icon=STATUS_ICON.get(status), border=True)
        st.metric("Duración", f"{turn['elapsed_s']:.1f} s", border=True)
        st.metric("Rondas LLM", int(turn["llm_requests"]), border=True)
        st.metric("Tokens entrada / salida",
                  f"{fmt_int(turn['prompt_tokens'])} / {fmt_int(turn['completion_tokens'])}", border=True)
        st.metric("Credencial", "usuario" if turn["delegated"] else "servicio", border=True)
    # Texto plano: el usuario, la pregunta y la respuesta no son confiables.
    st.text(f"{turn['user']} · {str(turn['ts_start'])[:19].replace('T', ' ')} · "
            f"{turn['provider']}:{turn['model']} · conversación {turn['conversation']}")
    with st.container(border=True):
        st.markdown("**Pregunta**")
        st.text(turn["question"])

    st.markdown("**Línea temporal**")
    llm = query("SELECT * FROM llm_calls WHERE turn_id = ?", [turn_id]).assign(kind="llm")
    tools = query("SELECT * FROM tool_calls WHERE turn_id = ?", [turn_id]).assign(kind="tool")
    events = pd.concat([llm, tools], ignore_index=True).sort_values(["ts", "kind"])
    for _, ev in events.iterrows():
        if ev["kind"] == "llm":
            st.markdown(
                f":material/psychology: **LLM #{int(ev['n'])}** · {ev['elapsed_s']:.1f} s · "
                f"entrada {fmt_int(ev['prompt_tokens'])} (cacheados {fmt_int(ev['cached_tokens'])}) · "
                f"salida {fmt_int(ev['completion_tokens'])} · ~{fmt_int(ev['est_tools'])} tok de herramientas, "
                f"{fmt_int(ev['n_messages'])} mensajes")
        else:
            flags = (" · error" if ev["is_error"] else "") + (" · recortado" if ev["truncated"] else "")
            # El nombre de la herramienta lo genera el LLM: se escapa para la etiqueta Markdown.
            name = str(ev["name"]).replace("\\", "\\\\").replace("*", "\\*").replace("_", "\\_")
            with st.expander(f"{name} · {ev['elapsed_s']:.2f} s · {fmt_int(ev['result_chars'])} caracteres{flags}",
                             icon=":material/error:" if ev["is_error"] else ":material/build:",
                             key=f"tool_{turn_id}_{int(ev['id'])}"):
                st.markdown("Argumentos")
                try:
                    st.json(json.loads(ev["args"] or "{}"))
                except json.JSONDecodeError:
                    st.code(ev["args"], language=None)
                st.markdown("Resultado")
                st.code(ev["result"] or "", language="json", wrap_lines=True)

    if status == "error":
        st.error(str(turn["error"]), title="Error")
        return
    with st.container(border=True):
        st.markdown("**Respuesta**")
        rendered = st.toggle("Ver con formato Markdown", key=f"md_{turn_id}",
                             help="El navegador cargará las imágenes o enlaces que incluya la respuesta.")
        if rendered:
            st.markdown(turn["answer"] or "")
        else:
            st.text(turn["answer"] or "")


def tab_conversations(where: str, params: list) -> None:
    text = st.text_input("Buscar en preguntas y respuestas", type="search", key="search")
    sql = (f"SELECT t.id, t.ts_start, t.user, t.status, t.question FROM turns t WHERE {where}")
    args = list(params)
    if text:
        sql += " AND (t.question LIKE ? ESCAPE '\\' OR t.answer LIKE ? ESCAPE '\\')"
        args += [like_pattern(text)] * 2
    view = query(sql + f" ORDER BY t.ts_start DESC LIMIT {SEARCH_ROWS}", args)
    if view.empty:
        st.info("Sin coincidencias." if text else "No hay preguntas en el periodo seleccionado.")
        return
    labels: dict[int, str] = {}
    for row in view.to_dict("records"):
        status = str(row["status"])
        labels[int(row["id"])] = (f"{str(row['ts_start'])[5:19].replace('T', ' ')} · "
                                  f"{STATUS_TEXT.get(status, status)} · {row['user']} · "
                                  f"{str(row['question'] or '')[:80]}")
    turn_id: int | None = st.selectbox(
        "Pregunta", list(labels), format_func=lambda i: labels[i], key="turn",
        help=f"Las {SEARCH_ROWS} más recientes que coinciden con los filtros.")
    if turn_id is None:
        return
    show_turn(turn_id)

    others = query("SELECT ts_start, user, status, question, prompt_tokens, completion_tokens FROM turns"
                   " WHERE conversation = (SELECT conversation FROM turns WHERE id = ?) ORDER BY ts_start",
                   [turn_id])
    if len(others) > 1:
        st.subheader("Conversación completa")
        st.dataframe(others.assign(status=others["status"].map(STATUS_TEXT).fillna(others["status"])),
                     hide_index=True, alt="Todas las preguntas de esta conversación")


def tab_errors(turns: pd.DataFrame, where: str, params: list) -> None:
    bad_turns = turns[turns["status"] != "ok"] if not turns.empty else turns
    st.subheader(f"Preguntas fallidas o sin respuesta final ({len(bad_turns)})")
    if not bad_turns.empty:
        st.dataframe(bad_turns[["id", "ts_start", "user", "estado", "error", "question", "tools"]],
                     hide_index=True, alt="Preguntas fallidas o sin respuesta final")
    bad_tools = query(
        "SELECT tc.turn_id, tc.ts, t.user, tc.name, tc.args, substr(tc.result, 1, 300) AS result"
        f" FROM tool_calls tc JOIN turns t ON t.id = tc.turn_id WHERE {where} AND tc.is_error = 1"
        f" ORDER BY tc.ts DESC LIMIT {ERROR_ROWS}", params)
    st.subheader(f"Herramientas con error ({len(bad_tools)}{'+' if len(bad_tools) == ERROR_ROWS else ''})")
    if not bad_tools.empty:
        st.dataframe(bad_tools, hide_index=True, alt="Llamadas a herramientas con error")


# ---------------------------------------------------------------------------
# Página
# ---------------------------------------------------------------------------

def filters() -> tuple[str, list, bool]:
    """Filtros del periodo/usuario/modelo. Se evalúan en cada refresco (opciones y fechas al día)."""
    with st.container(horizontal=True, vertical_alignment="bottom"):
        period = st.segmented_control("Periodo", list(PERIODS), default="24 h", required=True, key="period")
        custom = None
        if period == "Personalizado":
            today = date.today()
            custom = st.date_input("Fechas", (today - timedelta(days=7), today), max_value=today,
                                   key="custom_range")
        user_options: list[str] = query("SELECT DISTINCT user FROM turns ORDER BY user")["user"].tolist()
        model_options: list[str] = query("SELECT DISTINCT model FROM turns ORDER BY model")["model"].tolist()
        users: list[str] = st.multiselect("Usuarios", user_options, key="users", placeholder="Todos")
        models: list[str] = st.multiselect("Modelos", model_options, key="models", placeholder="Todos")
    start, end = period_bounds(period, custom)
    where, params = build_where(start, end, users, models)
    hourly = ((end or datetime.now()) - start) <= timedelta(days=2)
    return where, params, hourly


def dashboard() -> None:
    where, params, hourly = filters()
    st.caption(f"Actualizado {datetime.now():%H:%M:%S}")
    live, usage, tools_tab, convs, errs = st.tabs(
        [":material/monitoring: En vivo", ":material/token: Consumo", ":material/build: Herramientas",
         ":material/forum: Conversaciones", ":material/error: Errores"], key="tab", on_change="rerun")
    # Solo se calcula la pestaña abierta.
    turns = load_turns(where, params) if (live.open or usage.open or errs.open) else pd.DataFrame()
    if live.open:
        with live:
            tab_live(turns)
    if usage.open:
        with usage:
            tab_usage(turns, hourly)
    if tools_tab.open:
        with tools_tab:
            tab_tools(where, params)
    if convs.open:
        with convs:
            tab_conversations(where, params)
    if errs.open:
        with errs:
            tab_errors(turns, where, params)


st.set_page_config(page_title="Observabilidad chat Zabbix", page_icon=":material/monitoring:", layout="wide")
check_password()
st.title("Observabilidad del chat de Zabbix")

if not OBS.db_path.exists():
    st.warning(f"Aún no existe {OBS.db_path}. Arranca la pasarela (OBS_ENABLED=true) y haz una pregunta.")
    st.stop()

with st.sidebar:
    refresh_label = st.segmented_control("Refresco", list(REFRESH_OPTIONS), default="5 s", required=True,
                                         key="refresh")
    if OBS.phoenix_enabled:
        st.link_button("Trazas en Phoenix", os.getenv("PANEL_PHOENIX_URL") or OBS.phoenix_endpoint,
                       icon=":material/timeline:", width="stretch")
    st.caption(f"BD: {OBS.db_path}")

st.fragment(run_every=REFRESH_OPTIONS[refresh_label])(dashboard)()
