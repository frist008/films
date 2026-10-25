"""Rutor releases for ANY year of every title the owner «encounters» (R162, owner 2026-10-25: «Надо добавить конкретные
фильмы раньше 24 года в базу, которые тебе встречаются, + по франшизам»).

The main rutor sweep keeps only releases of 2024+; franchises (films.franchise_releases), [quality_watch] and the
watchlist have their own any-year collectors. This one covers the rest of what the owner has met, so the best copy /
dub and upgrades are known for it too:
  qbt       — torrents in the qBittorrent client (queued or done): qbt_torrents.title_id;
  plex      — files in Plex (present), jellyfin — files in Jellyfin, disk — video files on the local drives;
  lists     — the owner's lists: kostya_list_map.json, watchlist_map.json, [quality_watch] and [top] in settings.toml.
Watched-only titles are not included (no upgrade needed): a list title the owner has watched counts only if it is also
downloaded or queued. Titles whose every year is ≥ year_from + 1 are skipped — the main sweep already has all their
releases. Franchise members still waiting for films.franchise_releases (its `todo`) are left to that collector.

Per title (a film, or a series = all its encountered seasons): rutor search by the original title, the Latin title,
then the Russian one — SEPARATE queries, no year in the query (a query with > 300 hits is narrowed to «name year» for
year−1 … year+1 for films); categories: films 1 (+7 for animation), series 4 (+7 for animation), anime 10 only for
franchises with anime = true. A row is kept only if a name segment before «(year)» equals one of the title's names
(or contains a long one) and the year span overlaps year ±1. Titles searched in the last N days are skipped (cursor:
data/cache/encountered_cursor.json). Then the new rows' pages are fetched and parsed (DV5-only rows are kept and listed
in excluded_releases — nothing is deleted), and `match` runs unless another match process is running.

Run: python -X utf8 -m films.encountered collect [--days N] [--force] [--limit N] [--only SRC[,SRC]] [--dry] [--no-match]
     python -X utf8 -m films.encountered list      — who is covered, by source (no requests)
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
import zlib
from datetime import date, timedelta
from urllib.parse import quote

from . import db, rank

CURSOR = db.ROOT / "data" / "cache" / "encountered_cursor.json"
DAYS = 14
NARROW_AT = 300     # a name query with more hits is re-asked as «name year»
SOURCES = ("qbt", "plex", "jellyfin", "disk", "lists")


def _norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w]+", " ", (s or "").lower().replace("ё", "е"))).strip()


def _retry(fn, *a, **kw):
    """SQLite writes shared with other collectors: retry «database is locked» beyond busy_timeout."""
    for attempt in range(8):
        try:
            return fn(*a, **kw)
        except sqlite3.OperationalError as e:
            if "locked" not in str(e) or attempt == 7:
                raise
            time.sleep(5 * (attempt + 1))


# ------------------------------------------------------------------------------------------------ who is encountered

def _sources(conn) -> dict[int, set[str]]:
    """title_id → sources it was encountered in."""
    from .disks import linked_title_ids
    from . import watchlist
    out: dict[int, set[str]] = {}

    def add(tids, src):
        for t in tids:
            if t:
                out.setdefault(int(t), set()).add(src)

    add((r[0] for r in conn.execute("SELECT DISTINCT title_id FROM qbt_torrents WHERE title_id IS NOT NULL")), "qbt")
    add((r[0] for r in conn.execute("SELECT DISTINCT title_id FROM plex_items WHERE title_id IS NOT NULL "
                                    "AND coalesce(present, 1) = 1")), "plex")
    add((r[0] for r in conn.execute("SELECT DISTINCT title_id FROM jellyfin_items WHERE title_id IS NOT NULL")), "jellyfin")
    add(linked_title_ids(conn), "disk")
    lists: set[int] = set(watchlist.title_ids(conn))
    kmap = db.ROOT / "data" / "cache" / "kostya_list_map.json"
    if kmap.exists():
        lists |= {t for m in json.loads(kmap.read_text(encoding="utf-8")).values() for t in m.get("title_ids") or []}
    cfg = rank.settings()
    imdbs = list(cfg.get("quality_watch", {}).get("imdb") or []) + list(cfg.get("top", {}).get("imdb") or [])
    for tt in imdbs:
        lists |= {r[0] for r in conn.execute("SELECT id FROM titles WHERE imdb_id = ? OR series_imdb_id = ?", (tt, tt))}
    add(lists, "lists")
    return out


def _anime_imdb(conn) -> set[str]:
    import tomllib
    with open(db.ROOT / "config" / "franchises.toml", "rb") as f:
        keys = {fr["key"] for fr in tomllib.load(f)["franchise"] if fr.get("anime")}
    return {r[1] for r in conn.execute("SELECT franchise_key, imdb_id FROM franchise_members WHERE imdb_id IS NOT NULL "
                                       "AND confidence IN ('confirmed', 'single_source')") if r[0] in keys}


def units(conn, only: set[str] | None = None) -> tuple[list[dict], dict]:
    """Search units: a film, or a series with its encountered seasons."""
    from . import franchise_releases
    src = _sources(conn)
    watched = {r[0] for r in conn.execute("SELECT title_id FROM watched WHERE source NOT LIKE '%partial'")}
    franchise_todo = {w["imdb"] for w in franchise_releases.todo(conn)}
    anime = _anime_imdb(conn)
    year_from = db.years()[0]
    skipped = {"watched_list_only": 0, "covered_by_sweep": 0, "franchise_collector": 0, "no_name": 0}
    groups: dict[str, dict] = {}
    for tid, s in src.items():
        if only and not (s & only):
            continue
        if s == {"lists"} and tid in watched:
            skipped["watched_list_only"] += 1
            continue
        t = conn.execute("SELECT * FROM titles WHERE id = ?", (tid,)).fetchone()
        if not t:
            continue
        t = dict(t)
        key = f"s:{t['series_key'] or t['series_imdb_id'] or t['series_tmdb_id'] or tid}" if t["kind"] == "series" \
            else f"m:{t['imdb_id'] or tid}"
        g = groups.setdefault(key, {"key": key, "kind": t["kind"], "title_ids": [], "years": set(), "sources": set(),
                                    "names": [], "imdb": set(), "animation": False, "anime": False})
        g["title_ids"].append(tid)
        g["sources"] |= s
        if t["year"]:
            g["years"].add(t["year"])
        for n in (t["title_original"], t["title_latin"], t["title_ru"]):
            if n and n not in g["names"]:
                g["names"].append(n)
        g["imdb"] |= {x for x in (t["imdb_id"], t["series_imdb_id"]) if x}
        g["animation"] |= bool(t["is_animation"]) or t["tracker_category"] == "animation"
        g["anime"] |= bool(g["imdb"] & anime)
    out = []
    for g in groups.values():
        if g["years"] and min(g["years"]) > year_from:
            skipped["covered_by_sweep"] += 1
            continue
        if g["imdb"] & franchise_todo:
            skipped["franchise_collector"] += 1
            continue
        names = [n for n in g["names"] if len(_norm(n)) >= 2]
        if not names:
            skipped["no_name"] += 1
            continue
        g["names"] = names
        cats = [(4, "foreign_series")] if g["kind"] == "series" else [(1, "foreign_films")]
        if g["animation"]:
            cats.append((7, "animation"))
        if g["anime"]:
            cats.append((10, "anime"))
        g["cats"] = cats
        out.append(g)
    out.sort(key=lambda g: (min(g["years"]) if g["years"] else 0, g["key"]))
    return out, skipped


# ---------------------------------------------------------------------------------------------------- rutor search

YEAR_RE = re.compile(r"\((\d{4})(?:\s*[-–]\s*(\d{4}))?\)")


def _match(name: str, keys: set[str], years: set[int]) -> bool:
    m = YEAR_RE.search(name)
    if years:
        if not m:
            return False
        y1, y2 = int(m.group(1)), int(m.group(2) or m.group(1))
        if not (y1 <= max(years) + 1 and y2 >= min(years) - 1):
            return False
    head = name[:m.start()] if m else name
    for part in head.split("/"):
        p = _norm(re.sub(r"\[[^\]]*\]", " ", part))
        if not p:
            continue
        for k in keys:
            if p == k or (len(k) >= 8 and f" {k} " in f" {p} "):
                return True
    return False


def _search(cat: int, query: str) -> tuple[int, list[dict]]:
    from .sources import rutor
    out, page, total = [], 0, 0
    while True:
        text = rutor.get(f"{rutor.BASE}/search/{page}/{cat}/300/0/{quote(query)}")
        m = re.search(r"Результатов поиска (\d+)", text)
        total = int(m.group(1)) if m else 0
        rows = rutor.parse_listing(text)
        out += rows
        if not rows or len(out) >= total or (page == 0 and total > NARROW_AT and query.count(" ") < 12
                                              and not re.search(r"\b(19|20)\d\d$", query)):
            return total, out
        page += 1


def _query(name: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[!?:\"«»&#%+]+", " ", name)).strip()


def search_unit(g: dict, log=print) -> tuple[dict, int]:
    keys = {_norm(n) for n in g["names"]}
    years = set(g["years"])
    hits, queries = {}, 0
    for name in dict.fromkeys(_query(n) for n in g["names"]):
        if len(name) < 2:
            continue
        for cat, category in g["cats"]:
            try:
                total, rows = _search(cat, name)
                queries += 1
                if total > NARROW_AT and len(rows) < total:
                    if g["kind"] == "movie" and years:
                        rows = []
                        for y in sorted({y + d for y in years for d in (-1, 0, 1)}):
                            _, r = _search(cat, f"{name} {y}")
                            queries += 1
                            rows += r
                    else:   # a series name with hundreds of hits: all pages (rutor caps at 2000)
                        from .sources import rutor
                        _, rows = rutor.search_all(cat, name)
                        queries += 1
            except Exception as e:  # noqa: BLE001 — one failed query must not stop the sweep
                log(f"  rutor error [{name}] cat {cat}: {e}")
                continue
            for r in rows:
                if _match(r["name"], keys, years):
                    hits[r["source_id"]] = (r, category)
    return hits, queries


# ------------------------------------------------------------------------------------------------ pages, parse, match

def fetch_pages(conn, ids: list[int], log=print) -> int:
    from .sources import rutor
    if not ids:
        return 0
    todo = []
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        todo += conn.execute(
            f"""SELECT r.id, r.url FROM releases r LEFT JOIN release_raw w ON w.release_id = r.id
                WHERE r.id IN ({','.join('?' * len(chunk))}) AND w.release_id IS NULL AND r.page_skip = 0""", chunk).fetchall()
    done = 0
    for i, (rid, url) in enumerate(todo, 1):
        try:
            body = rutor.get(url)
        except RuntimeError as e:
            log(f"  FAILED {url}: {e}")
            continue
        if 'id="details"' not in body:
            log(f"  NO DETAILS {url}")
            continue

        def write():
            with conn:
                conn.execute("INSERT OR REPLACE INTO release_raw (release_id, body) VALUES (?, ?)",
                             (rid, zlib.compress(body.encode("utf-8"), 9)))
        _retry(write)
        done += 1
        if i % 100 == 0:
            log(f"  pages {i}/{len(todo)}")
    return done


def other_match_running() -> list[str]:
    from .watchdog import processes
    return [p for p in processes() if re.search(r"films(?:\.cli)?\s+match\b|films\.match\b|match_releases|link_exact_names", p)]


def run_match(conn, log=print) -> dict:
    from . import locks, match
    busy = other_match_running()
    if busy:
        log(f"  match skipped — another match process is running: {busy[0][-120:]}")
        return {"skipped": "another match process is running"}
    with locks.lock("match", timeout=60):
        res = _retry(match.match_releases, conn)
        res["exact_name_year"] = _retry(match.link_exact_names, conn)
    return res


# ------------------------------------------------------------------------------------------------------ upgrades

def upgrades(conn, groups: list[dict], new_ids: set[int]) -> list[str]:
    """Downloaded titles whose best linked release (now) is new from this run and better than the owner's Plex copy."""
    cfg = rank.settings()
    excluded = {r[0] for r in conn.execute(
        "SELECT source_id FROM excluded_releases WHERE source = 'rutor' AND reason NOT LIKE 'lifted:%'")}
    out = []
    for g in groups:
        for tid in g["title_ids"]:
            p = conn.execute("SELECT * FROM plex_items WHERE title_id = ? AND coalesce(present, 1) = 1 "
                             "ORDER BY resolution DESC LIMIT 1", (tid,)).fetchone()
            if not p:
                continue
            rels = [dict(r) for r in conn.execute(
                """SELECT r.* FROM releases r JOIN release_titles x ON x.release_id = r.id
                   WHERE x.title_id = ? AND r.parsed IS NOT NULL""", (tid,)) if r["source_id"] not in excluded]
            if not rels:
                continue
            best = min(rels, key=lambda r: rank.release_key(r, cfg))
            if best["id"] not in new_ids:
                continue
            why = rank.upgrade_reasons(rank.plex_quality(dict(p)), best, cfg)
            if why:
                t = conn.execute("SELECT title_ru, title_original, year, season_number FROM titles WHERE id = ?", (tid,)).fetchone()
                s = f" S{t['season_number']}" if t["season_number"] else ""
                out.append(f"{t['title_ru'] or t['title_original']}{s} ({t['year']}): {', '.join(why)} — {best['name'][:90]}")
    return out


# --------------------------------------------------------------------------------------------------------- collect

def collect(conn, days: int = DAYS, force: bool = False, limit: int | None = None, only: set[str] | None = None,
            dry: bool = False, do_match: bool = True, log=print) -> dict:
    groups, skipped = units(conn, only)
    cursor = json.loads(CURSOR.read_text(encoding="utf-8")) if CURSOR.exists() else {}
    since = (date.today() - timedelta(days=days)).isoformat()
    todo = [g for g in groups if force or cursor.get(g["key"], "") < since]
    if limit:
        todo = todo[:limit]
    by_src = {s: sum(1 for g in groups if s in g["sources"]) for s in SOURCES}
    st = {"units": len(groups), "by_source": by_src, "skipped": skipped, "recent_skipped": len(groups) - len([
        g for g in groups if force or cursor.get(g["key"], "") < since]), "searched": 0, "queries": 0, "kept": 0,
        "new": 0, "with_hits": 0, "no_hits": 0}
    log(f"encountered: {len(groups)} units, to search {len(todo)}; by source {by_src}; skipped {skipped}")
    hit_ids: set[str] = set()
    t0 = time.time()
    for i, g in enumerate(todo, 1):
        hits, q = search_unit(g, log)
        st["searched"] += 1
        st["queries"] += q
        st["kept"] += len(hits)
        if hits:
            st["with_hits"] += 1
            hit_ids |= set(hits)
            if not dry:
                from .sources import rutor
                for category in {c for _, c in hits.values()}:
                    st["new"] += _retry(rutor.upsert, conn, [r for r, c in hits.values() if c == category], category)
        else:
            st["no_hits"] += 1
        if not dry:
            cursor[g["key"]] = date.today().isoformat()
            if i % 10 == 0 or i == len(todo):
                CURSOR.write_text(json.dumps(cursor, ensure_ascii=False, indent=0), encoding="utf-8")
        if i % 25 == 0 or i == len(todo):
            log(f"  {i}/{len(todo)} {time.time() - t0:.0f}s queries {st['queries']} kept {st['kept']} new {st['new']} "
                f"— last: {g['names'][0]} {sorted(g['years'])[:3]} {len(hits)}")
    if dry:
        return st
    rel_ids = []
    sids = sorted(hit_ids)
    for i in range(0, len(sids), 500):
        chunk = sids[i:i + 500]
        rel_ids += [r[0] for r in conn.execute(
            f"SELECT id FROM releases WHERE source = 'rutor' AND source_id IN ({','.join('?' * len(chunk))})", chunk)]
    new_ids = {r for r in rel_ids if not conn.execute("SELECT 1 FROM release_titles WHERE release_id = ?", (r,)).fetchone()}
    log(f"pages: {len(rel_ids)} releases found, {len(new_ids)} not linked yet")
    st["pages_fetched"] = fetch_pages(conn, rel_ids, log)
    from .sources import rutor
    st["parse"] = _retry(rutor.parse_pages, conn, delete_dv5=False, ids=rel_ids)
    log(f"parse: {st['parse']}")
    if do_match:
        st["match"] = run_match(conn, log)
        log(f"match: {json.dumps(st['match'], ensure_ascii=False)[:400]}")
    linked = [r for r in new_ids if conn.execute("SELECT 1 FROM release_titles WHERE release_id = ?", (r,)).fetchone()]
    own = {tid for g in todo for tid in g["title_ids"]}
    st["linked_new"] = len(linked)
    st["linked_to_encountered"] = sum(1 for r in linked if conn.execute(
        f"SELECT 1 FROM release_titles WHERE release_id = ? AND title_id IN ({','.join('?' * len(own))})",
        (r, *own)).fetchone()) if own else 0
    st["upgrades"] = upgrades(conn, todo, set(linked))
    _retry(db.mark_sync, conn, "encountered", None, {k: v for k, v in st.items() if k != "upgrades"} | {"upgrades": len(st["upgrades"])})
    return st


def main(argv: list[str]) -> None:
    conn = db.connect()
    db.migrate(conn)
    cmd = argv[1] if len(argv) > 1 else "collect"
    arg = lambda k, d=None: argv[argv.index(k) + 1] if k in argv else d  # noqa: E731
    only = set(arg("--only").split(",")) if arg("--only") else None
    log = lambda m: print(m, file=sys.stderr, flush=True)  # noqa: E731
    if cmd == "list":
        groups, skipped = units(conn, only)
        print(json.dumps({"units": len(groups), "skipped": skipped,
                          "by_source": {s: sum(1 for g in groups if s in g["sources"]) for s in SOURCES},
                          "sample": [f"{g['names'][0]} {sorted(g['years'])} {sorted(g['sources'])} {[c for c, _ in g['cats']]}"
                                     for g in groups[:15]]}, ensure_ascii=False, indent=1))
        return
    st = collect(conn, days=int(arg("--days", DAYS)), force="--force" in argv,
                 limit=int(arg("--limit")) if arg("--limit") else None, only=only, dry="--dry" in argv,
                 do_match="--no-match" not in argv, log=log)
    print(json.dumps(st, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main(sys.argv)
