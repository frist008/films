"""The owner's own videos (his clips, editing projects: E:\\Vegas\\…, «Фарес» episodes in Plex) are not kept even in the
local DB (owner, 2026-10-25: «Этих видео личных даже в базе не должно быть»; 2026-10-29: «Удалить сейчас»).

`purge` removes the rows already stored; the disk walk, the Plex library scan and the Plex history sync skip them with
`is_path` / `is_plex_row`, so the next «обнови базу» does not bring them back. The show names the link step marked
'personal' live in data/cache/plex_show_map_private.json (git-ignored)."""
from __future__ import annotations

import json
import sys

from . import db

DIRS = ["/work/youtube/", "e:/vegas/"]   # folders of the owner's own videos (lower case, forward slashes)
NAMES_PATH = db.ROOT / "data" / "cache" / "plex_show_map_private.json"


def is_path(path: str | None) -> bool:
    sp = (path or "").replace(chr(92), "/").lower()
    return any(x in sp for x in DIRS)


def names() -> set[str]:
    try:
        return set(json.loads(NAMES_PATH.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return set()


def _named(v, ns: set[str]) -> bool:
    return bool(v) and (v in ns or any(v.startswith(n + " ") or v.startswith(n + "_") for n in ns))


def is_plex_row(d: dict, ns: set[str]) -> bool:
    """A plex_items / plex_history / jellyfin_items row of the owner's own video (by its file or its show name)."""
    if d.get("title_id") is not None:
        return False
    if any(is_path(d.get(c)) for c in ("file_path", "path")):
        return True
    return any(_named(d.get(c), ns) for c in ("show_title", "plex_title", "name", "original_title",
                                                "grandparent_title", "title"))


def purge(conn, dry: bool = False) -> dict:
    """Delete the owner's own videos from disk_files, plex_items, plex_history, jellyfin_items → counts per table."""
    ns, out = names(), {}
    paths = [p for (p,) in conn.execute("SELECT path FROM disk_files WHERE how = 'personal'")]
    paths += [p for (p,) in conn.execute("SELECT path FROM disk_files WHERE how IS NOT 'personal'") if is_path(p)]
    out["disk_files"] = len(paths)
    if not dry:
        with conn:
            conn.executemany("DELETE FROM disk_files WHERE path = ?", [(p,) for p in paths])
    for t, key in (("plex_items", "rating_key"), ("plex_history", "id"), ("jellyfin_items", "rowid")):
        cur = conn.execute(f"SELECT {key} AS k, * FROM {t}")
        cols = [x[0] for x in cur.description]
        keys = [r[0] for r in cur if is_plex_row(dict(zip(cols, r)), ns)]
        out[t] = len(keys)
        if not dry and keys:
            with conn:
                conn.executemany(f"DELETE FROM {t} WHERE {key} = ?", [(k,) for k in keys])
    return out


if __name__ == "__main__":
    c = db.connect()
    print(purge(c, dry="--dry" in sys.argv))
