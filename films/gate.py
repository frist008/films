"""Gates of the update order (owner, 2026-10-26: «И так всегда. Навсегда. Ждать всегда все страницы и вопросы ко мне. Потом
только таблица, а потом только ютуб»).

  1. DB complete — every collector finished (each recent data/logs/agent_src_*.log / agent_rutracker.log ends with its
     own AGENT_*_OK, films.watchdog.finished) and no open question to the owner in PLAN.md (an open item «- [ ]» marked
     «❓»);
  2. the sheet — `films sheet rebuild` refuses until gate 1 is open; a rebuild through the gate records
     data/cache/locks/gate_ok.json;
  3. YouTube — `films.playlist_notes desired` refuses unless the sheet was rebuilt after the gate (gate_ok.json newer than
     every collector log and the gate is still open).

    python -X utf8 -m films.gate          # what is still open
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

from . import db
from .watchdog import finished

LOGS = db.ROOT / "data" / "logs"
GATE_OK = db.ROOT / "data" / "cache" / "locks" / "gate_ok.json"
PLAN = db.ROOT / "PLAN.md"
RECENT = 7 * 86400


def open_collectors(logs: Path = LOGS, now: float | None = None) -> list[str]:
    now = now or time.time()
    out = []
    for f in sorted([*logs.glob("agent_src_*.log"), *logs.glob("agent_rutracker.log")]):
        if now - f.stat().st_mtime > RECENT:
            continue
        if not finished(f.read_text(encoding="utf-8", errors="replace").splitlines(), f.stem):
            out.append(f.stem.removeprefix("agent_"))
    return out


def open_questions(plan: Path = PLAN) -> list[str]:
    try:
        lines = plan.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [l.strip()[:120] for l in lines if re.match(r"\s*- \[ \]", l) and "❓" in l]


def check(logs: Path = LOGS, plan: Path = PLAN) -> dict:
    c, q = open_collectors(logs), open_questions(plan)
    return {"open": not c and not q, "collectors": c, "questions": q}


def record_ok(path: Path = GATE_OK) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"at": time.time(), "when": time.strftime("%Y-%m-%d %H:%M:%S")}), encoding="utf-8")


def require_db(logs: Path = LOGS, plan: Path = PLAN) -> None:
    st = check(logs, plan)
    if not st["open"]:
        raise SystemExit("Таблица — только когда собраны все страницы всех источников и нет вопросов к владельцу "
                         "(владелец 2026-10-26: «Ждать всегда все страницы и вопросы ко мне. Потом только таблица»). "
                         f"Не закончили: {', '.join(st['collectors']) or '—'}; вопросов открыто: {len(st['questions'])}")


def require_sheet(logs: Path = LOGS, plan: Path = PLAN, gate_ok: Path = GATE_OK) -> None:
    require_db(logs, plan)
    try:
        at = json.loads(gate_ok.read_text(encoding="utf-8"))["at"]
    except (OSError, ValueError, KeyError):
        raise SystemExit("YouTube — только после таблицы, собранной через ворота (`films sheet rebuild`): её ещё не было")
    newest = max((f.stat().st_mtime for f in [*logs.glob("agent_src_*.log"), *logs.glob("agent_rutracker.log")]), default=0)
    if newest > at:
        raise SystemExit("YouTube — только после таблицы: сборщики писали после последней пересборки — сначала `films sheet rebuild`")


if __name__ == "__main__":
    print(json.dumps(check(), ensure_ascii=False, indent=1))
    sys.exit(0)
