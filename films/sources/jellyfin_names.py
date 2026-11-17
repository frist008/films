"""Russian names of films, series and seasons in Jellyfin (owner, 2026-11-01: «убедиться что в джелефине все названия
фильмов на русском языке. Я не про сортировку а именно текст который виден в списке. Тоже самое для сезонов сериалов.
Только в джелефине. Название серий не трогай»; «Сразу подтверждение каждый сериал и фильм и скилы на это»).

A separate writer to Jellyfin, allowed by the owner for names only (2026-11-01). No API key: one javascript_tool call
in the logged-in Jellyfin tab of Chrome (owner, 2026-10-26: «Никакого ключа у тебя есть доступ в хром»), script
films/browser/jellyfin_names.js — sets the Name of a Movie / Series / Season, locks the field (LockedFields «Name», so a
metadata refresh does not bring the Latin name back) and reads each item back. Episodes and played state are never
touched; the watched marks stay with films/sources/jellyfin_guard.py and films/browser/jellyfin_guard.js.

The Russian name: the DB title linked to the item (titles.title_ru, when it is Cyrillic), else config/jellyfin_names.toml
(found by hand, with the source). An item with no Russian name is listed, not touched.

    python -X utf8 -m films.sources.jellyfin_names plan      # what would change (read-only)
    python -X utf8 -m films.sources.jellyfin_names code      # the text for the Jellyfin tab (one call, every item)
    python -X utf8 -m films.sources.jellyfin_names check     # fresh copy of jellyfin.db: nothing left without Cyrillic
    python -X utf8 -m films.sources.jellyfin_names rebind <item id> <title id>   # a film Jellyfin matched wrongly
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import tomllib

from .. import db
from . import jellyfin

CYR = re.compile(r"[А-Яа-яЁёІіЇїЄєҐґ]")
KINDS = {"movie": "Movie", "series": "Series", "season": "Season"}   # never Episode
CONFIG = db.ROOT / "config" / "jellyfin_names.toml"
SCRIPT = db.ROOT / "films" / "browser" / "jellyfin_names.js"


def _manual() -> dict[str, str]:
    try:
        return {k: v["name"] for k, v in tomllib.loads(CONFIG.read_text(encoding="utf-8")).get("names", {}).items()}
    except OSError:
        return {}


def _items() -> list[dict]:
    j = sqlite3.connect(f"file:{jellyfin.snapshot()}?mode=ro", uri=True)
    try:
        return jellyfin._items(j)
    finally:
        j.close()


def plan(conn) -> dict:
    """{'rename': [{item_id, kind, old, new, how}], 'no_name': [...]} — items whose visible name has no Cyrillic."""
    manual, out = _manual(), {"rename": [], "no_name": []}
    for i in _items():
        new, how = manual.get(i["name"]), "config/jellyfin_names.toml"
        if i["kind"] not in KINDS or (CYR.search(i["name"] or "") and not new):   # the owner's own name wins over Cyrillic
            continue
        if not new:
            row = conn.execute("SELECT t.title_ru FROM jellyfin_items x JOIN titles t ON t.id = x.title_id "
                               "WHERE x.item_id = ?", (i["item_id"],)).fetchone()
            if row and row[0] and CYR.search(row[0]):
                new, how = row[0], "titles.title_ru"
        (out["rename"] if new else out["no_name"]).append(
            {"item_id": i["item_id"], "kind": i["kind"], "old": i["name"], "new": new, "how": how if new else None})
    return out


def code(conn) -> str:
    """The text for one javascript_tool call in the Jellyfin tab: the names script + every planned rename."""
    items = [{"id": x["item_id"], "kind": x["kind"], "old": x["old"], "new": x["new"]} for x in plan(conn)["rename"]]
    user = db.private().get("jellyfin", {}).get("user")
    return (SCRIPT.read_text(encoding="utf-8")
            + f"\nawait __jfNames.rename({json.dumps(items, ensure_ascii=False)}, {json.dumps(user)});\n")


def rebind_code(conn, item_id: str, title_id: int) -> str:
    """The text that re-identifies a Jellyfin film matched to the wrong one as the DB title (its TMDB / IMDb ids)."""
    row = conn.execute("SELECT x.path, x.imdb_id, t.tmdb_id, t.imdb_id FROM jellyfin_items x, titles t "
                       "WHERE x.item_id = ? AND x.kind = 'movie' AND t.id = ?", (item_id, title_id)).fetchone()
    if not row or not row[2]:
        raise SystemExit("нет такого фильма в Jellyfin или у тайтла нет TMDB id")
    x = {"id": item_id, "file": row[0].replace(chr(92), "/").rsplit("/", 1)[-1], "wrong_imdb": row[1],
         "tmdb": row[2], "imdb": row[3]}
    user = db.private().get("jellyfin", {}).get("user")
    return (SCRIPT.read_text(encoding="utf-8")
            + f"\nawait __jfNames.rebind({json.dumps(x, ensure_ascii=False)}, {json.dumps(user)});\n")


def check(conn) -> dict:
    """After the tab run: every Movie / Series / Season name in a fresh copy of jellyfin.db must have Cyrillic."""
    items = _items()
    left = [{"item_id": i["item_id"], "kind": i["kind"], "name": i["name"]} for i in items
            if i["kind"] in KINDS and not CYR.search(i["name"] or "")]
    return {"checked": sum(i["kind"] in KINDS for i in items), "not_russian": left}


if __name__ == "__main__":
    c = db.connect()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "plan"
    if cmd == "rebind":
        print(rebind_code(c, sys.argv[2], int(sys.argv[3])))
    else:
        print(code(c) if cmd == "code" else json.dumps(check(c) if cmd == "check" else plan(c), ensure_ascii=False, indent=1))
