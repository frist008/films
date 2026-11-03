"""Health checks run on every «обнови базу» (owner, 2026-10-23).

Run: python -X utf8 -m films.health

1. Library folders: every folder of every Plex library (HTTP API /library/sections) and of every Jellyfin
   library (root\\default\\<library>\\*.mblink in the Jellyfin data folder) must exist on disk — a library that
   points at a deleted / renamed folder is reported (fixing it in Plex / Jellyfin is the owner's call).
2. qBittorrent queue: a title queued twice, and a queued title already downloaded (Plex / Jellyfin) — with both
   file names, sizes and folders, so the owner can decide (upgrade / copy for another library / duplicate).
"""
from __future__ import annotations

import json
import os
import tomllib
from collections import defaultdict
from pathlib import Path

from . import db


def library_folders() -> dict:
    from .sources import plex as plexsrc
    out = {"plex": [], "jellyfin": []}
    px = plexsrc.Plex()
    for s in px.get("/library/sections")["MediaContainer"].get("Directory") or []:
        for loc in s.get("Location") or []:
            out["plex"].append({"library": s["title"], "path": loc["path"], "exists": os.path.isdir(loc["path"])})
    with open(db.ROOT / "config" / "settings.toml", "rb") as f:
        data_dir = Path(tomllib.load(f)["jellyfin"]["data_dir"])
    root = data_dir.parent / "root" / "default"
    if root.is_dir():
        for lib in sorted(p for p in root.iterdir() if p.is_dir()):
            for link in lib.glob("*.mblink"):
                path = link.read_text(encoding="utf-8").strip()
                out["jellyfin"].append({"library": lib.name, "path": path, "exists": os.path.isdir(path)})
    out["dead"] = [dict(x, server=k) for k in ("plex", "jellyfin") for x in out[k] if not x["exists"]]
    return out


def queue_conflicts(conn) -> dict:
    q = conn.execute("""SELECT q.*, t.title_ru, t.year, t.season_number FROM qbt_torrents q JOIN titles t ON t.id = q.title_id
                        WHERE q.present = 1 AND q.progress < 0.999""").fetchall()
    by = defaultdict(list)
    for r in q:
        by[r["title_id"]].append(dict(r))
    gb = lambda b: round((b or 0) / 2 ** 30, 1)  # noqa: E731
    dup, have = [], []
    for tid, rs in by.items():
        if len(rs) > 1:
            dup.append({"title": rs[0]["title_ru"], "year": rs[0]["year"],
                        "queued": [{"name": r["name"], "gb": gb(r["size_bytes"]), "save_path": r["save_path"]} for r in rs]})
        files = [{"name": os.path.basename(p), "gb": gb(s), "folder": os.path.dirname(p), "library": lib, "server": srv}
                 for srv, p, s, lib in conn.execute(
                     """SELECT 'plex', file_path, size_bytes, library FROM plex_items WHERE title_id = ? AND present = 1
                        UNION SELECT 'jellyfin', path, size_bytes, library FROM jellyfin_items
                        WHERE title_id = ? AND kind IN ('movie', 'episode')""", (tid, tid))]
        # any other video file on the local drives (R85) — but not the queued torrent's own, still incomplete files
        known = {os.path.normcase(os.path.join(f["folder"], f["name"])) for f in files}
        own = [os.path.normcase(os.path.join(r["save_path"] or "", r["name"] or "")) for r in rs]
        try:
            for p, s in conn.execute("SELECT path, size_bytes FROM disk_files WHERE title_id = ? AND present = 1", (tid,)):
                n = os.path.normcase(p)
                if n not in known and not any(n == o or n.startswith(o + os.sep) for o in own):
                    files.append({"name": os.path.basename(p), "gb": gb(s), "folder": os.path.dirname(p),
                                  "library": None, "server": "disk"})
        except Exception:  # noqa: BLE001 — disk_files not migrated yet
            pass
        if files:
            have.append({"title": rs[0]["title_ru"], "year": rs[0]["year"], "season": rs[0]["season_number"],
                         "queued": [{"name": r["name"], "gb": gb(r["size_bytes"]), "save_path": r["save_path"]} for r in rs],
                         "downloaded": files})
    return {"queued_twice": dup, "queued_but_downloaded": have}


if __name__ == "__main__":
    c = db.connect()
    lf = library_folders()
    res = {"dead_library_folders": lf["dead"], **queue_conflicts(c)}
    db.mark_sync(c, "health", None, {k: len(v) for k, v in res.items()})
    print(json.dumps(res, ensure_ascii=False, indent=1))
