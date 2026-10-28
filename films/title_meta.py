"""Metadata survey answers (owner, 2026-10-25): title / people / store / Plex fields that are not part of stage 4.

Usage (progress goes to data/logs/agent_title_meta.log every few hundred items; every pass is resumable):
    python -X utf8 -m films.title_meta all              # every pass below, in this order
    python -X utf8 -m films.title_meta tmdb [--force]   # keywords, homepage, languages, Wikidata id, miniseries,
                                                        # credits creator / dop / producer / composer (TMDB cache)
    python -X utf8 -m films.title_meta imdb             # distributor, colour, cameras, film format, locations (GraphQL)
    python -X utf8 -m films.title_meta people [--force] # RU / UK spellings of the names the sheet shows
    python -X utf8 -m films.title_meta wiki [--force]   # Wikipedia uk / en / ru links (Wikidata sitelinks)
    python -X utf8 -m films.title_meta store            # purchase dates, Google TV «Хочу посмотреть» dates
    python -X utf8 -m films.title_meta plex             # plex_items.added_at from the scanned Plex metadata

What the owner chose («в таблицу» = a sheet column, else DB only):
  * distributor (IMDb, imdb_specs.pick_distributor) — sheet; keywords (TMDB, English: TMDB has no keyword translations) —
    sheet; DOP, producers — sheet; composer, series creator, film editor — DB (credits.role); miniseries, cameras / film format,
    colour (`titles.color`, used by rank for «ч/б»), filming locations, original / spoken languages — DB
    (no_dub_needed is never set from the language);
  * Russian names of people in the sheet, Ukrainian stored (TMDB /person/{id} + translations, cached in tmdb_raw);
    never Kinopoisk;
  * Wikipedia (uk, else en) and the official site — links in the sheet's «Ссылки» cell;
  * purchase date of owned store items, the date a film entered the Google TV list, Plex «date added» — DB
    (Plex date added is shown only on «Обновить качество»).
Rows are only added or updated, never deleted.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import requests

from . import db
from .sources import tmdb

LOG = db.ROOT / "data" / "logs" / "agent_title_meta.log"
CREDIT_ROLES = (("creators", "creator"), ("dops", "dop"), ("producers", "producer"), ("composers", "composer"),
                ("editors", "editor"))   # editor: owner 2026-10-25 «только хранить»


PASS = {"k": 0, "of": 1, "t0": {}}   # current pass of main() — for the PROGRESS lines of the watchdog table
COUNTER = re.compile(r"^\s*(\w+) (\d+)/(\d+)")


def log(msg: str) -> None:
    now = datetime.now()
    line = f"{now:%Y-%m-%d %H:%M:%S} {msg}"
    print(line, file=sys.stderr, flush=True)
    # a counter line («wikidata 50/2432») also becomes «PROGRESS N% ETA HH:MM — …» (owner 2026-10-27: every row of the
    # watchdog table with a forecast; the log had counters only, the table showed «?»). ETA = end of the current pass.
    m = COUNTER.match(msg)
    if m and int(m.group(3)) > 0:
        name, i, n = m.group(1), int(m.group(2)), int(m.group(3))
        t0 = PASS["t0"].setdefault(name, PASS.get("start") or time.time())   # counted from the start of the pass
        frac = min(1.0, i / n)
        pct = max(1, min(99, int(100 * (PASS["k"] + frac) / PASS["of"])))
        eta = "?"
        if frac > 0:
            end = datetime.fromtimestamp(time.time() + (time.time() - t0) / frac * (1 - frac))
            eta = f"{end:%H:%M}" if end.date() == now.date() else f"{end:%d.%m_%H:%M}"
        line += (f"\n{now:%Y-%m-%d %H:%M:%S} PROGRESS {pct}% ETA {eta} — title_meta проход {PASS['k'] + 1}/{PASS['of']}: "
                 f"{name} {i}/{n} (ETA — конец этого прохода)")
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _js(v):
    return json.dumps(v, ensure_ascii=False) if v else None


def _person_id(conn, p: dict) -> int:
    row = conn.execute("SELECT id FROM people WHERE tmdb_id = ?", (p["tmdb_id"],)).fetchone()
    if row:
        return row[0]
    return conn.execute("INSERT INTO people (tmdb_id, name) VALUES (?, ?)", (p["tmdb_id"], p["name"])).lastrowid


def _add_credits(conn, title_id: int, d: dict) -> int:
    """INSERT OR IGNORE the survey roles (existing credits are never deleted or rewritten here)."""
    n = 0
    for key, role in CREDIT_ROLES:
        for i, p in enumerate(d.get(key) or []):
            if "tmdb_id" not in p:
                p = {"tmdb_id": p["id"], **tmdb._pname(p)}
            cur = conn.execute(
                "INSERT OR IGNORE INTO credits (title_id, person_id, role, character, ord, job, episodes) "
                "VALUES (?, ?, ?, NULL, ?, ?, ?)",
                (title_id, _person_id(conn, p), role, i, ", ".join(p.get("jobs") or []) or None, p.get("episodes")))
            n += cur.rowcount
    return n


# ---------------------------------------------------------------------------------------------- TMDB

def tmdb_pass(conn, force: bool = False, max_age_days: float = 365) -> dict:
    """Survey fields + credits from the TMDB cache (a missing / incomplete cache entry is fetched once)."""
    where = "" if force else "AND meta_checked_at IS NULL"
    rows = conn.execute(
        f"""SELECT id, kind, tmdb_id, tmdb_type, series_tmdb_id, season_number FROM titles
            WHERE (tmdb_id IS NOT NULL OR series_tmdb_id IS NOT NULL) {where} ORDER BY id""").fetchall()
    st = {"titles": len(rows), "done": 0, "credits_added": 0, "no_tmdb_body": 0, "errors": 0}
    log(f"tmdb pass: {len(rows)} titles to fill")
    for i, r in enumerate(rows, 1):
        try:
            if r["kind"] == "series" or r["tmdb_type"] == "tv":
                tv_id = r["series_tmdb_id"] or r["tmdb_id"]
                tb = tmdb.tv_raw(conn, tv_id, max_age_days)
                sb = tmdb.season_raw(conn, tv_id, r["season_number"], max_age_days) if (tb and r["season_number"] is not None) else None
                if not tb:
                    st["no_tmdb_body"] += 1
                    continue
                ex = tmdb.title_extras(tb, "tv", sb)
                cr = dict(tmdb._aggregate_credits((sb or {}).get("aggregate_credits")) if sb else {})
                cr["creators"] = tb.get("created_by") or []
            else:
                b = tmdb.movie_raw(conn, r["tmdb_id"], max_age_days)
                if not b:
                    st["no_tmdb_body"] += 1
                    continue
                ex = tmdb.title_extras(b, "movie")
                cr = tmdb._movie_credits(b.get("credits"))
        except tmdb.TmdbError as e:
            st["errors"] += 1
            log(f"  !! title {r['id']}: {e}")
            continue
        with conn:
            conn.execute(
                """UPDATE titles SET keywords = ?, homepage = ?, original_language = ?, spoken_languages = ?,
                       wikidata_id = ?, is_miniseries = ?, meta_checked_at = datetime('now') WHERE id = ?""",
                (_js(ex["keywords"]), ex["homepage"], ex["original_language"], _js(ex["spoken_languages"]),
                 ex["wikidata_id"], ex["is_miniseries"], r["id"]))
            st["credits_added"] += _add_credits(conn, r["id"], cr)
        st["done"] += 1
        if i % 300 == 0:
            log(f"  tmdb {i}/{len(rows)} (credits added {st['credits_added']}, requests {tmdb.STATS['requests']}, "
                f"cache hits {tmdb.STATS['cache_hits']})")
    log(f"tmdb pass done: {st}")
    return st


def editors_pass(conn, force: bool = False, max_age_days: float = 36500) -> dict:
    """Film editor (owner, 2026-10-25: «только хранить») for every title with a TMDB id, from the TMDB cache of any age
    (movies: credits.crew job «Editor»; seasons: the season's aggregate_credits); only what was never cached is fetched.
    Titles that already have an editor credit are skipped unless force."""
    where = "" if force else "AND id NOT IN (SELECT title_id FROM credits WHERE role = 'editor')"
    rows = conn.execute(
        f"""SELECT id, kind, tmdb_id, tmdb_type, series_tmdb_id, season_number FROM titles
            WHERE (tmdb_id IS NOT NULL OR series_tmdb_id IS NOT NULL) AND duplicate_of IS NULL {where} ORDER BY id""").fetchall()
    st = {"titles": len(rows), "with_editor": 0, "no_editor_on_tmdb": 0, "credits_added": 0, "no_tmdb_body": 0,
          "errors": 0}
    log(f"editors pass: {len(rows)} titles")
    for i, r in enumerate(rows, 1):
        try:
            if r["kind"] == "series" or r["tmdb_type"] == "tv":
                tv_id = r["series_tmdb_id"] or r["tmdb_id"]
                if r["season_number"] is None:
                    st["no_tmdb_body"] += 1
                    continue
                sb = tmdb.season_raw(conn, tv_id, r["season_number"], max_age_days)
                eds = tmdb._aggregate_credits((sb or {}).get("aggregate_credits"))["editors"] if sb else None
            else:
                b = tmdb.movie_raw(conn, r["tmdb_id"], max_age_days)
                eds = tmdb._movie_credits(b.get("credits"))["editors"] if b else None
        except tmdb.TmdbError as e:
            st["errors"] += 1
            log(f"  !! title {r['id']}: {e}")
            continue
        if eds is None:
            st["no_tmdb_body"] += 1
            continue
        if not eds:
            st["no_editor_on_tmdb"] += 1
            continue
        with conn:
            st["credits_added"] += _add_credits(conn, r["id"], {"editors": eds})
        st["with_editor"] += 1
        if i % 1000 == 0:
            log(f"  editors {i}/{len(rows)} (added {st['credits_added']}, requests {tmdb.STATS['requests']})")
    log(f"editors pass done: {st}")
    return st


# ---------------------------------------------------------------------------------------------- people names

SHOWN_LIMITS = {"actor": 5, "director": 3, "writer": 3, "dop": 2, "producer": 3, "creator": 3}


def shown_people(conn) -> list[int]:
    """TMDB ids of the people the sheet can show: titles with a release, per title the first N of each role in the
    sheet's order (sheet._people: COALESCE(ord, 999), then person id)."""
    out = set()
    for role, n in SHOWN_LIMITS.items():
        out |= {r[0] for r in conn.execute(
            """SELECT tmdb_id FROM (SELECT p.tmdb_id, row_number() OVER (PARTITION BY c.title_id
                                    ORDER BY COALESCE(c.ord, 999), p.id) AS rn
                                    FROM credits c JOIN people p ON p.id = c.person_id
                                    WHERE c.role = ? AND c.title_id IN (SELECT title_id FROM release_titles))
               WHERE rn <= ? AND tmdb_id IS NOT NULL""", (role, n))}
    return sorted(out)


_tls = threading.local()


def _thread_conn():
    if getattr(_tls, "conn", None) is None:
        _tls.conn = db.connect()
    return _tls.conn


def _fetch_person(pid: int, max_age_days: float):
    try:
        return pid, tmdb.person(_thread_conn(), pid, max_age_days), None
    except tmdb.TmdbError as e:
        return pid, None, str(e)


def people_pass(conn, force: bool = False, max_age_days: float = 365, workers: int = 8) -> dict:
    ids = shown_people(conn)
    done = set() if force else {r[0] for r in conn.execute(
        "SELECT tmdb_id FROM people WHERE names_checked_at IS NOT NULL AND tmdb_id IS NOT NULL")}
    todo = [p for p in ids if p not in done]
    st = {"shown_people": len(ids), "todo": len(todo), "name_ru": 0, "name_uk": 0, "not_on_tmdb": 0, "errors": 0}
    log(f"people pass: {len(ids)} people can be shown, {len(todo)} to fetch")
    batch = []

    def flush():
        with conn:
            conn.executemany(
                """UPDATE people SET name_ru = COALESCE(?, name_ru), name_ru_source = COALESCE(?, name_ru_source),
                       name_uk = COALESCE(?, name_uk), name_uk_source = COALESCE(?, name_uk_source),
                       imdb_id = COALESCE(imdb_id, ?), names_checked_at = datetime('now') WHERE tmdb_id = ?""", batch)
        batch.clear()

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for i, (pid, p, err) in enumerate(ex.map(lambda x: _fetch_person(x, max_age_days), todo), 1):
            if err:
                st["errors"] += 1
                log(f"  !! person {pid}: {err}")
                continue
            if p is None:
                st["not_on_tmdb"] += 1
                batch.append((None, None, None, None, None, pid))
            else:
                imdb = p.get("imdb_id")
                if imdb and conn.execute("SELECT 1 FROM people WHERE imdb_id = ? AND tmdb_id != ?", (imdb, pid)).fetchone():
                    imdb = None   # people.imdb_id is UNIQUE: another row already holds it
                st["name_ru"] += bool(p.get("name_ru"))
                st["name_uk"] += bool(p.get("name_uk"))
                batch.append((p.get("name_ru"), p.get("name_ru_source"), p.get("name_uk"), p.get("name_uk_source"),
                              imdb, pid))
            if len(batch) >= 200:
                flush()
            if i % 500 == 0:
                log(f"  people {i}/{len(todo)} (ru {st['name_ru']}, uk {st['name_uk']}, requests {tmdb.STATS['requests']})")
    flush()
    log(f"people pass done: {st}")
    return st


# ---------------------------------------------------------------------------------------------- Wikipedia

WIKIDATA_API = "https://www.wikidata.org/w/api.php"
WIKI_LANGS = ("uk", "en", "ru")
_wd = requests.Session()
_wd.headers.update({"User-Agent": "films-db/0.1 (personal non-commercial film list; python-requests)"})


def _wd_fetch(ids: list[str]) -> dict[str, dict | None]:
    """wbgetentities for <= 50 ids -> {requested id: {lang: url}} (None = no such entity)."""
    for attempt in range(12):
        try:
            # maxlag 30, not the bots' 5: the query-service lag sits above 5 s for hours and these are plain reads
            r = _wd.get(WIKIDATA_API, params={"action": "wbgetentities", "ids": "|".join(ids), "props": "sitelinks/urls",
                                              "sitefilter": "|".join(f"{l}wiki" for l in WIKI_LANGS), "format": "json",
                                              "maxlag": 30}, timeout=60)
            if r.status_code == 200:
                j = r.json()
                if "error" in j:
                    if j["error"].get("code") == "maxlag":
                        time.sleep(min(60, 5 * (attempt + 1)))
                        continue
                    raise RuntimeError(j["error"])
                out = {}
                for key, ent in (j.get("entities") or {}).items():
                    src = ((ent.get("redirects") or {}).get("from")) or key
                    if "missing" in ent:
                        out[src] = None
                        continue
                    out[src] = {l: (ent.get("sitelinks") or {}).get(f"{l}wiki", {}).get("url") for l in WIKI_LANGS}
                    out[src] = {l: u for l, u in out[src].items() if u}
                return out
            time.sleep(2 ** attempt)
        except (requests.RequestException, ValueError) as e:
            log(f"    wikidata retry ({e})")
            time.sleep(2 ** attempt)
    raise RuntimeError(f"Wikidata failed for {ids[0]}…")


def wiki_links(conn, qids: list[str], max_age_days: float = 90) -> dict[str, dict]:
    """{Q…: {uk, en, ru}} from the tmdb_raw cache (kind 'wikidata'), fetching the missing ones 50 per request."""
    out, todo = {}, []
    for q in dict.fromkeys(qids):
        c = tmdb._cache_read(conn, "wikidata", q, "")
        if c and tmdb._age_days(c[0]) <= max_age_days:
            out[q] = c[2] or {}
        else:
            todo.append(q)
    for i in range(0, len(todo), 50):
        chunk = todo[i:i + 50]
        res = _wd_fetch(chunk)
        with conn:
            for q in chunk:
                v = res.get(q)
                tmdb._cache_write(conn, "wikidata", q, "", 200 if v is not None else 404, v)
                out[q] = v or {}
        time.sleep(1.0)
        if (i // 50) % 20 == 0:
            log(f"  wikidata {min(i + 50, len(todo))}/{len(todo)}")
    return out


def wiki_pass(conn, force: bool = False) -> dict:
    """titles.wiki_links: a season's own article first, then the series' (per language)."""
    rows = conn.execute("SELECT id, kind, tmdb_type, series_tmdb_id, tmdb_id, wikidata_id, wiki_links FROM titles "
                        "WHERE meta_checked_at IS NOT NULL").fetchall()
    tmdb.OFFLINE = True   # series bodies are cached by the tmdb pass
    series_q = {}
    tv_of = lambda r: (r["series_tmdb_id"] or r["tmdb_id"]) if (r["kind"] == "series" or r["tmdb_type"] == "tv") else None  # noqa: E731
    try:
        for r in rows:
            tv = tv_of(r)
            if tv and tv not in series_q:
                try:
                    b = tmdb.tv_raw(conn, tv, 1e9) or {}
                except tmdb.TmdbError:
                    b = {}
                series_q[tv] = (b.get("external_ids") or {}).get("wikidata_id")
    finally:
        tmdb.OFFLINE = False
    want = [r for r in rows if force or r["wiki_links"] is None]
    qids = [q for r in want for q in (r["wikidata_id"], series_q.get(tv_of(r))) if q]
    log(f"wiki pass: {len(want)} titles, {len(set(qids))} Wikidata ids")
    links = wiki_links(conn, qids)
    st = {"titles": len(want), "with_any": 0, **{l: 0 for l in WIKI_LANGS}}
    with conn:
        for r in want:
            own, ser = links.get(r["wikidata_id"]) or {}, links.get(series_q.get(tv_of(r))) or {}
            d = {l: own.get(l) or ser.get(l) for l in WIKI_LANGS}
            d = {l: u for l, u in d.items() if u}
            for l in d:
                st[l] += 1
            st["with_any"] += bool(d)
            conn.execute("UPDATE titles SET wiki_links = ? WHERE id = ?", (json.dumps(d, ensure_ascii=False), r["id"]))
    log(f"wiki pass done: {st}")
    return st


# ---------------------------------------------------------------------------------------------- store / Plex dates

_RU_MONTHS = {"янв": 1, "фев": 2, "мар": 3, "апр": 4, "мая": 5, "май": 5, "июн": 6, "июл": 7, "авг": 8, "сен": 9,
              "окт": 10, "ноя": 11, "дек": 12}
_DATE_RU = re.compile(r"(\d{1,2})\s+([а-яё]{3})[а-яё]*\.?\s+(\d{4})", re.I)
_ISO = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def ru_date(text: str | None) -> str | None:
    """'23 мар. 2021 г.' (YouTube «Приобретено», Play order history; narrow no-break spaces allowed) -> '2021-03-23'."""
    if not text:
        return None
    t = str(text).replace(" ", " ").replace("\xa0", " ")
    m = _DATE_RU.search(t)
    if not m or m.group(2).lower() not in _RU_MONTHS:
        return None
    return f"{int(m.group(3)):04d}-{_RU_MONTHS[m.group(2).lower()]:02d}-{int(m.group(1)):02d}"


def store_dates(raw: dict, store: str, in_wishlist, checked_at: str | None) -> dict:
    """purchase / Google TV list dates from store_items.raw (what the collectors already saved; no browser)."""
    out = {"purchased_at": None, "purchased_at_source": None, "wishlist_added_at": None, "wishlist_added_source": None}
    if raw.get("acquired"):
        out["purchased_at"] = ru_date(raw["acquired"])
        out["purchased_at_source"] = "youtube_acquired" if out["purchased_at"] else None
    orders = [ru_date(o.get("date")) for o in raw.get("play_orders") or []]
    orders = sorted(d for d in orders if d)
    if not out["purchased_at"] and orders:
        out["purchased_at"], out["purchased_at_source"] = orders[0], "play_order"
    if store == "google_tv" and in_wishlist:
        m = _ISO.search(str(raw.get("added") or ""))
        g = re.search(r"gtv_watchlist_guard (\d{4}-\d{2}-\d{2})", str(raw.get("how") or ""))
        if m:      # Google's own «added» time of the list entry (interests/saved ds:0 x[14][0]), kept to the day
            out["wishlist_added_at"], out["wishlist_added_source"] = m.group(0), "google_list"
        elif g:    # added by our guard script that day
            out["wishlist_added_at"], out["wishlist_added_source"] = g.group(1), "gtv_guard"
        elif checked_at:   # only when the collector first saw it: an upper bound
            out["wishlist_added_at"], out["wishlist_added_source"] = checked_at[:10], "first_seen"
    return out


def store_pass(conn) -> dict:
    st = {"items": 0, "purchased_at": {}, "wishlist_added_at": {}}
    rows = conn.execute("SELECT store, item_id, raw, in_wishlist, checked_at, wishlist_added_at FROM store_items").fetchall()
    with conn:
        for r in rows:
            try:
                raw = json.loads(r["raw"] or "{}") or {}
            except ValueError:
                raw = {}
            d = store_dates(raw, r["store"], r["in_wishlist"], r["checked_at"])
            if r["wishlist_added_at"] and d["wishlist_added_source"] == "first_seen":
                d["wishlist_added_at"] = d["wishlist_added_source"] = None   # keep the earlier first-seen date
            conn.execute("""UPDATE store_items SET purchased_at = COALESCE(?, purchased_at),
                                purchased_at_source = COALESCE(?, purchased_at_source),
                                wishlist_added_at = COALESCE(?, wishlist_added_at),
                                wishlist_added_source = COALESCE(?, wishlist_added_source) WHERE store = ? AND item_id = ?""",
                         (d["purchased_at"], d["purchased_at_source"], d["wishlist_added_at"], d["wishlist_added_source"],
                          r["store"], r["item_id"]))
            st["items"] += 1
            for k in ("purchased_at", "wishlist_added_at"):
                src = d[k + "_source"] if k == "purchased_at" else d["wishlist_added_source"]
                if src:
                    st[k][src] = st[k].get(src, 0) + 1
    log(f"store pass done: {st}")
    return st


def plex_pass(conn) -> dict:
    """plex_items.added_at from the item entry the Plex scan already stored (audio JSON, stream 'item')."""
    n = 0
    with conn:
        for rk, audio in conn.execute("SELECT rating_key, audio FROM plex_items WHERE added_at IS NULL").fetchall():
            try:
                item = next((e for e in json.loads(audio or "[]") if e.get("stream") == "item"), None)
            except ValueError:
                item = None
            if item and item.get("added_at"):
                conn.execute("UPDATE plex_items SET added_at = ? WHERE rating_key = ?", (item["added_at"], rk))
                n += 1
    log(f"plex pass done: added_at set for {n} items")
    return {"added_at_set": n}


def imdb_pass(conn) -> dict:
    from .sources import imdb_specs
    rep = imdb_specs.run(conn, log=log)
    conn.isolation_level = ""   # imdb_specs.run switches it to autocommit
    st = {k: v for k, v in rep["stats"].items() if k.endswith("_set") or k in ("tconsts", "rows")}
    log(f"imdb pass done: {st}; fetch failures {len(rep['fetch_failures'])}")
    return st


# ---------------------------------------------------------------------------------------------- coverage

def coverage(conn) -> dict:
    q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    rel = "id IN (SELECT title_id FROM release_titles)"
    cols = ("distributor", "keywords", "homepage", "wiki_links", "color", "cameras", "film_format", "filming_locations",
            "original_language", "spoken_languages", "is_miniseries")
    out = {"titles": q("SELECT count(*) FROM titles"), "titles_with_release": q(f"SELECT count(*) FROM titles WHERE {rel}")}
    for c in cols:
        cond = {"keywords": "keywords IS NOT NULL AND keywords != '[]'",
                "wiki_links": "wiki_links IS NOT NULL AND wiki_links != '{}'",
                "spoken_languages": "spoken_languages IS NOT NULL AND spoken_languages != '[]'",
                "is_miniseries": "is_miniseries = 1"}.get(c, f"{c} IS NOT NULL")
        out[c] = [q(f"SELECT count(*) FROM titles WHERE {cond}"), q(f"SELECT count(*) FROM titles WHERE {cond} AND {rel}")]
    out["distributor_by_country"] = dict(conn.execute(
        "SELECT distributor_country, count(*) FROM titles WHERE distributor IS NOT NULL GROUP BY 1 ORDER BY 2 DESC LIMIT 8").fetchall())
    for role in ("dop", "producer", "composer", "creator", "editor"):
        out[f"titles_with_{role}"] = [q(f"SELECT count(DISTINCT title_id) FROM credits WHERE role = '{role}'"),
                                      q(f"SELECT count(DISTINCT title_id) FROM credits WHERE role = '{role}' AND title_id IN "
                                        "(SELECT title_id FROM release_titles)")]
    out["wiki_uk"] = q("SELECT count(*) FROM titles WHERE json_extract(wiki_links, '$.uk') IS NOT NULL")
    out["wiki_en_only"] = q("SELECT count(*) FROM titles WHERE json_extract(wiki_links, '$.uk') IS NULL "
                            "AND json_extract(wiki_links, '$.en') IS NOT NULL")
    out["people_checked"] = q("SELECT count(*) FROM people WHERE names_checked_at IS NOT NULL")
    out["people_name_ru"] = q("SELECT count(*) FROM people WHERE name_ru IS NOT NULL")
    out["people_name_uk"] = q("SELECT count(*) FROM people WHERE name_uk IS NOT NULL")
    out["plex_added_at"] = q("SELECT count(*) FROM plex_items WHERE added_at IS NOT NULL")
    out["store_purchased_at"] = q("SELECT count(*) FROM store_items WHERE purchased_at IS NOT NULL")
    out["store_owned"] = q("SELECT count(*) FROM store_items WHERE owned = 1")
    out["gtv_added_at"] = dict(conn.execute("SELECT wishlist_added_source, count(*) FROM store_items "
                                            "WHERE wishlist_added_at IS NOT NULL GROUP BY 1").fetchall())
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(prog="films.title_meta")
    ap.add_argument("what", choices=["all", "tmdb", "editors", "imdb", "people", "wiki", "store", "plex", "coverage"])
    ap.add_argument("--force", action="store_true", help="redo titles / people already filled")
    a = ap.parse_args(argv)
    conn = db.connect()
    db.migrate(conn)
    t0 = time.time()
    passes = ["plex", "store", "tmdb", "imdb", "wiki", "people"] if a.what == "all" else [a.what]
    res = {}
    PASS["of"] = len(passes)
    for k, p in enumerate(passes):
        PASS["k"], PASS["start"] = k, time.time()
        if p == "coverage":
            continue
        fn = {"tmdb": tmdb_pass, "editors": editors_pass, "people": people_pass, "wiki": wiki_pass}.get(p)
        res[p] = fn(conn, force=a.force) if fn else {"store": store_pass, "plex": plex_pass, "imdb": imdb_pass}[p](conn)
    res["coverage"] = coverage(conn)
    log("coverage: " + json.dumps(res["coverage"], ensure_ascii=False))
    log(f"finished {a.what} in {time.time() - t0:.0f} s")
    log("AGENT_TITLE_META_OK")      # the watchdog table: finished (a marker counts only with no PROGRESS after it)
    print(json.dumps(res, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
