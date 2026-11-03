"""Backup of the database into the public GitHub repository (decision 2026-10-23: code + DB; owner 2026-10-24: «Делай ты всё»).

data/films.db is ~900 MB — far over GitHub's 100 MB per file, and a new binary blob on every update would bloat the
history. So the DB goes into git as text: db/schema.sql + db/<table>.<NNN>.jsonl (one JSON array per row, in rowid
order, chunks under CHUNK bytes). Git stores the next update as a delta, so the repository grows only by what changed.

Pushes carry only what changed (R309, owner 2026-11-03: «создать базу … данные которым больше года и хранить их отдельно,
чтобы они почти никогда или никогда не менялись … на каждый год. Создать её когда год закончен + один месяц»):
- chunk boundaries are rowids kept in db/manifest.json — a row that grows no longer shifts every later chunk, so an
  untouched chunk is the same git blob and costs nothing;
- releases of a finished year (by added_at, from 1 February of the next year) are frozen in db/archive/<year>/ and never
  rewritten; their seeders / leechers / last_seen_at (changed by every crawl) live in the small db/releases.seen.jsonl,
  and an archived row that really changed (re-match, re-parse) goes whole to db/releases.changes.jsonl on top of the
  archive. When changes and late finds of a year pass REFREEZE of its archive, that year is rewritten once.

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
from bisect import bisect_right
from collections import Counter
from datetime import date

from . import db
from . import personal as own

OUT = db.ROOT / "db"   # owner 2026-11-03: «Да, и назвать db» (was backup/ — read as «a second backup»)
CHUNK = 45_000_000
CACHE_TABLES = {"release_raw", "tmdb_raw", "imdb_basics", "imdb_akas", "imdb_ratings", "imdb_title_norm", "rt_pages"}
# table → (date column that picks the year, columns that change on every crawl); R309
ARCHIVE = {"releases": ("added_at", ("seeders", "leechers", "last_seen_at"))}
REFREEZE = 0.3
_CHUNK_NAME = re.compile(r"^(\w+)\.(\d{3})\.jsonl$")


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
        for c in ("plex_title", "original_title", "show_title", "name"):
            if c in d and d[c]:
                r[cols.index(c)] = "#" + hashlib.sha1(str(d[c]).encode("utf-8")).hexdigest()[:12]
    if table == "qbt_torrents" and d.get("title_id") is None:
        for c in ("name", "save_path"):
            if c in d:
                r[cols.index(c)] = _hide(d[c])
    return tuple(r)


def _personal(conn) -> dict:
    """The owner's own videos never go to git (owner, 2026-10-25: «Этих видео личных даже в базе не должно быть, они там на
    пару дней. Их не должно быть в гите»): disk files marked 'personal' and the shows the link step marked 'personal'
    (data/cache/plex_show_map_private.json) — their rows are left out of the copy entirely, not hashed."""
    # since 2026-10-29 such rows are not stored at all (films.personal); this stays as a second net
    paths = {p for (p,) in conn.execute("SELECT path FROM disk_files WHERE how = 'personal'")}
    return {"paths": paths, "names": own.names()}


def _skip(table: str, cols: list[str], row: tuple, personal: dict) -> bool:
    d = dict(zip(cols, row))
    if table == "disk_files":
        return d.get("how") == "personal" or own.is_path(d.get("path"))
    if table not in ("plex_items", "jellyfin_items", "plex_history") or d.get("title_id") is not None:
        return False
    if any(d.get(c) in personal["paths"] for c in ("file_path", "path") if d.get(c)):
        return True
    names = personal["names"]
    if own.is_plex_row(d, names):
        return True
    blob = " ".join(str(v) for v in row if isinstance(v, str) and '"show"' in v)
    return any(f'"show": "{n}"' in blob for n in names)


def _tables(conn) -> list[str]:
    return [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
                                       "ORDER BY name")]


def _line(row) -> str:
    return json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"


def _header(cols, **extra) -> str:
    return json.dumps({"columns": cols, **extra}, ensure_ascii=False) + "\n"


def _year(v) -> int | None:
    s = str(v or "")[:4]
    return int(s) if s.isdigit() else None


def _frozen(year: int, today: date) -> bool:
    """A year is archived from 1 February of the next one (owner: «Создать её когда год закончен + один месяц»)."""
    return today >= date(year + 1, 2, 1)


def _hash(cols, row) -> str:
    """Content of a row without its NULLs: a column added later (NULL in old rows) does not make the archive differ."""
    d = {c: v for c, v in zip(cols, row) if v is not None}
    return hashlib.sha1(json.dumps(d, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _manifest() -> dict:
    try:
        return json.loads((OUT / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


class _Chunks:
    """Size-cut chunks <dir>/<table>.NNN.jsonl written row by row (the frozen archive of one year)."""

    def __init__(self, folder, table, head):
        self.folder, self.table, self.head = folder, table, head
        self.f, self.part, self.size = None, 0, 0

    def write(self, line: str):
        b = len(line.encode())
        if self.f is None or self.size + b > CHUNK:
            if self.f:
                self.f.close()
            self.folder.mkdir(parents=True, exist_ok=True)
            self.f = (self.folder / f"{self.table}.{self.part:03}.jsonl").open("w", encoding="utf-8", newline="\n")
            self.f.write(self.head)
            self.part, self.size = self.part + 1, 0
        self.f.write(line)
        self.size += b

    def close(self):
        if self.f:
            self.f.close()


def _write_live(t, cols, items, starts, old) -> tuple[int, list[int]]:
    """Chunks of a table cut at the rowids kept in the manifest; only an oversized chunk is split (a new boundary), so
    the other chunks keep their rows — and an unchanged chunk is the same blob for git."""
    lines = [(rid, _line(row)) for rid, row in items]
    starts = sorted(starts or [])[:] or ([lines[0][0]] if lines else [])
    groups = [[] for _ in starts]
    for rid, ln in lines:
        groups[max(bisect_right(starts, rid) - 1, 0)].append((rid, ln))
    out_starts, out_groups = [], []
    for s, g in zip(starts, groups):
        cur, size, cs = [], 0, s
        for rid, ln in g:
            b = len(ln.encode())
            if cur and size + b > CHUNK:
                out_starts.append(cs)
                out_groups.append(cur)
                cur, size, cs = [], 0, rid
            cur.append(ln)
            size += b
        if cur:
            out_starts.append(cs)
            out_groups.append(cur)
    for i, g in enumerate(out_groups):
        path = OUT / f"{t}.{i:03}.jsonl"
        old.discard(path)
        with path.open("w", encoding="utf-8", newline="\n") as f:
            f.write(_header(cols))
            f.writelines(g)
    return len(lines), out_starts


def _archive(t, cols, rows, today: date, manifest: dict, old) -> tuple[list, dict]:
    """Frozen years of an ARCHIVE table (R309). Returns the live rows (not archived) and counts."""
    date_col, volatile = ARCHIVE[t]
    di, vi, ii = cols.index(date_col), [cols.index(c) for c in volatile], cols.index("id")
    adir = OUT / "archive"

    def stable(row):
        r = list(row)
        for i in vi:
            r[i] = None
        return r

    have, count = {}, Counter()           # archived id → (year, content hash); rows per archived year
    for path in sorted(adir.glob(f"*/{t}.*.jsonl")):
        y = int(path.parent.name)
        with path.open(encoding="utf-8") as f:
            acols = json.loads(f.readline())["columns"]
            aid, avi = acols.index("id"), [acols.index(c) for c in volatile if c in acols]
            for ln in f:
                r = json.loads(ln)
                for i in avi:
                    r[i] = None
                have[r[aid]] = (y, _hash(acols, r))
                count[y] += 1
    # pass 1: what changed in each archived year, and the late finds of a frozen year
    changed, late, ids = Counter(), Counter(), set()
    for _rid, row in rows():
        ids.add(row[ii])
        a = have.get(row[ii])
        if a:
            if _hash(cols, stable(row)) != a[1]:
                changed[a[0]] += 1
        elif (y := _year(row[di])) is not None and _frozen(y, today):
            late[y] += 1
    gone = sorted(set(have) - ids)
    for i in gone:
        changed[have[i][0]] += 1
    refreeze = {y for y in set(late) | set(changed) if late[y] + changed[y] > REFREEZE * count[y]}
    for y in refreeze:
        for p in (adir / str(y)).glob(f"{t}.*.jsonl"):
            p.unlink()
        have = {i: a for i, a in have.items() if a[0] != y}
    gone = [i for i in gone if i in have]
    # pass 2
    writers, live, n_changes = {}, [], 0
    head = _header(cols, volatile=list(volatile))
    seen_path, changes_path = OUT / f"{t}.seen.jsonl", OUT / f"{t}.changes.jsonl"
    old.discard(seen_path)
    old.discard(changes_path)
    with seen_path.open("w", encoding="utf-8", newline="\n") as seen, \
            changes_path.open("w", encoding="utf-8", newline="\n") as changes:
        seen.write(_header(["id", *volatile]))
        changes.write(_header(cols))
        for rid, row in rows():
            y = _year(row[di])
            a = have.get(row[ii])
            if a:
                if _hash(cols, stable(row)) != a[1]:
                    changes.write(_line(row))
                    n_changes += 1
            elif y in refreeze:
                if y not in writers:
                    writers[y] = _Chunks(adir / str(y), t, head)
                writers[y].write(_line(row))   # as of the freeze: seen.jsonl keeps them current
            else:
                live.append((rid, row))
                continue
            seen.write(_line([row[ii], *(row[i] for i in vi)]))
    for w in writers.values():
        w.close()
    manifest.setdefault("deleted", {})[t] = gone
    frozen = sorted({a[0] for a in have.values()} | set(writers))
    return live, {f"{t}_archived_years": frozen, f"{t}_refrozen": sorted(writers), f"{t}_changes": n_changes,
                  f"{t}_live": len(live)}


def export(today: date | None = None) -> dict:
    today = today or date.today()
    conn = sqlite3.connect(f"file:{db.DB_PATH}?mode=ro", uri=True)
    OUT.mkdir(exist_ok=True)
    old = set(OUT.glob("*.jsonl"))
    manifest = _manifest()
    schema = [r[0] for r in conn.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
                                         "ORDER BY type = 'table' DESC, name")]
    (OUT / "schema.sql").write_text(";\n".join(schema) + ";\n", encoding="utf-8")
    stats = {}
    personal = _personal(conn)
    chunks = {}
    for t in _tables(conn):
        if t in CACHE_TABLES:
            continue
        cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{t}")')]
        # compressed columns (releases.parsed, R308) go to git as plain JSON text: zlib blobs would not delta
        packed = [cols.index(c) for tt, c in db.COMPRESSED_COLUMNS if tt == t and c in cols]

        def rows(t=t, cols=cols, packed=packed):
            for rid, *row in conn.execute(f'SELECT rowid, * FROM "{t}" ORDER BY rowid'):
                for i in packed:
                    row[i] = db.parsed_text(row[i])
                if not _skip(t, cols, tuple(row), personal):
                    yield rid, _redact(t, cols, tuple(row))

        live = rows()
        if t in ARCHIVE:
            live, a = _archive(t, cols, rows, today, manifest, old)
            stats.update(a)
        stats[t], chunks[t] = _write_live(t, cols, live, (manifest.get("chunks") or {}).get(t), old)
    for stale in old:      # a table that shrank to fewer chunks
        stale.unlink()
    manifest["chunks"] = chunks
    manifest["user_version"] = conn.execute("PRAGMA user_version").fetchone()[0]
    (OUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                                       encoding="utf-8")
    conn.close()
    return stats


def _insert(conn, path, verb="INSERT") -> int:
    t = path.name.split(".")[0]
    with path.open(encoding="utf-8") as f:
        cols = json.loads(f.readline())["columns"]
        q = f'{verb} INTO "{t}" ({",".join(chr(34) + c + chr(34) for c in cols)}) VALUES ({",".join("?" * len(cols))})'
        rows = [json.loads(line) for line in f]
    packed = [cols.index(c) for tt, c in db.COMPRESSED_COLUMNS if tt == t and c in cols]
    for row in rows if packed else []:
        for i in packed:
            if isinstance(row[i], str):
                row[i] = db.dump_parsed(json.loads(row[i]))
    with conn:
        conn.executemany(q, rows)
    return len(rows)


def restore(target: str) -> dict:
    """archive years → live chunks → changes over the archive → current seeders / last_seen_at → user_version."""
    conn = sqlite3.connect(target)
    try:
        return _restore(conn)
    finally:
        conn.close()


def _restore(conn) -> dict:
    conn.executescript((OUT / "schema.sql").read_text(encoding="utf-8"))
    stats = {}
    files = sorted((OUT / "archive").glob("*/*.jsonl")) + sorted(p for p in OUT.glob("*.jsonl") if _CHUNK_NAME.match(p.name))
    for path in files:
        t = path.name.split(".")[0]
        stats[t] = stats.get(t, 0) + _insert(conn, path)
    manifest = _manifest()
    for t in ARCHIVE:
        if (OUT / f"{t}.changes.jsonl").exists():
            _insert(conn, OUT / f"{t}.changes.jsonl", "INSERT OR REPLACE")
        if (OUT / f"{t}.seen.jsonl").exists():
            with (OUT / f"{t}.seen.jsonl").open(encoding="utf-8") as f:
                vcols = json.loads(f.readline())["columns"][1:]
                rows = [json.loads(line) for line in f]
            with conn:
                conn.executemany(f'UPDATE "{t}" SET {", ".join(chr(34) + c + chr(34) + " = ?" for c in vcols)} WHERE id = ?',
                                 [(*r[1:], r[0]) for r in rows])
        gone = (manifest.get("deleted") or {}).get(t) or []
        with conn:
            conn.executemany(f'DELETE FROM "{t}" WHERE id = ?', [(i,) for i in gone])
        stats[t] = conn.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
    if manifest.get("user_version") is not None:   # else migrate() would replay every migration from the first
        conn.execute(f"PRAGMA user_version = {int(manifest['user_version'])}")
    return stats


if __name__ == "__main__":
    if sys.argv[1:2] == ["restore"]:
        print(json.dumps(restore(sys.argv[2]), ensure_ascii=False))
    else:
        print(json.dumps(export(), ensure_ascii=False))
