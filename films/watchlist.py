"""The owner's «хочу посмотреть» list with high priority (R48): config/watchlist.toml.

Run: python -X utf8 -m films.watchlist [resolve|releases|status]

resolve   — every item → TMDB (series first, then film; names split on « / »; an ambiguous name is decided by
            TMDB votes like the Plex show map), map saved to data/cache/watchlist_map.json (status per item;
            'manual' entries there are kept). Every season of a matched series gets a title (ensure_season),
            a film — ensure_movie.
releases  — rutor search for every resolved item, all years (no year filter): categories foreign_series (4),
            animation (7), anime (10) for series/anime, foreign_films (1) / animation for films; new rows go to
            `releases`, then the usual `films rutor pages` → `parse` → `match` link them to the titles.
In the sheet: a watchlist title shows a light-green row and «Хочу» in «У меня» (queued «В очереди» wins);
the year filter and the «мультсериал» rule do not hide it; sorting is unchanged.
"""
from __future__ import annotations

import json
import re
import sys
import tomllib
from datetime import date

from . import db
from .sources import tmdb

CFG = db.ROOT / "config" / "watchlist.toml"
MAP = db.ROOT / "data" / "cache" / "watchlist_map.json"


def items() -> list[dict]:
    with open(CFG, "rb") as f:
        return tomllib.load(f).get("item", [])


def _names(name: str) -> list[str]:
    out = []
    for part in name.split(" / "):
        part = re.sub(r"\(([^)]*)\)", lambda m: " " if not re.fullmatch(r"(19|20)\d{2}", m.group(1)) else " ", part)
        part = re.sub(r"[«»]", "", part).strip(" .")
        if part:
            out.append(part)
    return out


def _year(name: str) -> int | None:
    m = re.search(r"\((19|20)\d{2}\)", name)
    return int(m.group(0)[1:-1]) if m else None


def resolve(conn) -> dict:
    from .link import _popular_before, ensure_movie, ensure_season
    cur = json.loads(MAP.read_text(encoding="utf-8")) if MAP.exists() else {}
    stats = {"items": 0, "matched": 0, "ambiguous": [], "not_found": [], "titles": 0}
    today = date.today().isoformat()
    for it in items():
        stats["items"] += 1
        name = it["name"]
        if cur.get(name, {}).get("status") == "manual":
            m = cur[name]
        else:
            names, year = _names(name), _year(name)
            kinds = ["movie", "tv"] if it.get("kind") == "film" else ["tv", "movie"]
            m = {"status": "not_found"}
            for kind in kinds:
                res = tmdb.search(conn, names, year, kind)
                best = res.get("best")
                if res["status"] == "ambiguous" and not year:
                    best = _popular_before(conn, res, today) if kind == "tv" else None
                if best:
                    m = {"status": "matched" if res["status"] == "matched" else "auto_popular", "kind": kind,
                         "tmdb_id": best["tmdb_id"], "title": best.get("title"), "year": best.get("year")}
                    break
                if res["status"] == "ambiguous" and m["status"] == "not_found":
                    m = {"status": "ambiguous", "kind": kind, "candidates": [
                        {k: c.get(k) for k in ("tmdb_id", "title", "original_title", "year")}
                        for c in (res.get("candidates") or [])[:5]]}
            cur[name] = m
        if m.get("tmdb_id"):
            stats["matched"] += 1
            with conn:
                if m["kind"] == "tv":
                    tv = tmdb.tv(conn, m["tmdb_id"]) or {}
                    ids = []
                    for s in tv.get("seasons") or []:
                        if s.get("season_number"):
                            tid = ensure_season(conn, m["tmdb_id"], s["season_number"])
                            if tid:
                                ids.append(tid)
                    m["title_ids"] = ids
                else:
                    mv = tmdb.movie(conn, m["tmdb_id"]) or {}
                    tid = ensure_movie(conn, mv["imdb_id"], "watchlist") if mv.get("imdb_id") else None
                    m["title_ids"] = [tid] if tid else []
            stats["titles"] += len(m.get("title_ids") or [])
        else:
            stats[m["status"] if m["status"] in ("ambiguous", "not_found") else "not_found"].append(name)
    MAP.parent.mkdir(parents=True, exist_ok=True)
    MAP.write_text(json.dumps(cur, ensure_ascii=False, indent=1), encoding="utf-8")
    db.mark_sync(conn, "watchlist", None, {k: (len(v) if isinstance(v, list) else v) for k, v in stats.items()})
    return stats


def title_ids(conn=None) -> set[int]:
    if not MAP.exists():
        return set()
    return {t for m in json.loads(MAP.read_text(encoding="utf-8")).values() for t in m.get("title_ids") or []}


def releases(conn) -> dict:
    """rutor search for every resolved item, all years."""
    from .sources import rutor
    cur = json.loads(MAP.read_text(encoding="utf-8"))
    with open(CFG, "rb") as f:
        russian = {i["name"] for i in tomllib.load(f)["item"] if i.get("russian")}
    stats = {"queries": 0, "rows": 0, "new": 0}
    for name, m in cur.items():
        if not m.get("tmdb_id"):
            continue
        body = (tmdb.tv(conn, m["tmdb_id"]) if m["kind"] == "tv" else tmdb.movie(conn, m["tmdb_id"])) or {}
        queries = list(dict.fromkeys(q for q in (body.get("title_original"), body.get("title_en"), body.get("title_ru"))
                                     if q and len(q) >= 3))
        cats = [(4, "foreign_series"), (7, "animation"), (10, "anime")] if m["kind"] == "tv" else \
               [(1, "foreign_films"), (7, "animation"), (10, "anime")]
        if name in russian:   # our (Russian-language) series: rutor «Наши сериалы»
            cats = [(16, "russian_series")]
        for q in queries:
            for cat, category in cats:
                total, rows = rutor.search_all(cat, q)
                stats["queries"] += 1
                stats["rows"] += len(rows)
                stats["new"] += rutor.upsert(conn, rows, category)
        print(f"  {name}: {stats}", file=sys.stderr, flush=True)
    db.mark_sync(conn, "watchlist_releases", None, stats)
    return stats


if __name__ == "__main__":
    c = db.connect()
    db.migrate(c)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "resolve"
    out = resolve(c) if cmd == "resolve" else releases(c) if cmd == "releases" else {"titles": len(title_ids())}
    print(json.dumps(out, ensure_ascii=False, indent=1))
