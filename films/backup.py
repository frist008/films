"""Backup of the database into the public GitHub repository (decision 2026-10-23: code + DB; owner 2026-10-24: «Делай ты всё»).

data/films.db is ~900 MB — far over GitHub's 100 MB per file, and a new binary blob on every update would bloat the
history. So the DB goes into git as text: backup/schema.sql + backup/<table>.<NNN>.jsonl (one JSON array per row, in rowid
order, chunks under CHUNK bytes). Git stores the next update as a delta, so the repository grows only by what changed.

Not exported — third-party caches that are re-downloaded on demand and must not be republished (IMDb datasets are for
personal non-commercial use only; rutor / TMDB / RT pages belong to those sites): CACHE_TABLES. After a restore they are
filled again by `films imdb` / `films enrich` / `films ratings rt` / the release-page parser.

Run: python -X utf8 -m films.backup export | restore <new.db>
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys

from . import db

OUT = db.ROOT / "backup"
CHUNK = 45_000_000
CACHE_TABLES = {"release_raw", "tmdb_raw", "imdb_basics", "imdb_akas", "imdb_ratings", "imdb_title_norm", "rt_pages"}


def _hide(path: str | None) -> str | None:
    """«E:\\Vegas\\Network\\…» → «E:\\#<sha1>.<ext>»: names of the owner's own files (video projects, phone clips, work
    folders) identify him (sensitive-data review 2026-10-24); films keep their paths."""
    if not path:
        return path
    last = re.split(r"[\\/]", path)[-1]
    ext = last.rsplit(".", 1)[-1].lower() if "." in last else ""
    return f"{path[:2]}\\#{hashlib.sha1(path.encode('utf-8')).hexdigest()[:12]}" + (f".{ext}" if ext else "")


_DRIVE_PATH = re.compile(r"[A-Za-z]:[\\/]+[^\"\],}]+")
_USER = re.compile(r"(?i)([\\/]users[\\/])[^\\/]+")


def _redact(table: str, cols: list[str], row: tuple) -> tuple:
    """The published copy hides what identifies the owner: the Windows user name in every path, and the whole path of
    a file that is not a film (disk_files / qbt_torrents rows without a title)."""
    r = list(row)
    for i, v in enumerate(r):
        if isinstance(v, str) and ("\\" in v or "/" in v):
            r[i] = _USER.sub(r"\1<user>", v)
    d = dict(zip(cols, r))
    if table == "disk_files" and d.get("title_id") is None:
        for c in ("path", "folder", "nfo_path"):
            if c in d:
                r[cols.index(c)] = _hide(d[c])
        for c in ("parsed_title", "note"):
            if c in d:
                r[cols.index(c)] = None
    if table in ("plex_items", "jellyfin_items") and d.get("title_id") is None:
        # an unrecognised media-server file is often the owner's own video (a Plex library folder held his video
        # projects): every path in any cell, and the item's names, become hashes
        for i, v in enumerate(r):
            if isinstance(v, str):
                r[i] = _DRIVE_PATH.sub(lambda m: _hide(m.group(0).replace("\\\\", "\\")), v)
        # the names also sit inside JSON cells («files», «audio»: {"show": …}) — every occurrence of them in any cell
        # is replaced (a project name leaked that way: review 2026-10-24)
        names = {str(d[c]) for c in ("plex_title", "original_title", "show_title", "name") if d.get(c)}
        for v in r:
            if isinstance(v, str):
                names |= set(re.findall(r'"show":\s*"((?:[^"\\]|\\.)+)"', v))
        names = sorted((n for n in names if len(n) >= 3), key=len, reverse=True)
        h = lambda s: "#" + hashlib.sha1(s.encode("utf-8")).hexdigest()[:12]
        for i, v in enumerate(r):
            if isinstance(v, str):
                for n in names:
                    v = v.replace(n, h(n))
                r[i] = v
    if table == "plex_history" and d.get("title_id") is None:
        # a watch of an unrecognised item — often the owner's own video: its names become hashes too
        for c in ("title", "grandparent_title"):
            if c in d and d[c]:
                r[cols.index(c)] = "#" + hashlib.sha1(str(d[c]).encode("utf-8")).hexdigest()[:12]
    if table == "qbt_torrents" and d.get("title_id") is None:
        for c in ("name", "save_path"):
            if c in d:
                r[cols.index(c)] = _hide(d[c])
    return tuple(r)


def _tables(conn) -> list[str]:
    return [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
                                       "ORDER BY name")]


def export() -> dict:
    conn = sqlite3.connect(f"file:{db.DB_PATH}?mode=ro", uri=True)
    OUT.mkdir(exist_ok=True)
    old = set(OUT.glob("*.jsonl"))
    schema = [r[0] for r in conn.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
                                         "ORDER BY type = 'table' DESC, name")]
    (OUT / "schema.sql").write_text(";\n".join(schema) + ";\n", encoding="utf-8")
    stats = {}
    for t in _tables(conn):
        if t in CACHE_TABLES:
            continue
        cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{t}")')]
        part, size, n, f = 0, 0, 0, None
        for row in conn.execute(f'SELECT * FROM "{t}" ORDER BY rowid'):
            row = _redact(t, cols, row)
            line =json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
            if f is None or size + len(line.encode()) > CHUNK:
                if f:
                    f.close()
                path = OUT / f"{t}.{part:03}.jsonl"
                old.discard(path)
                f = path.open("w", encoding="utf-8", newline="\n")
                f.write(json.dumps({"columns": cols}, ensure_ascii=False) + "\n")
                part, size = part + 1, 0
            f.write(line)
            size += len(line.encode())
            n += 1
        if f:
            f.close()
        stats[t] = n
    for stale in old:      # a table that shrank to fewer chunks
        stale.unlink()
    return stats


def restore(target: str) -> dict:
    conn = sqlite3.connect(target)
    conn.executescript((OUT / "schema.sql").read_text(encoding="utf-8"))
    stats = {}
    for path in sorted(OUT.glob("*.jsonl")):
        t = path.name.split(".")[0]
        with path.open(encoding="utf-8") as f:
            cols = json.loads(f.readline())["columns"]
            q = f'INSERT INTO "{t}" ({",".join(chr(34) + c + chr(34) for c in cols)}) VALUES ({",".join("?" * len(cols))})'
            rows = [json.loads(line) for line in f]
        with conn:
            conn.executemany(q, rows)
        stats[t] = stats.get(t, 0) + len(rows)
    return stats


if __name__ == "__main__":
    if sys.argv[1:2] == ["restore"]:
        print(json.dumps(restore(sys.argv[2]), ensure_ascii=False))
    else:
        print(json.dumps(export(), ensure_ascii=False))
