"""IMDb technical specifications, IMAX verdict, box office, poster and UA akas -- plain HTTP, no browser.

www.imdb.com title pages sit behind an AWS WAF (HTTP 202 challenge) for plain HTTP.  IMDb's own GraphQL
API (the one its web site / apps call) answers plain HTTP requests as long as the request carries the
client-name header the web site sends; no cookies, no token, no challenge:

    POST https://caching.graphql.imdb.com/
    Content-Type: application/json
    x-imdb-client-name: imdb-web-next
    {"query": "query($ids: [ID!]!) { titles(ids: $ids) { ... } }", "variables": {"ids": ["tt…", …]}}

(without `x-imdb-client-name` -- or a `Referer: https://www.imdb.com/` -- the endpoint answers 403.)
Every response carries IMDb's disclaimer: "Public, commercial, and/or non-private use of the IMDb data
provided by this API is not allowed" -- this project is personal; see docs/imdb-specs.md about the
public GitHub backup.

Usage:
    python -X utf8 -m films.sources.imdb_specs run [--limit N] [--max-age-days D] [--ids tt… …] [--no-fetch]
    python -X utf8 -m films.sources.imdb_specs show tt15239678 [tt…]     # parsed verdict, no DB writes

What `run` does for every title with an IMDb id (movie: titles.imdb_id; season: titles.series_imdb_id --
IMDb has no season entity, so specs / poster are series-level and written to every season row):
  * di_resolution     <- "Digital Intermediate" process (see di_resolution())
  * aspect_ratios     <- JSON list of display strings, e.g. ["1.43 : 1 (IMAX 70mm & Dual Laser, some scenes)", …]
  * sound_mix         <- JSON list of display strings, e.g. ["Dolby Atmos", "IMAX 6-Track", "DTS (DTS: X)"]
  * is_imax, imax_source <- imax_verdict()
  * budget_usd / box_office_usd <- only where NULL, only USD, only movies (see apply())
  * poster_url (+ poster_source='imdb', poster_checked_at) <- only where poster_url is NULL; a full run then calls
    films.posters.fill: no IMDb poster -> Rotten Tomatoes -> TMDB -> the tracker page image (owner, 2026-10-25)
  * title_uk (+ title_uk_source) <- the IMDb Ukrainian aka (pick_uk_title), only where title_uk is empty: owner,
    2026-10-25 — IMDb is the fallback while TMDB has no Ukrainian title (TMDB's title replaces it in films.enrich).
    `uk` fills that alone for every title from the cache / the dataset akas, without a full specs run.

Raw responses are cached per tconst in data/cache/imdb_specs/{tconst}.json.gz (the whole GraphQL node --
specs, box office, akas, keywords, companies, release dates … -- so new fields can be extracted later
without re-fetching).  The last run's report is written to data/cache/imdb_specs/last_run.json.
"""
from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from films import db
from films.sources import imdb as imdb_ds

GQL_URL = "https://caching.graphql.imdb.com/"
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": imdb_ds.UA,
    "x-imdb-client-name": "imdb-web-next",
    "Referer": "https://www.imdb.com/",
}
CACHE_DIR = db.ROOT / "data" / "cache" / "imdb_specs"
BATCH = 25                 # ids per GraphQL request (100 also works; 25 keeps responses ~400 KB)
MIN_INTERVAL_S = 1.0       # <= 1 request / s (owner's limit is 2 / s)
MAX_ATTEMPTS = 6
DEFAULT_MAX_AGE_DAYS = 30
# node keys added to QUERY later: a cached node without one of them is re-fetched (metadata survey 2026-10-25)
REQUIRED_NODE_KEYS = ("distributors",)

_SPEC_FIELDS = """
  aspectRatios { total items { aspectRatio attributes { text } } }
  soundMixes { total items { text attributes { text } } }
  processes { total items { process attributes { text } } }
  cameras { total items { camera attributes { text } } }
  colorations { total items { text attributes { text } } }
  filmLengths { total items { filmLength countries { text } numReels } }
  laboratories { total items { laboratory attributes { text } } }
  negativeFormats { total items { negativeFormat attributes { text } } }
  printedFormats { total items { printedFormat attributes { text } } }
"""

QUERY = """
query($ids: [ID!]!) { titles(ids: $ids) {
  id
  meta { canonicalId publicationStatus }
  titleText { text } originalTitleText { text } titleType { id text } releaseYear { year endYear }
  releaseDate { day month year country { id } }
  runtime { seconds }
  countriesOfOrigin { countries { id text } }
  spokenLanguages { spokenLanguages { id text } }
  certificate { rating country { id } }
  ratingsSummary { aggregateRating voteCount }
  primaryImage { url width height }
  productionBudget { budget { amount currency } }
  lifetimeGross_ww: lifetimeGross(boxOfficeArea: WORLDWIDE) { total { amount currency } }
  lifetimeGross_dom: lifetimeGross(boxOfficeArea: DOMESTIC) { total { amount currency } }
  openingWeekendGross(boxOfficeArea: DOMESTIC) { gross { total { amount currency } } weekendEndDate theaterCount }
  akas(first: 250) { total edges { node { text country { id text } language { id text } attributes { text } } } }
  releaseDates(first: 250) { total edges { node { day month year country { id } attributes { text } } } }
  keywords(first: 50) { total edges { node { text } } }
  filmingLocations(first: 25) { total edges { node { text } } }
  companyCredits(first: 50) { total edges { node { company { id companyText { text } } category { id text } countries { id } attributes { text } } } }
  distributors: companyCredits(first: 250, filter: { categories: ["distribution"] }) { total edges { node { company { id companyText { text } } countries { id } attributes { text } } } }
  otherCompanies: companyCredits(first: 250, filter: { categories: ["miscellaneous"] }) { total edges { node { company { id companyText { text } } category { id text } countries { id } attributes { text } } } }
  episodes { seasons { number } episodes(first: 1) { total } }
  series { series { id } }
  technicalSpecifications { %s }
} }
""" % _SPEC_FIELDS

_last_request = 0.0


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ----------------------------------------------------------------------------------------------
# network + cache
# ----------------------------------------------------------------------------------------------

def _post(ids: list[str], log=print, query: str | None = None) -> dict:
    """One GraphQL request for up to BATCH ids -> {id: node}.  Rate-limited, retried with backoff."""
    global _last_request
    last_err = None
    for attempt in range(MAX_ATTEMPTS):
        wait = _last_request + MIN_INTERVAL_S - time.time()
        if wait > 0:
            time.sleep(wait)
        _last_request = time.time()
        try:
            r = requests.post(GQL_URL, json={"query": query or QUERY, "variables": {"ids": ids}}, headers=HEADERS, timeout=60)
            if r.status_code == 200:
                j = r.json()
                nodes = (j.get("data") or {}).get("titles")
                if nodes is None:
                    raise RuntimeError(f"no data: {json.dumps(j.get('errors'), ensure_ascii=False)[:500]}")
                if j.get("errors"):
                    # partial errors: keep the data, but say so (never silently)
                    log(f"    GraphQL partial errors for {ids[0]}…: {json.dumps(j['errors'], ensure_ascii=False)[:300]}")
                out = {}
                for asked, node in zip(ids, nodes):
                    out[asked] = node
                return out
            last_err = f"HTTP {r.status_code}: {r.text[:200]!r}"
            if r.status_code in (400, 401, 403) and attempt >= 1:
                break  # not transient: query rejected / access changed
        except (requests.RequestException, ValueError, RuntimeError) as e:
            last_err = f"{type(e).__name__}: {e}"
        backoff = min(120, 2 ** (attempt + 1))
        log(f"    request failed ({last_err}); retry in {backoff} s")
        time.sleep(backoff)
    raise RuntimeError(f"IMDb GraphQL failed for {len(ids)} ids ({ids[0]}…): {last_err}")


def _cache_path(tconst: str) -> Path:
    return CACHE_DIR / f"{tconst}.json.gz"


def load_cached(tconst: str, max_age_days: float | None = None) -> dict | None:
    p = _cache_path(tconst)
    if not p.exists():
        return None
    if max_age_days is not None and time.time() - p.stat().st_mtime > max_age_days * 86400:
        return None
    with gzip.open(p, "rt", encoding="utf-8") as f:
        return json.load(f)


def _save(rec: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    p = _cache_path(rec["tconst"])
    tmp = p.with_suffix(".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False)
    tmp.replace(p)


def _status(node: dict | None) -> str:
    if not node or not node.get("titleText"):
        return "not_found"
    return "ok"


def ensure(tconsts: list[str], max_age_days: float | None = DEFAULT_MAX_AGE_DAYS, fetch: bool = True,
           log=print) -> tuple[dict, list]:
    """Return ({tconst: cache record}, failures).  Fetches what is missing / older than max_age_days.

    A record is {"tconst", "fetched_at", "status": ok|not_found|redirect, "canonical": tt…|None, "node": {...}}.
    If IMDb answers with a different meta.canonicalId (merged duplicate), the canonical title is fetched too
    and its node is used; the record keeps both ids so the redirect is reported (titles.imdb_id is not changed).
    """
    recs: dict[str, dict] = {}
    todo = []
    for t in tconsts:
        rec = load_cached(t, max_age_days)
        if rec is not None and fetch and rec.get("node") and any(k not in rec["node"] for k in REQUIRED_NODE_KEYS):
            rec = None   # cached before a field was added to QUERY
        if rec is None:
            if fetch:
                todo.append(t)
            else:
                old = load_cached(t, None)
                if old is not None:
                    recs[t] = old
        else:
            recs[t] = rec
    failures = []
    if todo:
        log(f"  fetching {len(todo)} tconsts in {(len(todo) + BATCH - 1) // BATCH} requests "
            f"({len(recs)} from cache)")
    for i in range(0, len(todo), BATCH):
        chunk = todo[i:i + BATCH]
        try:
            nodes = _post(chunk, log=log)
        except RuntimeError as e:
            log(f"  !! {e}")
            failures += [(t, str(e)) for t in chunk]
            continue
        redirects = {}
        for t in chunk:
            node = nodes.get(t)
            canon = ((node or {}).get("meta") or {}).get("canonicalId")
            rec = {"tconst": t, "fetched_at": _now(), "status": _status(node), "canonical": None, "node": node}
            if canon and canon != t:
                rec["canonical"] = canon
                redirects[t] = canon
            recs[t] = rec
        if redirects:
            log(f"    redirects: {redirects}")
            try:
                cn = _post(sorted(set(redirects.values())), log=log)
                for t, c in redirects.items():
                    recs[t]["status"] = "redirect"
                    recs[t]["node_requested"] = recs[t]["node"]
                    recs[t]["node"] = cn.get(c)
            except RuntimeError as e:
                log(f"  !! redirect fetch failed: {e}")
                failures += [(t, f"redirect {c}: {e}") for t, c in redirects.items()]
        for t in chunk:
            _save(recs[t])
        done = min(i + BATCH, len(todo))
        if done % (BATCH * 10) == 0 or done == len(todo):
            log(f"    {done}/{len(todo)}")
    return recs, failures


OTHER_COMPANIES_QUERY = """
query($ids: [ID!]!) { titles(ids: $ids) { id
  otherCompanies: companyCredits(first: 250, filter: { categories: ["miscellaneous"] }) { total edges { node { company { id companyText { text } } category { id text } countries { id } attributes { text } } } }
} }
"""


def ensure_other_companies(tconsts: list[str], log=print) -> dict:
    """Back-fill node.otherCompanies into cache records fetched before 2026-10-25 whose companyCredits list was cut
    at 50 (one small query per 25 ids instead of re-fetching whole nodes).  -> counters."""
    todo = []
    for t in tconsts:
        rec = load_cached(t)
        node = (rec or {}).get("node")
        if not node or "otherCompanies" in node:
            continue
        cc = node.get("companyCredits") or {}
        if (cc.get("total") or 0) > len(cc.get("edges") or []):
            todo.append(t)
    st = {"checked": len(tconsts), "fetched": 0, "failed": 0}
    for i in range(0, len(todo), BATCH):
        chunk = todo[i:i + BATCH]
        try:
            nodes = _post(chunk, log=log, query=OTHER_COMPANIES_QUERY)
        except RuntimeError as e:
            log(f"  !! {e}")
            st["failed"] += len(chunk)
            continue
        for t in chunk:
            rec = load_cached(t)
            got = (nodes.get(t) or {}).get("otherCompanies")
            if rec and rec.get("node") is not None and got is not None:
                rec["node"]["otherCompanies"] = got
                rec["other_companies_fetched_at"] = _now()
                _save(rec)
                st["fetched"] += 1
        if (i // BATCH) % 10 == 9:
            log(f"    otherCompanies {min(i + BATCH, len(todo))}/{len(todo)}")
    return st


def refresh_imax(conn, tconsts: list[str] | None = None, log=print) -> dict:
    """Recompute only is_imax / imax_source from the cache (any age) for the given tconsts (all when None) --
    a targeted pass after a change of the IMAX rule, without a full specs run.  -> counters + changed rows."""
    targets = _targets(conn, ids=tconsts) if tconsts else _targets(conn)
    st = {"tconsts": len(targets), "rows": 0, "changed": 0, "to_1": 0, "to_0": 0, "to_null": 0}
    changed = []
    for t, rows in targets.items():
        rec = load_cached(t)
        if not rec or not rec.get("node"):
            continue
        s = summarize(rec)
        with conn:
            for r in rows:
                if suspect_link(r["kind"], r["year"], s["type"], s["year"]):
                    continue
                st["rows"] += 1
                old = conn.execute("SELECT is_imax, imax_source, title_original FROM titles WHERE id = ?", (r["id"],)).fetchone()
                if (old["is_imax"], old["imax_source"]) == (s["is_imax"], s["imax_source"]):
                    continue
                conn.execute("UPDATE titles SET is_imax = ?, imax_source = ?, updated_at = datetime('now') WHERE id = ?",
                             (s["is_imax"], s["imax_source"], r["id"]))
                st["changed"] += 1
                st[{1: "to_1", 0: "to_0", None: "to_null"}[s["is_imax"]]] += 1
                changed.append({"title_id": r["id"], "tconst": t, "title": old["title_original"], "year": r["year"],
                                "was": [old["is_imax"], old["imax_source"]], "now": [s["is_imax"], s["imax_source"]],
                                "evidence": s["imax_evidence"][:3], "notes": [n for n in s["imax_notes"] if "pre_dmr" in n]})
    st["changed_rows"] = changed
    return st


# ----------------------------------------------------------------------------------------------
# parsing (pure functions; unit-tested in tests/test_imdb_specs.py)
# ----------------------------------------------------------------------------------------------

def _attrs(item: dict) -> list[str]:
    return [a.get("text") for a in (item.get("attributes") or []) if a and a.get("text")]


def spec_items(specs: dict | None, key: str, field: str) -> list[tuple[str, list[str]]]:
    """[(value, [attributes])] of one technicalSpecifications list (empty when absent)."""
    block = (specs or {}).get(key) or {}
    return [(it.get(field), _attrs(it)) for it in (block.get("items") or []) if it and it.get(field)]


def display(value: str, attrs: list[str]) -> str:
    """IMDb's own display form: 'value (attr, attr)'."""
    return f"{value} ({', '.join(attrs)})" if attrs else value


SPEC_KEYS = {  # GraphQL list -> item field
    "aspectRatios": "aspectRatio", "soundMixes": "text", "processes": "process", "cameras": "camera",
    "colorations": "text", "laboratories": "laboratory", "negativeFormats": "negativeFormat",
    "printedFormats": "printedFormat", "filmLengths": "filmLength",
}
# lists whose presence means somebody actually filled in IMDb's technical specs for the title
# (coloration alone is filled for nearly everything and says nothing about the format)
EVIDENCE_KEYS = ("aspectRatios", "soundMixes", "processes", "cameras", "negativeFormats", "printedFormats")


def has_specs(specs: dict | None) -> bool:
    return any(spec_items(specs, k, SPEC_KEYS[k]) for k in EVIDENCE_KEYS)


# --- Digital Intermediate -------------------------------------------------------------------

_PARTIAL = re.compile(
    r"\b(scenes?|only|some|certain|select(?:ed)?|portions?|parts?|partly|segments?|sequences?|footage|shots?|"
    r"vfx|visual effects|archive|archival|trailers?|teasers?|episodes?|flashbacks?|prologue|epilogue|"
    r"versions?|releases?|imax|cinity|3-?d)\b", re.I)
# restrictions to a part of the film (a remaster entry carrying one of these is still partial)
_SCENES = re.compile(r"\b(scenes?|some|certain|portions?|parts?|partly|segments?|sequences?|footage|shots?|"
                     r"vfx|visual effects|only|imax|cinity|3-?d)\b", re.I)
# a remaster / restoration DI is the master today's 4K/HD releases are made from -> it wins over the original
_REMASTER = re.compile(r"\b(re-?master(?:ed)?|restor(?:ation|ed)|re-?scan(?:ned)?)\b", re.I)
_RES = re.compile(r"^\s*(upscaled\s+)?(\d+(?:\.\d+)?)\s*k\b", re.I)
_HD = re.compile(r"^\s*(hd|full hd|1080[pi]?|720p|high definition)\b", re.I)
_SD = re.compile(r"^\s*(sd|standard definition|480[pi]?|576[pi]?)\b", re.I)
_UHD = re.compile(r"^\s*(uhd|ultra hd|2160p)\b", re.I)


def _res_class(token: str) -> str | None:
    """Resolution class of one attribute: 'SD' / 'HD' / '2K' (2K..<3.8K) / '4K' / literal above ('4.6K', '8K').

    'upscaled 4K' -> '2K' (the master is an upscale, i.e. natively below 4K).  Non-resolution text -> None.
    """
    m = _RES.match(token)
    if m:
        k = float(m.group(2))
        if m.group(1):           # "upscaled 4K"
            return "2K"
        if k >= 3.8:
            lit = m.group(2)
            return (lit.rstrip("0").rstrip(".") if "." in lit else lit) + "K"
        return "2K"
    if _UHD.match(token):
        return "4K"
    if _HD.match(token):
        return "HD"
    if _SD.match(token):
        return "SD"
    return None


_RANK = {"SD": 0.5, "HD": 1.9, "2K": 2.0}


def _res_rank(label: str) -> float:
    return _RANK.get(label) or float(label[:-1])


def _is4k(label: str) -> bool:
    return _res_rank(label) >= 3.8


def di_resolution(specs: dict | None) -> tuple[str | None, str]:
    """Master format from 'Digital Intermediate' process entries -> (value, note).

    value: 'SD' / 'HD' / '2K' (anything 2K..<3.8K, or 'upscaled 4K') / '4K' / a higher literal ('4.6K', '8K');
    None when there is no DI entry, the DI has no resolution, an entry lists both a sub-4K and a 4K
    resolution ('2K, 4K, master format'), or unrestricted entries disagree.
    Entry priority: remaster/restoration entries (the master current releases come from) > unrestricted
    entries > restricted ones ('some scenes', '… scenes only', 'IMAX version', 'mainland China versions' …);
    a title with only restricted entries gets None.  note explains the decision (for the report).
    """
    entries = [(v, a) for v, a in spec_items(specs, "processes", "process")
               if re.search(r"digital\s+intermediate", v, re.I)]
    if not entries:
        return None, "no_di"
    remaster, full, partial = [], [], []
    for v, attrs in entries:
        # attributes are sometimes typed as one comma-joined text ("4K, 2024 remaster") -> split
        parts = [p.strip() for a in attrs for p in a.split(",") if p.strip()]
        res = [c for c in (_res_class(a) for a in parts) if c]
        res += [c for c in (_res_class(t) for t in re.findall(r"\d+(?:\.\d+)?\s*K\b|\bHD\b|\b2160p\b|\b1080p\b", v, re.I)) if c]
        other = [a for a in parts if not _res_class(a) and a.lower() not in ("master format", "source format")]
        if any(_REMASTER.search(a) for a in other) and not any(_SCENES.search(a) for a in other):
            remaster.append((res, display(v, attrs)))
        elif any(_PARTIAL.search(a) or _REMASTER.search(a) for a in other):
            partial.append((res, display(v, attrs)))
        else:
            full.append((res, display(v, attrs)))
    for group, kind in ((remaster, "remaster"), (full, "full"), (partial, "partial_only")):
        withres = [r for r, _ in group if r]
        if not withres:
            continue
        if kind == "partial_only":
            return None, "partial_only: " + " | ".join(d for _, d in group)
        if any(len({_is4k(x) for x in r}) > 1 for r in withres):
            return None, f"ambiguous_entry_{kind}: " + " | ".join(d for _, d in group)
        classes = {_is4k(r[0]) for r in withres}
        if len(classes) > 1:
            return None, f"conflict_{kind}: " + " | ".join(d for _, d in group)
        best = max((x for r in withres for x in r), key=_res_rank)
        return best, "ok" if kind == "full" else "ok_remaster"
    return None, "di_without_resolution"


def upscale_verdict(di: str | None) -> str:
    """titles.upscale_verdict from di_resolution: 'upscaled' (master 2K / HD / SD — every 4K release is a studio
    upscale), 'native' (4K or more), 'unknown' (no DI entry / no resolution / conflicting entries)."""
    if di in ("2K", "HD", "SD"):
        return "upscaled"
    return "native" if di else "unknown"


# --- IMAX -----------------------------------------------------------------------------------

# "IMAX" as a word, not preceded by "non-"/"non " (e.g. "non-IMAX scenes")
_IMAX = re.compile(r"(?<![\w-])(?<!non-)(?<!non )IMAX\b", re.I)
# home-video IMAX versions (Disney+ "IMAX Enhanced", "IMAX Blu-ray & 4K UHD") are not a theatrical release:
# that text is cut out before looking for IMAX
_IMAX_HOME = re.compile(r"(Disney\+\s*)?IMAX\s+(Enhanced(\s+Version)?|Blu-?ray)", re.I)

# order = strength of the evidence; imax_source lists every kind found, strongest first
IMAX_SOURCES = [
    ("imdb_camera", "cameras", "camera"),            # shot on IMAX cameras / "Filmed for IMAX" certified
    ("imdb_negative", "negativeFormats", "negativeFormat"),  # "65 mm (IMAX)" negative
    ("imdb_process", "processes", "process"),        # "IMAX DMR", "IMAX 3D", "Filmed for IMAX"
    ("imdb_printed", "printedFormats", "printedFormat"),     # "70 mm (IMAX)", "D-Cinema (IMAX)", "IMAX DMR blow-up"
    ("imdb_aspect", "aspectRatios", "aspectRatio"),  # "1.43 : 1 (IMAX)", "1.90 : 1 (IMAX version)"
    # sound alone counts (checked 2026-10-25 on 15 random sound-only titles: 13 confirmed IMAX by Wikipedia, 2 not
    # contradicted; the one false hit, Raiders 1981, is a re-release -> the pre-DMR rule in imax_verdict)
    ("imdb_sound", "soundMixes", "text"),            # "IMAX 6-Track", "IMAX 12-Track", "Sonics-DDP (IMAX version)"
    ("imdb_lab", "laboratories", "laboratory"),      # "… (IMAX prints)"
]


def _imax_hit(value: str, attrs: list[str]) -> str | None:
    """The text that names IMAX for a theatrical release / shoot, or None."""
    for t in [str(value)] + attrs:
        if _IMAX.search(_IMAX_HOME.sub(" ", t)):
            return t
    return None


# Evidence outside the technical specs (2026-10-25): IMDb's tech specs of most animated features (Inside Out 2,
# Moana 2, The Wild Robot, Frozen II …) never mention IMAX, but the same GraphQL node does --
#   * company credit "IMAX" (co0057495), category "Other companies", attributes "optimized for IMAX theatres"
#     (the IMAX DMR remaster of the original release) -> 'imdb_company';
#   * a release date with attribute "IMAX" / "IMAX version" / "IMAX 3D" within a year of the film's year
#     -> 'imdb_release'.
# Re-releases (Matrix 2021, Marvel 2018 IMAX re-releases, Ghibli), festival / fan / one-off screenings and IMAX as a
# re-release distributor are NOT the film going to IMAX on release: they are only noted.
IMAX_COMPANY_ID = "co0057495"
_IMAX_NOT_FIRST_RUN = re.compile(r"re-?release|re-?issue|anniversary|fan (event|screening)|screening|festival|"
                                 r"BFI\b|premiere", re.I)


# what an "IMAX" company credit has to say to count (2026-10-25, every attribute seen in the cache): optimized /
# (specially) formatted for IMAX theatres, filmed for IMAX, IMAX cameras; a bare "acknowledgement", "thanks",
# "courtesy of: <IMAX documentary footage>" (Tomorrowland, Dark Phoenix) or "specialty cameras" is only noted
_IMAX_CREDIT_COUNTS = re.compile(r"optimi[sz]e|formatted|filmed (for|with)|shot with|IMAX 3D|"
                                 r"cameras?( and| &)? lenses|camera equipment|IMAX 65mm cameras|IMAX (film )?cameras", re.I)


def _edge_nodes(node: dict | None, key: str) -> list[dict]:
    return [(e or {}).get("node") or {} for e in (((node or {}).get(key) or {}).get("edges") or [])]


def node_imax(node: dict | None) -> tuple[list[str], list[str], list[str]]:
    """IMAX evidence from the rest of the IMDb node -> (sources, evidence, notes); see the comment above."""
    sources, evidence, notes = [], [], []
    year = ((node or {}).get("releaseYear") or {}).get("year")
    # companyCredits(first: 50) is cut short for big releases (Zootopia 2: 108 credits, the IMAX one beyond the 50th);
    # otherCompanies = every "Other companies" credit (added 2026-10-25; ensure_other_companies() back-fills old caches)
    seen_credit = set()
    credits = []
    for n in _edge_nodes(node, "companyCredits") + _edge_nodes(node, "otherCompanies"):
        k = ((n.get("company") or {}).get("id"), (n.get("category") or {}).get("id"), tuple(_attrs(n)))
        if k not in seen_credit:
            seen_credit.add(k)
            credits.append(n)
    for n in credits:
        comp = n.get("company") or {}
        name = (comp.get("companyText") or {}).get("text") or ""
        if comp.get("id") != IMAX_COMPANY_ID and name.strip().upper() != "IMAX":
            continue
        cat = (n.get("category") or {}).get("id")
        attrs = _attrs(n)
        if (cat == "miscellaneous" and not any(_IMAX_NOT_FIRST_RUN.search(a) for a in attrs)
                and any(_IMAX_CREDIT_COUNTS.search(a) for a in attrs)):
            if "imdb_company" not in sources:
                sources.append("imdb_company")
            evidence.append(f"companyCredits: {display(name, attrs)} [{cat}]")
        else:
            notes.append(f"imax_company_not_counted: {display(name, attrs)} [{cat}]")
    for n in _edge_nodes(node, "distributors"):
        attrs = _attrs(n)
        name = ((n.get("company") or {}).get("companyText") or {}).get("text") or ""
        if (n.get("company") or {}).get("id") == IMAX_COMPANY_ID or any(_IMAX.search(a) for a in attrs):
            notes.append(f"imax_distribution_not_counted: {display(name, attrs)}")
    for n in _edge_nodes(node, "releaseDates"):
        attrs = _attrs(n)
        if not any(_imax_hit("", [a]) for a in attrs):
            continue
        where = f"{n.get('year')}-{n.get('month') or 0:02d} {((n.get('country') or {}).get('id')) or ''}".strip()
        first_run = (year and n.get("year") and int(n["year"]) <= int(year) + 1
                     and not any(_IMAX_NOT_FIRST_RUN.search(a) for a in attrs))
        if first_run:
            if "imdb_release" not in sources:
                sources.append("imdb_release")
            evidence.append(f"releaseDates: {where} ({', '.join(attrs)})")
        else:
            notes.append(f"imax_release_not_counted: {where} ({', '.join(attrs)})")
    return sources, evidence, notes


IMAX_DMR_FROM = 2002   # IMAX DMR (a feature remastered for IMAX on release) exists since 2002 (Apollo 13 / Attack of the Clones)
IMAX_PRE_DMR_PROOF = {"imdb_camera", "imdb_negative", "imdb_release"}  # shot on IMAX / IMAX release dated with the film


def imax_verdict(specs: dict | None, node: dict | None = None) -> tuple[int | None, list[str], list[str], list[str]]:
    """-> (is_imax, sources, evidence, notes).

    is_imax = 1 when any tech-spec list mentions IMAX for the shoot or the theatrical release (owner's rule:
    "снят на IMAX-камеры или шёл в IMAX"), or the rest of the IMDb node shows the original release in IMAX
    (node_imax: IMAX company credit / IMAX release date); 0 when IMDb has technical specs but nothing mentions IMAX;
    None when IMDb has no technical specs and no other evidence (unknown -- not the same as "not IMAX").
    notes: things that are NOT counted but are worth seeing ('IMAX Enhanced' only, bare 1.43 : 1 / 1.90 : 1,
    IMAX re-releases / screenings).
    """
    sources, evidence, notes = [], [], []
    for src, key, field in IMAX_SOURCES:
        for v, attrs in spec_items(specs, key, field):
            hit = _imax_hit(v, attrs)
            if hit:
                if src not in sources:
                    sources.append(src)
                evidence.append(f"{key}: {display(v, attrs)}")
            elif any(_IMAX_HOME.search(t) for t in [str(v)] + attrs):
                notes.append(f"imax_enhanced_only: {key}: {display(v, attrs)}")
    if not sources:
        for v, attrs in spec_items(specs, "aspectRatios", "aspectRatio"):
            if re.match(r"^\s*1\.43\s*:\s*1", v):
                notes.append(f"bare_1.43: {display(v, attrs)}")
            elif re.match(r"^\s*1\.90\s*:\s*1", v):
                notes.append(f"bare_1.90: {display(v, attrs)}")
    if node:
        n_src, n_ev, n_notes = node_imax(node)
        sources += [s for s in n_src if s not in sources]
        evidence += n_ev
        notes += n_notes
    year = ((node or {}).get("releaseYear") or {}).get("year")
    if sources and year and int(year) < IMAX_DMR_FROM and not set(sources) & IMAX_PRE_DMR_PROOF:
        # a film made before IMAX DMR (2002) and not shot on IMAX: its IMAX print / mix / remaster is a later
        # re-release (Raiders 1981 → 2023, Jurassic Park → 2013 3D, Titanic → 2012), not "шёл в IMAX" on release
        notes.append(f"imax_rerelease_pre_dmr ({year}): " + "; ".join(evidence))
        return (0 if has_specs(specs) else None), [], [], notes
    if sources:
        return 1, sources, evidence, notes
    if has_specs(specs):
        return 0, [], [], notes
    return None, [], [], notes


# --- money, poster, akas ----------------------------------------------------------------------

def _money(block: dict | None, path: tuple) -> tuple[int | None, str | None]:
    cur = block
    for k in path:
        cur = (cur or {}).get(k)
    if not cur or cur.get("amount") is None:
        return None, None
    return int(cur["amount"]), cur.get("currency")


def money(node: dict | None) -> dict:
    node = node or {}
    b, bc = _money(node.get("productionBudget"), ("budget",))
    g, gc = _money(node.get("lifetimeGross_ww"), ("total",))
    return {"budget": b, "budget_currency": bc, "gross_ww": g, "gross_ww_currency": gc}


def poster(node: dict | None) -> str | None:
    url = ((node or {}).get("primaryImage") or {}).get("url")
    return imdb_ds.poster_url(url) if url else None


def akas_for(node: dict | None, country: str, language: str | None = None) -> list[dict]:
    """Akas of `country` (and, when `language` is given, of that language in any country), in IMDb's order."""
    out = []
    for e in (((node or {}).get("akas") or {}).get("edges") or []):
        n = (e or {}).get("node") or {}
        c = ((n.get("country") or {}).get("id") or "").upper()
        lang = (n.get("language") or {}).get("id")
        if n.get("text") and (c == country.upper() or (language and (lang or "").lower() == language.lower())):
            out.append({"text": n["text"], "language": lang, "attributes": _attrs(n)})
    return out


# --- Ukrainian title fallback (owner, 2026-10-25, metadata survey: «Украинские названия с IMDb — запасной источник,
# если у TMDB нет UK-названия») ----------------------------------------------------------------------------------
# An IMDb aka of region UA (or language uk) becomes titles.title_uk only while title_uk is empty; TMDB's Ukrainian
# title, when it appears later, replaces it (films.enrich).  Choice among several akas:
#   * skipped: working / festival / literal / informal / pre-release / promotional / short / video-game titles and
#     the per-season titles of a series ("second season title") — a season row carries the series name, as with TMDB;
#   * skipped: text without Cyrillic (a Latin "One Piece" filed under UA is not a Ukrainian title) and text with
#     letters Ukrainian does not have (ы э ъ ё — a Russian title filed under UA), and language other than uk;
#   * preferred: an aka without attributes (IMDb's main title for Ukraine) > "new title" > "alternative title"
#     > anything else that is left; within a class IMDb's order.
_UK_SKIP_ATTR = re.compile(r"working|festival|literal|informal|pre-?release|promotional|short|video game|"
                           r"season title|episode|copyright|bootleg|fake|cable|complete title|series title", re.I)
_UK_NOT_UK = re.compile(r"[ыэъёЫЭЪЁ]")
_CYR = re.compile(r"[А-Яа-яІіЇїЄєҐґ]")


def _uk_rank(attrs: list[str]) -> int | None:
    a = [x.strip().lower() for x in attrs if x and x.strip()]
    if any(_UK_SKIP_ATTR.search(x) for x in a):
        return None
    if not a:
        return 0
    if a == ["new title"]:
        return 1
    if a == ["alternative title"]:
        return 2
    return 3


def pick_uk_title(akas: list[dict]) -> tuple[str | None, str | None]:
    """Best Ukrainian title among IMDb akas [{text, language, attributes}] -> (title, kind) or (None, None).
    kind = '' (main title) or the aka's attributes joined by ', ' (e.g. 'new title')."""
    best = None
    for i, a in enumerate(akas or []):
        text = (a.get("text") or "").strip()
        lang = (a.get("language") or "").lower()
        if not text or (lang and lang != "uk") or _UK_NOT_UK.search(text):
            continue
        if not _CYR.search(text):
            if re.search(r"[A-Za-z]", text):
                continue        # a Latin title filed under UA; a title of digits only («1917») is kept
        else:
            # IMDb's Ukrainian akas sometimes carry a Latin "i" for «і» («Покемон Фiльм: Бiле»)
            text = re.sub(r"(?<=[А-Яа-яІіЇїЄєҐґ'’])i|i(?=[А-Яа-яІіЇїЄєҐґ])", "і", text)
            text = re.sub(r"(?<=[А-Яа-яІіЇїЄєҐґ'’])I|I(?=[А-Яа-яІіЇїЄєҐґ])", "І", text)
        rank = _uk_rank(a.get("attributes") or [])
        if rank is None:
            continue
        if best is None or (rank, i) < best[0]:
            best = ((rank, i), text, ", ".join(x for x in (a.get("attributes") or []) if x))
    return (best[1], best[2]) if best else (None, None)


def uk_title_for(rec: dict | None, dataset_akas: list[dict] | None = None) -> tuple[str | None, str | None]:
    """(title_uk, title_uk_source) from a GraphQL cache record, else from the IMDb dataset akas (region UA)."""
    node = (rec or {}).get("node")
    if node:
        title, kind = pick_uk_title(akas_for(node, "UA", "uk"))
        if title:
            return title, "imdb_aka" + (f":{kind}" if kind else "")
    if dataset_akas:
        # the dataset's UA akas carry neither types nor attributes here: the first fitting one in IMDb's order
        title, kind = pick_uk_title([{"text": a.get("title"), "language": a.get("language"),
                                      "attributes": [x for x in (a.get("attributes") or "").split(",") if x]}
                                     for a in dataset_akas
                                     if not re.search(r"working|festival", a.get("types") or "", re.I)])
        if title:
            return title, "imdb_dataset_aka"
    return None, None


# --- metadata survey (owner, 2026-10-25): distributor, colour, cameras / film format, filming locations -----------

_HOME_VIDEO = re.compile(r"\b(DVD|Blu-?ray|VHS|Laser-?disc|UMD)\b", re.I)


def distribution_credits(node: dict | None) -> list[dict]:
    """IMDb distribution credits in IMDb's order -> [{name, country (first id), countries, attrs}]."""
    out = []
    for e in (((node or {}).get("distributors") or {}).get("edges") or []):
        n = (e or {}).get("node") or {}
        name = ((n.get("company") or {}).get("companyText") or {}).get("text")
        if not name:
            continue
        cs = [c.get("id") for c in n.get("countries") or [] if c and c.get("id")]
        out.append({"name": name, "country": cs[0] if cs else None, "countries": cs, "attrs": _attrs(n)})
    return out


def pick_distributor(credits: list[dict]) -> tuple[str | None, str | None, str]:
    """The distributor / rights holder shown in the sheet -> (name, country, rule).

    Owner (survey 2026-10-25): Ukrainian or worldwide if it can be told, otherwise the US one.  Home-video credits
    (DVD / Blu-ray) are not rights holders for a viewer and are skipped.  Order:
      1. 'ua'  — a Ukrainian credit (theatrical first);
      2. 'xww' — the worldwide credit, only when exactly one company holds it (a Netflix / Apple / Amazon original);
                 several worldwide companies are VOD storefronts (Dune: Part Two: Amazon, Apple TV+, HBO Max) — skipped;
      3. 'us'  — the US credit: theatrical, then TV, then VOD, then any;
      4. 'first' — the first theatrical credit of any country, else the first credit at all.
    """
    rights = [c for c in credits if not any(_HOME_VIDEO.search(a) for a in c["attrs"])]
    if not rights:
        return None, None, "none"

    def theatrical_first(cs):
        for want in ("theatrical", "tv", "vod", None):
            for c in cs:
                if want is None or any(a.lower() == want for a in c["attrs"]):
                    return c
        return None

    ua = [c for c in rights if "UA" in c["countries"]]
    if ua:
        c = theatrical_first(ua)
        return c["name"], "UA", "ua"
    ww = [c for c in rights if "XWW" in c["countries"]]
    if len({c["name"] for c in ww}) == 1:
        return ww[0]["name"], "XWW", "xww"
    us = [c for c in rights if "US" in c["countries"]]
    if us:
        c = theatrical_first(us)
        return c["name"], "US", "us"
    # a Russian distributor only when nobody else is listed (the owner is in Ukraine)
    pool = [c for c in rights if c["country"] not in ("RU", "BY")] or rights
    c = next((c for c in pool if any(a.lower() == "theatrical" for a in c["attrs"])), pool[0])
    return c["name"], c["country"], "first"


def color_text(specs: dict | None) -> str | None:
    """IMDb colorations as one string: 'Color', 'Black and White', 'Color | Black and White (flashback scenes)'."""
    vals = list(dict.fromkeys(display(v, a) for v, a in spec_items(specs, "colorations", "text")))
    return " | ".join(vals) or None


def film_format(specs: dict | None) -> dict | None:
    """{negative, printed, process} display lists (only the non-empty ones), None when IMDb lists none."""
    out = {k: [display(v, a) for v, a in spec_items(specs, key, field)]
           for k, key, field in (("negative", "negativeFormats", "negativeFormat"),
                                 ("printed", "printedFormats", "printedFormat"), ("process", "processes", "process"))}
    out = {k: v for k, v in out.items() if v}
    return out or None


def filming_locations(node: dict | None) -> list[str]:
    return [e["node"]["text"] for e in (((node or {}).get("filmingLocations") or {}).get("edges") or [])
            if (e or {}).get("node") and e["node"].get("text")]


def summarize(rec: dict | None) -> dict:
    """Everything `run` derives from one cache record (also what `show` prints)."""
    node = (rec or {}).get("node") or {}
    specs = node.get("technicalSpecifications") or {}
    is_imax, sources, evidence, notes = imax_verdict(specs, node)
    di, di_note = di_resolution(specs)
    aspects = [display(v, a) for v, a in spec_items(specs, "aspectRatios", "aspectRatio")]
    sounds = [display(v, a) for v, a in spec_items(specs, "soundMixes", "text")]
    truncated = [k for k in SPEC_KEYS if (specs.get(k) or {}).get("total") not in (None, len((specs.get(k) or {}).get("items") or []))]
    return {
        "tconst": (rec or {}).get("tconst"), "status": (rec or {}).get("status"),
        "canonical": (rec or {}).get("canonical"),
        "title": (node.get("titleText") or {}).get("text"),
        "original_title": (node.get("originalTitleText") or {}).get("text"),
        "year": (node.get("releaseYear") or {}).get("year"),
        "type": (node.get("titleType") or {}).get("id"),
        "has_specs": has_specs(specs),
        "di_resolution": di, "di_note": di_note,
        "aspect_ratios": aspects, "sound_mix": sounds,
        "is_imax": is_imax, "imax_source": ",".join(sources) or None, "imax_evidence": evidence,
        "imax_notes": notes,
        "cameras": [display(v, a) for v, a in spec_items(specs, "cameras", "camera")],
        "processes": [display(v, a) for v, a in spec_items(specs, "processes", "process")],
        "negative_formats": [display(v, a) for v, a in spec_items(specs, "negativeFormats", "negativeFormat")],
        "printed_formats": [display(v, a) for v, a in spec_items(specs, "printedFormats", "printedFormat")],
        "spec_truncated": truncated,
        "color": color_text(specs),
        "film_format": film_format(specs),
        "filming_locations": filming_locations(node),
        "distribution": distribution_credits(node),
        **dict(zip(("distributor", "distributor_country", "distributor_rule"),
                   pick_distributor(distribution_credits(node)))),
        **money(node),
        "poster_url": poster(node),
        "ua_akas": akas_for(node, "UA"),
    }


SERIES_TYPES = {"tvSeries", "tvMiniSeries"}


def suspect_link(kind: str, db_year, imdb_type: str | None, imdb_year) -> str | None:
    """Why the titles row's IMDb id probably points at a different title, or None."""
    if imdb_type == "tvEpisode":
        return "imdb_is_episode"
    if kind == "series" and imdb_type and imdb_type not in SERIES_TYPES:
        return f"series_row_but_imdb_{imdb_type}"
    if kind == "movie" and imdb_type in SERIES_TYPES:
        return f"movie_row_but_imdb_{imdb_type}"
    if kind == "movie" and db_year and imdb_year and abs(int(db_year) - int(imdb_year)) > 1:
        return "year_differs_by_more_than_1"
    return None


# A movie row whose IMDb id is a series / miniseries / episode of the SAME work (Batman: The Animated Series, a Family
# Guy special IMDb files as an episode): the IMDb poster is right for it even though the specs are not written.
_POSTER_SAME_WORK = ("movie_row_but_imdb_tvSeries", "movie_row_but_imdb_tvMiniSeries", "imdb_is_episode")


def _norm_title(text: str | None) -> str:
    t = (text or "").lower().replace("&", " and ").replace("'", "").replace("’", "")
    return " ".join(re.sub(r"[^\w]+", " ", t).split())


def poster_same_work(row, s: dict, why: str | None) -> bool:
    """True when a suspect link (why) still points at the same work: kind mismatch only (a movie row), years within
    one, and the IMDb title (or original title) equals the row's title or ends it after a word boundary
    ('Family Guy Presents: Something, Something, Something, Dark Side' ~ 'Something, Something, Something, Dark Side')."""
    if why not in _POSTER_SAME_WORK or row["kind"] != "movie":
        return False
    if not (row["year"] and s.get("year") and abs(int(row["year"]) - int(s["year"])) <= 1):
        return False
    keys = row.keys() if hasattr(row, "keys") else row
    mine = {_norm_title(row["title_original"]), _norm_title(row["title_latin"] if "title_latin" in keys else None)} - {""}
    theirs = {_norm_title(s.get("title")), _norm_title(s.get("original_title"))} - {""}
    return any(a == b or (len(b.split()) >= 2 and a.endswith(" " + b)) for a in mine for b in theirs)


# ----------------------------------------------------------------------------------------------
# DB
# ----------------------------------------------------------------------------------------------

def _targets(conn, limit: int | None = None, ids: list[str] | None = None) -> dict[str, list]:
    """{tconst: [title rows]} -- movies by imdb_id, seasons by series_imdb_id."""
    rows = conn.execute(
        "SELECT id, kind, season_number, imdb_id, series_imdb_id, title_original, title_latin, title_uk, year, "
        "poster_url, poster_source, budget_usd, box_office_usd, upscale_source FROM titles "
        "WHERE COALESCE(imdb_id, series_imdb_id) IS NOT NULL ORDER BY id").fetchall()
    by: dict[str, list] = {}
    for r in rows:
        t = r["imdb_id"] if r["kind"] == "movie" or r["season_number"] is None else r["series_imdb_id"]
        t = t or r["imdb_id"] or r["series_imdb_id"]
        by.setdefault(t, []).append(r)
    if ids:
        by = {t: by.get(t, []) for t in ids}
    if limit:
        by = dict(list(by.items())[:limit])
    return by


META_KEYS = ("color", "cameras", "film_format", "filming_locations", "distributor")


def apply(conn, targets: dict[str, list], recs: dict[str, dict], log=print) -> dict:
    """Write derived values; one short transaction per tconst group of rows.  Returns counters + lists."""
    now = _now()
    st = {k: 0 for k in ("tconsts", "rows", "rows_not_found", "rows_suspect_link", "rows_no_record", "di_set", "imax_1", "imax_0",
                         "imax_null", "aspect_set", "sound_set", "budget_filled", "gross_filled",
                         "budget_non_usd", "gross_non_usd", "budget_skipped_series", "gross_skipped_series",
                         "budget_disagree_kept", "gross_disagree_kept", "poster_set", "poster_missing_on_imdb", "poster_kept",
                         "poster_set_same_work",
                         "upscale_upscaled", "upscale_native", "upscale_unknown",
                         *(k + "_set" for k in META_KEYS))}
    lists = {"not_found": [], "redirects": [], "non_usd": [], "di_conflicts": [], "imax_notes": [],
             "spec_truncated": [], "uk_candidates": [], "year_mismatch": [], "money_disagree": []}
    for t, rows in targets.items():
        rec = recs.get(t)
        st["tconsts"] += 1
        if rec is None:
            st["rows_no_record"] += len(rows)
            continue
        s = summarize(rec)
        if rec.get("canonical"):
            lists["redirects"].append({"tconst": t, "canonical": rec["canonical"]})
        if s["status"] == "not_found" or not s["title"]:
            st["rows_not_found"] += len(rows)
            lists["not_found"].append(t)
            continue
        if not s["di_note"].startswith(("ok", "no_di", "di_without_resolution")):
            lists["di_conflicts"].append({"tconst": t, "title": s["title"], "note": s["di_note"]})
        if s["imax_notes"]:
            lists["imax_notes"].append({"tconst": t, "title": s["title"], "is_imax": s["is_imax"],
                                        "notes": s["imax_notes"]})
        if s["spec_truncated"]:
            lists["spec_truncated"].append({"tconst": t, "lists": s["spec_truncated"]})
        good_rows, poster_only = [], []
        for r in rows:
            # probable wrong IMDb link in titles: nothing is written for that row (it would copy another
            # title's specs / poster), it is only reported -- except the poster of the same work filed under
            # another IMDb type (poster_same_work)
            why = suspect_link(r["kind"], r["year"], s["type"], s["year"])
            if why:
                lists["year_mismatch"].append({"title_id": r["id"], "tconst": t, "why": why, "db_year": r["year"],
                                               "db_title": r["title_original"], "imdb_year": s["year"],
                                               "imdb_type": s["type"], "imdb_title": s["title"]})
                st["rows_suspect_link"] += 1
                if r["poster_url"] is None and s["poster_url"] and poster_same_work(r, s, why):
                    poster_only.append(r)
            else:
                good_rows.append(r)
        rows = good_rows
        conn.execute("BEGIN IMMEDIATE")
        try:
            for r in poster_only:
                cur = conn.execute("UPDATE titles SET poster_url = ?, poster_source = 'imdb', poster_checked_at = ? "
                                   "WHERE id = ? AND poster_url IS NULL", (s["poster_url"], now, r["id"]))
                st["poster_set_same_work"] += cur.rowcount
            for r in rows:
                st["rows"] += 1
                sets = {
                    "di_resolution": s["di_resolution"],
                    "aspect_ratios": json.dumps(s["aspect_ratios"], ensure_ascii=False) if s["aspect_ratios"] else None,
                    "sound_mix": json.dumps(s["sound_mix"], ensure_ascii=False) if s["sound_mix"] else None,
                    "is_imax": s["is_imax"],
                    "imax_source": s["imax_source"],
                    # metadata survey (owner, 2026-10-25): distributor shown, the rest DB only
                    "color": s["color"],
                    "cameras": json.dumps(s["cameras"], ensure_ascii=False) if s["cameras"] else None,
                    "film_format": json.dumps(s["film_format"], ensure_ascii=False) if s["film_format"] else None,
                    "filming_locations": (json.dumps(s["filming_locations"], ensure_ascii=False)
                                          if s["filming_locations"] else None),
                    "distributor": s["distributor"],
                    "distributor_country": s["distributor_country"],
                    "distributors": (json.dumps([{k: d[k] for k in ("name", "country", "attrs")} for d in s["distribution"]],
                                                ensure_ascii=False) if s["distribution"] else None),
                }
                for k in META_KEYS:
                    st[k + "_set"] += sets[k] is not None
                st["di_set"] += s["di_resolution"] is not None
                # R32: upscale verdict from the master format — a 4K release of a 2K / HD master is a studio upscale.
                # A verdict from another source (blu-ray.com, manual) is never overwritten.
                if r["upscale_source"] in (None, "imdb_di"):
                    sets["upscale_verdict"] = upscale_verdict(s["di_resolution"])
                    sets["upscale_source"] = "imdb_di"
                    st["upscale_" + sets["upscale_verdict"]] += 1
                st["aspect_set"] += bool(s["aspect_ratios"])
                st["sound_set"] += bool(s["sound_mix"])
                st[{1: "imax_1", 0: "imax_0", None: "imax_null"}[s["is_imax"]]] += 1
                is_season = r["kind"] == "series"
                # budget / worldwide gross: only NULL columns, only USD, only movies (a series-level budget
                # or gross does not describe one season)
                if s["budget"] is not None:
                    if is_season:
                        st["budget_skipped_series"] += 1
                    elif s["budget_currency"] != "USD":
                        st["budget_non_usd"] += 1
                        lists["non_usd"].append({"tconst": t, "title_id": r["id"], "field": "budget",
                                                 "amount": s["budget"], "currency": s["budget_currency"]})
                    elif r["budget_usd"] is None:
                        sets["budget_usd"] = s["budget"]
                        st["budget_filled"] += 1
                    elif r["budget_usd"] != s["budget"]:
                        st["budget_disagree_kept"] += 1
                        lists["money_disagree"].append({"tconst": t, "title_id": r["id"], "field": "budget",
                                                        "db": r["budget_usd"], "imdb": s["budget"]})
                if s["gross_ww"] is not None:
                    if is_season:
                        st["gross_skipped_series"] += 1
                    elif s["gross_ww_currency"] != "USD":
                        st["gross_non_usd"] += 1
                        lists["non_usd"].append({"tconst": t, "title_id": r["id"], "field": "gross_ww",
                                                 "amount": s["gross_ww"], "currency": s["gross_ww_currency"]})
                    elif r["box_office_usd"] is None:
                        sets["box_office_usd"] = s["gross_ww"]
                        st["gross_filled"] += 1
                    elif r["box_office_usd"] != s["gross_ww"]:
                        st["gross_disagree_kept"] += 1
                        lists["money_disagree"].append({"tconst": t, "title_id": r["id"], "field": "gross_ww",
                                                        "db": r["box_office_usd"], "imdb": s["gross_ww"]})
                where_poster = ""
                if r["poster_url"] is None:
                    if s["poster_url"]:
                        sets.update(poster_url=s["poster_url"], poster_source="imdb", poster_checked_at=now)
                        where_poster = " AND poster_url IS NULL"  # never overwrite a concurrent writer
                        st["poster_set"] += 1
                    else:
                        # checked, IMDb has no poster: remember the check (poster_url stays NULL)
                        sets["poster_checked_at"] = now
                        where_poster = " AND poster_url IS NULL"
                        st["poster_missing_on_imdb"] += 1
                else:
                    st["poster_kept"] += 1
                # budget / gross guarded the same way against concurrent writers
                guard = where_poster
                if "budget_usd" in sets:
                    guard += " AND budget_usd IS NULL"
                if "box_office_usd" in sets:
                    guard += " AND box_office_usd IS NULL"
                cols = ", ".join(f"{k} = ?" for k in sets)
                cur = conn.execute(f"UPDATE titles SET {cols} WHERE id = ?{guard}", [*sets.values(), r["id"]])
                if cur.rowcount == 0 and guard:
                    # a concurrent writer filled one of the guarded columns meanwhile: write only the spec columns
                    base = {k: sets[k] for k in ("di_resolution", "aspect_ratios", "sound_mix", "is_imax", "imax_source",
                                                 "upscale_verdict", "upscale_source", *META_KEYS,
                                                 "distributor_country", "distributors") if k in sets}
                    conn.execute(f"UPDATE titles SET {', '.join(f'{k} = ?' for k in base)} WHERE id = ?",
                                 [*base.values(), r["id"]])
                    log(f"    title {r['id']}: guarded column changed concurrently, wrote specs only")
                if not r["title_uk"] and s["ua_akas"]:
                    uk, uk_src = uk_title_for(rec)
                    lists["uk_candidates"].append({"title_id": r["id"], "tconst": t, "season": r["season_number"],
                                                   "title_original": r["title_original"],
                                                   "ua_akas": [a["text"] for a in s["ua_akas"]], "chosen": uk})
                    if uk:
                        # owner 2026-10-25: IMDb's Ukrainian aka is the fallback while TMDB has no Ukrainian title
                        cur = conn.execute("UPDATE titles SET title_uk = ?, title_uk_source = ? "
                                           "WHERE id = ? AND coalesce(title_uk, '') = ''", (uk, uk_src, r["id"]))
                        st["title_uk_filled"] = st.get("title_uk_filled", 0) + cur.rowcount
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"stats": st, "lists": lists}


def run(conn, limit: int | None = None, max_age_days: float = DEFAULT_MAX_AGE_DAYS, ids: list[str] | None = None,
        fetch: bool = True, log=print) -> dict:
    t0 = time.time()
    conn.isolation_level = None  # explicit BEGIN/COMMIT in apply()
    targets = _targets(conn, limit, ids)
    log(f"imdb_specs: {len(targets)} tconsts, {sum(len(v) for v in targets.values())} title rows")
    recs, failures = ensure(list(targets), max_age_days, fetch=fetch, log=log)
    res = apply(conn, targets, recs, log=log)
    st, lists = res["stats"], res["lists"]
    # UA aka coverage vs the IMDb dataset (imdb_akas, region UA) -- report only
    ds_ua = 0
    for c in lists["uk_candidates"]:
        if conn.execute("SELECT 1 FROM imdb_akas WHERE tconst = ? AND region = 'UA' LIMIT 1", (c["tconst"],)).fetchone():
            ds_ua += 1
    no_uk_rows = sum(1 for rows in targets.values() for r in rows if r["title_uk"] is None)
    report = {
        "finished_at": _now(), "seconds": round(time.time() - t0, 1),
        "args": {"limit": limit, "max_age_days": max_age_days, "ids": ids, "fetch": fetch},
        "stats": st,
        "fetch_failures": failures,
        "title_uk": {"rows_without_title_uk": no_uk_rows, "rows_with_ua_aka_on_imdb": len(lists["uk_candidates"]),
                     "of_which_also_in_dataset_akas": ds_ua,
                     "examples": lists["uk_candidates"][:40]},
        "not_found": lists["not_found"], "redirects": lists["redirects"], "non_usd": lists["non_usd"],
        "di_conflicts": lists["di_conflicts"], "imax_notes": lists["imax_notes"],
        "spec_truncated": lists["spec_truncated"], "year_mismatch": lists["year_mismatch"],
        "money_disagree": lists["money_disagree"],
        "uk_candidates_all": lists["uk_candidates"],
    }
    if ids is None and limit is None:
        # owner 2026-10-25: no IMDb poster → Rotten Tomatoes → TMDB → the tracker page's own image (films/posters.py);
        # tracker images are re-checked here too (they expire)
        from .. import posters
        report["poster_fallback"] = posters.fill(conn, fetch_imdb=False, log=log)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (CACHE_DIR / "last_run.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return report


def fill_title_uk(conn, fetch: bool = True, ids: list[int] | None = None, log=print) -> dict:
    """Targeted fill of the Ukrainian-title fallback for every title with an empty title_uk (no full specs run):
    the GraphQL cache of any age (fetching only tconsts never fetched), else the IMDb dataset akas (region UA).
    A row whose IMDb link looks wrong (suspect_link) is skipped, as in apply()."""
    where = "coalesce(title_uk, '') = '' AND duplicate_of IS NULL AND COALESCE(imdb_id, series_imdb_id) IS NOT NULL"
    params: list = []
    if ids:
        where += f" AND id IN ({','.join('?' * len(ids))})"
        params = list(ids)
    rows = conn.execute(f"SELECT id, kind, season_number, imdb_id, series_imdb_id, title_original, year FROM titles "
                        f"WHERE {where} ORDER BY id", params).fetchall()
    by: dict[str, list] = {}
    for r in rows:
        t = r["imdb_id"] if r["kind"] == "movie" or r["season_number"] is None else r["series_imdb_id"]
        by.setdefault(t or r["imdb_id"] or r["series_imdb_id"], []).append(r)
    recs = {t: load_cached(t) for t in by}
    missing = [t for t, rec in recs.items() if rec is None]
    failures: list = []
    if missing and fetch:
        got, failures = ensure(missing, None, fetch=True, log=log)
        recs.update(got)
    st = {"rows_without_title_uk": len(rows), "tconsts": len(by), "fetched": len(missing) if fetch else 0,
          "filled": 0, "filled_from_graphql": 0, "filled_from_dataset": 0, "skipped_suspect_link": 0,
          "no_ukrainian_aka": 0, "aka_rejected": 0}
    samples = []
    for t, trows in by.items():
        rec = recs.get(t)
        ds = [dict(a) for a in conn.execute("SELECT title, language, types, attributes FROM imdb_akas "
                                             "WHERE tconst = ? AND region = 'UA' ORDER BY ordering", (t,))]
        uk, src = uk_title_for(rec, ds)
        node = (rec or {}).get("node")
        if not uk:
            if ds or akas_for(node, "UA", "uk"):
                st["aka_rejected"] += len(trows)
            else:
                st["no_ukrainian_aka"] += len(trows)
            continue
        s_type = ((node or {}).get("titleType") or {}).get("id")
        s_year = ((node or {}).get("releaseYear") or {}).get("year")
        with conn:
            for r in trows:
                if node and suspect_link(r["kind"], r["year"], s_type, s_year):
                    st["skipped_suspect_link"] += 1
                    continue
                cur = conn.execute("UPDATE titles SET title_uk = ?, title_uk_source = ?, updated_at = datetime('now') "
                                   "WHERE id = ? AND coalesce(title_uk, '') = ''", (uk, src, r["id"]))
                if cur.rowcount:
                    st["filled"] += 1
                    st["filled_from_dataset" if src == "imdb_dataset_aka" else "filled_from_graphql"] += 1
                    if len(samples) < 30:
                        samples.append({"title_id": r["id"], "tconst": t, "original": r["title_original"],
                                        "title_uk": uk, "source": src})
    st["fetch_failures"] = failures
    st["samples"] = samples
    log(json.dumps({k: v for k, v in st.items() if k != "samples"}, ensure_ascii=False))
    return st


# ----------------------------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(prog="films.sources.imdb_specs")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run", help="fetch (cached) IMDb specs and update titles")
    p.add_argument("--limit", type=int, help="only the first N IMDb ids (by title id)")
    p.add_argument("--max-age-days", type=float, default=DEFAULT_MAX_AGE_DAYS,
                   help=f"re-fetch cache entries older than this (default {DEFAULT_MAX_AGE_DAYS}; 0 = always)")
    p.add_argument("--ids", nargs="+", help="only these tconsts")
    p.add_argument("--no-fetch", action="store_true", help="use the cache only (any age)")
    p = sub.add_parser("show", help="print the parsed verdict for tconsts (no DB writes)")
    p.add_argument("tconsts", nargs="+")
    p.add_argument("--max-age-days", type=float, default=DEFAULT_MAX_AGE_DAYS)
    p = sub.add_parser("uk", help="fill empty title_uk from IMDb Ukrainian akas (cache / dataset; no full specs run)")
    p.add_argument("--no-fetch", action="store_true", help="do not fetch tconsts missing from the cache")
    p.add_argument("--title-ids", nargs="+", type=int)
    p = sub.add_parser("imax", help="back-fill otherCompanies for cut company lists, then recompute is_imax only")
    p.add_argument("--ids", nargs="+", help="only these tconsts")
    p.add_argument("--no-fetch", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "imax":
        conn = db.connect()
        tconsts = a.ids or list(_targets(conn))
        oc = {} if a.no_fetch else ensure_other_companies(tconsts)
        st = refresh_imax(conn, a.ids)
        print(json.dumps({"other_companies": oc, **st}, ensure_ascii=False, indent=1))
        return
    if a.cmd == "uk":
        conn = db.connect()
        db.migrate(conn)
        st = fill_title_uk(conn, fetch=not a.no_fetch, ids=a.title_ids)
        print(json.dumps(st, ensure_ascii=False, indent=1))
        return
    if a.cmd == "show":
        recs, failures = ensure(a.tconsts, a.max_age_days, log=lambda *x: print(*x, file=sys.stderr))
        for t in a.tconsts:
            s = summarize(recs.get(t))
            s["ua_akas"] = [x["text"] for x in s["ua_akas"]]
            print(json.dumps(s, ensure_ascii=False, indent=1))
        if failures:
            print("FAILURES:", failures, file=sys.stderr)
        return
    conn = db.connect()
    rep = run(conn, a.limit, a.max_age_days, a.ids, fetch=not a.no_fetch)
    short = {k: v for k, v in rep.items() if k not in ("uk_candidates_all",)}
    short["title_uk"] = {k: v for k, v in rep["title_uk"].items() if k != "examples"}
    short["title_uk"]["examples"] = rep["title_uk"]["examples"][:10]
    for k in ("imax_notes", "di_conflicts", "non_usd", "not_found", "year_mismatch", "money_disagree"):
        if isinstance(short.get(k), list) and len(short[k]) > 15:
            short[k] = short[k][:15] + [f"... {len(rep[k]) - 15} more in data/cache/imdb_specs/last_run.json"]
    print(json.dumps(short, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
