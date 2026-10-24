"""Releases for every followed franchise work the owner has not seen (CLAUDE.md: franchise films are collected for any year;
PLAN 6б «Сбор раздач для всех членов вселенных за все годы», R65 — new universes).

For each confirmed member of a `scope = "all"` franchise that has no release yet, is not hidden by the owner's rules
(franchise_seen._too_old) and is neither watched nor downloaded: rutor search «<original> <year>», then «<RU> <year>»
(rutor names are «RU / Original (year) …»), in the category of its kind. Only rows naming the work (title + year ±1)
are kept — a short title would otherwise drag in thousands of foreign releases. Then the usual chain:
`films rutor pages` → `films rutor parse` → `films match` (IMDb link on the release page decides the title).

Run: python -X utf8 -m films.franchise_releases [--dry] [--only KEY[,KEY...]] [--with-single]
    --with-single: also single_source members (e.g. game adaptations confirmed by films.sources.games only)
"""
from __future__ import annotations

import json
import re
import sys
import tomllib

from . import db
from . import franchise_seen as fs
from .sources import rutor, tmdb

CATS = {"film": [(1, "foreign_films")], "short": [(1, "foreign_films"), (7, "animation")], "special": [(1, "foreign_films"), (4, "foreign_series")],
        "documentary": [(1, "foreign_films")], "animated film": [(7, "animation")], "animated series": [(7, "animation")],
        "series": [(4, "foreign_series")]}


def _norm(s: str | None) -> str:
    s = (s or "").lower().replace("ё", "е")
    return re.sub(r"\s+", " ", re.sub(r"[^\w]+", " ", s)).strip()


def todo(conn, only: set[str] | None = None, with_single: bool = False) -> list[dict]:
    from .seen_survey import have
    with open(db.ROOT / "config" / "franchises.toml", "rb") as f:
        frs = {fr["key"]: fr for fr in tomllib.load(f)["franchise"]}
    watched = {r[0] for r in conn.execute("""SELECT coalesce(t.imdb_id, t.series_imdb_id) FROM watched w JOIN titles t
                                             ON t.id = w.title_id WHERE w.source NOT LIKE '%partial'""")}
    got = have(conn)
    out = []
    for key, imdb, kind, title, title_ru, year, tmdb_id, tmdb_type in conn.execute(
            """SELECT franchise_key, imdb_id, kind, title, title_ru, year, tmdb_id, tmdb_type FROM franchise_members m
               WHERE confidence IN (?, ?) AND NOT EXISTS (SELECT 1 FROM titles t JOIN release_titles rt ON rt.title_id = t.id
                     WHERE t.imdb_id = m.imdb_id OR t.series_imdb_id = m.imdb_id)""",
            ("confirmed", "single_source" if with_single else "confirmed")):
        if only and key not in only:
            continue
        fr = frs.get(key)
        if not fr or fr.get("scope") != "all" or not imdb or imdb in watched or imdb in got:
            continue
        if fs._too_old(fr, {"kind": kind, "year": year, "imdb_id": imdb}):
            continue
        cats = list(CATS.get(kind, [(1, "foreign_films")]))
        if fr.get("anime"):
            cats.append((10, "anime"))
        out.append({"franchise": key, "imdb": imdb, "kind": kind, "title": title, "title_ru": title_ru, "year": year,
                    "tmdb": tmdb_id, "tmdb_type": tmdb_type, "cats": cats})
    return out


def _names(conn, w: dict) -> list[str]:
    names = [w["title"], w["title_ru"]]
    if w["tmdb"]:
        body = (tmdb.tv(conn, w["tmdb"]) if w["tmdb_type"] == "tv" else tmdb.movie(conn, w["tmdb"])) or {}
        names += [body.get("title_original"), body.get("title_en"), body.get("title_ru")]
    return [n for n in dict.fromkeys(n for n in names if n and len(_norm(n)) >= 2)]


def collect(conn, dry: bool = False, log=print, only: set[str] | None = None, with_single: bool = False) -> dict:
    works = todo(conn, only, with_single)
    st = {"works": len(works), "queries": 0, "rows": 0, "kept": 0, "new": 0, "with_hits": 0}
    for i, w in enumerate(works, 1):
        names = _names(conn, w)
        keys = {_norm(n) for n in names}
        hits = {}
        for name in names[:3]:
            q = f"{name} {w['year']}" if w["year"] else name
            for cat, category in w["cats"]:
                try:
                    _, rows = rutor.search_all(cat, q)
                except Exception as e:  # noqa: BLE001 — one failed query must not stop the sweep
                    log(f"  rutor error [{q}] cat {cat}: {e}")
                    continue
                st["queries"] += 1
                st["rows"] += len(rows)
                for r in rows:
                    n = _norm(r["name"])
                    years = {int(y) for y in re.findall(r"\b(?:19|20)\d\d\b", r["name"])}
                    if w["year"] and not any(abs(y - w["year"]) <= 1 for y in years):
                        continue
                    if not any(k and k in n for k in keys):
                        continue
                    hits[r["source_id"]] = (r, category)
            if hits:
                break
        if hits:
            st["with_hits"] += 1
            st["kept"] += len(hits)
            if not dry:
                for category in {c for _, c in hits.values()}:
                    st["new"] += rutor.upsert(conn, [r for r, c in hits.values() if c == category], category)
        if i % 25 == 0:
            log(f"  {i}/{len(works)} {st}")
    db.mark_sync(conn, "franchise_releases", None, st) if not dry else None
    return st


if __name__ == "__main__":
    c = db.connect()
    db.migrate(c)
    only = set(sys.argv[sys.argv.index("--only") + 1].split(",")) if "--only" in sys.argv else None
    print(json.dumps(collect(c, dry="--dry" in sys.argv, log=lambda m: print(m, file=sys.stderr, flush=True), only=only,
                             with_single="--with-single" in sys.argv),
                     ensure_ascii=False))
