"""Several copies of one film in Plex / Jellyfin — an error the owner wants to hear about at once.

Owner (metadata survey, 2026-10-25): «это баг, срочно говорить мне, чтобы я удалил дубликат».
A film (titles.kind = 'movie') is a duplicate when its present files across both servers are more than one copy:
  * a copy = one file, or the parts of one multi-part item (CD1 / CD2, part 1 / part 2 — one copy);
  * the same file seen by Plex and Jellyfin is one copy (compared by path);
  * several versions inside one Plex item (Plex merges them: Media[] > 1) are several copies;
  * different editions (Extended, Director's Cut …) are duplicates too, but the edition is named;
  * episodes / seasons are not checked.
Present = Plex items the last scan listed (plex_items.present = 1), Jellyfin items of the last sync.

    python -X utf8 -m films.duplicates            # the list (also a section of the «обнови базу» report)
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import PureWindowsPath

from . import db

_PART = re.compile(r"[ ._\-\[(]+(cd|disc|disk|dvd|part|pt)[ ._\-]?[0-9]{1,2}[\])]?(?=\.[a-z0-9]{2,4}$)", re.I)
_EDITION = [("Director's Cut", re.compile(r"director'?s[ ._\-]?cut|режисс[её]рск", re.I)),
            ("Extended", re.compile(r"extended|расширенн", re.I)),
            ("Theatrical", re.compile(r"theatrical|театральн|прокатн", re.I)),
            ("Unrated", re.compile(r"unrated", re.I)),
            ("Open Matte", re.compile(r"open[ ._\-]?matte", re.I)),
            ("IMAX", re.compile(r"(?<![a-z])imax(?![a-z])", re.I)),
            ("Remastered", re.compile(r"remaster", re.I))]


def copy_key(path: str | None) -> str | None:
    """Path normalised so the parts of one multi-part copy and the same file on both servers compare equal."""
    if not path:
        return None
    p = path.replace("/", "\\").lower().strip()
    return _PART.sub("", p)


def edition_of(path: str | None, plex_edition: str | None = None) -> str | None:
    if plex_edition:
        return plex_edition
    name = PureWindowsPath(path or "").name
    return next((label for label, rx in _EDITION if rx.search(name)), None)


def _plex_copies(conn) -> list[dict]:
    out = []
    for r in conn.execute(
            """SELECT p.rating_key, p.title_id, p.library, p.file_path, p.resolution, p.audio FROM plex_items p
               JOIN titles t ON t.id = p.title_id
               WHERE p.present = 1 AND t.kind = 'movie' AND p.kind IN ('movie', 'unmatched')"""):
        try:
            entries = json.loads(r["audio"] or "[]")
        except ValueError:
            entries = []
        item = next((e for e in entries if e.get("stream") == "item"), {}) or {}
        media: dict = {}
        for e in entries:
            if e.get("stream") == "part" and e.get("file"):
                media.setdefault(e.get("media", 0), []).append(e)
        if not media and r["file_path"]:
            media = {0: [{"file": r["file_path"], "size": None}]}
        for parts in media.values():
            out.append({"title_id": r["title_id"], "server": "Plex", "library": r["library"], "path": parts[0]["file"],
                        "key": copy_key(parts[0]["file"]), "parts": len(parts),
                        "size": sum(p.get("size") or 0 for p in parts) or None, "resolution": r["resolution"],
                        "edition": edition_of(parts[0]["file"], item.get("edition"))})
    return out


def _jellyfin_copies(conn) -> list[dict]:
    out = []
    for r in conn.execute(
            """SELECT j.title_id, j.library, j.path, j.size_bytes FROM jellyfin_items j JOIN titles t ON t.id = j.title_id
               WHERE j.kind = 'movie' AND t.kind = 'movie'
                 AND j.synced_at >= datetime((SELECT max(synced_at) FROM jellyfin_items), '-30 minutes')"""):
        out.append({"title_id": r["title_id"], "server": "Jellyfin", "library": r["library"], "path": r["path"],
                    "key": copy_key(r["path"]), "parts": 1, "size": r["size_bytes"], "resolution": None,
                    "edition": edition_of(r["path"])})
    return out


def find(conn) -> list[dict]:
    """[{title_id, title, year, copies: [{servers, libraries, path, parts, size, resolution, edition}]}] — only films
    with more than one copy, sorted by title."""
    by: dict[int, dict[str, dict]] = {}
    for c in _plex_copies(conn) + _jellyfin_copies(conn):
        if not c["key"]:
            continue
        cur = by.setdefault(c["title_id"], {}).get(c["key"])
        if cur is None:
            by[c["title_id"]][c["key"]] = dict(c, servers=[c["server"]], libraries=[c["library"]])
        else:
            for k, v in (("servers", c["server"]), ("libraries", c["library"])):
                if v and v not in cur[k]:
                    cur[k].append(v)
            for k in ("size", "resolution", "edition"):
                cur[k] = cur[k] or c[k]
    out = []
    for tid, copies in by.items():
        if len(copies) < 2:
            continue
        t = conn.execute("SELECT title_ru, title_original, year FROM titles WHERE id = ?", (tid,)).fetchone()
        out.append({"title_id": tid, "title": (t["title_ru"] or t["title_original"]) if t else str(tid),
                    "title_original": t["title_original"] if t else None, "year": t["year"] if t else None,
                    "copies": sorted(copies.values(), key=lambda c: c["path"].lower())})
    return sorted(out, key=lambda d: (d["title"] or "").lower())


def _copy_line(c: dict) -> str:
    bits = [", ".join(c["servers"]) + (f" «{', '.join(x for x in c['libraries'] if x)}»" if any(c["libraries"]) else "")]
    if c.get("edition"):
        bits.append(f"издание: {c['edition']}")
    if c.get("resolution"):
        bits.append(f"{c['resolution']}p")
    if c.get("size"):
        bits.append(f"{c['size'] / 2 ** 30:.1f} ГБ")
    if c.get("parts", 1) > 1:
        bits.append(f"{c['parts']} части")
    return f"  - `{c['path']}` — " + ", ".join(bits)


def report_lines(conn) -> list[str]:
    """Markdown section for the «обнови базу» report (films/update.py → «Проблемы»)."""
    dups = find(conn)
    L = ["", "## Дубликаты фильмов в Plex / Jellyfin — удалить лишнее", ""]
    if not dups:
        return L + ["Дубликатов нет (у каждого фильма один файл)."]
    L.append(f"**{len(dups)}** — у фильма больше одной копии (разные издания тоже считаются, издание названо):")
    for d in dups:
        L.append(f"- {d['title']}" + (f" / {d['title_original']}" if d["title_original"] and d["title_original"] != d["title"] else "")
                 + (f" ({d['year']})" if d["year"] else "") + f" — {len(d['copies'])} копии:")
        L += [_copy_line(c) for c in d["copies"]]
    return L


def main():
    conn = db.connect()
    db.migrate(conn)
    print("\n".join(report_lines(conn)).strip())
    sys.exit(0)


if __name__ == "__main__":
    main()
