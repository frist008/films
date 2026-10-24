"""Franchise candidates from the owner's lists (R57): his big 2021–2025 list (films.kostya_list), Dima's comedies
(config/dima.toml), the «хочу» series (config/watchlist.toml) — and any other set of titles.

Run: python -X utf8 -m films.franchise_candidates

For every listed title: its TMDB collection and, through Wikidata (P345 IMDb id → item), its P8345 «media
franchise» and P179 «part of the series». A candidate = a collection / franchise item that is not yet in
config/franchises.toml (neither selected nor «none») and whose works are not already members of a selected
universe (franchise_members). Output: data/cache/franchise_candidates.json, grouped, with the listed titles and
their list marker — the input for an owner-survey round (one franchise per question, category in the text).
"""
from __future__ import annotations

import json
import tomllib
from collections import defaultdict

from . import db, franchise, kostya_list, watchlist

OUT = db.ROOT / "data" / "cache" / "franchise_candidates.json"


def listed_titles(conn) -> dict[int, str]:
    """title_id → which list(s) it comes from."""
    src: dict[int, set] = defaultdict(set)
    for t, m in kostya_list.markers_by_title().items():
        src[t].add(f"список Кости {kostya_list.SYMBOL.get(m, '')}")
    for t in watchlist.title_ids():
        src[t].add("сериалы «хочу»")
    for (t,) in conn.execute("SELECT title_id FROM responsibility WHERE person = 'Дима' AND source = 'manual'"):
        src[t].add("комедии Димы")
    return {t: ", ".join(sorted(v)) for t, v in src.items()}


def candidates(conn) -> list[dict]:
    with open(db.ROOT / "config" / "franchises.toml", "rb") as f:
        cfg = tomllib.load(f)["franchise"]
    known_cols = {c for fr in cfg for c in fr.get("tmdb_collections", [])}
    members = {r[0] for r in conn.execute("SELECT imdb_id FROM franchise_members WHERE confidence != 'review'")}
    listed = listed_titles(conn)
    rows = {r["id"]: dict(r) for r in conn.execute(
        f"SELECT * FROM titles WHERE id IN ({','.join('?' * len(listed))})", list(listed))} if listed else {}
    groups: dict[str, dict] = {}

    def add(key, name, t):
        g = groups.setdefault(key, {"key": key, "name": name, "titles": {}})
        g["titles"][t["id"]] = {"title": t["title_ru"] or t["title_original"], "year": t["year"],
                                "kind": "сезон %s" % t["season_number"] if t["kind"] == "series" else
                                ("мультфильм" if t["is_animation"] else "фильм"), "list": listed[t["id"]]}

    imdb_of = {}
    for t in rows.values():
        imdb = t["imdb_id"] or t["series_imdb_id"]
        if imdb in members:
            continue  # already inside a selected universe
        cid = t.get("collection_id")
        if cid and cid not in known_cols:
            name = conn.execute("SELECT name_ru FROM collections WHERE id = ?", (cid,)).fetchone()
            add(f"tmdb:{cid}", (name[0] if name else str(cid)).replace(" (Коллекция)", ""), t)
        if imdb:
            imdb_of.setdefault(imdb, []).append(t)
    # Wikidata: media franchise / part of the series
    qids = franchise.qids_by_external("P345", list(imdb_of))
    ents = franchise.entities({q for qs in qids.values() for q in qs})
    parents = defaultdict(set)
    for imdb, qs in qids.items():
        for q in qs:
            claims = (ents.get(q) or {}).get("claims") or {}
            for prop in ("P8345", "P179"):
                for v in claims.get(prop) or []:
                    parents[imdb].add(v if isinstance(v, str) else v.get("id") if isinstance(v, dict) else str(v))
    pents = franchise.entities({p for ps in parents.values() for p in ps if p})
    for imdb, ps in parents.items():
        for p in ps:
            if not p:
                continue
            for t in imdb_of[imdb]:
                add(f"wd:{p}", franchise.label(pents.get(p)) or p, t)
    out = sorted(groups.values(), key=lambda g: (-len(g["titles"]), g["name"]))
    for g in out:
        g["titles"] = list(g["titles"].values())
    return out


if __name__ == "__main__":
    c = db.connect()
    res = candidates(c)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(len(res), "candidates")
    for g in res[:60]:
        print(f"  {g['name']} [{g['key']}]: " + "; ".join(f"{x['title']} {x['year']} ({x['kind']}, {x['list']})" for x in g["titles"][:4]))
