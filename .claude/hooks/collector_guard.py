"""PreToolUse: один адрес — один сборщик (владелец, 2026-11-23: «Как это вообще возможно? Нельзя один и тот же сайт
использовать одновременно в 2 вкладках. Это разрешено только для зеркал. Это баг хука!» — два сборщика rustorka в двух
вкладках вдвое чаще запрашивали сайт, и Cloudflare их отсекал; «Звучит так будто ты если одно зеркало сможешь её
использовать параллельно 100 раз» — нет: на каждом зеркале тоже один).

Запуск сборщика в Chrome (javascript_tool, и внутри browser_batch) с кодом из моста `…/queries/<kind>` или
`…/queries/<kind>_code` отклоняется, пока в мост приходят файлы (страница или статус — живой сборщик шлёт их не реже раза в
2 минуты; `data/cache/ingest/<сайт>-*.txt`, `<сайт>_status-*.txt`, сайт = kind до «_», rustorka_net → rustorka):
- от сборщика этого же задания (поле run = run задания `data/cache/<kind>/queries_run.json`) — он ещё жив;
- от сборщика, чьего run нет ни в одном текущем задании этого сайта, — старый сборщик в забытой вкладке.
Сборщик другой части задания (зеркало, R348: `rustorka split` — rustorka.com и rustorka.net) запуску не мешает.
Перезапуск после навигации вкладки (она убивает сборщик) — когда его файлы молчат ≥ QUIET секунд.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

KINDS = ("rutracker", "kinozal", "toloka", "rustorka_net", "rustorka")
LAUNCH = re.compile(r"/queries/(" + "|".join(KINDS) + r")(?:_code)?(?![a-z0-9_])")
RUN = re.compile(r'"run"\s*:\s*"([^"]+)"')
QUIET = 150


def scripts(tool: str, ti: dict):
    if tool.endswith("browser_batch"):
        for a in ti.get("actions") or []:
            yield from scripts(a.get("name", ""), a.get("input") or {})
        return
    if tool.endswith("javascript_tool"):
        yield ti.get("text") or ""


def job_run(cache: Path, kind: str) -> str | None:
    try:
        with open(cache / kind / "queries_run.json", encoding="utf-8") as f:
            m = RUN.search(f.read(4000))
        return m.group(1) if m else None
    except OSError:
        return None


def recent(cache: Path, site: str, now: float) -> list[tuple[float, str | None]]:
    """(возраст, run) файлов моста этого сайта моложе QUIET."""
    out = []
    for pat in (f"{site}-*.txt", f"{site}_status-*.txt"):
        for p in (cache / "ingest").glob(pat):
            age = now - p.stat().st_mtime
            if age < QUIET:
                try:
                    with open(p, encoding="utf-8", errors="replace") as f:
                        m = RUN.search(f.read(600))
                except OSError:
                    m = None
                out.append((age, m.group(1) if m else None))
    return out


def decide(tool: str, ti: dict, cache: Path, now: float | None = None) -> str:
    now = time.time() if now is None else now
    kinds = {m for s in scripts(tool, ti) for m in LAUNCH.findall(s)}
    for kind in sorted(kinds):
        site = kind.split("_")[0]
        mine = job_run(cache, kind)
        current = {r for k in KINDS if k.split("_")[0] == site for r in [job_run(cache, k)] if r}
        for age, run in sorted(recent(cache, site, now)):
            if mine and run and run != mine and run in current:
                continue   # другая часть задания — сборщик на зеркале
            who = "этого же задания" if run and run == mine else f"старого задания {run or '?'}"
            return (f"Сборщик {site} уже работает (файл {who} в мосте {int(age)} с назад). Один адрес — один сборщик "
                    "(владелец, 2026-11-23: «Нельзя один и тот же сайт использовать одновременно в 2 вкладках. Это "
                    "разрешено только для зеркал»). Остановить его (`__xxStop()` в его вкладке) или закрыть его вкладку; "
                    f"после навигации вкладки — подождать, пока его файлы замолчат ({QUIET} с), и запустить снова.")
    return ""


def main() -> None:
    try:
        d = json.loads(sys.stdin.buffer.read().decode("utf-8-sig") or "{}")
    except ValueError:
        return
    root = Path(os.environ.get("CLAUDE_PROJECT_DIR") or ".")
    why = decide(d.get("tool_name", ""), d.get("tool_input") or {}, root / "data" / "cache")
    if why:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                 "permissionDecisionReason": why}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
