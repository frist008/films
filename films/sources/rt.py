"""Rotten Tomatoes: search, page scrape (Tomatometer / Popcornmeter / poster / seasons), title matching.

Usage:
    python -X utf8 -m films.sources.rt search "Dune Part Two"
    python -X utf8 -m films.sources.rt fetch https://www.rottentomatoes.com/m/dune_part_two [--season-pages]
    python -X utf8 -m films.sources.rt find 2024 movie "Dune: Part Two" "Дюна: Часть вторая"

Access: plain HTTPS with a browser User-Agent.  Politeness: >= MIN_INTERVAL s between network requests
(process-wide), retries with exponential backoff on 429/5xx/network errors, raw responses cached under
data/cache/rt/ (HTML pages, search pages, Algolia JSON).  404 is cached too (as "no page").

Data sources inside a title page (no fragile CSS):
  * <script id="media-scorecard-json">  criticsScore / audienceScore (+ overlay: all vs verified audience,
    top critics) — scores, review counts, averages, certified flags;
  * <script type="application/ld+json">  name, image (poster), dateCreated, genre, containsSeason;
  * <script data-json="vanity">           type (movie / tvSeries / tvSeason), emsId, lifecycle;
  * <script id="curation-json">           rtId;
  * media-info list ([data-qa=item]) kept raw as `info` (Distributor, Original Language, Box Office,
    Release Date (Theaters/Streaming), Runtime, Sound Mix, Aspect Ratio, Network, ...);
  * TV: <tile-season> tiles (season, url, critics %, air date); optionally each season page
    (`season_pages=True`) for both scores of every season.

Search: the site search page (`/search?search=`) lists at most 10 movies + 10 TV shows; the same page
embeds the public Algolia search key used by the site's own search box, so `search_algolia()` is used as
a second channel (more hits, `aka` alternative titles, director) when the page search gives no
confident match.  `find()` never auto-accepts a weak or ambiguous match: it returns status
'ambiguous' / 'not_found' with the candidates for manual review.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import re
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

import requests
from bs4 import BeautifulSoup

from films import db
from films.sources.imdb import norm

BASE = "https://www.rottentomatoes.com"
CACHE_DIR = db.ROOT / "data" / "cache" / "rt"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/141.0.0.0 Safari/537.36")
MIN_INTERVAL = 1.2          # seconds between network requests
MAX_ATTEMPTS = 5
PAGE_MAX_AGE_DAYS = 7       # cached title page considered fresh (scores move slowly after release)
SEARCH_MAX_AGE_DAYS = 7

_session = requests.Session()
_session.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9",
                         "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})
_lock = threading.Lock()
_last_request = 0.0
stats = {"network": 0, "cache": 0, "retries": 0, "errors": 0}


class RTError(Exception):
    pass


class RTBlocked(RTError):
    """403 / 429 while STOP_ON_BLOCK is set: the caller stops the run and reports (never bypass)."""


STOP_ON_BLOCK = False       # True -> raise RTBlocked on the first 403/429 instead of backing off and retrying


# ----------------------------------------------------------------------------------------------
# HTTP + cache
# ----------------------------------------------------------------------------------------------

def _cache_path(key: str, ext: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", key).strip("_")[:120]
    h = hashlib.sha1(key.encode()).hexdigest()[:10]
    return CACHE_DIR / f"{safe}.{h}.{ext}"


def _fresh(p: Path, max_age_days: float | None) -> bool:
    if not p.exists():
        return False
    if max_age_days is None:
        return True
    return (time.time() - p.stat().st_mtime) < max_age_days * 86400


def _throttle():
    global _last_request
    with _lock:
        wait = _last_request + MIN_INTERVAL - time.time()
        if wait > 0:
            time.sleep(wait)
        _last_request = time.time()


def _request(method: str, url: str, **kw) -> requests.Response | None:
    """Network call with throttle + retries. Returns None for 404/410."""
    delay = 2.0
    for attempt in range(1, MAX_ATTEMPTS + 1):
        _throttle()
        stats["network"] += 1
        try:
            r = _session.request(method, url, timeout=30, **kw)
        except requests.RequestException as e:
            err = f"{type(e).__name__}: {e}"
        else:
            if r.status_code in (404, 410):
                return None
            if r.status_code == 200:
                return r
            err = f"HTTP {r.status_code}"
            if STOP_ON_BLOCK and r.status_code in (403, 429):
                stats["errors"] += 1
                raise RTBlocked(f"{url}: {err}")
            if r.status_code not in (403, 429) and r.status_code < 500:
                stats["errors"] += 1
                raise RTError(f"{url}: {err}")
            ra = r.headers.get("Retry-After")
            if ra and ra.isdigit():
                delay = max(delay, float(ra))
        if attempt == MAX_ATTEMPTS:
            stats["errors"] += 1
            raise RTError(f"{url}: {err} after {attempt} attempts")
        stats["retries"] += 1
        time.sleep(delay)
        delay *= 2
    return None


def get_html(url: str, max_age_days: float | None = PAGE_MAX_AGE_DAYS, refresh: bool = False) -> str | None:
    """HTML of an RT URL (cached). None if the page does not exist (404)."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    key = urlparse(url)._replace(scheme="", netloc="").geturl()
    p = _cache_path(key, "html")
    p404 = _cache_path(key, "404")
    if not refresh:
        if _fresh(p, max_age_days):
            stats["cache"] += 1
            return p.read_text(encoding="utf-8")
        if _fresh(p404, max_age_days):
            stats["cache"] += 1
            return None
    r = _request("GET", url)
    if r is None:
        p404.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
        return None
    r.encoding = "utf-8"
    p.write_text(r.text, encoding="utf-8")
    p404.unlink(missing_ok=True)
    return r.text


# ----------------------------------------------------------------------------------------------
# search
# ----------------------------------------------------------------------------------------------

def _attr(el, name):
    """search-page-media-row uses hyphenated attrs for movies and unhyphenated for TV."""
    v = el.get(name)
    if v is None:
        v = el.get(name.replace("-", ""))
    return v if v not in ("", None) else None


def _intv(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def search(query: str, refresh: bool = False) -> list[dict]:
    """Site search page. Up to 10 movies + 10 TV shows (the page's own limit)."""
    url = f"{BASE}/search?search={quote(query)}"
    html = get_html(url, SEARCH_MAX_AGE_DAYS, refresh)
    if html is None:
        return []
    soup = BeautifulSoup(html, "lxml")
    out = []
    for section in soup.find_all("search-page-result"):
        stype = section.get("type")          # movie / tvSeries
        kind = "movie" if stype == "movie" else "series" if stype == "tvSeries" else stype
        for row in section.find_all("search-page-media-row"):
            a = row.find("a", attrs={"slot": "title"}) or row.find("a")
            if not a or not a.get("href"):
                continue
            href = a["href"]
            img = row.find("img")
            cast = _attr(row, "cast")
            out.append({
                "kind": kind,
                "url": href if href.startswith("http") else BASE + href,
                "title": a.get_text(" ", strip=True),
                "year": _intv(_attr(row, "release-year")) or _intv(_attr(row, "start-year")),
                "release_year": _intv(_attr(row, "release-year")),
                "start_year": _intv(_attr(row, "start-year")),
                "end_year": _intv(_attr(row, "end-year")),
                "critics": _intv(_attr(row, "tomatometer-score")),
                "certified": _attr(row, "tomatometer-is-certified") == "true",
                "cast": [c.strip() for c in cast.split(",")] if cast else [],
                "thumb": img.get("src") if img else None,
                "aka": [],
                "via": "page",
            })
    return out


_algolia_keys = None


def _algolia_credentials() -> tuple[str, str]:
    global _algolia_keys
    if _algolia_keys is None:
        html = get_html(f"{BASE}/search?search=a", 30)
        m = re.search(r'"algoliaSearch"\s*:\s*\{\s*"aId"\s*:\s*"([^"]+)"\s*,\s*"sId"\s*:\s*"([^"]+)"', html or "")
        if not m:
            raise RTError("Algolia credentials not found on the search page (site changed?)")
        _algolia_keys = (m.group(1), m.group(2))
    return _algolia_keys


def search_algolia(query: str, hits: int = 20, refresh: bool = False) -> list[dict]:
    """Same backend as the site's search box: more hits, alternative (`aka`) titles, director."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    p = _cache_path(f"algolia_{hits}_{query}", "json")
    if not refresh and _fresh(p, SEARCH_MAX_AGE_DAYS):
        stats["cache"] += 1
        data = json.loads(p.read_text(encoding="utf-8"))
    else:
        global _algolia_keys
        body = {"requests": [{"indexName": "content_rt", "query": query,
                              "params": f"hitsPerPage={hits}&filters=isEmsSearchable%20%3D%201"}]}
        for attempt in (1, 2):
            app, key = _algolia_credentials()
            try:
                r = _request("POST", f"https://{app.lower()}-dsn.algolia.net/1/indexes/*/queries",
                             headers={"x-algolia-application-id": app, "x-algolia-api-key": key,
                                      "Referer": BASE + "/", "Origin": BASE,
                                      "Content-Type": "application/json"},
                             data=json.dumps(body))
                break
            except RTError:
                if attempt == 2:
                    raise
                # the public key may have rotated: re-read it from a fresh search page
                _algolia_keys = None
                get_html(f"{BASE}/search?search=a", refresh=True)
        if r is None:
            return []
        data = r.json()
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    out = []
    for h in data["results"][0].get("hits", []):
        t = h.get("type")
        if t not in ("movie", "tv") or not h.get("vanity"):
            continue
        rt = h.get("rottenTomatoes") or {}
        cc = (h.get("castCrew") or {}).get("crew") or {}
        directors = cc.get("Director") if isinstance(cc, dict) else None
        if not directors and isinstance(h.get("crew"), list):
            directors = [c.get("name") for c in h["crew"] if isinstance(c, dict) and c.get("role") == "Director"]
        cast = [c.get("name") for c in h.get("cast") or [] if isinstance(c, dict)] or \
               (h.get("castCrew") or {}).get("cast") or []
        out.append({
            "kind": "movie" if t == "movie" else "series",
            "url": f"{BASE}/{'m' if t == 'movie' else 'tv'}/{h['vanity']}",
            "title": h.get("title"),
            "year": h.get("releaseYear"),
            "release_year": h.get("releaseYear") if t == "movie" else None,
            "start_year": h.get("releaseYear") if t == "tv" else None,
            "end_year": None,
            "critics": rt.get("criticsScore"),
            "audience": rt.get("audienceScore"),
            "certified": bool(rt.get("certifiedFresh")),
            "cast": cast[:10],
            "director": directors or None,
            "thumb": h.get("posterImageUrl"),
            # `titles` holds truncated search prefixes ("The Last o"), so only `aka` is used
            "aka": list(dict.fromkeys(h.get("aka") or [])),
            "runtime": h.get("runTime"),
            "via": "algolia",
        })
    return out


# ----------------------------------------------------------------------------------------------
# title page
# ----------------------------------------------------------------------------------------------

def _json_blocks(html: str) -> dict:
    out = {}
    for m in re.finditer(r"<script([^>]*)>(.*?)</script>", html, re.S):
        attrs, body = m.group(1), m.group(2).strip()
        if "json" not in attrs or not body:
            continue
        name = None
        for pat in (r'id="([^"]+)"', r'data-json="([^"]+)"', r'type="(application/ld\+json)"'):
            mm = re.search(pat, attrs)
            if mm:
                name = mm.group(1)
                break
        try:
            val = json.loads(body)
        except ValueError:
            continue
        out.setdefault(name or "?", val)
    return out


def _score(d: dict | None) -> dict:
    if not d:
        return {}
    liked, notliked = d.get("likedCount"), d.get("notLikedCount")
    return {
        "score": _intv(d.get("score")),
        "certified": d.get("certified"),
        "sentiment": d.get("sentiment"),
        "average": d.get("averageRating"),
        "reviews": d.get("reviewCount"),
        "ratings": (liked + notliked) if isinstance(liked, int) and isinstance(notliked, int) else None,
        "liked": liked,
        "not_liked": notliked,
        "banded": d.get("bandedRatingCount"),
        "score_type": d.get("scoreType"),
        "title": d.get("title"),
    }


def _poster(ld: dict, blocks: dict) -> str | None:
    img = ld.get("image")
    if isinstance(img, dict):
        img = img.get("url")
    if isinstance(img, list):
        img = img[0] if img else None
    if img and "poster-default" not in img and "rt-poster-default" not in img:
        return img
    hero = (blocks.get("media-hero-json") or {}).get("content") or {}
    src = hero.get("posterSrc")
    if src and "poster-default" not in src:
        m = re.search(r"/v2/(https://.+)$", src)    # strip the resizing wrapper -> original
        return m.group(1) if m else src
    return None


_YEAR = re.compile(r"(19|20)\d\d")


def _parse_page(html: str, url: str) -> dict:
    blocks = _json_blocks(html)
    ld = blocks.get("application/ld+json") or {}
    vanity = blocks.get("vanity") or {}
    sc = blocks.get("media-scorecard-json") or {}
    ov = sc.get("overlay") or {}
    hero = (blocks.get("media-hero-json") or {}).get("content") or {}
    soup = BeautifulSoup(html, "lxml")
    info = {}
    for it in soup.select('[data-qa="item"]'):
        lab = it.select_one('[data-qa="item-label"]')
        val = it.select_one('[data-qa="item-value-group"]') or it.select_one('[data-qa="item-value"]')
        if lab and val:
            info[lab.get_text(" ", strip=True)] = re.sub(r"\s+,", ",", val.get_text(" ", strip=True))
    vtype = vanity.get("type")
    kind = {"movie": "movie", "tvSeries": "series", "tvSeason": "season"}.get(vtype, vtype)
    year = None
    for src in (info.get("Release Date (Theaters)"), info.get("Release Date"), ld.get("dateCreated"),
                info.get("Release Date (Streaming)"), " ".join(hero.get("metadataProps") or [])):
        m = _YEAR.search(src or "")
        if m:
            year = int(m.group(0))
            break
    crit, aud = _score(sc.get("criticsScore")), _score(sc.get("audienceScore"))
    res = {
        "url": url,
        "kind": kind,
        "title": vanity.get("title") or ld.get("name") or hero.get("title"),
        "year": year,
        "critics": crit.get("score"),
        "critics_reviews": crit.get("reviews"),
        "critics_average": crit.get("average"),
        "critics_certified": crit.get("certified"),
        "critics_top": _score(ov.get("criticsTop")).get("score"),
        # RT hides the % below its thresholds (seen: 8 critic reviews, < 50 audience ratings) while the
        # liked / not-liked counts are still in the JSON — kept raw, never turned into a fake score
        "critics_liked": crit.get("liked"),
        "critics_not_liked": crit.get("not_liked"),
        "critics_hidden": crit.get("score") is None and bool(crit.get("reviews")),
        "audience": aud.get("score"),
        "audience_liked": aud.get("liked"),
        "audience_not_liked": aud.get("not_liked"),
        "audience_hidden": aud.get("score") is None and bool(aud.get("liked") or aud.get("not_liked")),
        "audience_type": aud.get("score_type"),          # VERIFIED / ALL (what the page displays)
        "audience_ratings": aud.get("ratings"),
        "audience_banded": aud.get("banded"),            # e.g. "5,000+ Verified Ratings"
        "audience_all": _score(ov.get("audienceAll")).get("score"),
        "audience_verified": _score(ov.get("audienceVerified")).get("score"),
        "poster_url": _poster(ld, blocks),
        "genres": ld.get("genre") or hero.get("metadataGenres"),
        "ems_id": vanity.get("emsId"),
        "rt_id": (blocks.get("curation-json") or {}).get("rtId"),
        "lifecycle": vanity.get("lifecycleWindow"),
        "parent": (vanity.get("parents") or [None])[0],
        "info": info,
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
    }
    if kind == "series":
        seasons = []
        for t in soup.find_all("tile-season"):
            href = t.get("href") or ""
            sn = re.search(r"/s(\d+)$", href)
            cs = t.find(attrs={"slot": "critics-score"})
            ad = t.find(attrs={"slot": "air-date"})
            m = _YEAR.search(ad.get_text() if ad else "")
            seasons.append({"season": int(sn.group(1)) if sn else None, "url": BASE + href if href.startswith("/") else href,
                            "critics": _intv((cs.get_text(strip=True) if cs else "").rstrip("%")),
                            "air_date": ad.get_text(strip=True) if ad else None,
                            "year": int(m.group(0)) if m else None})
        if not seasons:   # fallback: ld+json containsSeason (no scores there)
            for s in ld.get("containsSeason") or []:
                sn = re.search(r"/s(\d+)$", s.get("url", ""))
                seasons.append({"season": int(sn.group(1)) if sn else None, "url": s.get("url"),
                                "critics": None, "air_date": None, "year": None})
        res["seasons"] = sorted(seasons, key=lambda s: s["season"] or 0)
        props = hero.get("metadataProps") or []
        res["years_label"] = next((p for p in props if _YEAR.search(p)), None)   # "2023 - Present"
        res["seasons_label"] = next((p for p in props if "Season" in p), None)
    return res


def fetch(url: str, season_pages: bool | list[int] = False, refresh: bool = False) -> dict | None:
    """Scrape one /m/ or /tv/ page. None = page does not exist.

    season_pages: True = also open every season page (both scores per season);
                  list of season numbers = only those.
    """
    if not url.startswith("http"):
        url = BASE + url
    html = get_html(url, refresh=refresh)
    if html is None:
        return None
    res = _parse_page(html, url)
    if res["kind"] == "series" and season_pages:
        want = None if season_pages is True else set(season_pages)
        for s in res["seasons"]:
            if want is not None and s["season"] not in want:
                continue
            sh = get_html(s["url"], refresh=refresh)
            if sh is None:
                s["page_missing"] = True
                continue
            sp = _parse_page(sh, s["url"])
            s.update({"critics": sp["critics"], "critics_reviews": sp["critics_reviews"],
                      "audience": sp["audience"], "audience_ratings": sp["audience_ratings"],
                      "audience_banded": sp["audience_banded"], "poster_url": sp["poster_url"],
                      "release_date": sp["info"].get("Release Date"), "year": sp["year"] or s["year"]})
    return res


# ----------------------------------------------------------------------------------------------
# matching
# ----------------------------------------------------------------------------------------------

_ARTICLES = ("the ", "a ", "an ")


def _name_fit(query: str, cand_titles: list[str]) -> float:
    q = norm(query)
    if not q:
        return 0.0
    best = 0.0
    for t in cand_titles:
        c = norm(t)
        if not c:
            continue
        if c == q:
            return 1.0
        qa, ca = q, c
        for a in _ARTICLES:
            qa = qa[len(a):] if qa.startswith(a) else qa
            ca = ca[len(a):] if ca.startswith(a) else ca
        if qa == ca:
            best = max(best, 0.95)
            continue
        # "Title: Subtitle" vs "Title"
        if ca.startswith(qa + " ") or qa.startswith(ca + " "):
            best = max(best, 0.7)
        r = difflib.SequenceMatcher(None, qa, ca).ratio()
        if r >= 0.92:
            best = max(best, 0.85)
        elif r >= 0.85:
            best = max(best, 0.6)
    return best


def _year_fit(year, kind, cand, series_start=None) -> tuple[float, int | None]:
    """Movies: |RT year - year| (0 -> 1.0, 1 -> 0.85, more -> rejected).
    Series: with series_start (first-air year from IMDb/TMDB) the show's start year must match (±1);
    without it, `year` must fall inside the show's run; open-ended runs that started > 10 years
    before `year` get 0.7 (old show with the same name is a frequent false friend, e.g. Shōgun 1980/2024).
    """
    if year is None and series_start is None:
        return 0.8, None
    if kind == "series" or cand["kind"] == "series":
        sy = cand.get("start_year") or cand.get("year")
        ey = cand.get("end_year")
        if sy is None:
            return 0.4, None
        if series_start is not None:
            diff = abs(sy - series_start)
            return {0: 1.0, 1: 0.85}.get(diff, 0.0), diff
        hi = ey or 9999
        diff = 0 if sy <= year <= hi else (sy - year if year < sy else year - hi)
        if diff == 0 and ey is None and year - sy > 10:
            return 0.7, diff
    else:
        cy = cand.get("year")
        if year is None:
            return 0.8, None
        if cy is None:
            return 0.4, None              # RT has no year yet (unreleased / stub page)
        diff = abs(cy - year)
    return {0: 1.0, 1: 0.85}.get(diff, 0.0), diff


def _people_fit(people: list[str] | None, cand) -> float | None:
    """1.0 = a given person is in the candidate's cast/director; 0.0 = none of them while the
    candidate's director is known; 0.5 = none of them but only a short cast list is known
    (page search shows 3 names); None = nothing to compare."""
    if not people:
        return None
    names = {norm(x) for x in (cand.get("cast") or []) + (cand.get("director") or []) if x}
    if not names:
        return None
    if any(norm(p) in names for p in people):
        return 1.0
    return 0.0 if cand.get("director") else 0.5


ACCEPT = 0.80        # min score to accept automatically
MARGIN = 0.15        # and the runner-up (different URL) must be at least this much lower


def _merge(pool: dict, cands: list[dict]):
    """Collect candidates by URL, merging what page search and Algolia each know (cast, director, aka)."""
    for c in cands:
        cur = pool.get(c["url"])
        if cur is None:
            pool[c["url"]] = dict(c, via=[c["via"]])
            continue
        for k, v in c.items():
            if k in ("cast", "aka"):
                cur[k] = list(dict.fromkeys((cur.get(k) or []) + (v or [])))
            elif k == "via":
                if v not in cur["via"]:
                    cur["via"].append(v)
            elif cur.get(k) in (None, "", []) and v not in (None, "", []):
                cur[k] = v


def _score_all(pool, titles, year, kind, people, series_start) -> dict:
    seen = {}
    for url, c in pool.items():
        if kind and c["kind"] != kind:
            continue
        nf = max(_name_fit(t, [c["title"]] + c.get("aka", [])) for t in titles)
        if nf < 0.6:
            continue
        yf, diff = _year_fit(year, kind, c, series_start)
        if yf == 0.0:
            continue
        pf = _people_fit(people, c)
        score = nf * yf
        if pf == 1.0:
            score = min(1.0, score + 0.1)
        elif pf == 0.0:
            score *= 0.75
        elif pf == 0.5:
            score *= 0.9
        seen[url] = dict(c, via="+".join(c["via"]), score=round(score, 3), name_fit=nf,
                         year_diff=diff, people_fit=pf)
    return seen


def _decide(seen: dict):
    ranked = sorted(seen.values(), key=lambda c: -c["score"])
    if not ranked:
        return "not_found", None, ranked
    best = ranked[0]
    second = ranked[1]["score"] if len(ranked) > 1 else 0.0
    if best["score"] >= ACCEPT and best["score"] - second >= MARGIN:
        return "matched", best, ranked
    return "ambiguous", None, ranked


def find(titles: list[str], year: int | None, kind: str | None = None,
         people: list[str] | None = None, series_start: int | None = None,
         use_algolia: bool = True) -> dict:
    """Find the RT page for a title.

    titles: every known title (original, Latin/English, RU, UK — order = priority). Duplicates and
            empties are ignored.  RT only knows English/original titles; RU/UK queries rarely hit.
    year:   movie: release year (±1 accepted with lower score); series: any year the show ran.
    kind:   'movie' / 'series' / None.
    people: optional director/cast names (from TMDB) — confirm or penalise candidates.
    series_start: series only — first-air year of the show (IMDb startYear / TMDB first_air_date);
            strongly recommended, it separates remakes/reboots with the same name.

    Returns {status: matched|ambiguous|not_found, match, confidence, candidates[:8], tried}.
    'matched' only when score >= ACCEPT and the runner-up is >= MARGIN below; otherwise the caller
    must put the candidates into manual review (match_issues), never pick one silently.
    """
    titles = [t.strip() for t in dict.fromkeys(titles) if t and t.strip()]
    pool: dict[str, dict] = {}
    tried = []
    score = lambda: _decide(_score_all(pool, titles, year, kind, people, series_start))
    for t in titles:
        tried.append(("page", t))
        _merge(pool, search(t))
        status, match, ranked = score()
        if status == "matched" and match["name_fit"] >= 0.95 and match.get("year_diff") == 0:
            break
    status, match, ranked = score()
    if status != "matched" and use_algolia:
        for t in titles:
            tried.append(("algolia", t))
            _merge(pool, search_algolia(t))
        status, match, ranked = score()
    return {"status": status, "match": match, "confidence": match["score"] if match else
            (ranked[0]["score"] if ranked else 0.0), "candidates": ranked[:8], "tried": tried}


# ----------------------------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(prog="films.sources.rt")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("search")
    p.add_argument("query")
    p.add_argument("--algolia", action="store_true")
    p = sub.add_parser("fetch")
    p.add_argument("url")
    p.add_argument("--season-pages", action="store_true")
    p.add_argument("--refresh", action="store_true")
    p = sub.add_parser("find")
    p.add_argument("year", type=int)
    p.add_argument("kind", choices=["movie", "series", "any"])
    p.add_argument("titles", nargs="+")
    p.add_argument("--people", nargs="*", help="director / cast names to confirm the match")
    p.add_argument("--series-start", type=int, help="series: first-air year")
    a = ap.parse_args(argv)
    if a.cmd == "search":
        res = search_algolia(a.query) if a.algolia else search(a.query)
        for c in res:
            print(f"{c['kind']:6} {c['year'] or '----'} {c['critics'] if c['critics'] is not None else '--':>3}% "
                  f"{c['title']}  {c['url']}" + (f"  aka={c['aka'][:3]}" if c.get("aka") else ""))
    elif a.cmd == "fetch":
        print(json.dumps(fetch(a.url, a.season_pages, a.refresh), ensure_ascii=False, indent=1))
    else:
        r = find(a.titles, a.year, None if a.kind == "any" else a.kind, people=a.people,
                 series_start=a.series_start)
        print(json.dumps({k: v for k, v in r.items() if k != "candidates"}, ensure_ascii=False, indent=1))
        for c in r["candidates"]:
            print(f"  {c['score']:.3f} {c['kind']:6} {c['year']} {c['title']}  {c['url']}  ({c['via']})")
    print(f"[requests: {stats}]", file=sys.stderr)


if __name__ == "__main__":
    main()
