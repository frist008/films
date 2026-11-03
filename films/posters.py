"""Poster for every title with a release: IMDb → Rotten Tomatoes → TMDB → the tracker page's own image.

Owner, 2026-10-25: «Да, TMDB — когда на IMDb и RT нет; а когда и там нет — с источников торрента». Before, only IMDb
(films/sources/imdb_specs.py) wrote `titles.poster_url`; RT posters sat in `rt_pages` unused.

Order per title (rows whose poster_url is NULL, plus rows holding a tracker or TMDB image — a better source replaces it):
  1. imdb    — `primaryImage` of the IMDb GraphQL record (imdb_specs cache, re-fetched when stale); a row whose IMDb id is
               suspect (imdb_specs.suspect_link) gets nothing, except the poster of the same work (poster_same_work);
  2. rt      — `rt_pages.poster_url` of the title's RT page (a season page, else its series page);
  3. tmdb    — TMDB `poster_path` (season → series for a season row) as https://image.tmdb.org/t/p/original/…;
  4. tracker — the first image of the release page description outside its spoilers (release_raw, already cached;
               rating badges such as s.rutor.info/imdb/*.gif are skipped, Kinopoisk images are never used; a dead
               image → the next one of the visible part, up to MAX_CANDIDATES — screenshots sit in spoilers). Tracker images live on image hosts
               and expire: the image must answer (HTTP, image/* content type) before it is written; `poster_release_id`
               keeps the release whose page gave it, `poster_alive_at` the last time it answered. Every run re-checks
               these rows: a better source first, then the image itself; a dead one is re-taken from the cached pages of
               the title's releases, then from a fresh rutor page (plain HTTP, rutor only — never a browser), else the
               poster is cleared and searched again next time.
`poster_source` = imdb / rt / tmdb / tracker. Nothing else of the title is touched.

Run: python -X utf8 -m films.posters run [--ids 1 2 …] [--no-check] [--dry]
     python -X utf8 -m films.posters stats
Called at the end of `python -X utf8 -m films.sources.imdb_specs run` (update-db step 4).
"""
from __future__ import annotations

import argparse
import html as _html
import json
import re
import sqlite3
import sys
import time
import zlib

from . import db

TMDB_IMG = "https://image.tmdb.org/t/p/original"
# images of a description that are never a poster: rating badges, tracker static, Kinopoisk (owner: never used at all)
_SKIP_IMG = re.compile(r"kinopoisk|kino-?poisk|s\.rutor\.info|rutor\.info/(?:imdb|s)/|/imdb/pic/|imdb\.com/|"
                       r"rating\.|/rating/|smile|smilies|emoji|/flags?/|counter|banner|button|"
                       r"rutracker\.org/(?:forum/)?(?:images|smiles)|static\.t-ru\.org", re.I)
ACTIVE_LINK = "coalesce(rt.how, '') NOT LIKE 'wrong_link%'"


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


# ------------------------------------------------------------------------------------------------ tracker image

def page_images(desc_html: str) -> list[str]:
    """Candidate poster URLs of a release description in page order (rutor <img src>, rutracker <var class="postImg"
    title=…>); an <img> wrapped in a link to a bigger image gives that image."""
    out = []
    # spoilers (rutor «Скриншоты», MediaInfo …) hold screenshots, never the poster: only the visible part is read
    desc_html = re.sub(r"<textarea\b[^>]*>.*?</textarea>", " ", desc_html or "", flags=re.S | re.I)
    pat = re.compile(r"(<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>\s*)?<img\b[^>]*?src=[\"']([^\"']+)[\"'][^>]*>"
                     r"|<var\b[^>]*class=[\"'][^\"']*postImg[^\"']*[\"'][^>]*title=[\"']([^\"']+)[\"']", re.I | re.S)
    for m in pat.finditer(desc_html or ""):
        href, src, var = m.group(2), m.group(3), m.group(4)
        url = var or src
        if href and re.search(r"\.(?:jpe?g|png|webp)(?:\?|$)", href, re.I) and not _SKIP_IMG.search(href):
            url = href
        url = _html.unescape((url or "").strip())
        if not url.lower().startswith(("http://", "https://")) or _SKIP_IMG.search(url):
            continue
        if re.search(r"\.gif(?:\?|$)", url, re.I):
            continue   # animated badges / dividers, never a poster on rutor
        if url not in out:
            out.append(url)
    return out


def _desc(conn, rid: int) -> str | None:
    row = conn.execute("SELECT body FROM release_raw WHERE release_id = ?", (rid,)).fetchone()
    if not row:
        return None
    page = zlib.decompress(row[0]).decode("utf-8", "replace")
    if 'id="details"' in page:
        from .parse.rutor import _split_details
        return _split_details(page)["desc_html"]
    return page   # rutracker / other: the whole page (postImg markup)


MAX_CANDIDATES = 3   # images of the visible description tried per page (the poster first; a dead host → the next one)


def tracker_posters(conn, rid: int) -> list[str]:
    """Poster candidates of the release page: the images of the visible description, first ones first."""
    d = _desc(conn, rid)
    return page_images(d)[:MAX_CANDIDATES] if d else []


def tracker_poster(conn, rid: int) -> str | None:
    """The first image of the release page description (the poster on rutor / rutracker pages), or None."""
    imgs = tracker_posters(conn, rid)
    return imgs[0] if imgs else None


_DEAD_HOSTS: set[str] = set()   # hosts that did not answer at all in this process (imageban.ru is not reachable from UA)


def image_alive(url: str, timeout: float = 10) -> bool:
    """The image host still serves the picture: HTTP 200 with an image/* content type (HEAD, else a short GET). A host
    that does not connect is remembered for the rest of the run (a blocked host would cost a timeout per image)."""
    import requests
    from urllib.parse import urlsplit
    host = urlsplit(url).hostname or ""
    if host in _DEAD_HOSTS:
        return False
    hdr = {"User-Agent": "Mozilla/5.0 (films-db poster check)"}
    try:
        r = requests.head(url, timeout=timeout, allow_redirects=True, headers=hdr)
        if r.status_code in (403, 405, 501) or not r.headers.get("content-type"):
            r = requests.get(url, timeout=timeout, allow_redirects=True, headers=hdr, stream=True)
            r.close()
        return r.status_code == 200 and r.headers.get("content-type", "").lower().startswith("image/")
    except (requests.ConnectionError, requests.Timeout):
        _DEAD_HOSTS.add(host)
        return False
    except requests.RequestException:
        return False


# ------------------------------------------------------------------------------------------------ the other sources

def imdb_poster(conn, row, fetch: bool = True) -> str | None:
    """IMDb primaryImage for the row (imdb_specs cache; fetched when missing / stale), guarded like imdb_specs.apply."""
    from .sources import imdb_specs
    t = row["imdb_id"] if row["kind"] == "movie" or row["season_number"] is None else row["series_imdb_id"]
    t = t or row["imdb_id"] or row["series_imdb_id"]
    if not t:
        return None
    recs, _ = imdb_specs.ensure([t], fetch=fetch, log=lambda *_: None)
    rec = recs.get(t)
    if not rec:
        return None
    s = imdb_specs.summarize(rec)
    if s["status"] == "not_found" or not s["title"] or not s["poster_url"]:
        return None
    why = imdb_specs.suspect_link(row["kind"], row["year"], s["type"], s["year"])
    if why and not imdb_specs.poster_same_work(row, s, why):
        return None
    return s["poster_url"]


def rt_poster(conn, row) -> str | None:
    """Poster of the title's Rotten Tomatoes page (rt_pages, filled by `films ratings rt`): its own page, else the
    series page of a season."""
    pages = conn.execute("SELECT url, parent_url, poster_url FROM rt_pages WHERE title_id = ? OR url = ? "
                         "ORDER BY (url = ?) DESC", (row["id"], row["rt_url"], row["rt_url"])).fetchall()
    for p in pages:
        if p["poster_url"]:
            return p["poster_url"]
    for p in pages:
        if p["parent_url"]:
            r = conn.execute("SELECT poster_url FROM rt_pages WHERE url = ?", (p["parent_url"],)).fetchone()
            if r and r[0]:
                return r[0]
    return None


def tmdb_poster(conn, row) -> str | None:
    """TMDB poster_path: the movie; a season row — the season's own, else the series'."""
    from .sources import tmdb
    try:
        if row["kind"] == "series" and (row["series_tmdb_id"] or row["tmdb_id"]):
            tv_id = row["series_tmdb_id"] or row["tmdb_id"]
            if row["season_number"] is not None:
                s = tmdb.season_raw(conn, tv_id, row["season_number"]) or {}
                if s.get("poster_path"):
                    return TMDB_IMG + s["poster_path"]
            t = tmdb.tv_raw(conn, tv_id) or {}
            return TMDB_IMG + t["poster_path"] if t.get("poster_path") else None
        if row["tmdb_id"]:
            raw = tmdb.tv_raw if row["tmdb_type"] == "tv" else tmdb.movie_raw
            m = raw(conn, row["tmdb_id"]) or {}
            return TMDB_IMG + m["poster_path"] if m.get("poster_path") else None
    except Exception:  # noqa: BLE001 — a TMDB outage must not stop the chain (the tracker image is next)
        return None
    return None


def _release_ids(conn, tid: int) -> list[int]:
    """The title's releases, best page first (rutor, most seeded, newest)."""
    return [r[0] for r in conn.execute(
        f"""SELECT r.id FROM release_titles rt JOIN releases r ON r.id = rt.release_id
            WHERE rt.title_id = ? AND {ACTIVE_LINK}
            ORDER BY (r.source = 'rutor') DESC, coalesce(r.seeders, 0) DESC, r.added_at DESC""", (tid,))]


def from_tracker(conn, tid: int, alive=image_alive, refetch: bool = False, skip: set | None = None,
                 log=print) -> tuple[str | None, int | None]:
    """(image url, release id) from the cached pages of the title's releases, the image checked alive; refetch: a page
    whose image is dead is downloaded again from rutor (plain HTTP) and its image tried once more."""
    skip = skip or set()
    dead_pages = []
    for rid in _release_ids(conn, tid):
        urls = [u for u in tracker_posters(conn, rid) if u not in skip]
        for url in urls:
            if alive(url):
                return url, rid
        if urls:
            dead_pages.append(rid)
    if refetch:
        from .sources import rutor
        for rid in dead_pages[:3]:
            src, page_url = conn.execute("SELECT source, url FROM releases WHERE id = ?", (rid,)).fetchone()
            if src != "rutor":
                continue   # rutracker pages need the browser — never fetched here
            try:
                body = rutor.get(page_url)
            except Exception as e:  # noqa: BLE001
                log(f"  page {rid} not re-fetched: {e}")
                continue
            if 'id="details"' not in body:
                continue
            with conn:
                conn.execute("UPDATE release_raw SET body = ?, fetched_at = datetime('now') WHERE release_id = ?",
                             (zlib.compress(body.encode("utf-8"), 9), rid))
            for url in tracker_posters(conn, rid):
                if url not in skip and alive(url):
                    return url, rid
    return None, None


# ------------------------------------------------------------------------------------------------ the chain

def targets(conn, ids: list[int] | None = None) -> list[sqlite3.Row]:
    """Rows to (re)check: titles with an active release (not a duplicate row) whose poster is missing, a tracker image or
    a TMDB one (an IMDb / RT poster found later — RT pages come with `films ratings rt` — takes its place)."""
    sql = f"""SELECT t.* FROM titles t
              WHERE t.duplicate_of IS NULL AND (t.poster_url IS NULL OR t.poster_source IN ('tracker', 'tmdb'))
                AND EXISTS (SELECT 1 FROM release_titles rt WHERE rt.title_id = t.id AND {ACTIVE_LINK})"""
    args: list = []
    if ids:
        sql += f" AND t.id IN ({','.join('?' * len(ids))})"
        args = list(ids)
    return conn.execute(sql + " ORDER BY t.id", args).fetchall()


def fill(conn, ids: list[int] | None = None, check: bool = True, dry: bool = False, fetch_imdb: bool = True,
         alive=image_alive, log=print) -> dict:
    """Run the chain over targets(); returns counts by source and the rows still without a poster (with the reason)."""
    rows = targets(conn, ids)
    st = {"targets": len(rows), "imdb": 0, "rt": 0, "tmdb": 0, "tracker": 0, "tracker_kept": 0, "tracker_retaken": 0,
          "tracker_cleared": 0, "none": 0, "none_list": []}
    now = _now()
    for i, r in enumerate(rows, 1):
        if i % 50 == 0:
            log(f"  posters {i}/{len(rows)}")
        was_tracker = r["poster_source"] == "tracker"
        got, src, rid = None, None, None
        for name, fn in (("imdb", lambda: imdb_poster(conn, r, fetch_imdb)), ("rt", lambda: rt_poster(conn, r)),
                         ("tmdb", lambda: tmdb_poster(conn, r))):
            if name == "imdb" and not (r["imdb_id"] or r["series_imdb_id"]):
                continue
            try:
                got = fn()
            except Exception as e:  # noqa: BLE001 — one source failing moves on to the next
                log(f"  title {r['id']}: {name} failed: {e}")
                got = None
            if got:
                src = name
                break
        if not got and was_tracker and r["poster_url"]:
            if not check or alive(r["poster_url"]):
                st["tracker_kept"] += 1
                if check and not dry:
                    with conn:
                        conn.execute("UPDATE titles SET poster_alive_at = ? WHERE id = ?", (now, r["id"]))
                continue
            got, rid = from_tracker(conn, r["id"], alive, refetch=True, skip={r["poster_url"]}, log=log)
            if got:
                src = "tracker"
                st["tracker_retaken"] += 1
        elif not got:
            got, rid = from_tracker(conn, r["id"], alive if check else (lambda u: True), refetch=False, log=log)
            if got:
                src = "tracker"
        if got:
            st[src] += 1
            if got == r["poster_url"] and src == r["poster_source"] and src != "tracker":
                continue   # the same TMDB poster again
            if not dry:
                with conn:
                    conn.execute("UPDATE titles SET poster_url = ?, poster_source = ?, poster_release_id = ?, "
                                 "poster_alive_at = ?, updated_at = datetime('now') WHERE id = ?",
                                 (got, src, rid if src == "tracker" else None, now if src == "tracker" else None, r["id"]))
            continue
        has_img = any(tracker_poster(conn, x) for x in _release_ids(conn, r["id"]))
        cached = any(conn.execute("SELECT 1 FROM release_raw WHERE release_id = ?", (x,)).fetchone()
                     for x in _release_ids(conn, r["id"]))
        why = ("tracker image dead, nothing else found" if was_tracker else
               "no IMDb/RT/TMDB poster; release pages not cached" if not cached else
               "no IMDb/RT/TMDB poster; no image on the release pages" if not has_img else
               "no IMDb/RT/TMDB poster; the release page images do not answer")
        if was_tracker:
            st["tracker_cleared"] += 1
            if not dry:
                with conn:
                    conn.execute("UPDATE titles SET poster_url = NULL, poster_source = NULL, poster_alive_at = NULL "
                                 "WHERE id = ? AND poster_source = 'tracker'", (r["id"],))
        st["none"] += 1
        st["none_list"].append({"title_id": r["id"], "title": r["title_ru"] or r["title_original"],
                                "season": r["season_number"], "year": r["year"], "why": why})
    return st


def stats(conn) -> dict:
    rel = f"EXISTS (SELECT 1 FROM release_titles rt WHERE rt.title_id = t.id AND {ACTIVE_LINK}) AND t.duplicate_of IS NULL"
    return {"with_release_by_source": dict(conn.execute(
        f"SELECT coalesce(poster_source, 'none'), count(*) FROM titles t WHERE {rel} GROUP BY 1").fetchall())}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="films.posters")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run", help="fill missing posters (IMDb → RT → TMDB → tracker) and re-check tracker images")
    p.add_argument("--ids", nargs="+", type=int)
    p.add_argument("--no-check", action="store_true", help="do not check that tracker images answer")
    p.add_argument("--dry", action="store_true")
    sub.add_parser("stats")
    a = ap.parse_args(argv)
    conn = db.connect()
    db.migrate(conn)
    if a.cmd == "stats":
        print(json.dumps(stats(conn), ensure_ascii=False, indent=1))
        return
    res = fill(conn, a.ids, check=not a.no_check, dry=a.dry, log=lambda *x: print(*x, file=sys.stderr))
    print(json.dumps(res, ensure_ascii=False, indent=1))
    print(json.dumps(stats(conn), ensure_ascii=False))


if __name__ == "__main__":
    main()
