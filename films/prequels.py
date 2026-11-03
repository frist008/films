"""Earlier parts of the sequels shown on the sheet (R180, owner 2026-10-25): «Если в фильмах или мультиках есть сиквелы
каких-то фильмов и первые части мы не видели — надо добавить их прошлые части в документ, даже если они вышли раньше
2024 года. А также разместить рядом … сортировка должна быть такая же и со своей логикой в плейлистах YouTube».

A sequel = a film shown on «Фильмы» / «Мультфильмы» (rank.compute row without hidden_reasons, kind movie) that belongs
to a TMDB collection with earlier released parts. An earlier part is kept when:
  * it is out (release date ≤ today) and earlier than the sequel (by release date);
  * the owner has not watched it (`watched`, any source except *_partial — by TMDB id or IMDb id);
  * it is a feature (TMDB runtime ≥ MIN_RUNTIME minutes, or unknown) — shorts / TV specials of a collection are not
    part of its story line;
  * its collection is not in a franchise the owner set to «none», and it is not older than the franchise's `from_year`
    («с 2011 года (перезапуск), классику не собирать», «Только 2024–26» — the owner's own answer for that franchise).
Followed franchises (`scope = "all"`) need nothing here: their members are collected for any year by
films.franchise_releases and never hidden by the year filter (rank `in_franchise_scope`).

What happens to them:
  1. `ensure` — a titles row from TMDB (films.link.ensure_movie by the IMDb id of TMDB's external ids);
     data/cache/prequels.json keeps {title_id: {sequel ids, collection}} for the next steps and the report;
  2. their rutor releases for any year — films.encountered, source «prequels» (same search by original / Latin /
     Russian names, page fetch into release_raw, parse, match);
  3. rank.compute does not hide them by year (`year_exempt`: the same rules, computed from the DB alone); the other
     filters (no voice-over, IMDb < 5 and old, «не интересен», downloaded) still apply;
  4. the sheet keeps a collection together, by year, at the place of its top row (sheet._group_keys / _grouped);
  5. playlists: the first unseen earlier part's trailer instead of the sequel's (R97), at the sheet's position.

Run: python -X utf8 -m films.prequels scan            — what would be added (no writes; TMDB from cache when --offline)
     python -X utf8 -m films.prequels ensure          — create the missing titles, write data/cache/prequels.json
     python -X utf8 -m films.prequels collect [--force|--missing] — ensure + films.encountered collect --only prequels
                                                     (--missing: search again only the parts still without a release)
     python -X utf8 -m films.prequels report          — shown / hidden (and why) of the earlier parts
"""
from __future__ import annotations

import json
import sys
from datetime import date

from . import db

CACHE = db.ROOT / "data" / "cache" / "prequels.json"
MIN_RUNTIME = 40          # minutes: a shorter collection part is a short / special, not a story part


# ------------------------------------------------------------------------------------------------------- rules

def franchise_rules() -> tuple[set[int], dict[int, int]]:
    """(collections of franchises set to «none», collection → from_year of a franchise with one)."""
    from . import rank
    ignored, from_year = set(), {}
    for f in rank.franchise_cfg():
        cols = f.get("tmdb_collections") or []
        if f.get("scope") == "none":
            ignored.update(cols)
        elif f.get("from_year"):
            for c in cols:
                from_year[c] = max(from_year.get(c, 0), f["from_year"])
    return ignored, from_year


def _date(t: dict) -> str:
    return (t.get("release_date") or "")[:10] or (f"{t['year']}-12-31" if t.get("year") else "")


def shown_sequel_dates(rows: list[dict], ignored: set[int]) -> dict[int, str]:
    """collection id → release date of its latest film shown on «Фильмы» / «Мультфильмы» (rank.compute rows)."""
    latest: dict[int, str] = {}
    for r in rows:
        t = r["title"]
        cid = t.get("collection_id")
        if (r["variant_index"] or r["hidden_reasons"] or r.get("quality_watch") or t["kind"] != "movie"
                or not cid or cid in ignored):
            continue
        d = _date(t)
        if d and d > latest.get(cid, ""):
            latest[cid] = d
    return latest


def year_exempt(rows: list[dict], ignored: set[int], from_year: dict[int, int], today: str | None = None) -> set[int]:
    """R180 for rank.compute: title ids hidden ONLY for their year («год …») that are an earlier part of a shown
    sequel — they are shown. Watched parts keep «видел»; other reasons (voice, IMDb, «не интересен») keep them hidden."""
    today = today or date.today().isoformat()
    latest = shown_sequel_dates(rows, ignored)
    out = set()
    for r in rows:
        t = r["title"]
        cid = t.get("collection_id")
        if r["variant_index"] or t["kind"] != "movie" or cid not in latest:
            continue
        hr = r["hidden_reasons"]
        if not any(h.startswith("год ") for h in hr):
            continue
        d = _date(t)
        if not d or d >= latest[cid] or d > today:
            continue
        if (t.get("year") or 0) < from_year.get(cid, 0):
            continue
        if t.get("runtime_min") is not None and t["runtime_min"] < MIN_RUNTIME:
            continue
        out.add(t["id"])
    return out


# ------------------------------------------------------------------------------------------------ TMDB scan

def _watched(conn) -> tuple[set[int], set[str]]:
    q = ("SELECT t.tmdb_id, t.imdb_id FROM titles t JOIN watched w ON w.title_id = t.id "
         "WHERE t.kind = 'movie' AND w.source NOT LIKE '%\\_partial' ESCAPE '\\'")
    rows = conn.execute(q).fetchall()
    return {r[0] for r in rows if r[0]}, {r[1] for r in rows if r[1]}


def scan(conn, rows: list[dict] | None = None, log=print) -> list[dict]:
    """Every unseen earlier part of every shown sequel: [{tmdb_id, title, year, release_date, collection_id,
    sequels: [title ids], title_id (None when not in the DB yet), skip (reason or None)}]."""
    from . import rank
    from .sources import tmdb
    rows = rows if rows is not None else rank.compute(conn)
    ignored, from_year = franchise_rules()
    latest = shown_sequel_dates(rows, ignored)
    seq_by_col: dict[int, list[dict]] = {}
    for r in rows:
        t = r["title"]
        if (not r["variant_index"] and not r["hidden_reasons"] and not r.get("quality_watch") and t["kind"] == "movie"
                and t.get("collection_id") in latest):
            seq_by_col.setdefault(t["collection_id"], []).append(t)
    w_tmdb, w_imdb = _watched(conn)
    today = date.today().isoformat()
    parts_out: dict[int, dict] = {}
    for cid, seqs in seq_by_col.items():
        try:
            coll = tmdb.collection(conn, cid)
        except tmdb.TmdbError as e:
            log(f"  collection {cid}: {e}")
            continue
        for p in (coll or {}).get("parts") or []:
            if p.get("media_type") not in (None, "movie") or not p.get("release_date") or p["release_date"] > today:
                continue
            later = [s for s in seqs if p["release_date"] < _date(s) and s.get("tmdb_id") != p["tmdb_id"]]
            if not later:
                continue
            e = parts_out.setdefault(p["tmdb_id"], {"tmdb_id": p["tmdb_id"], "title": p.get("title_ru") or p.get("title_original"),
                                                    "year": p.get("year"), "release_date": p["release_date"],
                                                    "collection_id": cid, "sequels": [], "title_id": None, "skip": None})
            e["sequels"] += [s["id"] for s in later if s["id"] not in e["sequels"]]
    for e in parts_out.values():
        row = conn.execute("SELECT id, imdb_id, runtime_min FROM titles WHERE tmdb_id = ? AND kind = 'movie'",
                           (e["tmdb_id"],)).fetchone()
        if row:
            e["title_id"], e["imdb_id"], runtime = row[0], row[1], row[2]
        else:
            try:
                raw = tmdb.movie_raw(conn, e["tmdb_id"]) or {}
            except tmdb.TmdbError:
                raw = {}
            e["imdb_id"] = raw.get("imdb_id") or (raw.get("external_ids") or {}).get("imdb_id")
            runtime = raw.get("runtime") or None
        if e["tmdb_id"] in w_tmdb or (e.get("imdb_id") and e["imdb_id"] in w_imdb):
            e["skip"] = "видел"
        elif (e["year"] or 0) < from_year.get(e["collection_id"], 0):
            e["skip"] = f"франшиза — с {from_year[e['collection_id']]} года"
        elif runtime and runtime < MIN_RUNTIME:
            e["skip"] = f"короткий ({runtime} мин)"
        elif not e.get("imdb_id") and not e["title_id"]:
            e["skip"] = "нет IMDb id в TMDB"
    return sorted(parts_out.values(), key=lambda e: (e["release_date"], e["tmdb_id"]))


def ensure(conn, log=print) -> dict:
    """Create the missing titles of the kept earlier parts; write CACHE."""
    from . import link
    from .encountered import _retry
    parts = scan(conn, log=log)
    made, failed = 0, []
    keep = {}
    for e in parts:
        if e["skip"]:
            continue
        if not e["title_id"]:
            def mk():
                with conn:
                    return link.ensure_movie(conn, e["imdb_id"], "prequel")
            try:
                e["title_id"] = _retry(mk)
            except Exception as ex:  # noqa: BLE001 — one TMDB failure must not stop the rest
                log(f"  ensure {e['imdb_id']} {e['title']}: {ex}")
            if e["title_id"]:
                made += 1
            else:
                failed.append(f"{e['title']} ({e['year']}) tmdb {e['tmdb_id']}")
                continue
        keep[str(e["title_id"])] = {"tmdb_id": e["tmdb_id"], "title": e["title"], "year": e["year"],
                                    "collection_id": e["collection_id"], "sequels": e["sequels"]}
    CACHE.write_text(json.dumps({"generated_at": date.today().isoformat(), "parts": keep,
                                 "skipped": [{k: e[k] for k in ("tmdb_id", "title", "year", "skip", "sequels")}
                                             for e in parts if e["skip"]]},
                                ensure_ascii=False, indent=1), encoding="utf-8")
    st = {"sequel_collections": len({e["collection_id"] for e in parts}),
          "sequels": len({s for e in parts for s in e["sequels"]}),
          "earlier_parts": len(parts), "kept": len(keep), "created": made, "failed": failed,
          "skipped": {}}
    for e in parts:
        if e["skip"]:
            k = e["skip"].split(" (")[0]
            st["skipped"][k] = st["skipped"].get(k, 0) + 1
    return st


def title_ids() -> set[int]:
    """Earlier parts kept by the last `ensure` (films.encountered source «prequels»)."""
    if not CACHE.exists():
        return set()
    return {int(k) for k in json.loads(CACHE.read_text(encoding="utf-8")).get("parts", {})}


def report(conn) -> dict:
    """Earlier parts kept by `ensure`: with releases, shown, hidden (by reason)."""
    from . import rank
    rows = rank.compute(conn)
    ids = title_ids()
    by_id = {r["title"]["id"]: r for r in rows if not r["variant_index"]}
    st = {"kept": len(ids), "with_releases": 0, "shown": 0, "hidden": {}, "no_releases": []}
    parts = json.loads(CACHE.read_text(encoding="utf-8"))["parts"] if CACHE.exists() else {}
    for tid in sorted(ids):
        r = by_id.get(tid)
        if not r:
            p = parts.get(str(tid), {})
            st["no_releases"].append(f"{p.get('title')} ({p.get('year')})")
            continue
        st["with_releases"] += 1
        if r["hidden_reasons"]:
            for h in r["hidden_reasons"]:
                k = h.split(" ")[0] if h.startswith(("год", "IMDb")) else h
                st["hidden"][k] = st["hidden"].get(k, 0) + 1
        else:
            st["shown"] += 1
    return st


def main(argv: list[str]) -> None:
    from .sources import tmdb
    tmdb.OFFLINE = "--offline" in argv
    conn = db.connect()
    cmd = argv[1] if len(argv) > 1 else "scan"
    log = lambda m: print(m, file=sys.stderr, flush=True)  # noqa: E731
    if cmd == "scan":
        parts = scan(conn, log=log)
        for e in parts:
            print(f"{e['release_date']} {e['title']} tmdb {e['tmdb_id']} title {e['title_id']} → sequels {e['sequels']}"
                  f"{' — ' + e['skip'] if e['skip'] else ''}")
        print(f"{len(parts)} earlier parts, {sum(1 for e in parts if not e['skip'])} kept", file=sys.stderr)
    elif cmd == "ensure":
        print(json.dumps(ensure(conn, log=log), ensure_ascii=False, indent=1))
    elif cmd == "collect":
        from . import encountered
        # --missing works from the last `ensure` (data/cache/prequels.json); a DB shared with other collectors may be
        # locked for a while — retried like films.encountered does
        st = {} if "--missing" in argv else encountered._retry(ensure, conn, log=log)
        log(json.dumps(st, ensure_ascii=False))
        ids = None
        if "--missing" in argv:   # search again only the kept parts that still have no release (e.g. after a match fix)
            ids = {t for t in title_ids() if not conn.execute("SELECT 1 FROM release_titles WHERE title_id = ?", (t,)).fetchone()}
            log(f"parts without releases: {len(ids)}")
        res = encountered.collect(conn, force="--force" in argv or ids is not None, only={"prequels"}, log=log, ids=ids)
        print(json.dumps({"ensure": st, "collect": {k: v for k, v in res.items() if k != "upgrades"}},
                         ensure_ascii=False, indent=1))
    elif cmd == "report":
        print(json.dumps(report(conn), ensure_ascii=False, indent=1))
    else:
        sys.exit("usage: python -X utf8 -m films.prequels [scan|ensure|collect [--force|--missing]|report] [--offline]")


if __name__ == "__main__":
    main(sys.argv)
