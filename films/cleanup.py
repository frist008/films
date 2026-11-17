"""Leftovers to clean up after every «обнови базу» iteration and at the end of any work (owner, 2026-10-25 / 2026-10-28:
«не забывать чистить за собой мусор и не нужный набор всего»). Only lists; deleting stays a deliberate step (owner-rules
§4: by name, own files only). `films.stage done` refuses to close a stage while this finds anything.

    python -X utf8 -m films.cleanup check      # prints the leftovers, exit 1 when there are any

Not leftovers (read again by the project): data/films.db, release_raw, tmdb_raw, data/cache/imdb_specs/,
data/cache/youtube/audio_checks.jsonl, state.json / queries_run*.json / ingested.json of the collectors, logs, reports.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from . import db

CACHE = db.ROOT / "data" / "cache"
MEDIA = (".part", ".ytdl", ".webm", ".m4a", ".wav", ".mp4", ".mkv")
# file-name prefixes of this project's temporary scripts in the session scratchpads (owner-rules §4: «<агент>_*»)
SCRATCH_PREFIXES = ("main_", "review_", "nk_", "ua_", "rk_")


def _ingested() -> set[str]:
    done = set()
    for f in CACHE.glob("*/ingested.json"):
        try:
            v = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        done |= {Path(x).name for x in (v if isinstance(v, list) else v.keys() if isinstance(v, dict) else [])}
    return done


def _scratchpads() -> list[Path]:
    base = Path(os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp") / "claude" / "D--work-films"
    return [p for p in base.glob("*/scratchpad") if p.is_dir()]


def check() -> list[str]:
    out = []
    # data/cache/jellyfin/jellyfin.db is the read-only copy films/sources/jellyfin.py works from — not a leftover
    out += [f"копия базы: {p.relative_to(db.ROOT)}" for p in CACHE.rglob("*.db") if p.parent.name != "jellyfin"]
    done = _ingested()
    out += [f"занесённый файл моста: {p.relative_to(db.ROOT)}" for p in (CACHE / "ingest").glob("*")
            if p.is_file() and p.name in done]
    # a collector's heartbeat («kinozal_status-…txt», review 2026-10-28) is only for the run that wrote it
    out += [f"пульс сборщика: {p.relative_to(db.ROOT)}" for p in (CACHE / "ingest").glob("*_status-*.txt")]
    out += [f"одноразовая страница: {p.relative_to(db.ROOT)}" for p in (CACHE / "people_photos").glob("candidates*.html")]
    out += [f"скрипт прошлого прогона: {p.relative_to(db.ROOT)}" for p in CACHE.glob("*/run_inline_*.js")]
    out += [f"звук/видео: {p.relative_to(db.ROOT)}" for p in CACHE.rglob("*") if p.is_file() and p.suffix in MEDIA]
    # dated copies of a collector's job (write_run keeps one per run): only the newest is worth keeping (review 2026-10-28)
    for d in {p.parent for p in CACHE.glob("*/queries_run_2*.json")}:
        out += [f"старое задание сборщика: {p.relative_to(db.ROOT)}" for p in sorted(d.glob("queries_run_2*.json"))[:-1]]
    # «<kind>_code» folders serve a collector's code through the bridge for one run (films.bridge /queries/<kind>_code)
    out += [f"код сборщика для моста: {p.relative_to(db.ROOT)}" for p in CACHE.glob("*_code") if p.is_dir()]
    wip = CACHE / "locks" / "wip.txt"
    if wip.exists() and wip.read_text(encoding="utf-8").strip():
        out.append("data/cache/locks/wip.txt не пуст — убрать строки агентов, которые уже закончили")
    for sp in _scratchpads():
        out += [f"черновик: {p}" for p in sp.iterdir() if p.name.startswith(SCRATCH_PREFIXES)]
    return out


if __name__ == "__main__":
    if sys.argv[1:2] == ["check"]:
        left = check()
        print("\n".join(left) if left else "мусора нет")
        sys.exit(1 if left else 0)
    print(__doc__)
