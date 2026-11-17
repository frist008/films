"""Fix wrong Jellyfin identification (owner, 2026-10-25: «Всё сам исправь или хотя бы убери сопоставление и напиши
правильное имя. И номер серии»). Metadata only — never touches played state.

- Movies identified as other films: CURE (1997) as «Say It, Fight It, Cure It»; «Шаолинь» (2011) — item is right, but
  its .nfo (written by Jellyfin) says «Chhota Bheem» → re-apply the right film (RemoteSearch/Movie → RemoteSearch/Apply);
  Jellyfin rewrites the .nfo.
- Episodes in Yana's MOVIE library (a movie library can't hold episodes): «Уэнздей» S01E02–E08 seen as «Wednesday's
  Child», «Любовь, смерть и роботы» S01E17/E18 as «Transit 17» / «Vasco Live» → provider ids removed, name =
  «<series> — S01E02», metadata locked so a refresh won't re-match them.
Needs the Jellyfin API key (see films/sources/jellyfin_guard.py). Run: python -X utf8 -m films.jellyfin_fix [--apply]
"""
import json
import re
import sys

from . import db
from .sources import jellyfin_guard

URL = jellyfin_guard.URL
MOVIES = {  # item path fragment → correct IMDb id
    "CURE.1997.FRA.4K": "tt0123948",
    "Shaolin 2011 BluRay": "tt1533749",
}
SERIES = {"Wednesday (2022)": "Уэнздей", "Love, Death & Robots (2019)": "Любовь, смерть и роботы"}
EP = re.compile(r"S(\d{2})E(\d{2})", re.I)


def plan(conn) -> list[dict]:
    out = []
    for frag, imdb in MOVIES.items():
        for item_id, name in conn.execute("SELECT item_id, name FROM jellyfin_items WHERE path LIKE ?", (f"%{frag}%",)):
            out.append({"kind": "movie", "item_id": item_id, "now": name, "imdb": imdb})
    wrong = {x["path"] for x in json.loads((db.ROOT / "data" / "cache" / "disk_conflicts.json").read_text(encoding="utf-8"))
             if "jellyfin" in x}   # only the episodes Jellyfin matched to a film (the others are fine)
    for folder, ru in SERIES.items():
        for item_id, name, path in conn.execute(
                "SELECT item_id, name, path FROM jellyfin_items WHERE kind = 'movie' AND path LIKE ?", (f"%{folder}%",)):
            m = EP.search(path)
            if m and path in wrong:
                out.append({"kind": "episode", "item_id": item_id, "now": name,
                            "name": f"{ru} — S{m.group(1)}E{m.group(2)}"})
    return out


def apply(conn) -> list[dict]:
    jf = jellyfin_guard.Jellyfin()
    if not jf.key:
        return [{"error": "no Jellyfin API key"}]
    res = []
    for p in plan(conn):
        iid = p["item_id"]
        try:
            if p["kind"] == "movie":
                found = jf.s.post(f"{URL}/Items/RemoteSearch/Movie", timeout=60, json={
                    "SearchInfo": {"ProviderIds": {"Imdb": p["imdb"]}}, "ItemId": iid}).json()
                pick = next((f for f in found if (f.get("ProviderIds") or {}).get("Imdb") == p["imdb"]), None)
                if not pick:
                    res.append(p | {"error": "not found by IMDb id"})
                    continue
                jf.s.post(f"{URL}/Items/RemoteSearch/Apply/{iid}", params={"replaceAllImages": "true"},
                          json=pick, timeout=120).raise_for_status()
            else:
                dto = jf.s.get(f"{URL}/Items/{iid}", params={"userId": jf.user_id}, timeout=30).json()
                dto.update({"Name": p["name"], "OriginalTitle": p["name"], "ProviderIds": {}, "LockData": True})
                jf.s.post(f"{URL}/Items/{iid}", json=dto, timeout=60).raise_for_status()
            after = jf.s.get(f"{URL}/Items/{iid}", params={"userId": jf.user_id}, timeout=30).json()
            res.append(p | {"after": after.get("Name"), "ids": after.get("ProviderIds")})
        except Exception as e:  # report every item, keep going
            res.append(p | {"error": str(e)[:200]})
    return res


if __name__ == "__main__":
    c = db.connect()
    print(json.dumps(apply(c) if "--apply" in sys.argv else plan(c), ensure_ascii=False, indent=1))
