"""The owner's own videos are not kept in the database at all (owner, 2026-10-24: «Эти видео не нужны. Они личные.
Их в базу не надо») — an exception to «ничего не удалять из БД» named by the owner himself.

What counts as personal: disk files the disk scan marked 'personal' (films/disks.py: PERSONAL folders, user-profile
clips), and shows the link step put under status 'personal' (data/cache/plex_show_map_private.json). Their Plex items,
Jellyfin items, Jellyfin plays and Plex history rows are removed, then the disk rows themselves.

Every source sync re-reads these files, so purge() runs after each of them (cli: `films disks`, `films link`;
`films update --finish`). What it learned is kept in data/cache/personal_seen.json (not in git: only sha1 of paths
and the show names, needed to recognise the rows on the next sync).

Run: python -X utf8 -m films.personal
"""
from __future__ import annotations

import hashlib
import json
import sys

from . import db

SEEN = db.ROOT / "data" / "cache" / "personal_seen.json"
PRIVATE_MAP = db.ROOT / "data" / "cache" / "plex_show_map_private.json"


def _sha(path: str) -> str:
    return hashlib.sha1(path.replace("\\", "/").lower().encode("utf-8")).hexdigest()


def _load() -> dict:
    try:
        d = json.loads(SEEN.read_text(encoding="utf-8"))
        return {"paths": set(d.get("paths", [])), "names": set(d.get("names", []))}
    except (OSError, ValueError):
        return {"paths": set(), "names": set()}


def _is_personal_path(path: str | None, seen: dict) -> bool:
    from .disks import PERSONAL, _slash
    if not path:
        return False
    sp = _slash(path)
    return _sha(path) in seen["paths"] or any(x in sp for x in PERSONAL)


def purge(conn) -> dict:
    seen = _load()
    seen["paths"] |= {_sha(p) for (p,) in conn.execute("SELECT path FROM disk_files WHERE how = 'personal'")}
    try:
        seen["names"] |= {k for k, v in json.loads(PRIVATE_MAP.read_text(encoding="utf-8")).items()
                          if v.get("status") == "personal"}
    except (OSError, ValueError):
        pass
    names = seen["names"]

    def named(*vals) -> bool:
        return any(v and (v in names or any(v.startswith(n + " ") or v.startswith(n + "_") for n in names)) for v in vals)

    plex = [rk for rk, fp, st, pt, audio, files in conn.execute(
                "SELECT rating_key, file_path, show_title, plex_title, audio, files FROM plex_items WHERE title_id IS NULL")
            if _is_personal_path(fp, seen) or named(st, pt)
            or any(f'"show": "{n}"' in (audio or "") + (files or "") for n in names)]
    jf = [i for i, p, n, ot in conn.execute(
              "SELECT item_id, path, name, original_title FROM jellyfin_items WHERE title_id IS NULL")
          if _is_personal_path(p, seen) or named(n, ot)]
    hist = [i for i, t, gt in conn.execute(
                "SELECT id, title, grandparent_title FROM plex_history WHERE title_id IS NULL") if named(t, gt)]
    out = {"plex_items": len(plex), "jellyfin_items": len(jf), "plex_history": len(hist)}
    with conn:
        for i in range(0, len(plex), 500):
            ch = plex[i:i + 500]
            conn.execute(f"DELETE FROM plex_items WHERE rating_key IN ({','.join('?' * len(ch))})", ch)
        for i in range(0, len(jf), 500):
            ch = jf[i:i + 500]
            out["jellyfin_played"] = out.get("jellyfin_played", 0) + conn.execute(
                f"DELETE FROM jellyfin_played WHERE item_id IN ({','.join('?' * len(ch))}) AND title_id IS NULL", ch).rowcount
            conn.execute(f"DELETE FROM jellyfin_items WHERE item_id IN ({','.join('?' * len(ch))})", ch)
        for i in range(0, len(hist), 500):
            ch = hist[i:i + 500]
            conn.execute(f"DELETE FROM plex_history WHERE id IN ({','.join('?' * len(ch))})", ch)
        out["disk_files"] = conn.execute("DELETE FROM disk_files WHERE how = 'personal'").rowcount
    SEEN.parent.mkdir(parents=True, exist_ok=True)
    SEEN.write_text(json.dumps({"paths": sorted(seen["paths"]), "names": sorted(names)}, ensure_ascii=False), encoding="utf-8")
    return out


if __name__ == "__main__":
    c = db.connect()
    print(json.dumps(purge(c), ensure_ascii=False))
    sys.exit(0)
