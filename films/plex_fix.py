"""Fix a wrong Plex match of a library file («Исправить сопоставление») — owner, 2026-10-25: «Всё сам исправь или хотя бы
убери сопоставление и напиши правильное имя». Plex matched «Talk to Me» (2022) as the German «Sprich mit mir» (2023) and
the US «The Grudge 2» (2006) as the Japanese one (2003).

Only metadata: GET /library/metadata/{rk}/matches (manual search) → PUT /library/metadata/{rk}/match?guid=…&name=….
The watch state lives on the ratingKey, which does not change; viewCount is read before and after and reported.
Run: python -X utf8 -m films.plex_fix <ratingKey> <imdb id> "<title>" <year> [--apply]
"""
import json
import sys

from . import db
from .sources import plex as plexsrc


def candidates(px, rk: str, title: str, year: int) -> list[dict]:
    data = px.get(f"/library/metadata/{rk}/matches", params={"manual": 1, "title": title, "year": year})
    return [{"guid": m.get("guid"), "name": m.get("name"), "year": m.get("year"), "score": m.get("score")}
            for m in (data.get("MediaContainer") or {}).get("SearchResult") or []]


def _meta(px, rk: str) -> dict:
    m = ((px.get(f"/library/metadata/{rk}").get("MediaContainer") or {}).get("Metadata") or [{}])[0]
    return {"title": m.get("title"), "year": m.get("year"), "viewCount": m.get("viewCount", 0),
            "guids": [g.get("id") for g in m.get("Guid") or []]}


def fix(rk: str, imdb_id: str, title: str, year: int, apply: bool = False) -> dict:
    px = plexsrc.Plex()
    before = _meta(px, rk)
    cands = candidates(px, rk, title, year)
    out = {"ratingKey": rk, "before": before, "candidates": cands[:5]}
    pick = None
    for cand in cands:
        if cand["year"] == year:
            pick = cand
            break
    out["pick"] = pick
    if apply and pick:
        r = px.s.put(f"{plexsrc.PLEX_URL}/library/metadata/{rk}/match",
                     params={"guid": pick["guid"], "name": pick["name"], "year": pick["year"]}, timeout=60)
        r.raise_for_status()
        after = _meta(px, rk)
        out["after"] = after
        out["imdb_ok"] = f"imdb://{imdb_id}" in after["guids"]
        out["view_count_kept"] = after["viewCount"] == before["viewCount"]
    return out


if __name__ == "__main__":
    a = sys.argv[1:]
    print(json.dumps(fix(a[0], a[1], a[2], int(a[3]), apply="--apply" in a), ensure_ascii=False, indent=1))
