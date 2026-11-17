"""Panel de observabilidad del chat de Zabbix (Streamlit).

Lee en solo lectura la base de datos que escribe la pasarela (OBS_DB, por defecto
logs/observabilidad.db) y se refresca solo: preguntas en tiempo real, consumo de tokens,
uso y errores de herramientas, y el detalle de cada conversación.

Uso:
    streamlit run panel.py                                   # http://localhost:8501
    streamlit run panel.py --server.address 0.0.0.0 --server.port 8501

Si PANEL_PASSWORD está definida en .env, el panel pide esa contraseña.
"""

from __future__ import annotations

import hmac
import json
import os
import sqlite3
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
STATUS_ICON = {"ok": "✅", "max_rounds": "⚠️", "error": "❌"}


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


def connect() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{OBS.db_path.as_posix()}?mode=ro", uri=True)


def query(sql: str, params: list | tuple = ()) -> pd.DataFrame:
    with connect() as conn:
        return pd.read_sql_query(sql, conn, params=list(params))


def build_where(start: date, end: date, users: list[str], models: list[str]) -> tuple[str, list]:
    clauses = ["t.ts_start >= ?", "t.ts_start < ?"]
    params: list = [datetime.combine(start, datetime.min.time()).isoformat(),
                    datetime.combine(end + timedelta(days=1), datetime.min.time()).isoformat()]
    if users:
        clauses.append(f"t.user IN ({','.join('?' * len(users))})")
        params += users
    if models:
        clauses.append(f"t.model IN ({','.join('?' * len(models))})")
        params += models
    return " AND ".join(clauses), params


def load_turns(where: str, params: list) -> pd.DataFrame:
    df = query(
        "SELECT t.*, (SELECT group_concat(name, ', ') FROM tool_calls WHERE turn_id = t.id) AS tools"
        f" FROM turns t WHERE {where} ORDER BY t.ts_start DESC", params)
    if not df.empty:
        df["ts"] = pd.to_datetime(df["ts_start"])
        df["cost"] = cost(df)
    return df


def load_tool_calls(where: str, params: list) -> pd.DataFrame:
    return query(
        "SELECT tc.*, t.user, t.conversation FROM tool_calls tc JOIN turns t ON t.id = tc.turn_id"
        f" WHERE {where} ORDER BY tc.ts DESC", params)


def cost(df: pd.DataFrame) -> pd.Series:
    """Coste estimado (los tokens cacheados están incluidos en prompt_tokens)."""
    cached = df["cached_tokens"].fillna(0)
    return ((df["prompt_tokens"].fillna(0) - cached) * PRICE_IN + cached * PRICE_CACHED
            + df["completion_tokens"].fillna(0) * PRICE_OUT) / 1_000_000


def fmt_int(value: float) -> str:
    return f"{int(value):,}".replace(",", ".")


# ---------------------------------------------------------------------------
# Pestañas
# ---------------------------------------------------------------------------

def tab_live(turns: pd.DataFrame) -> None:
    if turns.empty:
        st.info("No hay preguntas en el periodo seleccionado.")
        return
    errors = (turns["status"] != "ok").mean() * 100
    cols = st.columns(7)
    cols[0].metric("Preguntas", fmt_int(len(turns)))
    cols[1].metric("Usuarios", fmt_int(turns["user"].nunique()))
    cols[2].metric("Tokens entrada", fmt_int(turns["prompt_tokens"].sum()))
    cols[3].metric("Tokens salida", fmt_int(turns["completion_tokens"].sum()))
    cols[4].metric("Cacheados", fmt_int(turns["cached_tokens"].sum()))
    cols[5].metric("Errores", f"{errors:.1f} %")
    cols[6].metric("Latencia p50 / p95", f"{turns['elapsed_s'].median():.1f} / "
                                          f"{turns['elapsed_s'].quantile(0.95):.1f} s")
    if HAS_PRICES:
        st.caption(f"Coste estimado del periodo: **{turns['cost'].sum():.4f}**")

    st.subheader("Últimas preguntas")
    feed = turns.head(100).assign(
        estado=lambda d: d["status"].map(STATUS_ICON).fillna("?"),
        hora=lambda d: d["ts"].dt.strftime("%d/%m %H:%M:%S"),
        tokens=lambda d: d["prompt_tokens"] + d["completion_tokens"],
        permisos=lambda d: d["delegated"].map({1: "usuario", 0: "servicio"}),
    )
    st.dataframe(
        feed[["estado", "hora", "user", "question", "tools", "llm_requests", "tokens", "elapsed_s", "permisos"]],
        hide_index=True, width="stretch",
        column_config={
            "user": "Usuario", "question": st.column_config.TextColumn("Pregunta", width="large"),
            "tools": "Herramientas", "llm_requests": "Rondas LLM", "tokens": "Tokens",
            "elapsed_s": st.column_config.NumberColumn("Duración (s)", format="%.1f"),
            "permisos": "Credencial",
        })


def tab_usage(turns: pd.DataFrame, start: date, end: date) -> None:
    if turns.empty:
        st.info("No hay datos en el periodo seleccionado.")
        return
    freq = "h" if (end - start).days <= 2 else "D"
    by_time = turns.set_index("ts").resample(freq).agg(
        {"id": "count", "prompt_tokens": "sum", "completion_tokens": "sum", "cached_tokens": "sum"})
    left, right = st.columns(2)
    with left:
        st.subheader("Tokens " + ("por hora" if freq == "h" else "por día"))
        st.line_chart(by_time[["prompt_tokens", "completion_tokens", "cached_tokens"]].rename(columns={
            "prompt_tokens": "entrada", "completion_tokens": "salida", "cached_tokens": "cacheados"}))
    with right:
        st.subheader("Preguntas " + ("por hora" if freq == "h" else "por día"))
        st.bar_chart(by_time["id"].rename("preguntas"))

    agg = {"preguntas": ("id", "count"), "tokens_entrada": ("prompt_tokens", "sum"),
           "tokens_salida": ("completion_tokens", "sum"), "cacheados": ("cached_tokens", "sum"),
           "rondas_medias": ("llm_requests", "mean"), "duracion_media_s": ("elapsed_s", "mean"),
           "errores": ("status", lambda s: int((s != "ok").sum()))}
    if HAS_PRICES:
        agg["coste"] = ("cost", "sum")
    st.subheader("Por usuario")
    st.dataframe(turns.groupby("user").agg(**agg).sort_values("tokens_entrada", ascending=False),
                 width="stretch")
    st.subheader("Por modelo")
    st.dataframe(turns.groupby(["provider", "model"]).agg(**agg), width="stretch")

    left, right = st.columns(2)
    with left:
        st.subheader("Rondas LLM por pregunta")
        st.bar_chart(turns["llm_requests"].value_counts().sort_index().rename("preguntas"))
    with right:
        st.subheader("Tokens de entrada por pregunta")
        st.bar_chart(pd.cut(turns["prompt_tokens"], bins=10).value_counts().sort_index()
                     .rename("preguntas").rename(index=str))
    if not HAS_PRICES:
        st.caption("Define PANEL_PRICE_INPUT_PER_MTOK / PANEL_PRICE_OUTPUT_PER_MTOK / "
                   "PANEL_PRICE_CACHED_PER_MTOK en .env para ver costes estimados.")


def tab_tools(tools: pd.DataFrame) -> None:
    if tools.empty:
        st.info("No hay llamadas a herramientas en el periodo seleccionado.")
        return
    stats = tools.groupby("name").agg(
        llamadas=("id", "count"), usuarios=("user", "nunique"),
        error_pct=("is_error", lambda s: s.mean() * 100),
        latencia_media_s=("elapsed_s", "mean"), latencia_p95_s=("elapsed_s", lambda s: s.quantile(0.95)),
        resultado_medio_chars=("result_chars", "mean"), recortado_pct=("truncated", lambda s: s.mean() * 100),
    ).sort_values("llamadas", ascending=False)
    st.bar_chart(stats["llamadas"])
    st.dataframe(stats, width="stretch", column_config={
        "error_pct": st.column_config.NumberColumn("% error", format="%.1f"),
        "recortado_pct": st.column_config.NumberColumn("% recortado", format="%.1f",
                                                       help="Resultado recortado por TOOL_RESULT_MAX_CHARS"),
        "latencia_media_s": st.column_config.NumberColumn("Latencia media (s)", format="%.2f"),
        "latencia_p95_s": st.column_config.NumberColumn("Latencia p95 (s)", format="%.2f"),
        "resultado_medio_chars": st.column_config.NumberColumn("Resultado medio (car.)", format="%d"),
    })


def show_turn(turn_id: int) -> None:
    turn = query("SELECT * FROM turns WHERE id = ?", [turn_id]).iloc[0]
    cols = st.columns(5)
    cols[0].metric("Estado", f"{STATUS_ICON.get(turn['status'], '?')} {turn['status']}")
    cols[1].metric("Duración", f"{turn['elapsed_s']:.1f} s")
    cols[2].metric("Rondas LLM", int(turn["llm_requests"]))
    cols[3].metric("Tokens entrada / salida", f"{fmt_int(turn['prompt_tokens'])} / "
                                              f"{fmt_int(turn['completion_tokens'])}")
    cols[4].metric("Credencial", "usuario" if turn["delegated"] else "servicio")
    st.markdown(f"**{turn['user']}** · {turn['ts_start'][:19].replace('T', ' ')} · "
                f"`{turn['provider']}:{turn['model']}` · conversación `{turn['conversation']}`")
    st.markdown("**Pregunta**")
    st.text(turn["question"])

    st.markdown("**Línea temporal**")
    llm = query("SELECT * FROM llm_calls WHERE turn_id = ?", [turn_id]).assign(kind="llm")
    tools = query("SELECT * FROM tool_calls WHERE turn_id = ?", [turn_id]).assign(kind="tool")
    events = pd.concat([llm, tools], ignore_index=True).sort_values(["ts", "kind"])
    for _, ev in events.iterrows():
        if ev["kind"] == "llm":
            st.markdown(
                f"🧠 **LLM #{int(ev['n'])}** · {ev['elapsed_s']:.1f} s · entrada {fmt_int(ev['prompt_tokens'] or 0)}"
                f" (cacheados {fmt_int(ev['cached_tokens'] or 0)}) · salida {fmt_int(ev['completion_tokens'] or 0)}"
                f" · ~{fmt_int(ev['est_tools'] or 0)} tok de herramientas, {int(ev['n_messages'] or 0)} mensajes")
        else:
            flags = (" · ❌ error" if ev["is_error"] else "") + (" · ✂️ recortado" if ev["truncated"] else "")
            with st.expander(f"⚙️ {ev['name']} · {ev['elapsed_s']:.2f} s · "
                             f"{fmt_int(ev['result_chars'] or 0)} caracteres{flags}"):
                st.markdown("Argumentos")
                try:
                    st.json(json.loads(ev["args"] or "{}"))
                except json.JSONDecodeError:
                    st.code(ev["args"])
                st.markdown("Resultado")
                st.code(ev["result"] or "", language="json", wrap_lines=True)

    st.markdown("**Respuesta**" if turn["status"] != "error" else "**Error**")
    if turn["status"] == "error":
        st.error(turn["error"])
    else:
        st.markdown(turn["answer"] or "")


def tab_conversations(turns: pd.DataFrame) -> None:
    if turns.empty:
        st.info("No hay preguntas en el periodo seleccionado.")
        return
    text = st.text_input("Buscar en preguntas y respuestas", key="search")
    view = turns
    if text:
        mask = (turns["question"].str.contains(text, case=False, regex=False, na=False)
                | turns["answer"].str.contains(text, case=False, regex=False, na=False))
        view = turns[mask]
    if view.empty:
        st.info("Sin coincidencias.")
        return
    labels = {int(r.id): f"{STATUS_ICON.get(r.status, '?')} {r.ts:%d/%m %H:%M:%S} · {r.user} · "
                         f"{(r.question or '')[:80]}" for r in view.itertuples()}
    turn_id = st.selectbox("Pregunta", list(labels), format_func=labels.get, key="turn")
    show_turn(turn_id)

    conversation = turns.loc[turns["id"] == turn_id, "conversation"].iloc[0]
    others = query("SELECT id, ts_start, question, status, prompt_tokens, completion_tokens FROM turns"
                   " WHERE conversation = ? ORDER BY ts_start", [conversation])
    if len(others) > 1:
        st.subheader("Resto de la conversación")
        st.dataframe(others, hide_index=True, width="stretch")


def tab_errors(turns: pd.DataFrame, tools: pd.DataFrame) -> None:
    bad_turns = turns[turns["status"] != "ok"] if not turns.empty else turns
    st.subheader(f"Preguntas fallidas o sin respuesta final ({len(bad_turns)})")
    if not bad_turns.empty:
        st.dataframe(bad_turns[["id", "ts_start", "user", "status", "error", "question", "tools"]],
                     hide_index=True, width="stretch")
    bad_tools = tools[tools["is_error"] == 1] if not tools.empty else tools
    st.subheader(f"Herramientas con error ({len(bad_tools)})")
    if not bad_tools.empty:
        st.dataframe(bad_tools.assign(result=bad_tools["result"].str.slice(0, 300))[
            ["turn_id", "ts", "user", "name", "args", "result"]], hide_index=True, width="stretch")


# ---------------------------------------------------------------------------
# Página
# ---------------------------------------------------------------------------

def main() -> None:
    st.set_page_config(page_title="Observabilidad chat Zabbix", page_icon="📊", layout="wide")
    check_password()

    if not OBS.db_path.exists():
        st.title("Observabilidad del chat de Zabbix")
        st.warning(f"Aún no existe {OBS.db_path}. Arranca la pasarela (OBS_ENABLED=true) y haz una pregunta.")
        st.stop()

    with st.sidebar:
        st.header("Filtros")
        refresh = REFRESH_OPTIONS[st.radio("Refresco", list(REFRESH_OPTIONS), horizontal=True)]
        today = date.today()
        period = st.date_input("Periodo", (today - timedelta(days=7), today), max_value=today)
        start, end = (period if isinstance(period, tuple) and len(period) == 2
                      else ((period[0], period[0]) if isinstance(period, tuple) else (period, period)))
        users = st.multiselect("Usuarios", query("SELECT DISTINCT user FROM turns ORDER BY user")["user"])
        models = st.multiselect("Modelos", query("SELECT DISTINCT model FROM turns ORDER BY model")["model"])
        if OBS.phoenix_enabled:
            st.link_button("Trazas en Phoenix", os.getenv("PANEL_PHOENIX_URL", OBS.phoenix_endpoint))
        st.caption(f"BD: {OBS.db_path}")

    where, params = build_where(start, end, users, models)

    @st.fragment(run_every=refresh)
    def body() -> None:
        turns = load_turns(where, params)
        tools = load_tool_calls(where, params)
        st.title("Observabilidad del chat de Zabbix")
        st.caption(f"Actualizado {datetime.now():%H:%M:%S}")
        live, usage, tools_tab, convs, errs = st.tabs(
            ["En vivo", "Consumo", "Herramientas", "Conversaciones", "Errores"])
        with live:
            tab_live(turns)
        with usage:
            tab_usage(turns, start, end)
        with tools_tab:
            tab_tools(tools)
        with convs:
            tab_conversations(turns)
        with errs:
            tab_errors(turns, tools)

    body()


main()
