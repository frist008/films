"""Owner's manual lists of who is responsible for a title (config/<person>.toml, path in settings
[responsible.<person>] list): every entry → a title (TMDB search; created when out of tracker scope) →
responsibility(person, source='manual').  The automatic genre/animation rule is in films.rank.apply_responsible.

Run: python -X utf8 -m films.responsible [dima|yana|all]
Unmatched or ambiguous names are printed — ask the owner, never guess.
"""
from __future__ import annotations

import json
import sys
import tomllib

from . import db
from .link import ensure_movie, store_names
from .sources import tmdb


def apply_list(conn, key: str) -> dict:
    with open(db.ROOT / "config" / "settings.toml", "rb") as f:
        d = tomllib.load(f)["responsible"][key]
    path = db.ROOT / d["list"]
    entries = tomllib.loads(path.read_text(encoding="utf-8")).get("title", []) if path.exists() else []
    out = {"person": d["name"], "entries": len(entries), "assigned": 0, "unmatched": []}
    for e in entries:
        tid = None
        if e.get("imdb_id"):
            tid = ensure_movie(conn, e["imdb_id"], f"list:{key}")
            tv = None if tid else tmdb.find_by_imdb(conn, e["imdb_id"]).get("tv")
            if tv:   # a series (kids' cartoon series of a franchise): every season is its own title
                from .link import ensure_season
                for s in (tmdb.tv_raw(conn, tv) or {}).get("seasons", []):
                    try:
                        stid = ensure_season(conn, tv, s["season_number"]) if s.get("season_number") else None
                    except Exception as ex:   # namesake series_key clash (R82) — skip that season, keep going
                        out["unmatched"].append({"name": e["name"], "season": s.get("season_number"), "error": str(ex)})
                        continue
                    if stid:
                        with conn:
                            conn.execute("INSERT OR REPLACE INTO responsibility (title_id, person, source) VALUES (?, ?, 'manual')",
                                         (stid, d["name"]))
                out["assigned"] += 1
                continue
            if not tid:
                out["unmatched"].append({"name": e["name"], "imdb_id": e["imdb_id"]})
                continue
        elif e.get("tmdb_id"):
            m = tmdb.movie(conn, e["tmdb_id"])
            tid = ensure_movie(conn, m["imdb_id"], f"list:{key}") if m and m.get("imdb_id") else None
        else:
            names = [n for part in e["name"].split(" / ") for n in store_names(part, [])]
            res = tmdb.search(conn, names, e.get("year"), "movie")
            m = tmdb.movie(conn, res["best"]["tmdb_id"]) if res["status"] == "matched" else None
            if not m and d.get("animation"):
                # a list of cartoons: of several namesakes take the only animated one (Дамбо 1941, not 2019)
                anim = [x for x in (tmdb.movie(conn, c["tmdb_id"]) for c in (res.get("candidates") or [])
                                    if (c.get("score") or 0) >= 0.75) if x and x.get("is_animation")]
                m = anim[0] if len(anim) == 1 else None
            if m and m.get("imdb_id"):
                tid = ensure_movie(conn, m["imdb_id"], f"list:{key}")
            if not tid:
                out["unmatched"].append({"name": e["name"], "status": res["status"],
                                         "candidates": [(c.get("title"), c.get("year"), c.get("tmdb_id"))
                                                        for c in (res.get("candidates") or [])[:4]]})
                continue
        with conn:
            conn.execute("INSERT OR REPLACE INTO responsibility (title_id, person, source) VALUES (?, ?, 'manual')",
                         (tid, d["name"]))
        out["assigned"] += 1
        if e.get("must_download"):
            out.setdefault("must_download", []).append(tid)
    if out.get("must_download"):   # 🟢 «обязательно скачать» → top of the sheet (films.rank)
        path_md = db.ROOT / "data" / "cache" / "must_download.json"
        cur = json.loads(path_md.read_text(encoding="utf-8")) if path_md.exists() else {}
        cur[key] = out["must_download"]
        path_md.write_text(json.dumps(cur, ensure_ascii=False), encoding="utf-8")
    return out


def urgent_titles() -> set[int]:
    """Very top of the sheet: 🟢 must_download (owner's lists), 🧞‍♀ orders and the ❤️ titles the owner did not tick
    for the «Костя Хочет» playlist (data/cache/kostya_want_urgent.json, written after that survey)."""
    out = set()
    for name in ("must_download.json", "kostya_want_urgent.json"):
        p = db.ROOT / "data" / "cache" / name
        if p.exists():
            v = json.loads(p.read_text(encoding="utf-8"))
            out |= {t for ids in (v.values() if isinstance(v, dict) else [v]) for t in ids}
    from .kostya_list import markers_by_title
    out |= {t for t, m in markers_by_title().items() if m == "order"}
    return out


if __name__ == "__main__":
    c = db.connect()
    db.migrate(c)
    keys = ["dima", "yana"] if (sys.argv[1:] or ["all"])[0] == "all" else sys.argv[1:]
    print(json.dumps([apply_list(c, k) for k in keys], ensure_ascii=False, indent=1))
