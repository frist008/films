"""The open stage of the plan (owner, 2026-10-26: «комит и пуш только на этап плана можно, не чаще»).

While a stage is open, nothing is committed or pushed (hook .claude/hooks/git-guard.sh denies git commit / push, and the
Stop hook plan_guard.py does not ask for them); `done` closes it — then commit + push of the whole stage (the Stop hook
asks for it), then `start` of the next one. State: data/cache/locks/stage.json {"stage", "started"}.

    python -X utf8 -m films.stage start "A. База полностью"     # open a stage
    python -X utf8 -m films.stage done                          # close it: commit + push follow
    python -X utf8 -m films.stage                               # which stage is open
"""
from __future__ import annotations

import json
import sys
import time

from . import db

PATH = db.ROOT / "data" / "cache" / "locks" / "stage.json"
# a stage ended and its work is not on GitHub yet: only then the Stop hook asks for commit + push (owner, 2026-11-22:
# «харе меня задрачивать с каждым изменением на комит пуш … у нас нельзя так часто комитить и пушить!»); small fixes
# between stages wait. Set by `done` and by `films update --finish` (the end of «обнови базу»), cleared by films.publish.
CLOSED = db.ROOT / "data" / "cache" / "locks" / "stage_closed.json"


def mark_closed(name: str) -> None:
    CLOSED.parent.mkdir(parents=True, exist_ok=True)
    CLOSED.write_text(json.dumps({"stage": name, "closed": time.strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False),
                      encoding="utf-8")


def clear_closed() -> None:
    CLOSED.unlink(missing_ok=True)


def current() -> dict | None:
    try:
        return json.loads(PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def start(name: str) -> dict:
    cur = current()
    if cur:
        raise SystemExit(f"этап уже открыт: {cur['stage']} — сначала `films.stage done`, коммит и пуш")
    st = {"stage": name, "started": time.strftime("%Y-%m-%d %H:%M:%S")}
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    return st


def done() -> dict | None:
    from .cleanup import check   # leftovers first (owner, 2026-10-28: «не забывать чистить за собой мусор»)
    left = check()
    if left:
        raise SystemExit("этап не закрыт — сначала убрать мусор (films.cleanup check):\n" + "\n".join(left))
    cur = current()
    if cur:
        PATH.unlink()
    mark_closed(cur["stage"] if cur else "этап")
    return cur


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["start"] and len(a) > 1:
        print(json.dumps(start(" ".join(a[1:])), ensure_ascii=False))
    elif a[:1] == ["done"]:
        print(json.dumps({"closed": done()}, ensure_ascii=False))
    else:
        print(json.dumps({"open": current()}, ensure_ascii=False))
