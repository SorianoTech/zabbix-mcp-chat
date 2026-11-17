"""Analiza logs/llm_requests.jsonl: qué se envió al LLM en cada petición y cuánto ocupó.

Uso:
    python analizar_contexto.py                 # resumen de todas las peticiones
    python analizar_contexto.py --sesion ULTIMA # solo la última sesión
    python analizar_contexto.py --ver 3         # contexto completo de la petición nº 3 (última sesión)
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

DEFAULT_LOG = Path(__file__).resolve().parent / "logs" / "llm_requests.jsonl"


def load(path: Path) -> list[dict]:
    if not path.exists():
        sys.exit(f"No existe {path}. Ejecuta el cliente con LOG_LLM_PAYLOADS=true.")
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def summary(records: list[dict]) -> None:
    print(f"{'sesión':16s} {'#':>3s} {'est.':>7s} {'tools':>7s} {'system':>6s} {'user':>6s} "
          f"{'asist.':>6s} {'tool':>7s} {'prompt':>7s} {'resp.':>6s} {'cache':>6s}")
    total_prompt = total_completion = 0
    for r in records:
        b, u = r["breakdown"], r.get("usage") or {}
        roles = b["est_by_role"]
        cached = ((u.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0
        total_prompt += u.get("prompt_tokens") or 0
        total_completion += u.get("completion_tokens") or 0
        print(f"{r['session']:16s} {r['request']:3d} {b['est_total']:7d} {b['est_tools']:7d} "
              f"{roles.get('system', 0):6d} {roles.get('user', 0):6d} {roles.get('assistant', 0):6d} "
              f"{roles.get('tool', 0):7d} {u.get('prompt_tokens', '-')!s:>7s} "
              f"{u.get('completion_tokens', '-')!s:>6s} {cached:6d}")
    print(f"\nTotal real (usage): prompt={total_prompt} respuesta={total_completion} "
          f"en {len(records)} peticiones")
    print("Columnas 'est.' a 'tool' = estimación (~4 caracteres/token); 'prompt'/'resp.' = dato del proveedor.")

    last = records[-1]["breakdown"]
    if last["top_tools"]:
        print("\nHerramientas que más ocupan en cada petición (última):")
        for name, tok in last["top_tools"]:
            print(f"  {name:40s} ~{tok} tok")
    results = [x for r in records for x in r["breakdown"]["top_tool_results"]]
    if results:
        print("\nResultados de herramientas más grandes (todas las peticiones):")
        for name, tok in sorted(results, key=lambda x: x[1], reverse=True)[:8]:
            print(f"  {name:40s} ~{tok} tok")


def show(record: dict) -> None:
    payload = record["request_payload"]
    print(f"=== Sesión {record['session']} petición #{record['request']} — modelo {payload['model']} ===")
    print(f"Herramientas enviadas ({len(payload.get('tools', []))}): "
          + ", ".join(t["function"]["name"] for t in payload.get("tools", [])))
    for m in payload["messages"]:
        print(f"\n--- {m['role'].upper()} ---")
        if m.get("content"):
            print(m["content"])
        for tc in m.get("tool_calls") or []:
            print(f"[llamada] {tc['function']['name']}({tc['function']['arguments']})")
    print("\n--- RESPUESTA DEL LLM ---")
    print(json.dumps(record["response_message"], ensure_ascii=False, indent=2))
    print(f"\nusage: {record.get('usage')}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG)
    parser.add_argument("--sesion", help="ID de sesión o ULTIMA")
    parser.add_argument("--ver", type=int, help="Muestra el contexto completo de la petición N")
    args = parser.parse_args()

    records = load(args.log)
    if not records:
        sys.exit("El log está vacío.")
    session = args.sesion
    if session == "ULTIMA" or (args.ver and not session):
        session = records[-1]["session"]
    if session:
        records = [r for r in records if r["session"] == session]
    if args.ver:
        match = [r for r in records if r["request"] == args.ver]
        if not match:
            sys.exit(f"No hay petición #{args.ver} en la sesión {session}.")
        show(match[0])
    else:
        summary(records)


if __name__ == "__main__":
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
