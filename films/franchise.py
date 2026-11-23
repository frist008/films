"""Franchise universes (R34): every screen work of every franchise in config/franchises.toml.

A franchise is the WHOLE universe (owner, 2026-10-23): numbered films, spin-offs, prequels, reboots/remakes of
the same IP, TV series based on the films and films based on the series, animated films/series, specials.
Membership is collected from four independent sources and cross-checked; nothing is matched by title alone.

Usage:
    python -X utf8 -m films.franchise resolve [--only KEY[,KEY...]] [--refresh] [--workers N]
    python -X utf8 -m films.franchise report [--only KEY] [--review] [--source-diff]
    python -X utf8 -m films.franchise show KEY            # every member with its sources / evidence

Sources (labels as stored in franchise_members.sources):
  * ``wikidata``      — SPARQL (query.wikidata.org) + wbgetentities.  Anchors = TMDB collection parts (P4947 /
                        P345) → their P8345 «media franchise» / P179 «part of the series» / P1434 «takes place in
                        fictional universe» / P361 «part of» items (+ one or two levels up: film series → franchise)
                        = seed items; then every item linked DOWN to a seed by P8345 / P179 / P1434 / P1080 / P361 /
                        seed P527 («has part»), walking sub-franchises and film series; plus weaker links: works
                        «based on» (P144) a member, P155/P156 neighbours, P4969 derivative works.  Umbrellas
                        (marvel, dc) add: production company (P272), based on a work of the publisher / universe,
                        characters (P674) from the universe.
  * ``wikipedia:<lang>`` — en / ru / uk: the seed items' articles (film/TV sections only), their navbox
                        («topic's main template» P1424) and their category tree («topic's main category» P910),
                        plus curated list articles for the umbrellas; links → Wikidata via pageprops.
  * ``fandom:<wiki>`` — the franchise's fandom wiki(s) (Wikidata P6262 of the seed / anchor items + curated):
                        film / TV categories via api.php; pages → IMDb via the page's IMDb external link, else a
                        strict TMDB search, else an exact label match to a work another source already found.
  * ``tmdb_collection`` — the parts of the TMDB collections listed in franchises.toml.

Confidence: ``confirmed`` = at least two source FAMILIES agree (wikidata / wikipedia / fandom / tmdb_collection;
the three Wikipedia languages are one family, as are several fandom wikis); ``single_source`` = one family;
``review`` = doubtful whatever the count: only a weak Wikidata link (based-on / neighbour / character / derivative),
a work based on an imprint (Vertigo, Icon, Malibu …) rather than the shared universe, a non-structural
documentary, a TV pilot, a fandom title matched by name only, a row no longer found by the latest run.
See docs/franchises.md for the rules and the decisions behind them.

Politeness: plain HTTPS with ``requests`` and a descriptive User-Agent; <= 2 requests/s per host (process-wide,
thread-safe), exponential backoff on 429 / 5xx / network errors honouring Retry-After.  Every raw response is
cached under data/cache/franchise/<host>/ (gzip JSON; gitignored) and reused for DEFAULT_MAX_AGE_DAYS;
``--refresh`` refetches everything once per run.  TMDB goes through films.sources.tmdb (cached in tmdb_raw).

Writes: only table franchise_members (upsert; nothing is deleted — rows the latest run no longer finds stay,
marked ``review`` with a note).  Per-franchise details (seeds, pages, wikis, evidence, unresolved, excluded,
failures) go to data/cache/franchise/resolved/<key>.json for `report` / `show`.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import sys
import threading
import time
import tomllib
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests

from films import db
from films.sources import tmdb

UA_BASE = "films-db/1.0"
UA_NOTE = "personal, non-commercial film catalogue"
CACHE = db.ROOT / "data" / "cache" / "franchise"
RESOLVED = CACHE / "resolved"
CONFIG = db.ROOT / "config" / "franchises.toml"
MIN_INTERVAL = 0.55            # seconds between requests to one host (<= 2 req/s)
# Wikimedia (all *.wikipedia.org + www.wikidata.org action API) is ONE rate-limit bucket per client (2026 policy,
# https://www.mediawiki.org/wiki/Wikimedia_APIs/Rate_limits): a User-Agent without contact information counts as
# "unidentified" = 10 requests/min; with a contact (FILMS_CONTACT in the environment or .env) = 200/min.
WIKIMEDIA_INTERVAL_ANON = 6.5
WDQS_INTERVAL = 1.0            # query.wikidata.org (own limits: 60 s of query time per minute)
MAX_ATTEMPTS = 7
TIMEOUT = 90
DEFAULT_MAX_AGE_DAYS = 30
LANGS = ("en", "ru", "uk")

WDQS = "https://query.wikidata.org/sparql"
WDAPI = "https://www.wikidata.org/w/api.php"

STATS = {"requests": 0, "cache_hits": 0, "retries": 0, "errors": 0}
_stats_lock = threading.Lock()


def _stat(k, n=1):
    with _stats_lock:
        STATS[k] += n


class FetchError(RuntimeError):
    pass


# ----------------------------------------------------------------------------------------------
# HTTP with per-host politeness and a file cache
# ----------------------------------------------------------------------------------------------

_FRESH_AFTER = time.time() - DEFAULT_MAX_AGE_DAYS * 86400     # cache files older than this are refetched
_host_locks: dict[str, threading.Lock] = {}
_host_last: dict[str, float] = {}
_glock = threading.Lock()
_session_local = threading.local()


def set_refresh(refresh: bool, max_age_days: float = DEFAULT_MAX_AGE_DAYS):
    """--refresh: everything fetched before this moment is stale (each URL is fetched once per run)."""
    global _FRESH_AFTER
    _FRESH_AFTER = time.time() if refresh else time.time() - max_age_days * 86400


def contact() -> str | None:
    """Operator contact for the User-Agent (Wikimedia policy): FILMS_CONTACT from the environment or .env.
    Never printed or written anywhere (it may be a personal identifier)."""
    import os
    v = os.environ.get("FILMS_CONTACT")
    if not v:
        env = db.ROOT / ".env"
        try:
            for line in env.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("FILMS_CONTACT="):
                    v = line.split("=", 1)[1].strip().strip('"').strip("'")
        except OSError:
            pass
    return v or None


def user_agent() -> str:
    c = contact()
    note = f"{c}; {UA_NOTE}" if c else UA_NOTE
    return f"{UA_BASE} ({note}) python-requests/{requests.__version__}"


def _session() -> requests.Session:
    s = getattr(_session_local, "s", None)
    if s is None:
        s = requests.Session()
        s.headers["User-Agent"] = user_agent()
        _session_local.s = s
    return s


def _bucket(host: str) -> tuple[str, float]:
    """Rate-limit bucket and minimal interval for a host."""
    if host == "query.wikidata.org":
        return host, WDQS_INTERVAL
    if host.endswith(("wikipedia.org", "wikidata.org", "mediawiki.org", "wikimedia.org")):
        return "wikimedia", (MIN_INTERVAL if contact() else WIKIMEDIA_INTERVAL_ANON)
    return host, MIN_INTERVAL


def _wait_turn(host: str):
    bucket, interval = _bucket(host)
    with _glock:
        lk = _host_locks.setdefault(bucket, threading.Lock())
    with lk:
        dt = _host_last.get(bucket, 0.0) + interval - time.monotonic()
        if dt > 0:
            time.sleep(dt)
        _host_last[bucket] = time.monotonic()


def _penalize(host: str, seconds: float):
    """After a 429 with Retry-After: nobody in the bucket sends before `seconds` have passed."""
    bucket, interval = _bucket(host)
    with _glock:
        lk = _host_locks.setdefault(bucket, threading.Lock())
    with lk:
        _host_last[bucket] = max(_host_last.get(bucket, 0.0), time.monotonic() + seconds - interval)


def cache_key(url: str, params: dict | None, post: bool) -> str:
    key = json.dumps([url, sorted((params or {}).items()), bool(post)], ensure_ascii=False, default=str)
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def _cache_path(url: str, h: str) -> Path:
    host = urlparse(url).hostname or "unknown"
    return CACHE / host / h[:2] / f"{h}.json.gz"


def fetch_json(url: str, params: dict | None = None, *, post: bool = False, accept: str = "application/json"):
    """GET/POST returning parsed JSON (None for HTTP 404).  Cached; polite; retries; raises FetchError."""
    h = cache_key(url, params, post)
    path = _cache_path(url, h)
    if path.exists() and path.stat().st_mtime >= _FRESH_AFTER:
        try:
            with gzip.open(path, "rt", encoding="utf-8") as f:
                _stat("cache_hits")
                return json.load(f)["body"]
        except (OSError, ValueError):
            pass                                    # broken cache file: refetch
    host = urlparse(url).hostname or ""
    last_err = None
    for attempt in range(MAX_ATTEMPTS):
        _wait_turn(host)
        _stat("requests")
        try:
            if post:
                r = _session().post(url, data=params, headers={"Accept": accept}, timeout=TIMEOUT)
            else:
                r = _session().get(url, params=params, headers={"Accept": accept}, timeout=TIMEOUT)
        except requests.RequestException as e:
            last_err = f"{type(e).__name__}: {e}"
            _stat("retries")
            time.sleep(min(120, 2 ** attempt * 2))
            continue
        if r.status_code == 429 or r.status_code >= 500:
            last_err = f"HTTP {r.status_code}"
            _stat("retries")
            ra = r.headers.get("Retry-After")
            delay = float(ra) if ra and ra.replace(".", "", 1).isdigit() else 2 ** attempt * 3
            delay = min(300, max(delay, 1))
            _penalize(host, delay)
            continue
        if r.status_code == 404 or (r.history and not urlparse(r.url).path.endswith(("api.php", "sparql"))):
            body = None                            # missing page / wiki (fandom redirects unknown wikis away)
        elif r.status_code != 200:
            _stat("errors")
            raise FetchError(f"{url}: HTTP {r.status_code} {r.text[:200]!r}")
        else:
            try:
                body = r.json()
            except ValueError:
                last_err = f"not JSON: {r.text[:120]!r}"
                _stat("retries")
                if attempt >= 2:
                    break
                time.sleep(min(120, 2 ** attempt * 2))
                continue
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            json.dump({"url": url, "params": params, "post": post, "status": r.status_code,
                       "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "body": body},
                      f, ensure_ascii=False)
        tmp.replace(path)
        return body
    _stat("errors")
    raise FetchError(f"{url}: gave up after {MAX_ATTEMPTS} attempts ({last_err})")


# ----------------------------------------------------------------------------------------------
# Wikidata
# ----------------------------------------------------------------------------------------------

def _qid(uri: str) -> str:
    return uri.rsplit("/", 1)[-1]


def sparql(query: str) -> list[dict]:
    body = fetch_json(WDQS, {"query": query, "format": "json"}, post=True,
                      accept="application/sparql-results+json")
    out = []
    for b in (body or {}).get("results", {}).get("bindings", []):
        out.append({k: (_qid(v["value"]) if v.get("type") == "uri" and "wikidata.org/entity/" in v["value"]
                        else v["value"]) for k, v in b.items()})
    return out


def _values(qids) -> str:
    return " ".join(f"wd:{q}" for q in qids)


def _chunks(seq, n):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


# properties kept per entity (truthy values only)
KEEP_PROPS = ("P31", "P345", "P577", "P580", "P136", "P4947", "P4983", "P8345", "P179", "P1434", "P1080", "P361",
              "P144", "P155", "P156", "P4969", "P272", "P6262", "P910", "P1424", "P123")
SITES = {"https://en.wikipedia.org/": "enwiki", "https://ru.wikipedia.org/": "ruwiki",
         "https://uk.wikipedia.org/": "ukwiki"}
_entity_mem: dict[str, dict] = {}
_entity_lock = threading.Lock()


def entities(qids) -> dict[str, dict]:
    """Trimmed entities {qid: {id, labels{en,ru,uk,mul}, sitelinks{enwiki,ruwiki,ukwiki}, claims{P: [values]}}}
    via SPARQL (the action API is rate-limited much harder), cached per entity on disk."""
    want = [q for q in dict.fromkeys(qids) if q and re.fullmatch(r"Q\d+", q)]
    out, missing = {}, []
    edir = CACHE / "entities"
    for q in want:
        with _entity_lock:
            if q in _entity_mem:
                out[q] = _entity_mem[q]
                continue
        p = edir / q[-2:] / f"{q}.json.gz"
        if p.exists() and p.stat().st_mtime >= _FRESH_AFTER:
            try:
                with gzip.open(p, "rt", encoding="utf-8") as f:
                    out[q] = json.load(f)
                _stat("cache_hits")
                continue
            except (OSError, ValueError):
                pass
        missing.append(q)
    for batch in _chunks(missing, 120):
        got = {q: {"id": q, "missing": True, "labels": {}, "sitelinks": {}, "claims": {}} for q in batch}
        vals = _values(batch)
        pv = " ".join(f'(wdt:{p} "{p}")' for p in KEEP_PROPS)
        for r in sparql(f"SELECT ?item ?prop ?v WHERE {{ VALUES ?item {{ {vals} }} VALUES (?wdt ?prop) {{ {pv} }} "
                        f"?item ?wdt ?v . }}"):
            e = got[r["item"]]
            lst = e["claims"].setdefault(r["prop"], [])
            if r["v"] not in lst:
                lst.append(r["v"])
        for r in sparql(f"SELECT ?item ?l (LANG(?l) AS ?lang) WHERE {{ VALUES ?item {{ {vals} }} "
                        f'?item rdfs:label ?l . FILTER(LANG(?l) IN ("en", "ru", "uk", "mul")) }}'):
            got[r["item"]]["labels"][r["lang"]] = r["l"]
            got[r["item"]]["missing"] = False
        for r in sparql(f"SELECT ?item ?site ?name WHERE {{ VALUES ?item {{ {vals} }} ?a schema:about ?item ; "
                        f"schema:isPartOf ?site ; schema:name ?name . FILTER(?site IN "
                        f"(<https://en.wikipedia.org/>, <https://ru.wikipedia.org/>, <https://uk.wikipedia.org/>)) }}"):
            got[r["item"]]["sitelinks"][SITES[r["site"]]] = r["name"]
        for q, e in got.items():
            if e["claims"] or e["sitelinks"]:
                e["missing"] = False
            out[q] = e
            p = edir / q[-2:] / f"{q}.json.gz"
            p.parent.mkdir(parents=True, exist_ok=True)
            with gzip.open(p, "wt", encoding="utf-8") as f:
                json.dump(e, f, ensure_ascii=False)
    with _entity_lock:
        _entity_mem.update(out)
    return out


def label(e: dict | None, langs=("en", "ru", "uk")) -> str | None:
    if not e:
        return None
    for lg in tuple(langs) + ("mul",):
        if e["labels"].get(lg):
            return e["labels"][lg]
    return next(iter(e["labels"].values()), None)


def earliest_year(e: dict) -> int | None:
    years = []
    for t in e["claims"].get("P577", []) + e["claims"].get("P580", []):
        m = re.match(r"[+-]?(\d{4})-", t)
        if m and int(m.group(1)) > 1800:
            years.append(int(m.group(1)))
    return min(years) if years else None


# --- class closure: which "root" classes an item's P31 classes reach via P279* ---------------------

# screen-work roots → tag
SCREEN_ROOTS = {
    "Q11424": "film", "Q18011172": "film", "Q506240": "film",          # film, film project, television film
    "Q5398426": "series", "Q526877": "series", "Q1259759": "series",   # TV series, web series, miniseries
    "Q15416": "tvprog",                                                # television program (generic)
    "Q1261214": "special", "Q653916": "pilot",                         # television special, television pilot
    "Q24862": "short", "Q93204": "documentary",
    "Q202866": "animated", "Q581714": "animated", "Q117467246": "animated", "Q20650540": "animated",
    "Q63952888": "animated", "Q220898": "animated", "Q113687694": "animated", "Q17517379": "animated",
}
# hard exclusions (not a work the owner can watch as a title of its own)
EXCLUDE_ROOTS = {
    "Q21191270": "episode", "Q3464665": "season", "Q7889": "video game", "Q99935529": "fictional film",
    "Q15306849": "fictional work", "Q11796892": "advertisement", "Q193977": "music video", "Q622550": "trailer",
    "Q21514702": "abandoned project", "Q2301591": "fan film", "Q4504495": "award ceremony",
    "Q16913666": "award ceremony",
}
# soft flags (kept, noted)
FLAG_ROOTS = {"Q26225765": "version", "Q914242": "version", "Q240862": "version"}
# franchise-like containers that are walked (not works themselves)
SERIES_ROOTS = {"Q196600": "media franchise", "Q24856": "film series", "Q559618": "fictional universe",
                "Q3275581": "shared universe", "Q130371093": "film franchise", "Q104771028": "short film series",
                "Q113791292": "animated short film series", "Q138511811": "series of television specials",
                "Q110940888": "television film series", "Q133829894": "adaptations of works"}
ALL_ROOTS = {**SCREEN_ROOTS, **EXCLUDE_ROOTS, **FLAG_ROOTS, **SERIES_ROOTS}

ANIMATION_GENRES = {"Q202866", "Q581714", "Q1107", "Q11425", "Q20650540", "Q63952888", "Q117467246", "Q1047299"}
DOCUMENTARY_GENRES = {"Q93204", "Q133731271", "Q137613441", "Q133731350"}
PARODY_GENRES = {"Q622548", "Q170539", "Q16254232", "Q185529"}     # parody film, parody, porn parody, porn film

# the closure depends on the set of roots: a new root gets a new cache file
_class_file = CACHE / f"class_roots_{hashlib.sha1(' '.join(sorted(ALL_ROOTS)).encode()).hexdigest()[:10]}.json"
_class_mem: dict[str, list[str]] | None = None
_class_lock = threading.Lock()


def class_roots(classes) -> dict[str, list[str]]:
    """{class_qid: [root qids it reaches via P279*]} (persisted; recomputed only for unseen classes)."""
    global _class_mem
    with _class_lock:
        if _class_mem is None:
            try:
                _class_mem = json.loads(_class_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                _class_mem = {}
        todo = [c for c in dict.fromkeys(classes) if c and c not in _class_mem]
    for batch in _chunks(todo, 60):
        rows = sparql(f"SELECT ?c ?root WHERE {{ VALUES ?c {{ {_values(batch)} }} "
                      f"VALUES ?root {{ {_values(ALL_ROOTS)} }} ?c wdt:P279* ?root . }}")
        found: dict[str, list[str]] = {c: [] for c in batch}
        for r in rows:
            found.setdefault(r["c"], []).append(r["root"])
        with _class_lock:
            _class_mem.update(found)
            _class_file.parent.mkdir(parents=True, exist_ok=True)
            _class_file.write_text(json.dumps(_class_mem, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    with _class_lock:
        return {c: _class_mem.get(c, []) for c in classes}


def classify(types: list[str]) -> dict:
    """{'screen': set(tags), 'exclude': set(reasons), 'flags': set, 'series_like': bool} for P31 values."""
    roots = set()
    cr = class_roots(types)
    for t in types:
        roots.update(cr.get(t, []))
        if t in ALL_ROOTS:
            roots.add(t)
    return {"screen": {SCREEN_ROOTS[r] for r in roots if r in SCREEN_ROOTS},
            "exclude": {EXCLUDE_ROOTS[r] for r in roots if r in EXCLUDE_ROOTS},
            "flags": {FLAG_ROOTS[r] for r in roots if r in FLAG_ROOTS},
            "series_like": any(r in SERIES_ROOTS for r in roots)}


def container_work(claims: dict, screen: set | None = None) -> str | None:
    """A walked franchise container (series_like P31) that is also a work of its own -> 'screen' / 'ids', else None.
    'screen': a screen work per P31 with its own IMDb id (an anime series Wikidata also files as a series container —
    «Code Geass: Rozé of the Recapture», «The Animatrix», «I Am Groot»); 'ids': no screen P31 but its own IMDb id and
    a TMDB movie / tv id («Psycho-Pass: Sinners of the System», P31 «film trilogy»).  2026-10-25 audit."""
    imdb = any(re.fullmatch(r"tt\d+", v) for v in claims.get("P345", []))
    if not imdb:
        return None
    if screen is None:
        screen = classify(claims.get("P31", []))["screen"]
    if screen:
        return "screen"
    return "ids" if (claims.get("P4947") or claims.get("P4983")) else None


def qids_by_external(prop: str, values) -> dict[str, list[str]]:
    """{external value: [qids]} for P345 (IMDb) / P4947 (TMDB movie) / P4983 (TMDB tv)."""
    out: dict[str, list[str]] = {}
    for batch in _chunks(sorted(set(map(str, values))), 150):
        vals = " ".join(json.dumps(v) for v in batch)
        for r in sparql(f"SELECT ?v ?item WHERE {{ VALUES ?v {{ {vals} }} ?item wdt:{prop} ?v . }}"):
            out.setdefault(r["v"], []).append(r["item"])
    return out


# ----------------------------------------------------------------------------------------------
# MediaWiki (Wikipedia, Fandom)
# ----------------------------------------------------------------------------------------------

def wiki_api(lang: str) -> str:
    return f"https://{lang}.wikipedia.org/w/api.php"


def fandom_api(wiki: str) -> str:
    """'harrypotter' → https://harrypotter.fandom.com/api.php; 'ru.marvel' → https://marvel.fandom.com/ru/api.php"""
    if "." in wiki:
        lang, name = wiki.split(".", 1)
        return f"https://{name}.fandom.com/{lang}/api.php"
    return f"https://{wiki}.fandom.com/api.php"


def mw_query(api: str, params: dict, max_pages: int = 40) -> list[dict]:
    """action=query with continuation; returns the list of raw response bodies."""
    p = {"action": "query", "format": "json", "formatversion": "2", **params}
    out, cont = [], {}
    from urllib.parse import urlencode
    for _ in range(max_pages):
        q = {**p, **cont}
        # 50 Cyrillic titles percent-encoded exceed the URL limit (HTTP 414) → POST (the API accepts it for reads)
        body = fetch_json(api, q, post=len(urlencode(q)) > 1800)
        if body is None:
            break
        if "error" in body:
            raise FetchError(f"{api}: {body['error'].get('code')}: {body['error'].get('info')}")
        out.append(body)
        if "continue" not in body:
            break
        cont = body["continue"]
    return out


def mw_parse(api: str, page: str) -> dict | None:
    """action=parse wikitext + sections (follows redirects).  None when the page does not exist."""
    body = fetch_json(api, {"action": "parse", "page": page, "prop": "wikitext|sections", "redirects": "1",
                            "format": "json", "formatversion": "2"})
    if body is None or "error" in body:
        if body and body["error"].get("code") not in ("missingtitle", "invalidtitle"):
            raise FetchError(f"{api} parse {page}: {body['error']}")
        return None
    return body["parse"]


def titles_to_qids(lang: str, titles) -> dict[str, str | None]:
    """Wikipedia titles → Wikidata QIDs: exact sitelink names via SPARQL first (no action-API cost), the rest
    (redirects, other spellings) via the wiki's pageprops with redirects followed.  Missing pages map to None."""
    titles = list(dict.fromkeys(t for t in titles if t))
    out: dict[str, str | None] = {}
    site = f"https://{lang}.wikipedia.org/"
    for batch in _chunks(titles, 150):
        vals = " ".join(json.dumps(t, ensure_ascii=False) + f"@{lang}" for t in batch)
        for r in sparql(f"SELECT ?name ?item WHERE {{ VALUES ?name {{ {vals} }} ?a schema:name ?name ; "
                        f"schema:isPartOf <{site}> ; schema:about ?item . }}"):
            out[r["name"]] = r["item"]
    rest = [t for t in titles if t not in out]
    for batch in _chunks(rest, 50):
        for body in mw_query(wiki_api(lang), {"prop": "pageprops", "ppprop": "wikibase_item",
                                              "titles": "|".join(batch), "redirects": "1"}):
            q = body.get("query") or {}
            alias = {n["from"]: n["to"] for n in q.get("normalized", [])}
            redir = {r["from"]: r["to"] for r in q.get("redirects", [])}
            by_title = {pg["title"]: (pg.get("pageprops") or {}).get("wikibase_item") for pg in q.get("pages", [])}
            for t in batch:
                t2 = alias.get(t, t)
                t3 = redir.get(t2, t2)
                if by_title.get(t3):
                    out[t] = by_title[t3]
                else:
                    out.setdefault(t, None)
    return out


def _page_cache(api: str, title: str) -> Path:
    h = hashlib.sha1(f"{api}|{title}".encode("utf-8")).hexdigest()
    return CACHE / "pages" / (urlparse(api).hostname or "x") / h[:2] / f"{h}.json.gz"


def wiki_contents(api: str, titles) -> dict[str, tuple[str, str] | None]:
    """{requested title: (resolved title, wikitext) | None} — 50 pages per request, redirects followed.
    Cached per page (so a prefetch for many franchises at once serves each franchise later)."""
    out: dict[str, tuple[str, str] | None] = {}
    todo = []
    for t in dict.fromkeys(t for t in titles if t):
        pc = _page_cache(api, t)
        if pc.exists() and pc.stat().st_mtime >= _FRESH_AFTER:
            try:
                with gzip.open(pc, "rt", encoding="utf-8") as f:
                    d = json.load(f)
                out[t] = (d["resolved"], d["text"]) if d.get("text") is not None else None
                _stat("cache_hits")
                continue
            except (OSError, ValueError, KeyError):
                pass
        todo.append(t)
    for batch in _chunks(todo, 50):
        got: dict[str, tuple[str, str] | None] = {}
        for body in mw_query(api, {"prop": "revisions", "rvprop": "content", "rvslots": "main",
                                   "titles": "|".join(batch), "redirects": "1"}):
            q = body.get("query") or {}
            alias = {n["from"]: n["to"] for n in q.get("normalized", [])}
            redir = {r["from"]: r["to"] for r in q.get("redirects", [])}
            pages = {}
            for pg in q.get("pages", []):
                revs = pg.get("revisions") or []
                if revs:
                    pages[pg["title"]] = revs[0].get("slots", {}).get("main", {}).get("content", "")
            for t in batch:
                t3 = redir.get(alias.get(t, t), alias.get(t, t))
                if t3 in pages:
                    got[t] = (t3, pages[t3])
                else:
                    got.setdefault(t, None)
        for t, v in got.items():
            pc = _page_cache(api, t)
            pc.parent.mkdir(parents=True, exist_ok=True)
            with gzip.open(pc, "wt", encoding="utf-8") as f:
                json.dump({"requested": t, "resolved": v[0] if v else None, "text": v[1] if v else None}, f,
                          ensure_ascii=False)
            out[t] = v
    return out


# ----------------------------------------------------------------------------------------------
# wikitext: film / TV sections and the links in them
# ----------------------------------------------------------------------------------------------

# a section is used when its heading (or an ancestor heading) matches ALLOW and no heading on its path matches DENY
SECTION_ALLOW = {
    "en": r"film|movie|televis|\btv\b|series|shows?\b|spin-?offs?|animat|cartoon|anime|special|shorts?\b|feature|"
          r"future|upcoming|projects?|sequel|prequel|reboot|remake|filmograph|live[- ]action|streaming|web\b|"
          r"install?ments|entries|chronolog|overview|releases?|one-shots?|adaptations?",
    "ru": r"фильм|кино|сериал|мультфильм|мультсериал|анимац|аниме|экранизац|спин-?офф?|спецвыпуск|короткометраж|"
          r"будущ|планируем|проект|приквел|сиквел|перезапуск|ремейк|фильмограф|сводная|телевиден|адаптац",
    "uk": r"фільм|кіно|серіал|мультфільм|мультсеріал|анімац|аніме|екранізац|спін-?оф|спецвипуск|короткометраж|"
          r"майбутн|заплановані|проєкт|проект|приквел|сиквел|перезапуск|ремейк|фільмограф|зведена|телебачен|"
          r"адаптац",
}
SECTION_DENY = {
    "en": r"video ?games?|\bgames?\b|comics?|novels?|books?|literature|music|soundtrack|merchandis|toys?\b|"
          r"theme park|attraction|\brides?\b|reception|box office|critical|accolade|award|\bcast\b|crew|"
          r"characters?|development|production|inspiration|influence|legacy|parod|documentar|see also|"
          r"references|notes|external|further reading|bibliography|in popular culture|cultural|other media|"
          r"unproduced|cancel+ed|abandoned|fan films?|home media|marketing|controvers|lawsuit|stage|theatre|"
          r"podcast|radio|potential|possible|proposed|other projects|crossover|inspired|homage|unofficial|"
          r"\bfan\b|mockbuster|knock-?off|imitat",
    "ru": r"возможн|другие проекты|кроссовер|неофициальн|подражан|фанатск|вдохнов|игр|комикс|роман|книг|литератур|музык|саундтрек|товар|игрушк|аттракцион|критик|сбор|касс|бюджет|"
          r"награ|актёр|актер|в ролях|персонаж|съёмочн|создани|работа над|вдохновен|влияни|наследи|пароди|"
          r"документ|см\. также|примечани|ссылки|источник|в культуре|другие медиа|других медиа|отменён|отменен|"
          r"нереализ|фанат|театр|радио|подкаст|издани",
    "uk": r"можлив|інші проєкти|інші проекти|кросовер|неофіційн|наслідув|фанатськ|натхнен|ігр|гра\b|комікс|роман|книг|літератур|музик|саундтрек|товар|іграшк|атракціон|критик|збор|касов|"
          r"бюджет|нагород|актор|у ролях|персонаж|знімальн|створенн|робота над|натхнен|вплив|спадщин|пароді|"
          r"документ|див\. також|примітки|посилання|джерела|у культурі|інші медіа|інших медіа|скасован|"
          r"нереаліз|фанат|театр|радіо|подкаст|видання",
}
_TAIL_SECTIONS = re.compile(r"see also|references|notes|external|further reading|bibliography|см\. также|"
                            r"примечани|ссылки|литература|источники|див\. також|примітки|посилання|джерела|"
                            r"література", re.I)
_HEADING = re.compile(r"^(={2,6})\s*(.+?)\s*\1\s*$", re.M)
_COMMENT = re.compile(r"<!--.*?-->", re.S)
_REF = re.compile(r"<ref[^>/]*/>|<ref[^>]*>.*?</ref>", re.S | re.I)


def clean_heading(h: str) -> str:
    h = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]", r"\1", h)
    h = re.sub(r"\{\{[^{}]*\}\}", "", h)
    h = re.sub(r"<[^>]+>", "", h)
    return h.replace("''", "").strip()


SEQUEL_ALLOW = {
    "en": r"sequel|prequel|spin-?off|future|franchise|follow-?up|remake|reboot|television series|tv series|"
          r"animated series|series",
    "ru": r"продолжени|сиквел|приквел|спин-?офф?|будущ|франшиз|ремейк|перезапуск|сериал|мультсериал",
    "uk": r"продовженн|сиквел|приквел|спін-?оф|майбутн|франшиз|ремейк|перезапуск|серіал|мультсеріал",
}


def film_sections(wikitext: str, lang: str, whole: bool = False, allow_re: str | None = None
                  ) -> list[tuple[str, str]]:
    """[(heading path, text)] of the film / TV sections.  `whole` (curated list articles): every section
    except See also / References / Notes / External links, the lead included.  `allow_re` replaces the
    allowed-heading pattern (a single film's article: only its sequel / franchise sections)."""
    text = _REF.sub("", _COMMENT.sub("", wikitext or ""))
    allow = re.compile(allow_re or SECTION_ALLOW.get(lang, SECTION_ALLOW["en"]), re.I)
    deny = re.compile(SECTION_DENY.get(lang, SECTION_DENY["en"]), re.I)
    marks = list(_HEADING.finditer(text))
    out, path = [], []
    if whole:
        out.append(("(lead)", text[:marks[0].start()] if marks else text))
    for i, m in enumerate(marks):
        level, head = len(m.group(1)), clean_heading(m.group(2))
        path = [p for p in path if p[0] < level] + [(level, head)]
        body = m.group(0) + "\n" + text[m.end(): marks[i + 1].start() if i + 1 < len(marks) else len(text)]
        heads = [h for _, h in path]
        if whole:
            if not any(_TAIL_SECTIONS.search(h) for h in heads):
                out.append((" / ".join(heads), body))
            continue
        if any(deny.search(h) for h in heads):
            continue
        if any(allow.search(h) for h in heads):
            out.append((" / ".join(heads), body))
    return out


_LIST_LINE = re.compile(r"^\s*(?:[|!*#;:]|\{\||={2,6}|\{\{\s*(?:main|further|see also|details|основная статья|"
                        r"главная|подробнее|основна стаття|детальніше|докладніше)\b)", re.I)


def list_lines(body: str) -> str:
    """Only the lines of a section that list works — headings, table rows, bullets, {{Main}} hatnotes — not the
    prose (prose mentions other films: «Reeves filmed it after The Matrix Resurrections»)."""
    return "\n".join(l for l in body.splitlines() if _LIST_LINE.match(l))


_LINK = re.compile(r"\[\[\s*([^\[\]|#{}]+?)\s*(?:#[^\[\]|]*)?(?:\|[^\[\]]*)?\]\]")
_TPL = re.compile(r"\{\{\s*([^{}|]+?)\s*\|([^{}]*)\}\}")
MAIN_TEMPLATES = re.compile(r"^(main|main article|further|see also|details|основная статья|главная|подробнее|"
                            r"основна стаття|детальніше|докладніше|див\. також|см\. также)$", re.I)
IW_TEMPLATES = re.compile(r"^(нп\d?|не переведено(?: \d)?|iw|не перекладено|interlanguage link|ill)$", re.I)
_NS_SKIP = re.compile(r"^(file|image|category|template|wikipedia|wp|help|portal|special|user|talk|wikt|"
                      r"файл|изображение|категория|шаблон|википедия|портал|участник|обсуждение|"
                      r"зображення|категорія|вікіпедія|користувач|обговорення|commons|s|q|d|n|b|v|mw)\s*:", re.I)
_LANG_PREFIX = re.compile(r"^:?(en|ru|uk|de|fr|es|it|ja|pl|pt|zh|ko)\s*:\s*(.+)$", re.I)


def wikitext_links(text: str, lang: str) -> list[tuple[str, str]]:
    """[(lang, title)] of article links: [[...]], {{Main|...}} and interlanguage templates ({{Нп3|ru|text|en|en
    title}}, {{ill|title|de|German title}}) — for an untranslated ru/uk link the foreign-wiki title is added."""
    out = []
    for m in _LINK.finditer(text):
        t = m.group(1).strip()
        lm = _LANG_PREFIX.match(t)
        if lm:
            out.append((lm.group(1).lower(), lm.group(2).strip()))
            continue
        if t.startswith(":"):
            t = t[1:]
        if not t or _NS_SKIP.match(t):
            continue
        out.append((lang, t))
    for m in _TPL.finditer(text):
        name, args = m.group(1).strip(), [a.strip() for a in m.group(2).split("|")]
        pos = [a for a in args if "=" not in a]
        if MAIN_TEMPLATES.match(name):
            out += [(lang, a) for a in pos if a and not _NS_SKIP.match(a)]
        elif IW_TEMPLATES.match(name) and pos:
            out.append((lang, pos[0]))
            if name.lower() in ("ill", "interlanguage link"):
                # {{ill|Local title|de|German title}}
                if len(pos) >= 3 and re.fullmatch(r"[a-z]{2,3}", pos[1]):
                    out.append((pos[1], pos[2]))
            elif len(pos) >= 4 and re.fullmatch(r"[a-z]{2,3}", pos[2] or "en"):
                # {{Нп3|Русское название|текст|en|English title}}
                out.append((pos[2] or "en", pos[3]))
            elif len(pos) == 3 and re.fullmatch(r"[a-z]{2,3}", pos[2]):
                out.append((pos[2], pos[0]))
    seen, res = set(), []
    for lg, t in out:
        t = re.sub(r"\s+", " ", t.replace("_", " ")).strip()
        if not t:
            continue
        t = t[0].upper() + t[1:]
        if (lg, t) not in seen:
            seen.add((lg, t))
            res.append((lg, t))
    return res


# ----------------------------------------------------------------------------------------------
# fandom: which categories hold the films / series
# ----------------------------------------------------------------------------------------------

FANDOM_CAT_PREFIXES = ("Film", "Movie", "Television", "TV", "Series", "Show", "Animated", "Animation", "Live",
                       "Special", "Short", "One-Shot", "Feature", "Theatrical", "Direct", "Disney", "Netflix",
                       "Upcoming", "Released", "Mini", "Web", "Cartoon", "Anime", "OVA", "Spin", "Prequel",
                       "Sequel", "Reboot", "Remake", "Canon", "Official", "Hulu", "Max", "HBO", "Amazon",
                       "Paramount", "Peacock", "Apple", "Live-action", "Stop")
_CAT_MODS = (r"live[- ]action|animated|theatrical|feature|upcoming|released|direct[- ]to[- ](?:video|dvd)|"
             r"television|tv|streaming|disney\+|netflix|hulu|max|hbo|amazon|paramount\+|peacock|apple tv\+|"
             r"canon|official|anime|spin-?off|prequel|sequel|reboot|remake|cgi|stop[- ]motion|short|original")
_CAT_HEADS = (r"films?|movies?|feature films|television series|tv series|series|tv shows?|television shows?|"
              r"shows|television films?|tv films?|tv movies?|television specials?|tv specials?|specials|"
              r"short films?|shorts|one-shots|miniseries|mini-series|web series|webseries|animated series|"
              r"cartoons?|ovas?|anime|television|television programmes?|tv programmes?|programmes|programs")
FANDOM_CAT_OK = re.compile(rf"^(?:(?:{_CAT_MODS})\s+)*(?:{_CAT_HEADS})$"
                           rf"|^.{{2,60}}\s(?:films|movies|television series|tv series|tv shows|television shows|"
                           rf"animated series|specials|short films)$"
                           rf"|^.{{2,60}}\s\((?:film series|films)\)$", re.I)
FANDOM_CAT_DENY = re.compile(
    r"fictional|in-universe|in universe|\bcast\b|\bcrew\b|characters?|images?|gallery|galleries|soundtracks?|"
    r"\bscores?\b|novels?|novelizations?|comics?|games?|appearances?|unproduced|cancel+ed|unmade|unreleased|"
    r"fan[- ]?(?:made|films?|fiction)|parod|merchandis|\btoys?\b|deleted|trailers?|posters?|"
    r"quotes?|transcripts?|episodes?|music|songs?|stubs?|templates?|featured|candidates|disambig|redirects?|"
    r"articles|needing|without|\blists?\b|behind the scenes|documentar|locations?|events?|organi[sz]ations?|"
    r"species|vehicles?|weapons?|\bitems?\b|objects?|actors?|actresses|directors?|writers?|producers?|"
    r"composers?|staff|people|reviews?|ratings?|box office|production|studios?|companies|distributors?|"
    r"networks?|channels?|by year|\b\d{4}\b|books?|magazines?|cards?|figures?|lego|video|audio|radio|"
    r"podcasts?|attractions?|rides?|stage|musicals?|plays?|concepts?|title cards?|credits?|logos?|"
    r"in-jokes?|references|crossovers? with|\bnon-|legends|alternate|infobox|images|screenshots?|"
    r"collections?|box sets?|dvds?|blu-?rays?|home (?:video|media)|releases", re.I)


def fandom_category_ok(name: str) -> bool:
    """Film / TV category of a fandom wiki?  «(real-world)» marks out-of-universe categories («Films (real-world)»
    on the Harry Potter wiki) and is ignored."""
    name = re.sub(r"\s*\(real[- ]?world\)\s*$", "", name, flags=re.I)
    return bool(FANDOM_CAT_OK.search(name)) and not FANDOM_CAT_DENY.search(name)


_DISAMBIG = re.compile(r"\s*\(([^()]*)\)\s*$")


def clean_work_title(title: str) -> tuple[str, int | None, str | None]:
    """'Halloween (2007 film)' → ('Halloween', 2007, 'movie'); 'Daredevil (TV series)' → ('Daredevil', None,
    'tv'); a non-work disambiguation ('Mercury (planet)') is left in the title."""
    t, year, kind = title.strip(), None, None
    m = _DISAMBIG.search(t)
    if m:
        d = m.group(1).lower()
        if re.search(r"film series|franchise|saga|trilogy|universe|серия фильмов|франшиза|франшиза|трилогия", d):
            return t[:m.start()].strip(), None, "collection"
        y = re.search(r"\b(19\d\d|20\d\d)\b", d)
        is_work = re.search(r"film|movie|series|\btv\b|show|special|short|anime|animated|фильм|сериал|"
                            r"мультфильм|фільм|серіал|мультфільм", d)
        if is_work or (y and re.fullmatch(r"\s*(19|20)\d\d\s*", d)):
            t = t[:m.start()].strip()
            year = int(y.group(1)) if y else None
            if re.search(r"series|\btv\b|show|сериал|серіал", d) and not re.search(r"tv film|tv movie", d):
                kind = "tv"
            elif re.search(r"film|movie|фильм|фільм|short|special", d):
                kind = "movie"
    return t, year, kind


def norm_title(s: str | None) -> str:
    s = unicodedata.normalize("NFKD", s or "").lower()
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.replace("&", " and ").replace("ё", "е")
    s = re.sub(r"[^\w]+", " ", s)
    s = re.sub(r"^(the|a|an) ", "", s.strip())
    return re.sub(r"\s+", " ", s).strip()


def tmdb_call(fn, *args, **kw):
    """films.sources.tmdb writes its cache into data/films.db; other collectors write there too, so a call can
    hit «database is locked» despite busy_timeout — retry a few times before giving up."""
    import sqlite3
    for attempt in range(6):
        try:
            return fn(*args, **kw)
        except sqlite3.OperationalError as e:
            if "locked" not in str(e) or attempt == 5:
                raise
            time.sleep(5 * (attempt + 1))


# ----------------------------------------------------------------------------------------------
# curated per-franchise additions (verified by hand; see docs/franchises.md)
# ----------------------------------------------------------------------------------------------
# seeds            extra Wikidata franchise / series / universe items to walk (besides the anchor-derived ones)
# seeds_exclude    items never walked (too broad: they would pull in another universe)
# exclude          works never listed (with the reason)
# umbrella         {universes, publishers, imprints, companies}: works produced by the companies, set in /
#                  based on the universe or a publisher's work are members; imprint-only works → review
# wikipedia        {lang: [("page"|"category", title, whole)]}: list articles / categories read in addition
# fandom           extra fandom wikis (subdomain);  fandom_skip: wikis from Wikidata P6262 not to use
# fandom_categories {wiki: [category names]} used in addition to the auto-discovered ones
EXTRA: dict[str, dict] = {
    "marvel": {
        "seeds": ["Q642878",      # Marvel Cinematic Universe
                  "Q55537283",    # Sony's Spider-Man Universe
                  "Q2307877",     # Spider-Man in film
                  "Q2006869",     # X-Men (film series)
                  "Q3745432",     # Fantastic Four in film
                  "Q7758753",     # Punisher in film
                  "Q109311969",   # Venom (film series)
                  "Q85784060",    # Marvel's Netflix television series
                  "Q18121909"],   # Marvel Animated Features
        "umbrella": {"universes": ["Q931597", "Q2246088"],     # Marvel Universe, Earth-616 (characters' P1080)
                     "publishers": ["Q173496"],                # Marvel Comics
                     "imprints": ["Q2482589", "Q1414981", "Q2465548", "Q13580443"],  # Icon, Malibu, Epic, Star
                     # Marvel Studios, Marvel Television, Marvel Animation, Marvel Entertainment, Marvel Studios
                     # Animation (not Marvel Productions: it also made Transformers, My Little Pony, D&D cartoons)
                     "companies": ["Q367466", "Q6778202", "Q2425877", "Q1200552", "Q117377427"]},
        "wikipedia": [("page", "List of films based on Marvel Comics publications", True),
                      ("page", "List of television series based on Marvel Comics publications", True),
                      ("page", "List of Marvel Cinematic Universe films", True),
                      ("page", "List of Marvel Cinematic Universe television series", True),
                      ("category", "Category:Films based on Marvel Comics", False),
                      ("category", "Category:Television shows based on Marvel Comics", False)],
        "fandom": ["marvelcinematicuniverse", "marvel-movies"],
        "fandom_skip": ["marvel"],      # Marvel Database: the comics; its film pages duplicate marvel-movies
    },
    "dc": {
        "seeds": ["Q18281265",    # DC Extended Universe
                  "Q116545465",   # DC Universe (franchise, 2025–)
                  "Q900829",      # DC Animated Universe
                  "Q20775209",    # Arrowverse
                  "Q115475274",   # Tomorrowverse
                  "Q2405799",     # DC Universe Animated Original Movies
                  "Q2111133",     # Batman in film
                  "Q2158362",     # Superman in film
                  "Q117026559"],  # DC Elseworlds (label for adaptations outside the shared universes)
        "umbrella": {"universes": ["Q1152150"],                # DC Universe (comics)
                     "publishers": ["Q2924461"],               # DC Comics
                     "imprints": ["Q122669", "Q1076724", "Q7134444", "Q56276653", "Q2251870"],
                     # Vertigo, WildStorm, Paradox Press, Black Label, America's Best Comics
                     "companies": ["Q3011161", "Q31197934", "Q2924461"]},   # DC Entertainment, DC Studios, DC Comics
        "wikipedia": [("page", "List of films based on DC Comics publications", True),
                      ("page", "List of television series based on DC Comics publications", True),
                      ("page", "DC Universe Animated Original Movies", True),
                      ("category", "Category:Films based on DC Comics", False),
                      ("category", "Category:Television shows based on DC Comics", False)],
        "fandom": ["dcextendeduniverse", "dcmovies", "dcau", "arrow", "dc"],
    },
    "conjuring": {"fandom": ["theconjuring"]},
    "wizarding_world": {"fandom": ["harrypotter"]},
    "star_wars": {"fandom": ["starwars"]},
    "alien_predator": {"fandom": ["avp"]},
    "middle_earth": {"seeds": ["Q6841185",     # Middle-earth in motion pictures (Bakshi, Rankin/Bass, Rings of Power …)
                               "Q109534468",   # Middle-earth in audiovisual media
                               "Q7466102"],    # film adaptations of The Lord of the Rings
                     "fandom": ["lotr"]},
    "godzilla": {"fandom": ["godzilla"]},
    "league_of_legends": {
        "seeds": ["Q123173259",   # League of Legends (media franchise)
                  "Q66364628",    # League of Legends universe (Runeterra)
                  "Q105221896"],  # League of Legends (video game series)
        "wikipedia": [("page", "League of Legends", False), ("page", "Arcane (TV series)", False)],
        "fandom": ["leagueoflegends", "arcane"],
    },
    "tmdb_2602": {"fandom": ["scream"]},
    "tmdb_87359": {"fandom": ["missionimpossible"]},
    "tmdb_645": {"fandom": ["jamesbond"]},
    "tmdb_8650": {"fandom": ["transformers"]},
    "tmdb_328": {"fandom": ["jurassicpark"]},
    "tmdb_173710": {"fandom": ["planetoftheapes"]},
    "tmdb_680435": {"seeds": ["Q207981"], "fandom": ["codegeass"]},          # Code Geass (media franchise)
    "tmdb_2344": {"fandom": ["matrix"]},
    "tmdb_126125": {"fandom": ["expendables"]},
    "tmdb_228446": {"fandom": ["insidious"]},
    "tmdb_495527": {"fandom": ["jumanji"]},
    "tmdb_9743": {"fandom": ["hannibal"]},
    "tmdb_2326": {"fandom": ["underworld"]},
    "tmdb_9518": {"fandom": ["transporter"]},
    "tmdb_656": {"fandom": ["saw"]},
    "tmdb_2794": {"fandom": ["riddick"]},
    "tmdb_91361": {"fandom": ["halloweenseries"], "fandom_skip": ["halloween"]},   # halloween.fandom = the holiday
    "tmdb_87096": {"fandom": ["james-camerons-avatar"]},
    "tmdb_726871": {"fandom": ["dune"]},
    "tmdb_8945": {"fandom": ["madmax"]},
    "tmdb_14890": {"fandom": ["badboys"]},
    "tmdb_1100788": {"fandom": ["smile"]},
    "tmdb_9485": {"fandom": ["fastandfurious"]},
    "tmdb_135468": {"fandom": ["gijoe"]},
    "tmdb_422837": {"fandom": ["bladerunner"]},
    "tmdb_519457": {"fandom": ["bossbaby"]},
    "tmdb_1431054": {"anchors_qids": ["Q57390937"],                          # the 2018 anime series
                     "seeds": ["Q22101190"],                                  # the light novel series (P144 base)
                     "fandom": ["violet-evergarden"]},
    "tmdb_135179": {"fandom": ["sincity"]},
    "tmdb_404609": {"fandom": ["johnwick"]},
    "tmdb_929336": {"fandom": ["knivesout"]},
    "tmdb_165369": {"fandom": ["thecollector"]},
    "tmdb_137696": {"fandom": ["monstersinc"]},
    "tmdb_848690": {"fandom": ["mortalkombat"]},
    "tmdb_742536": {"fandom": ["themeg"]},
    "tmdb_179919": {"fandom": ["riordan"]},
    "tmdb_1974": {"fandom": ["thegrudge"]},
    "tmdb_256322": {"fandom": ["thepurge"]},
    "tmdb_608101": {"fandom": ["hitmansbodyguard"]},
    "tmdb_528": {"fandom": ["terminator"]},
    "tmdb_363369": {"fandom": ["pacificrim"]},
    "tmdb_1565": {"fandom": ["28dayslater"]},
    "tmdb_1531466": {"fandom_skip": ["weapons"]},     # weapons.fandom.com is about real weapons
    "tmdb_8864": {"fandom": ["finaldestination"]},
    "tmdb_720879": {"fandom": ["sonic"]},
    "tmdb_1738564": {"fandom": ["kpopdemonhunters"]},
    "tmdb_1239686": {"fandom": ["woodywoodpecker"]},
    "tmdb_945475": {"fandom": ["beetlejuice"]},
    "tmdb_86066": {"fandom": ["despicableme"]},
    "tmdb_1069584": {"fandom": ["gladiator"]},
    "tmdb_1084247": {"fandom": ["zootopia"]},
    "tmdb_382685": {"fandom": ["nowyouseeme"]},
    "tmdb_1449595": {"fandom": ["nezha"]},
    "tmdb_2980": {"fandom": ["ghostbusters"]},
    "tmdb_8580": {"fandom": ["karatekid"]},
    "tmdb_1240438": {"fandom": ["fnaf"]},
    "tmdb_64748": {"fandom": ["silenthill"]},
    "tmdb_1434561": {"fandom": ["mario"]},
    "tmdb_521226": {"fandom": ["aquietplace"]},
    "tmdb_63043": {"fandom": ["tron"]},
    "tmdb_727761": {"fandom": ["terrifier"]},
    # круг 2 и названные владельцем напрямую
    "sherlock_holmes": {"character_works": ["Q4653"],      # Sherlock Holmes: every work with him / based on a story
                        "seeds": ["Q1970905"],                 # Adaptations of Sherlock Holmes
                        "wikipedia": [("page", "Adaptations of Sherlock Holmes", True)],
                        "fandom": ["bakerstreet"]},
    # Космический джем: the anchors' P1434 also name the Looney Tunes / Bugs Bunny universes (every Looney Tunes
    # short since 1930) — not walked; whether Looney Tunes belongs here is the owner's call (report)
    "tmdb_722961": {"seeds_exclude": ["Q109322855", "Q110307469"]},
    "little_mermaid": {"anchors_imdb": ["tt0097757", "tt5971474"],          # 1989, 2023
                       "seeds": ["Q124026159"],                              # The Little Mermaid universe
                       "fandom": ["littlemermaid"]},
    "101_dalmatians": {"anchors_imdb": ["tt0055254", "tt0115433", "tt3228774"],   # 1961, 1996, Cruella
                       "seeds": ["Q124040697"],                              # 101 Dalmatians universe
                       "fandom": ["101dalmatians"]},
    "enchanted": {"fandom": ["enchanted"]},
    "tmdb_77816": {"fandom": ["kungfupanda"]},
    "tmdb_1458864": {"fandom": ["howtotrainyourdragon"]},
    "tmdb_1022790": {"fandom": ["insideout"]},
    "tmdb_762512": {"fandom": ["lionking"]},
    "tmdb_1241984": {"fandom": ["moana"]},
    "tmdb_1504372": {"fandom": ["liloandstitch"]},
    "tmdb_1510620": {"fandom": ["garfield"]},
    "tmdb_4246": {"fandom": ["scarymovie"]},
    "tmdb_934765": {"fandom": ["rebelmoon"]},
    "tmdb_10919": {"fandom": ["omen"]},
    "tmdb_925006": {"fandom": ["downtonabbey"]},
    "tmdb_1062948": {"fandom": ["twisters"]},
    "tmdb_1461530": {"fandom": ["minecraft"]},
    "tmdb_722971": {"fandom": ["knivesout"]},
    # названы владельцем напрямую (без коллекций TMDB): сиды — элементы франшиз Wikidata, проверены вручную
    "cloverfield": {"seeds": ["Q25136301"], "fandom": ["cloverfield"]},                 # Cloverfield (film series)
    "evil_dead": {"seeds": ["Q921605"], "fandom": ["evildead"]},                        # Evil Dead (franchise)
    "eastrail_177": {"seeds": ["Q43302274"], "fandom": ["unbreakable"]},                # Unbreakable film series
    "the_gentlemen": {"anchors_qids": ["Q60614686", "Q115860329"]},                     # 2019 film, 2024 series
    "south_park": {"seeds": ["Q54622175", "Q101199114", "Q110761626"], "anchors_qids": ["Q16538"],
                   "fandom": ["southpark"]},                    # media franchise, universe, film series; the series
    "the_boys": {"anchors_imdb": ["tt1190634"], "fandom": ["amazons-the-boys"]},          # the series; Wikidata walks the rest
    "rick_and_morty": {"seeds": ["Q107264299"], "anchors_qids": ["Q15659308"], "fandom": ["rickandmorty"]},
    "pokemon": {"seeds": ["Q864", "Q17562848", "Q97138261"], "fandom": ["pokemon"]},   # franchise, universe, films
    "death_note": {"anchors_qids": ["Q718624", "Q1200673", "Q22077183", "Q19961524"], "fandom": ["deathnote"]},
    "psycho_pass": {"seeds": ["Q110541501", "Q110541411"], "fandom": ["psychopass"]},
    "gantz": {"seeds": ["Q97097940"], "fandom": ["gantz"]},
    "naruto": {"seeds": ["Q642", "Q25922646"], "fandom": ["naruto"]},                  # Naruto, Boruto franchises
    "bleach": {"seeds": ["Q904"], "fandom": ["bleach"]},
    "one_piece": {"seeds": ["Q673", "Q16682047", "Q4431905"], "fandom": ["onepiece"]},
    # экранизации игр (source = "games"): game_groups — серии игр из таблицы game_adaptations (films.sources.games)
    "assassins_creed": {"game_groups": ["Q420292"], "fandom": ["assassinscreed"]},
    "bloodborne": {"game_groups": ["Q17154554"], "fandom": ["bloodborne"]},
    "dead_space": {"game_groups": ["Q1180862"], "fandom": ["deadspace"]},
    "death_stranding": {"game_groups": ["Q115682937"], "fandom": ["deathstranding"]},
    "devil_may_cry": {"game_groups": ["Q436567", "Q3291789"], "fandom": ["devilmaycry"]},
    "ghost_of_tsushima": {"game_groups": ["Q124935200"], "fandom": ["ghostoftsushima"]},
    "god_of_war": {"game_groups": ["Q390137"], "fandom": ["godofwar"]},
    "half_life_portal": {"game_groups": ["Q752241", "Q7231475"], "fandom": ["half-life"]},
    "halo": {"game_groups": ["Q1747150"], "fandom": ["halo"]},
    "life_is_strange": {"game_groups": ["Q38609053"], "fandom": ["lifeisstrange"]},
    "mass_effect": {"game_groups": ["Q953242"], "fandom": ["masseffect"]},
    "max_payne": {"game_groups": ["Q238015"], "fandom": ["maxpayne"]},
    "metal_gear": {"game_groups": ["Q216655"], "fandom": ["metalgear"]},
    "postal": {"game_groups": ["Q5228690"], "fandom": ["postal"]},
    "tekken": {"game_groups": ["Q219623"], "fandom": ["tekken"]},
    "the_witcher": {"game_groups": ["Q18935909"],
                    "seeds": ["Q1989642"],                                   # The Witcher universe (Sapkowski)
                    "anchors_qids": ["Q1979337"],                            # Wiedźmin / The Hexer (2002 series)
                    "fandom": ["witcher"]},
    "tomb_raider": {"game_groups": ["Q270503"], "fandom": ["tombraider"]},
    "twisted_metal": {"game_groups": ["Q2995061"], "fandom": ["twistedmetal"]},
    "alan_wake": {"seeds": ["Q575505", "Q21010056"],                         # Alan Wake (game), the character
                  "anchors_qids": ["Q11339166"],                             # Bright Falls (2010 web series)
                  "fandom": ["alanwake"]},
    # экранизации игр, круг 2 (R36/R62, 2026-10-24): группы game_adaptations с подтверждёнными экранизациями,
    # у которых не было ни одного члена франшизы; fandom — из подсказок Wikidata P6262 (только вики своей серии)
    "alone_in_the_dark": {"game_groups": ["Q776174"]},
    "among_us": {"game_groups": ["Q96417649"], "fandom": ["among-us"]},
    "ark": {"game_groups": ["Q20031747"]},
    "battlefield": {"game_groups": ["Q474587"]},
    "bayonetta": {"game_groups": ["Q20062305"]},
    "borderlands": {"game_groups": ["Q16820462"]},
    "castlevania": {"game_groups": ["Q2515184"], "fandom": ["castlevania"]},
    "company_of_heroes": {"game_groups": ["Q28959835"]},
    "final_fantasy": {"game_groups": ["Q99416119", "Q952649", "Q1343367"], "fandom": ["finalfantasy"]},
    "cuphead": {"game_groups": ["Q18149495"]},
    "cyberpunk": {"game_groups": ["Q123017171"], "fandom": ["cyberpunk"]},
    "dead_by_daylight": {"game_groups": ["Q24514682"]},
    "dead_or_alive": {"game_groups": ["Q1180890"]},
    "dragon_age": {"game_groups": ["Q1766436"]},
    "dragon_ball": {"game_groups": ["Q16515147"], "fandom": ["dragonball"]},
    "dragons_dogma": {"game_groups": ["Q108370830"]},
    "drakengard_nier": {"game_groups": ["Q16000078"]},
    "fallout": {"game_groups": ["Q167835"], "fandom": ["fallout"]},
    "far_cry": {"game_groups": ["Q2667817"], "fandom": ["farcry"]},
    "fatal_frame": {"game_groups": ["Q586400"]},
    "gears_of_war": {"game_groups": ["Q3100030"]},
    "hatsune_miku": {"game_groups": ["Q553201"], "fandom": ["projectsekai"]},
    "mini_ninjas": {"game_groups": ["Q2529074"]},
    "need_for_speed": {"game_groups": ["Q55679"]},
    "persona": {"game_groups": ["Q1083352"], "fandom": ["megamitensei"]},
    "prince_of_persia": {"game_groups": ["Q157387"], "fandom": ["princeofpersia"]},
    "quake": {"game_groups": ["Q902784"], "fandom": ["quake"]},
    "ratchet_clank": {"game_groups": ["Q5170139"]},
    "shenmue": {"game_groups": ["Q7238655"], "fandom": ["shenmue"]},
    "street_fighter": {"game_groups": ["Q288035"], "fandom": ["streetfighter"]},
    "streets_of_rage": {"game_groups": ["Q8216664"]},
    "team_fortress": {"game_groups": ["Q18396192"]},
    "the_exit_8": {"game_groups": ["Q124042357"]},
    "king_of_fighters": {"game_groups": ["Q340517"]},
    "the_last_of_us": {"game_groups": ["Q28062624"], "fandom": ["thelastofus"]},
    "mortuary_assistant": {"game_groups": ["Q114968540"]},
    "the_sims": {"game_groups": ["Q4897444"], "fandom": ["sims"]},
    "uncharted": {"game_groups": ["Q1064135"]},
    "until_dawn": {"game_groups": ["Q108371663"]},
    "watch_dogs": {"game_groups": ["Q29919397"]},
    "white_day": {"game_groups": ["Q108371713"]},
    "yakuza": {"game_groups": ["Q2594935"]},
    "monster_hunter": {"game_groups": ["Q13822229", "Q107611460"]},
    # франшизы без членов (R65/R78, 2026-10-24): сиды — элементы Wikidata, проверены вручную (wbsearchentities)
    "star_trek": {"seeds": ["Q1092", "Q18043309"], "fandom": ["memory-alpha"]},       # media franchise, universe
    "men_in_black": {"seeds": ["Q6816260", "Q3305670"], "anchors_qids": ["Q2557243"],  # franchise, film series
                     "fandom": ["meninblack"]},                                       # + The Series (animated)
    "buffyverse": {"seeds": ["Q2524381"], "fandom": ["buffy"]},                       # Buffyverse (universe)
    "highlander": {"seeds": ["Q1990805"], "anchors_qids": ["Q156539", "Q1520493"],     # franchise; 1986 film, series
                   "fandom": ["highlander"]},
    "stargate": {"seeds": ["Q11927", "Q17486229"], "fandom": ["stargate"]},           # franchise, universe
    "lost": {"anchors_qids": ["Q23567"], "fandom": ["lostpedia"]},                    # Lost (TV series)
    "are_you_afraid": {"anchors_qids": ["Q1418462", "Q107208800"]},                   # 1990 series, 2019 series
    "godfather": {"seeds": ["Q3225260"]},                                             # The Godfather film series
}

# franchise / series items that are a studio's catalogue, not a universe («Walt Disney Animation Studios feature
# film», «Studio Ghibli Feature Films», «Warner Bros. Animation film» …): never walked unless curated
STUDIO_CATALOGUE = re.compile(r"(studios?|pictures|animation|productions?|entertainment|films?)\b.*\b(films?|features?|"
                              r"feature films?|movies)$|^list of|national film registry|renaissance|\bcanon\b|"
                              r"filmography|original (films|movies)|direct-to-video|"
                              r"\b(pixar|dreamworks|warner bros\.?|disney|universal|illumination|blue sky|"
                              r"sony pictures|paramount|lucasfilm|20th century)\b.*\buniverse\b", re.I)

# series items seen as anchors' P179 that are catalogues (checked by hand when reviewing the seeds)
GLOBAL_SEEDS_EXCLUDE = {"Q56070713",    # Walt Disney Animation Studios feature film
                        "Q823422",      # National Film Registry
                        "Q111667354",   # Pixar universe
                        "Q125402598",   # DreamWorks universe
                        "Q109172033",   # Warner Bros. Animation film universe
                        "Q109172151"}   # Warner Bros. universe

# fandom wikis that cover far more than one franchise (a studio, a network, film in general) — never used as a
# franchise source even when Wikidata links a work to them
GENERIC_FANDOMS = {"disney", "pixar", "dreamworks", "warnerbros", "movies", "film", "netflix", "hbo", "hbomax",
                   "amazon", "paramount", "universal", "sony", "lionsgate", "nickelodeon", "cartoonnetwork",
                   "lego", "brickipedia", "muppet", "jimhenson", "christmas-specials", "horror", "horrorfilms",
                   "villains", "heroes", "fictionalcrossover", "crossover", "awards", "academyawards",
                   "the-movie-database", "imdb", "tvtropes", "dubbing", "voice-actors", "behindthevoiceactors",
                   "soundtrack", "logos", "closinglogos", "audiovisual-identity", "aviation", "cars", "weapons",
                   "imfdb", "gun", "toys", "hasbro", "mattel", "funko", "videogames", "gaming", "steam", "anime",
                   "manga", "ultimate-pop-culture", "pop-culture", "wiki", "community", "fandom", "fanon",
                   "deathbattle", "vsbattles", "powerlisting", "superpower", "battlefordreamisland",
                   "marvel-dc-crossover", "disneyplus", "hulu", "max", "peacock", "appletv", "starz", "showtime",
                   "amc", "fx", "abc", "nbc", "cbs", "fox", "bbc", "itv", "channel4", "toho", "tsuburaya",
                   "netflix-original", "kids-shows", "theflash", "simpsons"}


# ----------------------------------------------------------------------------------------------
# candidates
# ----------------------------------------------------------------------------------------------

STRONG = "strong"
WEAK = "weak"


@dataclass
class Cand:
    key: str
    qid: str | None = None
    imdb: str | None = None
    sources: dict = field(default_factory=dict)     # label → [evidence]
    strength: set = field(default_factory=set)      # wikidata link strengths seen
    flags: set = field(default_factory=set)
    title_hint: str | None = None
    year_hint: int | None = None
    kind_hint: str | None = None                    # 'movie' / 'tv' (fandom / TMDB)
    tmdb_hint: tuple | None = None                  # (tmdb_type, tmdb_id)


# sources built from the others (not independent evidence)
DERIVED_SOURCES = {"game_adaptations"}


def families(sources) -> set[str]:
    return {s.split(":", 1)[0] for s in sources}


REVIEW_FLAGS = {"weak_link_only", "imprint", "documentary_unofficial", "pilot", "title_match_only", "stale",
                "one_wikipedia_only", "fandom_only", "parody", "unverified", "listed_review"}


def confidence(c: Cand) -> str:
    """confirmed (2+ source families) / single_source / review (doubtful whatever the count)."""
    if c.flags & REVIEW_FLAGS:
        return "review"
    return "confirmed" if len(families(c.sources) - DERIVED_SOURCES) >= 2 else "single_source"


def kind_of(screen: set, genres: set, tmdb_type: str | None, tmdb_genres: set, runtime: int | None) -> str:
    """film / series / animated film / animated series / special / short / documentary."""
    if "special" in screen or "pilot" in screen:
        base = "special"
    elif "series" in screen:
        base = "series"
    elif "film" in screen or "short" in screen or "documentary" in screen:
        base = "film"
    elif "tvprog" in screen:
        base = "series" if tmdb_type != "movie" else "film"
    else:
        base = "series" if tmdb_type == "tv" else "film"
    animated = "animated" in screen or bool(genres & ANIMATION_GENRES) or 16 in tmdb_genres
    documentary = "documentary" in screen or bool(genres & DOCUMENTARY_GENRES) or 99 in tmdb_genres
    if documentary and not animated:
        return "documentary"
    if base == "film":
        if "short" in screen or (runtime and 0 < runtime < 40):
            return "short"
        return "animated film" if animated else "film"
    if base == "series":
        return "animated series" if animated else "series"
    return "special"


# SPARQL: everything linked DOWN to a set of franchise items
MEMBER_QUERY = """SELECT ?w ?p ?f (GROUP_CONCAT(DISTINCT STRAFTER(STR(?t), "entity/"); separator=" ") AS ?types)
       (SAMPLE(?imdb0) AS ?imdb) WHERE {
  VALUES ?f { %s }
  { ?w wdt:P8345 ?f . BIND("P8345" AS ?p) } UNION { ?w wdt:P179 ?f . BIND("P179" AS ?p) }
  UNION { ?w wdt:P1434 ?f . BIND("P1434" AS ?p) } UNION { ?w wdt:P1080 ?f . BIND("P1080" AS ?p) }
  UNION { ?w wdt:P361 ?f . BIND("P361" AS ?p) } UNION { ?f wdt:P527 ?w . BIND("P527" AS ?p) }
  OPTIONAL { ?w wdt:P31 ?t }
  OPTIONAL { ?w wdt:P345 ?imdb0 }
} GROUP BY ?w ?p ?f"""

# works with an IMDb id «based on» (P144) one of the items
BASED_ON_QUERY = """SELECT ?w ?m (GROUP_CONCAT(DISTINCT STRAFTER(STR(?t), "entity/"); separator=" ") AS ?types) WHERE {
  VALUES ?m { %s }
  ?w wdt:P144 ?m . ?w wdt:P345 [] .
  OPTIONAL { ?w wdt:P31 ?t }
} GROUP BY ?w ?m"""


class Resolver:
    """Collects one franchise's members from all sources; `run()` returns the result dict."""

    def __init__(self, fr: dict, conn, log=print):
        self.fr = fr
        self.key = fr["key"]
        self.conn = conn
        self.say = log
        self.extra = EXTRA.get(self.key, {})
        self.cands: dict[str, Cand] = {}
        self.problems: list[dict] = []
        self.unresolved: list[dict] = []
        self.excluded: list[dict] = []
        self.dropped_years: list[dict] = []
        self.info: dict = {"anchors": [], "seeds": {}, "walked": {}, "pages": [], "templates": [],
                           "categories": [], "fandom": {}, "nonscreen_wikipedia": 0}
        self.anchor_qids: list[str] = []
        self.seed_qids: dict[str, dict] = {}          # anchor-derived + curated seeds (and their parents)
        self.walked: dict[str, str] = {}              # every franchise container walked (qid → label)
        self.nonscreen: set[str] = set()              # non-screen members (characters, games, novels …)
        self.fandom_pending: list[dict] = []
        self.seeds_exclude = set(self.extra.get("seeds_exclude", []))

    # --- helpers ---------------------------------------------------------------------------
    def problem(self, source: str, msg: str):
        self.problems.append({"source": source, "message": msg})
        self.say(f"  ! {self.key} {source}: {msg}")

    def _get(self, qid=None, imdb=None) -> Cand:
        if qid and qid in self.cands:
            c = self.cands[qid]
            other = self.cands.get(imdb) if imdb else None
            if other is not None and other is not c and other.qid is None:   # IMDb-only twin: fold it in
                self._merge(c, other)
                del self.cands[imdb]
        elif imdb and imdb in self.cands:
            c = self.cands[imdb]
            if qid and c.qid is None:                 # re-key an IMDb-only candidate under its QID
                del self.cands[imdb]
                c.key, c.qid = qid, qid
                if qid in self.cands:
                    self._merge(self.cands[qid], c)
                    return self.cands[qid]
                self.cands[qid] = c
        else:
            k = qid or imdb
            c = self.cands[k] = Cand(key=k, qid=qid, imdb=imdb)
        if imdb and not c.imdb:
            c.imdb = imdb
        return c

    @staticmethod
    def _merge(into: Cand, other: Cand):
        for s, ev in other.sources.items():
            into.sources.setdefault(s, []).extend(e for e in ev if e not in into.sources.get(s, []))
        into.strength |= other.strength
        into.flags |= other.flags
        into.imdb = into.imdb or other.imdb
        into.title_hint = into.title_hint or other.title_hint
        into.year_hint = into.year_hint or other.year_hint
        into.kind_hint = into.kind_hint or other.kind_hint
        into.tmdb_hint = into.tmdb_hint or other.tmdb_hint

    def add(self, *, qid=None, imdb=None, source: str, evidence: str, strength: str | None = None,
            flags=(), title=None, year=None, kind=None, tmdb_hint=None) -> Cand:
        c = self._get(qid, imdb)
        ev = c.sources.setdefault(source, [])
        if evidence not in ev and len(ev) < 12:
            ev.append(evidence)
        if strength:
            c.strength.add(strength)
        c.flags |= set(flags)
        c.title_hint = c.title_hint or title
        c.year_hint = c.year_hint or year
        c.kind_hint = c.kind_hint or kind
        c.tmdb_hint = c.tmdb_hint or tmdb_hint
        return c

    # --- 1. TMDB collections (anchors) ---------------------------------------------------
    def tmdb_collections(self):
        parts = []
        for cid in self.fr.get("tmdb_collections", []):
            try:
                col = tmdb_call(tmdb.collection, self.conn, cid)
            except Exception as e:                       # noqa: BLE001 — reported, not swallowed
                self.problem("tmdb_collection", f"collection {cid}: {e}")
                continue
            if not col:
                self.problem("tmdb_collection", f"collection {cid}: not found on TMDB")
                continue
            for p in col["parts"]:
                mtype = p.get("media_type") or "movie"
                try:
                    raw = tmdb_call(tmdb.movie_raw if mtype == "movie" else tmdb.tv_raw, self.conn, p["tmdb_id"])
                except Exception as e:                   # noqa: BLE001
                    self.problem("tmdb_collection", f"part {mtype} {p['tmdb_id']}: {e}")
                    raw = None
                imdb = (raw or {}).get("imdb_id") or ((raw or {}).get("external_ids") or {}).get("imdb_id")
                parts.append({"collection": cid, "collection_name": col.get("name_en") or col.get("name"),
                              "tmdb_id": p["tmdb_id"], "tmdb_type": "tv" if mtype == "tv" else "movie",
                              "title": p.get("title_original") or p.get("title_ru"), "year": p.get("year"),
                              "imdb": imdb or None})
        extra_anchors = self.extra.get("anchors_imdb", [])
        by_imdb = qids_by_external("P345", [p["imdb"] for p in parts if p["imdb"]] + extra_anchors) \
            if parts or extra_anchors else {}
        for imdb in extra_anchors:          # curated anchors: seed the Wikidata walk and are members themselves
            qs = by_imdb.get(imdb) or []
            if qs:
                self.anchor_qids.append(qs[0])
            else:
                self.problem("wikidata", f"curated anchor {imdb} has no Wikidata item")
            self.add(qid=qs[0] if qs else None, imdb=imdb, source="curated",
                     evidence="anchor chosen by hand from the owner's note in franchises.toml")
        for q in self.extra.get("anchors_qids", []):
            self.anchor_qids.append(q)
            self.add(qid=q, source="curated", evidence="anchor chosen by hand from the owner's note in franchises.toml")
        by_tmdb = qids_by_external("P4947", [p["tmdb_id"] for p in parts if p["tmdb_type"] == "movie"]) \
            if parts else {}
        for p in parts:
            qs = by_imdb.get(p["imdb"] or "") or by_tmdb.get(str(p["tmdb_id"])) or []
            qid = qs[0] if qs else None
            p["qid"] = qid
            self.info["anchors"].append(p)
            if qid:
                self.anchor_qids.append(qid)
            if not p["imdb"]:
                if qid:     # Wikidata may know the IMDb id TMDB lacks
                    self.add(qid=qid, source="tmdb_collection",
                             evidence=f"collection {p['collection']} {p['collection_name']}", title=p["title"],
                             year=p["year"], tmdb_hint=(p["tmdb_type"], p["tmdb_id"]))
                else:
                    self.unresolved.append({"source": "tmdb_collection", "title": p["title"], "year": p["year"],
                                            "reason": f"TMDB {p['tmdb_type']} {p['tmdb_id']} has no IMDb id"})
                continue
            self.add(qid=qid, imdb=p["imdb"], source="tmdb_collection",
                     evidence=f"collection {p['collection']} {p['collection_name']}", title=p["title"],
                     year=p["year"], tmdb_hint=(p["tmdb_type"], p["tmdb_id"]))

    # --- 2. Wikidata: seeds -----------------------------------------------------------------
    def wikidata_seeds(self):
        ents = entities(self.anchor_qids)
        via: dict[str, set] = {}
        for q in self.anchor_qids:
            for p in ("P8345", "P179", "P1434", "P361"):
                for v in ents.get(q, {}).get("claims", {}).get(p, []):
                    via.setdefault(v, set()).add(f"anchor {p}")
        for q in self.extra.get("seeds", []):
            via.setdefault(q, set()).add("curated")
        frontier = set(via)
        for depth in range(3):
            frontier -= self.seeds_exclude
            frontier -= set(self.seed_qids)
            if not frontier:
                break
            es = entities(frontier)
            class_roots({t for q in frontier for t in es.get(q, {}).get("claims", {}).get("P31", [])})
            nxt: dict[str, set] = {}
            for q in sorted(frontier):
                e = es.get(q)
                if not e or e.get("missing"):
                    continue
                c = classify(e["claims"].get("P31", []))
                if c["series_like"] and "curated" not in via.get(q, set()) and (
                        q in GLOBAL_SEEDS_EXCLUDE or STUDIO_CATALOGUE.search(label(e, ("en",)) or "")):
                    self.info.setdefault("seeds_dropped", {})[q] = f"{label(e)} — studio catalogue, not a universe"
                    continue
                if c["series_like"] or "curated" in via.get(q, set()):
                    self.seed_qids[q] = {"label": label(e), "depth": depth, "via": sorted(via.get(q, []))}
                    if depth < 2 and "curated" not in via.get(q, set()):
                        # upward only through «media franchise» / «part of the series»: «part of» / «universe» lead
                        # to studio-wide universes (Pixar universe, DreamWorks universe, Warner Bros. universe)
                        for p in ("P8345", "P179"):
                            for v in e["claims"].get(p, []):
                                if v not in self.seed_qids:
                                    nxt.setdefault(v, set()).add(f"{label(e)} {p}")
                elif c["screen"] and not c["exclude"]:
                    # a work used as a series (e.g. a TV series the film belongs to)
                    self.add(qid=q, source="wikidata", evidence=f"anchor's series/franchise item {label(e)}",
                             strength=STRONG)
            for k, v in nxt.items():
                via.setdefault(k, set()).update(v)
            frontier = set(nxt)
        self.info["seeds"] = self.seed_qids
        if not self.seed_qids:
            self.problem("wikidata", "no franchise / series item found from the anchors")

    # --- 3. Wikidata: members (downward walk) ----------------------------------------------
    def wikidata_members(self):
        walked = {q: s["label"] for q, s in self.seed_qids.items()}
        frontier = list(walked)
        for _round in range(6):
            if not frontier:
                break
            rows = []
            for batch in _chunks(frontier, 20):
                try:
                    rows += sparql(MEMBER_QUERY % _values(batch))
                except FetchError as e:
                    self.problem("wikidata", f"member query failed for {batch}: {e}")
            class_roots({t for r in rows for t in r.get("types", "").split()})
            new = []
            for r in rows:
                w, p, f = r["w"], r["p"], r["f"]
                if not re.fullmatch(r"Q\d+", w):
                    continue
                c = classify(r.get("types", "").split())
                if c["series_like"] and w not in walked and w not in self.seeds_exclude:
                    walked[w] = None
                    new.append(w)
                if c["screen"]:
                    self.add(qid=w, imdb=r.get("imdb"), source="wikidata",
                             evidence=f"{p} → {walked.get(f) or f}", strength=STRONG)
                elif not c["series_like"]:
                    self.nonscreen.add(w)
            if new:
                es = entities(new)
                for q in new:
                    walked[q] = label(es.get(q)) or q
                    # a container that is a work in its own right (2026-10-25: «Psycho-Pass: Sinners of the System» —
                    # a film trilogy with its own IMDb + TMDB entries; P31 says only «film trilogy»): a member too
                    cl = (es.get(q) or {}).get("claims", {})
                    if container_work(cl) == "ids":
                        own_imdb = [v for v in cl.get("P345", []) if re.fullmatch(r"tt\d+", v)]
                        self.add(qid=q, imdb=own_imdb[0], source="wikidata",
                                 evidence="container with its own IMDb / TMDB entry", strength=STRONG)
            frontier = new
        self.walked = walked
        self.info["walked"] = walked

    def wikidata_weak(self):
        """works based on a member (P144), P155/P156 neighbours, P4969 derivative works — weak links."""
        screen_qids = [c.qid for c in self.cands.values() if c.qid and "wikidata" in c.sources]
        members = sorted(set(screen_qids) | self.nonscreen | set(self.anchor_qids))
        rows = []
        for batch in _chunks(members, 150):
            try:
                rows += sparql(BASED_ON_QUERY % _values(batch))
            except FetchError as e:
                self.problem("wikidata", f"based-on query failed: {e}")
        class_roots({t for r in rows for t in r.get("types", "").split()})
        for r in rows:
            c = classify(r.get("types", "").split())
            if c["screen"]:
                self.add(qid=r["w"], source="wikidata", evidence=f"P144 based on {r['m']}", strength=WEAK)
        es = entities(screen_qids)
        for q in screen_qids:
            for p in ("P155", "P156", "P4969", "P144"):
                for v in es.get(q, {}).get("claims", {}).get(p, []):
                    self.add(qid=v, source="wikidata", evidence=f"{p} of {label(es.get(q)) or q}", strength=WEAK,
                             flags=("_needs_screen_check",))

    def game_adaptations(self):
        """Candidates already collected by films.sources.games (table game_adaptations), rows not marked stale,
        for the franchise's game series (EXTRA game_groups).  Built from Wikidata + Wikipedia by another
        collector, so it is recorded as a source but does not count as an independent family."""
        groups = set(self.extra.get("game_groups", []))
        if not groups:
            return
        n = 0
        for r in self.conn.execute("SELECT store, game_id, imdb_id, title, year, kind, sources FROM game_adaptations"):
            try:
                meta = json.loads(r["sources"] or "{}")
            except ValueError:
                meta = {}
            if meta.get("stale") or meta.get("group_qid") not in groups:
                continue
            n += 1
            qid = meta.get("work_qid")
            self.add(qid=qid, imdb=r["imdb_id"], source="game_adaptations",
                     evidence=f"{r['store']} game {r['game_id']}: {', '.join(meta.get('relation') or [])}"[:200],
                     title=r["title"], year=r["year"])
            if qid:
                self.anchor_qids.append(qid)
        self.info["game_adaptations_rows"] = n
        if n == 0:
            self.problem("game_adaptations", f"no non-stale rows for game series {sorted(groups)}")
        # the game series themselves seed the Wikidata walk (P8345 / P179 of the series → media franchise)
        self.extra.setdefault("seeds", [])
        self.extra["seeds"] = list(dict.fromkeys(self.extra["seeds"] + sorted(groups)))

    def wikidata_characters(self):
        """Franchises defined by a character (owner: «Шерлок весь интересен, все вариации»): every work with the
        character (P674) or based on a work featuring him (P144 → P674) — strong links."""
        for ch in self.extra.get("character_works", []):
            q = f"""SELECT DISTINCT ?w ?how WHERE {{
                {{ ?w wdt:P674 wd:{ch} . BIND("character" AS ?how) }}
                UNION {{ ?w wdt:P144 ?b . ?b wdt:P674 wd:{ch} . BIND("based on a work with the character" AS ?how) }}
                ?w wdt:P345 [] . }}"""
            try:
                rows = sparql(q)
            except FetchError as e:
                self.problem("wikidata", f"character query {ch} failed: {e}")
                continue
            self.info.setdefault("character_queries", {})[ch] = len({r["w"] for r in rows})
            for r in rows:
                self.add(qid=r["w"], source="wikidata", evidence=f"{r['how']} {ch}", strength=STRONG,
                         flags=("_needs_screen_check",))

    def wikidata_umbrella(self):
        u = self.extra.get("umbrella")
        if not u:
            return
        queries = []
        if u.get("companies"):
            queries.append(("company", STRONG, f"""SELECT ?w ?x WHERE {{ VALUES ?x {{ {_values(u['companies'])} }}
                ?w wdt:P272 ?x . ?w wdt:P345 [] . }}"""))
        if u.get("universes"):
            queries.append(("universe", STRONG, f"""SELECT ?w ?x WHERE {{ VALUES ?x {{ {_values(u['universes'])} }}
                {{ ?w wdt:P1434 ?x }} UNION {{ ?w wdt:P1080 ?x }} . ?w wdt:P345 [] . }}"""))
            queries.append(("based on a character/work of the universe", STRONG, f"""SELECT ?w ?x WHERE {{
                VALUES ?u {{ {_values(u['universes'])} }} ?w wdt:P144 ?x . ?w wdt:P345 [] .
                {{ ?x wdt:P1080 ?u }} UNION {{ ?x wdt:P1434 ?u }} UNION {{ ?x wdt:P1441 ?u }} }}"""))
            queries.append(("character from the universe", WEAK, f"""SELECT ?w ?x WHERE {{
                VALUES ?u {{ {_values(u['universes'])} }} ?w wdt:P674 ?x . ?x wdt:P1080 ?u . ?w wdt:P345 [] . }}"""))
        if u.get("publishers"):
            queries.append(("based on a work of the publisher", STRONG, f"""SELECT ?w ?x WHERE {{
                VALUES ?pub {{ {_values(u['publishers'])} }} ?w wdt:P144 ?x . ?x wdt:P123 ?pub . ?w wdt:P345 [] . }}"""))
        if u.get("imprints"):
            queries.append(("imprint", None, f"""SELECT ?w ?x WHERE {{
                VALUES ?pub {{ {_values(u['imprints'])} }} ?w wdt:P144 ?x . ?x wdt:P123 ?pub . ?w wdt:P345 [] . }}"""))
        hits: dict[str, list] = {}
        for name, strength, q in queries:
            try:
                rows = sparql(q)
            except FetchError as e:
                self.problem("wikidata", f"umbrella query '{name}' failed: {e}")
                continue
            self.info.setdefault("umbrella_queries", {})[name] = len({r["w"] for r in rows})
            for r in rows:
                hits.setdefault(r["w"], []).append((name, strength, r["x"]))
        for w, hs in hits.items():
            strengths = {s for _, s, _ in hs if s}
            names = {n for n, _, _ in hs}
            flags = {"_needs_screen_check"}
            if "imprint" in names and not (names - {"imprint", "character from the universe"}):
                flags.add("imprint")          # based on an imprint's comic, nothing ties it to the universe
            self.add(qid=w, source="wikidata", evidence="; ".join(sorted(f"{n} {x}" for n, _, x in hs))[:300],
                     strength=STRONG if STRONG in strengths else WEAK, flags=flags)

    # --- 4. Wikipedia en / ru / uk ---------------------------------------------------------
    def wiki_targets(self) -> dict[str, dict]:
        """{lang: {'articles': [(title, whole, why)], 'templates': [...], 'categories': [...]}} for the seeds."""
        seeds = list(self.seed_qids)
        es = entities(seeds)
        tpl_items = {v: q for q in seeds for v in es.get(q, {}).get("claims", {}).get("P1424", [])}
        cat_items = {v: q for q in seeds for v in es.get(q, {}).get("claims", {}).get("P910", [])}
        es.update(entities(list(tpl_items) + list(cat_items)))
        # curated list articles / categories are given by their English title; ru / uk via the item's sitelinks
        curated = self.extra.get("wikipedia", [])
        cur_q = titles_to_qids("en", [t for _, t, _ in curated]) if curated else {}
        cur_e = entities([q for q in cur_q.values() if q])
        for kind, t, _ in curated:
            if not cur_q.get(t) and not getattr(self, "_curated_reported", False):
                self.problem("wikipedia:en", f"curated {kind} '{t}' not found")
        self._curated_reported = True
        out = {}
        # no franchise item on Wikidata: read the anchor works' own articles (sequel / franchise sections) and
        # find their franchise navbox (a navbox linking two or more of the anchors)
        anchor_mode = not seeds
        ae = entities(self.anchor_qids) if anchor_mode else {}
        for lang in LANGS:
            site = f"{lang}wiki"
            articles = [(es[q]["sitelinks"][site], False, f"article of {label(es[q])}") for q in seeds
                        if site in es.get(q, {}).get("sitelinks", {})]
            if anchor_mode:
                articles += [(e["sitelinks"][site], False, "anchor article") for e in ae.values()
                             if site in e.get("sitelinks", {})]
            templates = [es[t]["sitelinks"][site] for t in tpl_items if site in es.get(t, {}).get("sitelinks", {})]
            categories = [es[c]["sitelinks"][site] for c in cat_items if site in es.get(c, {}).get("sitelinks", {})]
            for kind, en_title, whole in curated:
                title = en_title if lang == "en" else cur_e.get(cur_q.get(en_title) or "", {}).get(
                    "sitelinks", {}).get(site)
                if not title:
                    continue
                if kind == "page":
                    articles.append((title, whole, "curated list"))
                elif kind == "category":
                    categories.append(title)
                elif kind == "template":
                    templates.append(title)
            out[lang] = {"articles": articles, "templates": list(dict.fromkeys(templates)),
                         "categories": list(dict.fromkeys(categories))}
        return out

    def wikipedia(self):
        targets = self.wiki_targets()
        for lang in LANGS:
            api = wiki_api(lang)
            source = f"wikipedia:{lang}"
            articles = targets[lang]["articles"]
            templates = targets[lang]["templates"]
            categories = targets[lang]["categories"]
            links: list[tuple[str, str, str]] = []          # (link lang, title, evidence)
            try:
                texts = wiki_contents(api, [a for a, _, _ in articles] + templates)
            except FetchError as e:
                self.problem(source, f"page fetch failed: {e}")
                texts = {}
            anchor_titles = {texts[a][0] for a, _, why in articles if why == "anchor article" and texts.get(a)}
            if len(anchor_titles) >= 2:
                templates = templates + self.find_navboxes(api, lang, [texts[a][1] for a, _, why in articles
                                                                       if why == "anchor article" and texts.get(a)],
                                                           anchor_titles)
                texts.update(wiki_contents(api, templates))
            for title, whole, why in articles:
                got = texts.get(title)
                self.info["pages"].append({"lang": lang, "title": title, "why": why, "found": bool(got)})
                if not got:
                    self.problem(source, f"article '{title}' ({why}) not found")
                    continue
                allow_re = SEQUEL_ALLOW.get(lang, SEQUEL_ALLOW["en"]) if why == "anchor article" else None
                for path, body in film_sections(got[1], lang, whole, allow_re):
                    if not whole:
                        body = list_lines(body)
                    for lg, t in wikitext_links(body, lang):
                        links.append((lg, t, (f"list {got[0]} § {path}" if whole else f"{got[0]} § {path}")[:200]))
            for title in templates:
                got = texts.get(title)
                self.info["templates"].append({"lang": lang, "title": title, "found": bool(got)})
                if not got:
                    self.problem(source, f"navbox '{title}' not found")
                    continue
                for lg, t, group in navbox_links(got[1], lang):
                    links.append((lg, t, f"navbox {got[0]}" + (f" / {group}" if group else "")))
            for cat in categories:
                try:
                    pages, cats_used = walk_category(lang, cat)
                except FetchError as e:
                    self.problem(source, f"category '{cat}' failed: {e}")
                    continue
                self.info["categories"].append({"lang": lang, "title": cat, "subcats": cats_used,
                                                "pages": len(pages)})
                links += [(lang, t, f"category {c}") for t, c in pages]
            # resolve link titles → QIDs, per link language
            by_lang: dict[str, list] = {}
            for lg, t, ev in links:
                by_lang.setdefault(lg, []).append(t)
            qmap: dict[tuple, str | None] = {}
            for lg, ts in by_lang.items():
                if lg not in ("en", "ru", "uk", "de", "fr", "es", "it", "ja", "pl", "pt", "zh", "ko"):
                    continue
                try:
                    for t, q in titles_to_qids(lg, ts).items():
                        qmap[(lg, t)] = q
                except FetchError as e:
                    self.problem(source, f"title → Wikidata mapping failed ({lg}): {e}")
            n = 0
            for lg, t, ev in links:
                q = qmap.get((lg, t))
                if q:
                    self.add(qid=q, source=source, evidence=ev, flags=("_needs_screen_check",))
                    n += 1
            self.say(f"  {self.key} {source}: {len(articles)} articles, {len(templates)} navboxes, "
                     f"{len(categories)} categories → {len(links)} links, {n} with a Wikidata item")

    def _franchise_words(self) -> list[str]:
        """Normalized franchise names: TMDB collection names without «Collection» / «коллекция»."""
        out = []
        for a in self.info.get("anchors", []):
            n = norm_title(re.sub(r"(?i)\b(collection|коллекция|colección|saga|series)\b", "",
                                  a.get("collection_name") or ""))
            if len(n) >= 3:
                out.append(n)
        return list(dict.fromkeys(out))

    def find_navboxes(self, api: str, lang: str, texts: list[str], anchor_titles: set[str]) -> list[str]:
        """Templates placed on their own line in the anchor articles whose links cover >= 2 anchors."""
        ns = {"en": "Template:", "ru": "Шаблон:", "uk": "Шаблон:"}.get(lang, "Template:")
        cands: dict[str, int] = {}
        for t in texts:
            for m in re.finditer(r"^\{\{\s*([^{}|\n]+?)\s*\}\}\s*$", t or "", re.M):
                name = m.group(1).strip()
                if re.match(r"(reflist|примечания|refbegin|refend|portal|authority control|клюква|"
                            r"external|внешние ссылки|зовнішні посилання|commons|defaultsort|clear|"
                            r"кинопоиск|imdb|rotten|metacritic|stub|заготовка|film date|lang)", name, re.I):
                    continue
                cands[ns + name[0].upper() + name[1:]] = cands.get(ns + name[0].upper() + name[1:], 0) + 1
        names = list(cands)[:40]
        if not names:
            return []
        got = wiki_contents(api, names)
        keep = []
        norm = {x.replace("_", " ") for x in anchor_titles}
        # the navbox's topic on Wikidata (P1423): a person's or a company's navbox is not a franchise's
        topic_kind: dict[str, str] = {}
        try:
            tq = titles_to_qids(lang, [got[n][0] for n in names if got.get(n)])
            inv = {q: t for t, q in tq.items() if q}
            if inv:
                for r in sparql(f"SELECT ?t ?cls WHERE {{ VALUES ?t {{ {_values(inv)} }} ?t wdt:P1423 ?topic . "
                                f"?topic wdt:P31 ?cls . }}"):
                    k = "person" if r["cls"] == "Q5" else ("company" if r["cls"] in (
                        "Q4830453", "Q1762059", "Q783794", "Q891723", "Q6881511", "Q2085381") else "work")
                    if topic_kind.get(inv[r["t"]]) in (None, "work"):
                        topic_kind[inv[r["t"]]] = k
            # no P1423: the article named like the template tells («Sam Raimi» → a human, «Ju-on» → a franchise)
            rest = [n for n in names if got.get(n) and got[n][0] not in topic_kind]
            art = {n: got[n][0].split(":", 1)[-1] for n in rest}
            aq = titles_to_qids(lang, list(art.values())) if art else {}
            ae = entities([q for q in aq.values() if q])
            cr = class_roots({c for e in ae.values() for c in e["claims"].get("P31", [])})
            for n, a in art.items():
                e = ae.get(aq.get(a) or "")
                if not e:
                    continue
                p31 = e["claims"].get("P31", [])
                if "Q5" in p31:
                    topic_kind[got[n][0]] = "person"
                elif set(p31) & {"Q4830453", "Q1762059", "Q783794", "Q891723", "Q6881511", "Q2085381"}:
                    topic_kind[got[n][0]] = "company"
                elif any(set(cr.get(c, [])) & (set(SERIES_ROOTS) | set(SCREEN_ROOTS)) for c in p31):
                    topic_kind[got[n][0]] = "work"
        except FetchError:
            pass
        for n in names:
            g = got.get(n)
            if not g:
                continue
            linked = {t for _, t, _ in navbox_links(g[1], lang)}
            hit = len(linked & norm)
            # a franchise navbox: its name is the franchise's, or the anchors are a good part of it (a studio's or
            # director's navbox also links two anchors, among dozens of other films)
            names_ok = any(w and w in norm_title(n.split(":", 1)[-1]) for w in self._franchise_words())
            tk = topic_kind.get(g[0])
            if hit >= 2 and tk in ("person", "company"):
                self.info.setdefault("navboxes_rejected", {}).setdefault(lang, []).append(f"{n} (navbox of a {tk})")
                continue
            ratio = hit / max(1, len(linked))
            # a franchise-topic navbox may still be an umbrella («Disney Princess»: 2 of 60 links) → ratio >= 10 %
            if hit >= 2 and (names_ok or ratio >= 0.25 or (tk == "work" and ratio >= 0.1)):
                keep.append(n)
            elif hit >= 2:
                self.info.setdefault("navboxes_rejected", {}).setdefault(lang, []).append(
                    f"{n} ({hit} of {len(linked)} links are anchors)")
        self.info.setdefault("navboxes_found", {})[lang] = keep
        return keep

    # --- 5. fandom ---------------------------------------------------------------------------
    def fandom_wikis(self) -> list[str]:
        """Curated wikis only.  Wikidata P6262 («Fandom article ID») of the seeds / anchors also names general
        wikis (TV Tropes mirrors, dubbing databases, Memory Alpha, «to-hollywood-and-beyond» …) whose film
        categories hold thousands of unrelated titles, so those are only listed as suggestions in the report."""
        wikis = {w: "curated" for w in self.extra.get("fandom", [])}
        suggested: dict[str, str] = {}
        es = entities(list(self.seed_qids) + self.anchor_qids)
        for q, e in es.items():
            for v in e.get("claims", {}).get("P6262", []):
                w = v.split(":", 1)[0].lower()
                if "." in w or w in wikis or w in GENERIC_FANDOMS or w in self.extra.get("fandom_skip", ()):
                    continue
                suggested.setdefault(w, f"Wikidata P6262 of {label(e) or q}")
        self.info["fandom"] = {w: {"why": why} for w, why in wikis.items()}
        self.info["fandom_suggested"] = suggested
        if not wikis:
            self.problem("fandom", "no fandom wiki configured for this franchise"
                         + (f" (Wikidata suggests: {', '.join(sorted(suggested))})" if suggested else ""))
        return list(wikis)

    def fandom(self):
        for wiki in self.fandom_wikis():
            source = f"fandom:{wiki}"
            api = fandom_api(wiki)
            rec = self.info["fandom"][wiki]
            try:
                si = fetch_json(api, {"action": "query", "meta": "siteinfo", "format": "json", "formatversion": "2"})
            except FetchError as e:
                self.problem(source, f"wiki not reachable: {e}")
                rec["error"] = str(e)[:200]
                continue
            if not si or "query" not in si:
                self.problem(source, "wiki does not exist")
                rec["error"] = "does not exist"
                continue
            rec["sitename"] = si["query"]["general"].get("sitename")
            try:
                cats = fandom_find_categories(api) + [c for c in self.extra.get("fandom_categories", {}).get(wiki, [])]
                pages, used = fandom_category_pages(api, list(dict.fromkeys(cats)))
            except FetchError as e:
                self.problem(source, f"category listing failed: {e}")
                rec["error"] = str(e)[:200]
                continue
            rec["categories"] = used
            rec["pages"] = len(pages)
            if not pages:
                self.problem(source, "no film / TV category found")
                continue
            try:
                ids = fandom_imdb_ids(api, list(pages))
                years = fandom_page_years(api, list(pages))
            except FetchError as e:
                self.problem(source, f"IMDb link lookup failed: {e}")
                ids, years = {}, {}
            n_imdb = 0
            for title, cat in pages.items():
                tts = ids.get(title) or []
                ctitle, year, kind = clean_work_title(title)
                year = year or years.get(title)
                if kind == "collection":
                    continue                     # «X (film series)» — an overview page, not a work
                if kind is None:
                    kind = "tv" if re.search(r"series|shows?|television|tv", cat, re.I) else (
                        "movie" if re.search(r"films?|movies?|shorts?|specials?|one-shots", cat, re.I) else None)
                if len(tts) == 1:
                    n_imdb += 1
                    self.add(imdb=tts[0], source=source, evidence=f"{cat}: {title} (IMDb link)", title=ctitle,
                             year=year, kind=kind)
                else:
                    self.fandom_pending.append({"wiki": wiki, "title": title, "clean": ctitle, "year": year,
                                                "kind": kind, "category": cat, "imdb_candidates": tts})
            rec["imdb_linked"] = n_imdb
            self.say(f"  {self.key} {source}: {len(used)} categories, {len(pages)} pages, {n_imdb} with an IMDb link")

    def resolve_fandom_pending(self, es: dict):
        """Fandom pages without exactly one IMDb link: exact title (+year, type) match to a work another source
        already found for this franchise; otherwise unresolved (never a new member by title alone)."""
        index: dict[str, list[Cand]] = {}
        class_roots({t for e in es.values() for t in e.get("claims", {}).get("P31", [])})
        screen_of: dict[str, set] = {}
        for c in self.cands.values():
            # only works another source places in the franchise firmly: a strong Wikidata link, the TMDB
            # collection, or Wikipedia in two or more languages (not a single article mention)
            firm = (STRONG in c.strength or "tmdb_collection" in c.sources
                    or sum(1 for s in c.sources if s.startswith("wikipedia:")) >= 2)
            if not firm:
                continue
            e = es.get(c.qid) if c.qid else None
            if e:
                cl = classify(e.get("claims", {}).get("P31", []))
                if not cl["screen"] or cl["exclude"]:
                    continue
                screen_of[c.key] = cl["screen"]
            names = set(filter(None, [c.title_hint] + ([e["labels"].get(l) for l in ("en", "mul")] if e else [])))
            for n in names:
                index.setdefault(norm_title(n), []).append(c)
        for p in self.fandom_pending:
            source = f"fandom:{p['wiki']}"
            hits = []
            for c in index.get(norm_title(p["clean"]), []):
                if c in hits:
                    continue
                y = c.year_hint or (earliest_year(es[c.qid]) if c.qid and c.qid in es else None)
                if p["year"] and y and abs(y - p["year"]) > 1:
                    continue
                sc = screen_of.get(c.key, set())
                if p["kind"] == "tv" and sc and not sc & {"series", "tvprog", "special"}:
                    continue
                if p["kind"] == "movie" and sc and not sc & {"film", "short", "documentary", "special"}:
                    continue
                hits.append(c)
            if p["imdb_candidates"]:
                hits = [c for c in hits if c.imdb in p["imdb_candidates"]] or hits
            if len(hits) == 1:
                self.add(qid=hits[0].qid, imdb=hits[0].imdb, source=source,
                         evidence=f"{p['category']}: {p['title']} (title match)")
                continue
            if len(hits) > 1:
                self.unresolved.append({"source": source, "title": p["title"], "year": p["year"],
                                        "reason": "title matches several works: " +
                                                  ", ".join(f"{c.qid or c.imdb}" for c in hits[:6])})
                continue
            # A fandom page is never turned into a NEW member by its title alone (a TMDB title search matched
            # unrelated works — «The Circus», and the horror-holiday wiki's 340 films).  Only pages with their own
            # IMDb link, or whose title (+year, type) equals a work another source already placed in this
            # franchise, count.  The rest is reported.
            self.unresolved.append({"source": source, "title": p["title"], "year": p["year"],
                                    "reason": "no IMDb link on the page and no other source has this title "
                                              "(title-only matches are not accepted)"})

    # --- 6. finalize ----------------------------------------------------------------------
    def finalize(self) -> list[dict]:
        # IMDb-only candidates (fandom / TMDB) → their Wikidata item
        imdb_only = [c.imdb for c in self.cands.values() if not c.qid and c.imdb]
        if imdb_only:
            for imdb, qs in qids_by_external("P345", imdb_only).items():
                self._get(qid=qs[0], imdb=imdb)
        es = entities([c.qid for c in self.cands.values() if c.qid])
        if self.fandom_pending:
            self.resolve_fandom_pending(es)
            imdb_only = [c.imdb for c in self.cands.values() if not c.qid and c.imdb]
            for imdb, qs in (qids_by_external("P345", imdb_only).items() if imdb_only else []):
                self._get(qid=qs[0], imdb=imdb)
            es.update(entities([c.qid for c in self.cands.values() if c.qid and c.qid not in es]))
        class_roots({t for e in es.values() for t in e.get("claims", {}).get("P31", [])})
        from_year = self.fr.get("from_year")
        members, seen_imdb = [], {}
        # a franchise whose own anchors are parodies (Scary Movie, The Naked Gun): parody is not a doubt there
        anchor_e = [es.get(q) or entities([q]).get(q) for q in self.anchor_qids]
        parody_franchise = bool(anchor_e) and sum(
            1 for e in anchor_e if e and set(e["claims"].get("P136", [])) & PARODY_GENRES) * 2 >= len(anchor_e)
        for c in list(self.cands.values()):
            e = es.get(c.qid) if c.qid else None
            if e and e.get("missing"):
                e = None
            types = (e or {}).get("claims", {}).get("P31", [])
            cls = classify(types) if e else {"screen": set(), "exclude": set(), "flags": set(), "series_like": False}
            srcs = sorted(c.sources)
            name = label(e) or c.title_hint or c.qid or c.imdb
            # a walked container is a member only when it is a work of its own (container_work, 2026-10-25)
            own_work = container_work(e["claims"], cls["screen"]) if e and c.qid in self.walked else None
            if e and not cls["screen"] and own_work != "ids":
                # not a film / series per Wikidata (person, character, game, book, franchise item …)
                if families(c.sources) - {"wikipedia", "wikidata"} and not cls["series_like"]:
                    self.excluded.append({"qid": c.qid, "imdb": c.imdb, "title": name, "sources": srcs,
                                          "reason": "not a screen work per Wikidata P31: " + ", ".join(types[:4])})
                else:
                    self.info["nonscreen_wikipedia"] += 1
                continue
            if cls["exclude"]:
                self.excluded.append({"qid": c.qid, "imdb": c.imdb, "title": name, "sources": srcs,
                                      "reason": ", ".join(sorted(cls["exclude"]))})
                continue
            if e and c.qid in self.walked and not own_work:
                continue
            imdbs = [v for v in (e or {}).get("claims", {}).get("P345", []) if re.fullmatch(r"tt\d+", v)]
            imdb = c.imdb if c.imdb and (not imdbs or c.imdb in imdbs) else (imdbs[0] if imdbs else c.imdb)
            if e and not imdb:
                for prop, mt in (("P4947", "movie"), ("P4983", "tv")):
                    for tid in e["claims"].get(prop, []):
                        try:
                            raw = tmdb_call(tmdb.movie_raw if mt == "movie" else tmdb.tv_raw, self.conn, int(tid))
                        except Exception:                           # noqa: BLE001
                            raw = None
                        imdb = imdb or (raw or {}).get("imdb_id") or ((raw or {}).get("external_ids") or {}).get(
                            "imdb_id")
            if not imdb:
                self.unresolved.append({"source": ",".join(srcs), "title": name, "qid": c.qid,
                                        "year": earliest_year(e) if e else c.year_hint,
                                        "reason": "no IMDb id (Wikidata P345 / TMDB)"})
                continue
            year = (earliest_year(e) if e else None) or c.year_hint
            flags = {f for f in c.flags if not f.startswith("_")}
            flags |= cls["flags"]
            if "pilot" in cls["screen"]:
                flags.add("pilot")
            if "_title_matched" in c.flags and families(c.sources) == {"fandom"}:
                flags.add("title_match_only")
            elif families(c.sources) == {"fandom"}:
                flags.add("fandom_only")                 # fandom film categories also list cameo / reference films
            if c.sources.keys() == {"wikidata"} and c.strength == {WEAK}:
                flags.add("weak_link_only")
            wp = [s for s in c.sources if s.startswith("wikipedia:")]
            if wp and families(c.sources) == {"wikipedia"} and len(wp) == 1 and not any(
                    ev.startswith("list ") for ev in c.sources[wp[0]]):
                flags.add("one_wikipedia_only")          # one language and nothing else (curated lists exempt)
            if set((e or {}).get("claims", {}).get("P136", [])) & PARODY_GENRES and not parody_franchise:
                flags.add("parody")
            if len(imdbs) > 1:
                flags.add("several IMDb ids: " + " ".join(imdbs))
            m = {"imdb_id": imdb, "qid": c.qid, "title": label(e, ("en",)) if e else c.title_hint,
                 "title_ru": (e or {}).get("labels", {}).get("ru"), "year": year, "sources": srcs,
                 "evidence": c.sources, "flags": sorted(flags), "screen": sorted(cls["screen"]),
                 "genres": (e or {}).get("claims", {}).get("P136", []), "tmdb_hint": c.tmdb_hint,
                 "kind_hint": c.kind_hint, "strength": sorted(c.strength)}
            if imdb in seen_imdb:                     # two Wikidata items with one IMDb id: merge
                prev = seen_imdb[imdb]
                prev["sources"] = sorted(set(prev["sources"]) | set(srcs))
                for k2, v2 in c.sources.items():
                    prev["evidence"].setdefault(k2, []).extend(v2)
                prev["flags"] = sorted(set(prev["flags"]) | flags)
                continue
            seen_imdb[imdb] = m
            members.append(m)
        self.tmdb_enrich(members)
        out = []
        review_list = set(self.fr.get("review", []))
        confirm_list = set(self.fr.get("confirm", []))
        for m in members:
            if from_year and m["year"] and m["year"] < from_year:
                self.dropped_years.append({"imdb_id": m["imdb_id"], "title": m["title"], "year": m["year"]})
                continue
            flags = set(m["flags"])
            if m["kind"] == "documentary" and not ("strong" in m["strength"] or "tmdb_collection" in m["sources"]):
                flags.add("documentary_unofficial")
            if m["imdb_id"] in review_list:      # franchises.toml `review`: fan works / unofficial — never raised
                flags.add("listed_review")
            elif m["imdb_id"] in confirm_list:   # franchises.toml `confirm`: canon work a doubt flag misjudged
                flags -= {"parody", "weak_link_only", "one_wikipedia_only", "fandom_only", "title_match_only"}
            m["flags"] = sorted(flags)
            c = Cand(key=m["imdb_id"], sources={s: [] for s in m["sources"]}, flags=flags)
            m["confidence"] = confidence(c)
            out.append(m)
        out.sort(key=lambda m: (m["year"] or 9999, m["title"] or ""))
        # a fandom wiki whose works other sources hardly know is probably about something else
        for w in self.info.get("fandom", {}):
            mine = [m for m in out if f"fandom:{w}" in m["sources"]]
            shared = [m for m in mine if families(m["sources"]) - {"fandom"}]
            self.info["fandom"][w].update(works=len(mine), shared=len(shared))
            if len(mine) >= 10 and len(shared) < 0.25 * len(mine):
                self.problem(f"fandom:{w}", f"looks off-topic: only {len(shared)} of its {len(mine)} works are "
                                            f"known to another source — its own works are not stored")
                for m in [m for m in mine if families(m["sources"]) == {"fandom"}]:
                    out.remove(m)
                    self.excluded.append({"qid": m.get("qid"), "imdb": m["imdb_id"], "title": m["title"],
                                          "sources": m["sources"], "reason": f"only from off-topic fandom:{w}"})
        return out

    def tmdb_enrich(self, members: list[dict]):
        """TMDB id / type, kind (animation / documentary / runtime) and the Russian title fallback."""
        for m in members:
            tt, tid = (m["tmdb_hint"] or (None, None))
            if not tid:
                try:
                    f = tmdb_call(tmdb.find_by_imdb, self.conn, m["imdb_id"])
                except Exception as e:                              # noqa: BLE001
                    self.problem("tmdb", f"find {m['imdb_id']}: {e}")
                    f = {}
                if f.get("movie") and f.get("tv"):
                    want_tv = bool({"series", "tvprog"} & set(m["screen"])) or m["kind_hint"] == "tv"
                    tt, tid = ("tv", f["tv"]) if want_tv else ("movie", f["movie"])
                elif f.get("movie"):
                    tt, tid = "movie", f["movie"]
                elif f.get("tv"):
                    tt, tid = "tv", f["tv"]
            raw = None
            if tid:
                try:
                    raw = tmdb_call(tmdb.movie_raw, self.conn, tid) if tt == "movie" else tmdb_call(tmdb.tv_raw, self.conn, tid)
                except Exception as e:                              # noqa: BLE001
                    self.problem("tmdb", f"{tt} {tid}: {e}")
            m["tmdb_id"], m["tmdb_type"] = (tid, tt) if raw else (None, None)
            genres = {g["id"] for g in (raw or {}).get("genres") or []}
            runtime = (raw or {}).get("runtime") if tt == "movie" else None
            m["kind"] = kind_of(set(m["screen"]), set(m["genres"]), m["tmdb_type"], genres, runtime)
            if raw:
                m["title"] = m["title"] or raw.get("original_title") or raw.get("original_name")
                ru = raw.get("title") if tt == "movie" else raw.get("name")
                if not m["title_ru"] and ru and re.search(r"[А-Яа-яЁё]", ru):
                    m["title_ru"] = ru
                if not m["year"]:
                    d = raw.get("release_date") or raw.get("first_air_date") or ""
                    m["year"] = int(d[:4]) if d[:4].isdigit() else None
            elif not m["screen"]:
                m["flags"] = sorted(set(m["flags"]) | {"unverified"})      # IMDb id unknown to Wikidata and TMDB

    # --- run ------------------------------------------------------------------------------
    def run(self, sources=("tmdb", "wikidata", "wikipedia", "fandom")) -> dict:
        t0 = time.time()
        steps = []
        if "tmdb" in sources or "wikidata" in sources:
            steps.append(("tmdb_collection", self.tmdb_collections))
            steps.append(("game_adaptations", self.game_adaptations))
        if "wikidata" in sources:
            steps += [("wikidata", self.wikidata_seeds), ("wikidata", self.wikidata_members),
                      ("wikidata", self.wikidata_umbrella), ("wikidata", self.wikidata_characters),
                      ("wikidata", self.wikidata_weak)]
        elif "wikipedia" in sources or "fandom" in sources:
            steps.append(("wikidata", self.wikidata_seeds))
        if "wikipedia" in sources:
            steps.append(("wikipedia", self.wikipedia))
        if "fandom" in sources:
            steps.append(("fandom", self.fandom))
        for name, fn in steps:
            try:
                fn()
            except FetchError as e:
                self.problem(name, f"{fn.__name__} failed: {e}")
            except Exception as e:                                  # noqa: BLE001 — report and go on
                self.problem(name, f"{fn.__name__} crashed: {type(e).__name__}: {e}")
        members = self.finalize()
        return {"key": self.key, "name": self.fr.get("name"), "from_year": self.fr.get("from_year"),
                "resolved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "seconds": round(time.time() - t0, 1), "sources_run": list(sources), "members": members,
                "unresolved": self.unresolved, "excluded": self.excluded, "dropped_years": self.dropped_years,
                "problems": self.problems, "info": self.info}


# ----------------------------------------------------------------------------------------------
# Wikipedia navboxes and categories, fandom categories and IMDb links
# ----------------------------------------------------------------------------------------------

_NAV_GROUP = re.compile(r"^\s*\|\s*(group|группа|група|title|заголовок|above|вверху|below|внизу)\d*\s*=(.*)$", re.I)
_NAV_LIST = re.compile(r"^\s*\|\s*(list|список)\d*\s*=", re.I)


def navbox_links(wikitext: str, lang: str) -> list[tuple[str, str, str]]:
    """[(lang, title, group label)] from a navbox; groups about games / comics / books / people are skipped."""
    deny = re.compile(SECTION_DENY.get(lang, SECTION_DENY["en"]) + r"|people|persons|люди|персоналии|особи",
                      re.I)
    text = _REF.sub("", _COMMENT.sub("", re.sub(r"<noinclude>.*?</noinclude>", "", wikitext or "", flags=re.S)))
    out, group = [], ""
    for line in text.splitlines():
        m = _NAV_GROUP.match(line)
        if m:
            group = clean_heading(m.group(2))
            if m.group(1).lower() in ("title", "заголовок"):
                group = ""
            body = m.group(2)
        else:
            body = line
        if group and deny.search(group):
            continue
        for lg, t in wikitext_links(body, lang):
            out.append((lg, t, group))
    return out


CAT_SUB_ALLOW = re.compile(r"film|movie|televis|\btv\b|series|animat|anime|short|special|фильм|сериал|мульт|аниме|"
                           r"экраниз|фільм|серіал|екраніз|кино|кіно", re.I)
CAT_SUB_DENY = re.compile(r"character|actor|actress|\bcast\b|crew|game|comic|novel|book|music|soundtrack|album|song|"
                          r"image|location|organi[sz]|toy|merchandis|attraction|\brides?\b|episode|award|fiction|"
                          r"documentar|people|director|writer|персонаж|актёр|актер|игр|комикс|роман|книг|музык|"
                          r"альбом|песн|изображ|локац|организац|игрушк|аттракцион|эпизод|вымышлен|документ|"
                          r"режисс|актор|ігр|комікс|музик|пісн|зображ|епізод|вигадан|режисер|сценарист|"
                          r"by year|по годам|за роками|\b\d{4}\b|unofficial|parod|\bfan\b|неофициальн|пароди|"
                          r"фанат|неофіційн|пароді", re.I)
CATEGORY_PAGE_CAP = 5000


def walk_category(lang: str, root: str, depth: int = 2) -> tuple[list[tuple[str, str]], list[str]]:
    """Pages (ns 0) of a category and of its film / TV subcategories (2 levels).  → ([(title, category)], used)"""
    api = wiki_api(lang)
    pages, used, seen = [], [], {root}
    queue = [(root, 0)]
    while queue and len(pages) < CATEGORY_PAGE_CAP:
        cat, d = queue.pop(0)
        used.append(cat)
        for body in mw_query(api, {"list": "categorymembers", "cmtitle": cat, "cmtype": "page|subcat",
                                   "cmlimit": "500"}):
            for mem in (body.get("query") or {}).get("categorymembers", []):
                if mem["ns"] == 14:
                    name = mem["title"].split(":", 1)[-1]
                    if (d < depth and mem["title"] not in seen and CAT_SUB_ALLOW.search(name)
                            and not CAT_SUB_DENY.search(name)):
                        seen.add(mem["title"])
                        queue.append((mem["title"], d + 1))
                elif mem["ns"] == 0:
                    pages.append((mem["title"], cat))
    return pages, used


FANDOM_SEARCH_TERMS = ("films", "movies", "television series", "TV series", "animated series", "shows", "specials",
                       "short films", "series")


def fandom_find_categories(api: str) -> list[str]:
    """Film / TV categories of a fandom wiki: category-namespace search for a few terms, filtered by name."""
    found = []
    for term in FANDOM_SEARCH_TERMS:
        for body in mw_query(api, {"list": "search", "srsearch": term, "srnamespace": "14", "srlimit": "100"},
                             max_pages=1):
            for r in (body.get("query") or {}).get("search", []):
                name = r["title"].split(":", 1)[-1]
                if fandom_category_ok(name) and r["title"] not in found:
                    found.append(r["title"])
    return found


def fandom_category_pages(api: str, cats: list[str], depth: int = 2) -> tuple[dict[str, str], list[str]]:
    """{page title: category} of the categories and their film / TV subcategories."""
    pages: dict[str, str] = {}
    used, seen = [], set(cats)
    queue = [(c if ":" in c else f"Category:{c}", 0) for c in cats]
    while queue and len(pages) < CATEGORY_PAGE_CAP:
        cat, d = queue.pop(0)
        used.append(cat)
        for body in mw_query(api, {"list": "categorymembers", "cmtitle": cat, "cmtype": "page|subcat",
                                   "cmlimit": "500"}):
            for mem in (body.get("query") or {}).get("categorymembers", []):
                if mem["ns"] == 14:
                    name = mem["title"].split(":", 1)[-1]
                    if d < depth and mem["title"] not in seen and fandom_category_ok(name):
                        seen.add(mem["title"])
                        queue.append((mem["title"], d + 1))
                elif mem["ns"] == 0:
                    pages.setdefault(mem["title"], cat.split(":", 1)[-1])
    return pages, used


_TT = re.compile(r"\b(tt\d{7,8})\b")
_IMDB_TPL = re.compile(r"\{\{\s*imdb[ _]?(?:title)?\s*\|\s*(?:id\s*=\s*)?(?:tt)?(\d{7,8})", re.I)
_IMDB_FIELD = re.compile(r"\|\s*imdb(?:_id)?\s*=\s*(?:tt)?(\d{7,8})", re.I)


_INFOBOX_YEAR = re.compile(r"^\s*\|\s*(?:release(?:d|_date| date)?|premiere(?:d)?|first[ _]aired|air[ _]?date|aired|"
                           r"date|year|released?)\s*=\s*[^\n]*?\b((?:19|20)\d\d)\b", re.I | re.M)


def fandom_page_years(api: str, titles: list[str]) -> dict[str, int]:
    """{page: year} from the infobox release / premiere field of the page (page cache, no extra requests when
    fandom_imdb_ids already read the pages)."""
    out = {}
    for t, got in wiki_contents(api, titles).items():
        if got:
            m = _INFOBOX_YEAR.search(got[1])
            if m:
                out[t] = int(m.group(1))
    return out


def fandom_imdb_ids(api: str, titles: list[str]) -> dict[str, list[str]]:
    """{page: [IMDb ids]} from the page's IMDb external links and IMDb templates / infobox fields; the ids that
    occur most often first (a page may also cite other titles)."""
    found: dict[str, dict[str, int]] = {}
    for batch in _chunks(titles, 50):
        for body in mw_query(api, {"prop": "extlinks", "titles": "|".join(batch), "ellimit": "max",
                                   "elquery": "www.imdb.com/title/"}):
            for pg in (body.get("query") or {}).get("pages", []):
                for el in pg.get("extlinks") or []:
                    url = el.get("url") or el.get("*") or ""
                    for tt in _TT.findall(url):
                        found.setdefault(pg["title"], {}).setdefault(tt, 0)
                        found[pg["title"]][tt] += 1
        for t, got in wiki_contents(api, batch).items():
            if not got:
                continue
            text = got[1]
            for rx, w in ((_IMDB_FIELD, 5), (_IMDB_TPL, 3)):
                for num in rx.findall(text):
                    tt = f"tt{num}"
                    found.setdefault(t, {}).setdefault(tt, 0)
                    found[t][tt] += w
    out = {}
    for t, d in found.items():
        best = sorted(d.items(), key=lambda kv: -kv[1])
        top = best[0][1]
        out[t] = [tt for tt, n in best if n == top]           # ties → several → resolved by title later
    return out


# ----------------------------------------------------------------------------------------------
# storage
# ----------------------------------------------------------------------------------------------

def load_config() -> list[dict]:
    with open(CONFIG, "rb") as f:
        return tomllib.load(f).get("franchise", [])


def note_of(m: dict) -> str | None:
    parts = []
    if m.get("qid"):
        parts.append(m["qid"])
    parts += [f for f in m.get("flags", [])]
    return "; ".join(parts)[:500] or None


def write_members(conn, key: str, members: list[dict]) -> dict:
    """Upsert one franchise's members (short transaction).  Rows of earlier runs that this run did not find are
    kept and marked confidence='review' with a 'stale: …' note (nothing is deleted)."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    found = {m["imdb_id"] for m in members}
    with conn:
        existing = {r["imdb_id"]: r["note"] for r in conn.execute(
            "SELECT imdb_id, note FROM franchise_members WHERE franchise_key = ?", (key,))}
        # rows added by hand or by an owner's rule (sources manual / owner_rule_*) are not resolve's to find: never stale
        hand = {r["imdb_id"] for r in conn.execute(
            "SELECT imdb_id, sources FROM franchise_members WHERE franchise_key = ?", (key,))
            if any(s == "manual" or s.startswith("owner_rule") for s in json.loads(r["sources"] or "[]"))}
        for m in members:
            conn.execute(
                """INSERT INTO franchise_members (franchise_key, imdb_id, tmdb_id, tmdb_type, kind, title, title_ru,
                       year, sources, confidence, note, checked_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                   ON CONFLICT (franchise_key, imdb_id) DO UPDATE SET tmdb_id = excluded.tmdb_id,
                       tmdb_type = excluded.tmdb_type, kind = excluded.kind, title = excluded.title,
                       title_ru = excluded.title_ru, year = excluded.year, sources = excluded.sources,
                       confidence = excluded.confidence, note = excluded.note, checked_at = excluded.checked_at""",
                (key, m["imdb_id"], m.get("tmdb_id"), m.get("tmdb_type"), m["kind"], m.get("title"),
                 m.get("title_ru"), m.get("year"), json.dumps(m["sources"], ensure_ascii=False), m["confidence"],
                 note_of(m)))
        stale = [i for i in existing if i not in found and i not in hand]
        for imdb in stale:
            note = existing[imdb] or ""
            if not note.startswith("stale:"):
                conn.execute("UPDATE franchise_members SET confidence = 'review', note = ? "
                             "WHERE franchise_key = ? AND imdb_id = ?",
                             (f"stale: not found by resolve on {today}; {note}"[:500], key, imdb))
    return {"written": len(members), "stale": len(stale), "new": len(found - set(existing))}


def save_resolved(result: dict):
    RESOLVED.mkdir(parents=True, exist_ok=True)
    (RESOLVED / f"{result['key']}.json").write_text(json.dumps(result, ensure_ascii=False, indent=1, default=list),
                                                     encoding="utf-8")


def load_resolved(key: str) -> dict | None:
    p = RESOLVED / f"{key}.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ----------------------------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------------------------

_print_lock = threading.Lock()


def _say(msg: str):
    with _print_lock:
        print(msg, flush=True)


def resolve_one(fr: dict, sources) -> dict:
    conn = db.connect()
    try:
        res = Resolver(fr, conn, log=_say).run(sources)
        save_resolved(res)
        w = write_members(conn, fr["key"], res["members"])
        res["write"] = w
        save_resolved(res)
        return res
    finally:
        conn.close()


def prefetch_wikipedia(frs: list[dict]):
    """Wikipedia is the slow source (Wikimedia rate limit): fetch every franchise's articles and navboxes in
    shared batches of 50 pages first; the per-franchise step then reads them from the page cache."""
    conn = db.connect()
    want: dict[str, list[str]] = {lg: [] for lg in LANGS}
    try:
        for fr in frs:
            r = Resolver(fr, conn, log=lambda *_: None)
            try:
                r.tmdb_collections()
                r.wikidata_seeds()
                for lg, t in r.wiki_targets().items():
                    want[lg] += [a for a, _, _ in t["articles"]] + t["templates"]
            except Exception as e:                                  # noqa: BLE001 — the real run reports it
                _say(f"  prefetch: {fr['key']} skipped ({type(e).__name__}: {e})")
    finally:
        conn.close()
    for lg, titles in want.items():
        titles = list(dict.fromkeys(titles))
        try:
            wiki_contents(wiki_api(lg), titles)
            _say(f"  prefetch wikipedia:{lg}: {len(titles)} pages")
        except FetchError as e:
            _say(f"  prefetch wikipedia:{lg} failed: {e}")


def cmd_resolve(args):
    set_refresh(args.refresh)
    frs = load_config()
    if args.only:
        want = set(args.only.split(","))
        unknown = want - {f["key"] for f in frs}
        if unknown:
            sys.exit(f"unknown franchise key(s): {', '.join(sorted(unknown))}")
        frs = [f for f in frs if f["key"] in want]
    elif not args.all_scopes:
        skipped = [f["key"] for f in frs if f.get("scope") == "none"]
        frs = [f for f in frs if f.get("scope") != "none"]
        if skipped:
            _say(f"skipping scope='none' (owner: no priority): {', '.join(skipped)}  (--all-scopes to include)")
    sources = tuple(args.sources.split(",")) if args.sources else ("tmdb", "wikidata", "wikipedia", "fandom")
    if not contact():
        _say("note: no FILMS_CONTACT set — Wikimedia treats the client as unidentified (10 requests/min), "
             "Wikipedia steps will be slow; see docs/franchises.md")
    _say(f"resolving {len(frs)} franchise(s), sources: {', '.join(sources)}")
    if "wikipedia" in sources and len(frs) > 1:
        prefetch_wikipedia(frs)
    failed = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        futs = {ex.submit(resolve_one, fr, sources): fr for fr in frs}
        for fut in as_completed(futs):
            fr = futs[fut]
            try:
                r = fut.result()
            except Exception as e:                                  # noqa: BLE001
                failed.append(fr["key"])
                _say(f"FAILED {fr['key']}: {type(e).__name__}: {e}")
                continue
            conf = {}
            for m in r["members"]:
                conf[m["confidence"]] = conf.get(m["confidence"], 0) + 1
            _say(f"{fr['key']:<22} {len(r['members']):>4} members {conf}  unresolved {len(r['unresolved'])}  "
                 f"excluded {len(r['excluded'])}  problems {len(r['problems'])}  "
                 f"(new {r['write']['new']}, stale {r['write']['stale']}, {r['seconds']} s)")
    _say(f"HTTP: {STATS}")
    if failed:
        _say(f"failed: {', '.join(failed)}")
        sys.exit(1)


KINDS = ("film", "animated film", "series", "animated series", "special", "short", "documentary")


def cmd_report(args):
    conn = db.connect()
    frs = load_config()
    if args.only:
        frs = [f for f in frs if f["key"] in set(args.only.split(","))]
    tot_src: dict[str, int] = {}
    only_src: dict[str, int] = {}
    print(f"{'franchise':<22} {'all':>4} " + " ".join(f"{k[:9]:>9}" for k in KINDS) +
          "  confirmed single review (stale) | unres excl problems")
    review_rows, problems = [], []
    for fr in frs:
        rows = conn.execute("SELECT * FROM franchise_members WHERE franchise_key = ? ORDER BY year",
                            (fr["key"],)).fetchall()
        res = load_resolved(fr["key"]) or {}
        kinds = {k: 0 for k in KINDS}
        conf = {"confirmed": 0, "single_source": 0, "review": 0}
        stale = sum(1 for r in rows if (r["note"] or "").startswith("stale:"))
        for r in rows:
            kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
            conf[r["confidence"]] = conf.get(r["confidence"], 0) + 1
            srcs = json.loads(r["sources"] or "[]")
            for s in srcs:
                tot_src[s.split(":")[0]] = tot_src.get(s.split(":")[0], 0) + 1
            fams = families(srcs)
            if len(fams) == 1:
                f1 = next(iter(fams))
                only_src[f1] = only_src.get(f1, 0) + 1
            if r["confidence"] == "review":
                review_rows.append((fr["key"], r))
        print(f"{fr['key']:<22} {len(rows):>4} " + " ".join(f"{kinds.get(k, 0):>9}" for k in KINDS) +
              f"  {conf['confirmed']:>9} {conf['single_source']:>6} {conf['review']:>6} {'(' + str(stale) + ')':>7} | "
              f"{len(res.get('unresolved', [])):>5} {len(res.get('excluded', [])):>4} {len(res.get('problems', [])):>8}"
              + ("" if res else "  (not resolved yet)"))
        for p in res.get("problems", []):
            problems.append((fr["key"], p))
    print("\nworks per source family (a work counts once per family it appears in):", tot_src)
    print("works known to ONE family only:", only_src)
    if args.review:
        print(f"\nitems needing review ({len(review_rows)}):")
        for key, r in review_rows:
            print(f"  {key:<20} {r['imdb_id']:<11} {r['year'] or '':<5} {r['kind']:<15} {r['title']}  "
                  f"[{', '.join(json.loads(r['sources']))}]  {r['note'] or ''}")
    print(f"\nproblems ({len(problems)}):")
    for key, p in problems[:400]:
        print(f"  {key:<20} {p['source']}: {p['message'][:200]}")
    if args.source_diff:
        print("\nper-source contribution (works only that family found):")
        for fr in frs:
            res = load_resolved(fr["key"])
            if not res:
                continue
            by: dict[str, list] = {}
            for m in res["members"]:
                fams = families(m["sources"])
                if len(fams) == 1:
                    by.setdefault(next(iter(fams)), []).append(f"{m['title']} ({m['year']})")
            if by:
                print(f"  {fr['key']}:")
                for f1, items in sorted(by.items()):
                    print(f"    only {f1}: {len(items)}: " + "; ".join(items[:25]) + (" …" if len(items) > 25 else ""))


def cmd_show(args):
    res = load_resolved(args.key)
    if not res:
        sys.exit(f"{args.key}: not resolved yet (run `resolve --only {args.key}`)")
    print(f"{res['key']} — {res['name']}  (resolved {res['resolved_at']}, {len(res['members'])} members)")
    print("seeds:", "; ".join(f"{q} {s['label']} (depth {s['depth']}, {', '.join(s['via'])[:80]})"
                              for q, s in res["info"].get("seeds", {}).items()))
    print("walked:", len(res["info"].get("walked", {})), "containers")
    for m in res["members"]:
        if args.confidence and m["confidence"] != args.confidence:
            continue
        print(f"  {m['imdb_id']:<11} {m['year'] or '':<5} {m['kind']:<15} {m['confidence']:<13} "
              f"{m['title']} / {m['title_ru'] or ''}  [{', '.join(m['sources'])}] {'; '.join(m['flags'])}")
        if args.evidence:
            for s, ev in m["evidence"].items():
                print(f"        {s}: {' | '.join(ev[:4])}")
    for name in ("unresolved", "excluded", "dropped_years", "problems"):
        items = res.get(name) or []
        print(f"{name} ({len(items)}):")
        for it in items[: args.limit]:
            print("   ", json.dumps(it, ensure_ascii=False)[:300])
    print("fandom:", json.dumps(res["info"].get("fandom"), ensure_ascii=False)[:2000])
    print("wikipedia pages:", json.dumps(res["info"].get("pages"), ensure_ascii=False)[:2000])
    print("wikipedia categories:", json.dumps(res["info"].get("categories"), ensure_ascii=False)[:2000])


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m films.franchise", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("resolve", help="collect every franchise's works into franchise_members")
    r.add_argument("--only", help="comma-separated franchise keys")
    r.add_argument("--refresh", action="store_true", help="refetch everything (ignore the cache) once per run")
    r.add_argument("--workers", type=int, default=2, help="franchises resolved in parallel (default 2)")
    r.add_argument("--sources", help="debug: subset of tmdb,wikidata,wikipedia,fandom")
    r.add_argument("--all-scopes", action="store_true", help="also franchises with scope = 'none'")
    p = sub.add_parser("report", help="counts per franchise / kind / confidence, per source, review items")
    p.add_argument("--only")
    p.add_argument("--review", action="store_true", help="list every item needing review")
    p.add_argument("--source-diff", action="store_true", help="works only one source family knows")
    s = sub.add_parser("show", help="one franchise's members with sources, evidence, unresolved, problems")
    s.add_argument("key")
    s.add_argument("--evidence", action="store_true")
    s.add_argument("--confidence")
    s.add_argument("--limit", type=int, default=60)
    args = ap.parse_args(argv)
    {"resolve": cmd_resolve, "report": cmd_report, "show": cmd_show}[args.cmd](args)


if __name__ == "__main__":
    main()
