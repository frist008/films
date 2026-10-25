"""TMDB API v3: IMDb-id lookup, title search, movie / TV series / season / collection / person metadata.

Usage (prints compact JSON; the API key is never printed):
    python -X utf8 -m films.sources.tmdb find tt15239678
    python -X utf8 -m films.sources.tmdb movie 693134 [--full]
    python -X utf8 -m films.sources.tmdb tv 1399 [--season 2]
    python -X utf8 -m films.sources.tmdb search "Дюна: Часть вторая" 2024 movie ["Dune: Part Two" ...]
    python -X utf8 -m films.sources.tmdb collection 726871
    python -X utf8 -m films.sources.tmdb person 1190668
    common options: --max-age DAYS (cache freshness, default 30; 0 = refetch), --offline (cache only)

Access: plain HTTPS to api.themoviedb.org with `requests`; key = TMDB_API_KEY from .env, passed as the
`api_key` query parameter, read at runtime and never logged (exception texts are scrubbed of it).
Rate: <= MAX_RPS requests/s process-wide (thread-safe limiter); 429 / 5xx / network errors are retried
with exponential backoff, honouring Retry-After.

Cache: every response with HTTP 200 or 404 is stored in table `tmdb_raw` (zlib-compressed JSON, migration
5) under (kind, key, lang) and read from there first; `max_age_days` decides when a cached row is stale.
Other 4xx (401 bad key, 422 bad params) are errors in *our* request, so they raise TmdbError and are not
cached.  A cached "full" movie/tv/season response that lacks one of the appended sub-responses (because
APPEND changed in the code) counts as stale.

One request per movie: /movie/{id}?language=ru-RU&append_to_response=credits,release_dates,videos,keywords,
external_ids,translations,alternative_titles&include_video_language=ru,uk,en.  The uk / en titles come from
`translations` (so no uk-UA / en-US requests are needed); Russian genre names come from the ru-RU body.
TV: one request per series (/tv/{id}) and one per season (/tv/{id}/season/{n} + aggregate_credits, videos,
external_ids).  Country names in Russian: one cached /configuration/countries?language=ru-RU request.

Nothing here writes to `titles` / `people` / `credits`: functions return plain dicts, and `movie_row()` /
`season_row()` map them onto `titles` columns for the caller.  Ambiguous search results are returned as
such (status 'ambiguous' with candidates), never silently resolved.
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import statistics
import sys
import threading
import time
import unicodedata
import zlib
from datetime import date, datetime, timedelta, timezone

import requests

from films import db
from films.sources.imdb import norm

API = "https://api.themoviedb.org/3"
MAX_RPS = 20                      # TMDB tolerates ~40-50/s; we stay at <= 20
MAX_ATTEMPTS = 6
TIMEOUT = 30
DEFAULT_MAX_AGE_DAYS = 30
VIDEO_LANGS = "ru,uk,en"

MOVIE_APPEND = ("credits", "release_dates", "videos", "keywords", "external_ids", "translations",
                "alternative_titles")
TV_APPEND = ("external_ids", "keywords", "translations", "videos", "alternative_titles")
SEASON_APPEND = ("aggregate_credits", "videos", "external_ids")
COLLECTION_APPEND = ("translations",)
PERSON_APPEND = ("external_ids", "translations")

STATS = {"requests": 0, "cache_hits": 0, "retries": 0, "errors": 0, "net_seconds": 0.0}


_stats_lock = threading.Lock()


def _stat(name: str, inc=1):
    with _stats_lock:
        STATS[name] += inc


class TmdbError(RuntimeError):
    pass


# ----------------------------------------------------------------------------------------------
# key, HTTP, rate limit
# ----------------------------------------------------------------------------------------------

_key_cache: str | None = None


def _api_key() -> str:
    global _key_cache
    if _key_cache:
        return _key_cache
    k = os.environ.get("TMDB_API_KEY")
    if not k:
        env = db.ROOT / ".env"
        if env.exists():
            for line in env.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("TMDB_API_KEY="):
                    k = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    if not k:
        raise TmdbError("TMDB_API_KEY not found in environment or .env")
    _key_cache = k
    return k


def _scrub(text: str) -> str:
    k = _key_cache
    return text.replace(k, "***") if k else text


_session = requests.Session()
_session.headers.update({"Accept": "application/json",
                         "User-Agent": "films-db/0.1 (personal, non-commercial)"})
_rate_lock = threading.Lock()
_next_slot = 0.0


def _wait_slot():
    global _next_slot
    with _rate_lock:
        now = time.monotonic()
        t = max(now, _next_slot)
        _next_slot = t + 1.0 / MAX_RPS
    d = t - time.monotonic()
    if d > 0:
        time.sleep(d)


def _http_get(path: str, params: dict) -> tuple[int, dict | None]:
    """GET API+path; returns (status, json) for 200/404, raises TmdbError otherwise (after retries)."""
    q = dict(params)
    q["api_key"] = _api_key()
    last = ""
    for attempt in range(MAX_ATTEMPTS):
        _wait_slot()
        t0 = time.monotonic()
        try:
            r = _session.get(API + path, params=q, timeout=TIMEOUT)
        except requests.RequestException as e:
            _stat("net_seconds", time.monotonic() - t0)
            last = f"{type(e).__name__}: {_scrub(str(e))}"
            _stat("retries", 1)
            time.sleep(min(2 ** attempt, 30))
            continue
        _stat("net_seconds", time.monotonic() - t0)
        _stat("requests", 1)
        if r.status_code in (200, 404):
            try:
                return r.status_code, r.json()
            except ValueError:
                if r.status_code == 404:
                    return 404, None
                last = "invalid JSON in 200 response"
                _stat("retries", 1)
                time.sleep(min(2 ** attempt, 30))
                continue
        if r.status_code == 429 or r.status_code >= 500:
            ra = r.headers.get("Retry-After")
            try:
                wait = float(ra) if ra else 2 ** attempt
            except ValueError:
                wait = 2 ** attempt
            last = f"HTTP {r.status_code}"
            _stat("retries", 1)
            time.sleep(min(max(wait, 0.5), 60))
            continue
        _stat("errors", 1)
        try:
            msg = r.json().get("status_message")
        except ValueError:
            msg = r.text[:200]
        raise TmdbError(f"TMDB {path}: HTTP {r.status_code}: {_scrub(str(msg))}")
    _stat("errors", 1)
    raise TmdbError(f"TMDB {path}: gave up after {MAX_ATTEMPTS} attempts ({last})")


# ----------------------------------------------------------------------------------------------
# cache (tmdb_raw)
# ----------------------------------------------------------------------------------------------

OFFLINE = False   # True: never touch the network; a cache miss raises TmdbError


def _age_days(fetched_at: str) -> float:
    try:
        t = datetime.strptime(fetched_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return 1e9
    return (datetime.now(timezone.utc) - t).total_seconds() / 86400


def _cache_read(conn, kind, key, lang):
    row = conn.execute("SELECT fetched_at, status, body FROM tmdb_raw WHERE kind = ? AND key = ? AND lang = ?",
                       (kind, key, lang)).fetchone()
    if row is None:
        return None
    body = json.loads(zlib.decompress(row[2]).decode("utf-8")) if row[2] is not None else None
    return row[0], row[1], body


def _cache_write(conn, kind, key, lang, status, body):
    blob = zlib.compress(json.dumps(body, ensure_ascii=False).encode("utf-8"), 6) if body is not None else None
    was_open = conn.in_transaction
    conn.execute("INSERT OR REPLACE INTO tmdb_raw (kind, key, lang, fetched_at, status, body)"
                 " VALUES (?, ?, ?, datetime('now'), ?, ?)", (kind, key, lang, status, blob))
    if not was_open:          # don't commit a transaction the caller opened
        conn.commit()


def get(conn, kind: str, key: str, lang: str, path: str, params: dict | None = None,
        max_age_days: float = DEFAULT_MAX_AGE_DAYS, require: tuple = ()) -> tuple[int, dict | None]:
    """Cached GET.  Returns (http_status, body); status is 200 or 404.

    `require`: top-level keys that must be in a cached 200 body for it to count (appended responses).
    """
    c = _cache_read(conn, kind, key, lang)
    if c is not None:
        fetched_at, status, body = c
        complete = status != 200 or all(k in (body or {}) for k in require)
        if complete and (OFFLINE or _age_days(fetched_at) <= max_age_days):
            _stat("cache_hits", 1)
            return status, body
    if OFFLINE:
        raise TmdbError(f"offline and not cached: {kind} {key} {lang}")
    p = dict(params or {})
    if lang:
        p["language"] = lang
    status, body = _http_get(path, p)
    _cache_write(conn, kind, key, lang, status, body)
    return status, body


# ----------------------------------------------------------------------------------------------
# small helpers
# ----------------------------------------------------------------------------------------------

def _year(d: str | None) -> int | None:
    return int(d[:4]) if d and len(d) >= 4 and d[:4].isdigit() else None


def _nz(v):
    """TMDB uses 0 for 'unknown' in budget / revenue / runtime."""
    return v if v else None


def is_latin(s: str | None) -> bool:
    """True when every letter of s is Latin script (digits/punctuation ignored)."""
    if not s:
        return True
    for ch in s:
        if ch.isalpha() and "LATIN" not in unicodedata.name(ch, ""):
            return False
    return True


def _translation(tr_list: list, lang: str, field: str, regions: tuple = ()) -> tuple[str | None, str | None]:
    """Non-empty data[field] of the translation for iso_639_1 == lang, preferring the given regions."""
    found = [t for t in tr_list or [] if t.get("iso_639_1") == lang and ((t.get("data") or {}).get(field) or "").strip()]
    if not found:
        return None, None
    found.sort(key=lambda t: regions.index(t.get("iso_3166_1")) if t.get("iso_3166_1") in regions else len(regions))
    t = found[0]
    return t["data"][field].strip(), f"{t.get('iso_639_1')}-{t.get('iso_3166_1')}"


def _alt_title(alts: list, regions: tuple, field: str = "title") -> str | None:
    for reg in regions:
        for a in alts or []:
            if a.get("iso_3166_1") == reg and (a.get(field) or "").strip():
                return a[field].strip()
    return None


_COUNTRY_RU_OVERRIDE = {
    # TMDB's ru-RU native names are long official forms for a few; trackers / owner use the short ones
    "US": "США", "GB": "Великобритания", "KR": "Южная Корея", "KP": "Северная Корея",
    "CZ": "Чехия", "AE": "ОАЭ", "RU": "Россия", "TW": "Тайвань", "MK": "Северная Македония",
    "HK": "Гонконг",
}


def countries_ru(conn, max_age_days: float = 365) -> dict:
    """ISO 3166-1 -> Russian country name (TMDB /configuration/countries?language=ru-RU + overrides)."""
    status, body = get(conn, "config", "countries", "ru-RU", "/configuration/countries", max_age_days=max_age_days)
    out = {}
    for c in body or []:
        out[c["iso_3166_1"]] = c.get("native_name") or c.get("english_name") or c["iso_3166_1"]
    out.update(_COUNTRY_RU_OVERRIDE)
    return out


def _countries(conn, codes: list[str]) -> list[str]:
    m = countries_ru(conn)
    return [m.get(c, c) for c in codes]


# ----------------------------------------------------------------------------------------------
# videos / trailers
# ----------------------------------------------------------------------------------------------

_SUBBED = re.compile(r"субтитр|\bsub(s|titles|bed)?\b|\[eng|оригинал|original|на английском", re.I)
# tagged Trailer/Teaser on TMDB but really an episode cut / clip / review
_NOT_TRAILER = re.compile(r"\bсерия\b|\bэпизод\b|\bepisode\b|короткая версия|отрывок|фрагмент|\bclip\b|"
                          r"обзор|\breview\b|реакция|reaction|разбор", re.I)
_TYPE_RANK = {"Trailer": 0, "Teaser": 1}


def _video(v: dict) -> dict:
    return {"key": v.get("key"), "url": f"https://www.youtube.com/watch?v={v.get('key')}",
            "name": v.get("name"), "type": v.get("type"), "official": v.get("official"),
            "lang": v.get("iso_639_1"), "region": v.get("iso_3166_1"),
            "published_at": v.get("published_at"), "size": v.get("size"),
            "subtitled": bool(_SUBBED.search(v.get("name") or "")),
            "not_trailer": bool(_NOT_TRAILER.search(v.get("name") or ""))}


def _rank_trailers(videos: list, lang: str) -> list[dict]:
    """YouTube Trailer/Teaser videos in `lang`, best first: real trailers before mis-tagged clips /
    episode cuts / reviews, Trailer > Teaser, not subtitled, official, newest."""
    vs = [v for v in videos if v.get("site") == "YouTube" and v.get("iso_639_1") == lang
          and v.get("type") in _TYPE_RANK and v.get("key")]

    vs.sort(key=lambda v: v.get("published_at") or "", reverse=True)          # newest first ...
    vs.sort(key=lambda v: (1 if _NOT_TRAILER.search(v.get("name") or "") else 0,  # ... within (stable sort)
                           _TYPE_RANK[v["type"]],
                           1 if _SUBBED.search(v.get("name") or "") else 0,
                           0 if v.get("official") else 1))
    return [_video(v) for v in vs]


def pick_trailers(videos_body: dict | None) -> dict:
    """{'trailer_ru': best ru video|None, 'trailer_ru_url', 'trailers_ru': [...], 'trailers_uk': [...],
    'trailer_en_url', 'videos_ru_other': [non-trailer ru videos]}."""
    vids = (videos_body or {}).get("results") or []
    ru = _rank_trailers(vids, "ru")
    uk = _rank_trailers(vids, "uk")
    en = _rank_trailers(vids, "en")
    ru_other = [_video(v) for v in vids if v.get("iso_639_1") == "ru" and v.get("type") not in _TYPE_RANK
                and v.get("site") == "YouTube"]
    best = next((v for v in ru if not v["not_trailer"]), None)   # a clip / episode cut is never "the trailer"
    return {"trailer_ru": best, "trailer_ru_url": best["url"] if best else None,
            "trailers_ru": ru, "trailers_uk": uk, "trailer_en_url": en[0]["url"] if en else None,
            "videos_ru_other": ru_other}


# ----------------------------------------------------------------------------------------------
# credits
# ----------------------------------------------------------------------------------------------

DIRECTOR_JOBS = ("Director", "Co-Director")
# Writing-department jobs that are authorship of the story/script (kept in `writers`, job kept);
# the rest of the Writing department (Story Editor, Script Consultant, Staff Writer, ...) -> `writing_other`
WRITER_JOBS = {"Screenplay", "Writer", "Story", "Novel", "Characters", "Original Story", "Book", "Author",
               "Teleplay", "Original Series Creator", "Comic Book", "Graphic Novel", "Short Story", "Play",
               "Theatre Play", "Idea", "Original Concept", "Adaptation", "Screenstory", "Co-Writer", "Dialogue",
               "Original Film Writer", "Head Writer", "Creator", "Novelist", "Musical", "Opera", "Poem",
               "Article", "Memoir", "Books", "Original Creator", "Original Idea", "Characters By",
               "Story By", "Written By"}
# metadata survey (owner, 2026-10-25): DOP and producers are shown in the sheet, the composer is stored only.
# Producers: a film's creative producers carry the job «Producer» (Executive / Co- / Associate Producer are financiers,
# studio executives and line producers — not shown).  In TV the creative producers / showrunners are credited
# «Executive Producer», while a season's «Producer» is a line / supervising producer, so seasons use Executive Producer.
DOP_JOBS = {"Director of Photography"}
MOVIE_PRODUCER_JOBS = {"Producer"}
TV_PRODUCER_JOBS = {"Executive Producer"}
COMPOSER_JOBS = {"Original Music Composer", "Music", "Composer"}
_CYR = re.compile(r"[А-Яа-яЁё]")


def _pname(c: dict) -> dict:
    """In a ru-RU response `name` is the Russian translation when TMDB has one, `original_name` the
    original.  -> {'name': original, 'name_ru': Russian or None}."""
    orig = c.get("original_name") or c.get("name")
    loc = c.get("name")
    ru = loc if loc and _CYR.search(loc) and not re.search(r"[ІіЇїЄєҐґЎў]", loc) else None
    return {"name": orig, "name_ru": ru}


def _people_by_job(crew: list, pred) -> list[dict]:
    """Group crew rows per person (keeps first-seen order), collecting jobs."""
    out: dict[int, dict] = {}
    for c in crew or []:
        if not pred(c):
            continue
        p = out.setdefault(c["id"], {"tmdb_id": c["id"], **_pname(c), "jobs": []})
        if c.get("job") and c["job"] not in p["jobs"]:
            p["jobs"].append(c["job"])
    return list(out.values())


def _movie_credits(credits: dict) -> dict:
    crew = (credits or {}).get("crew") or []
    cast = sorted((credits or {}).get("cast") or [], key=lambda c: c.get("order", 1e9))
    return {
        "directors": _people_by_job(crew, lambda c: c.get("job") in DIRECTOR_JOBS),
        "writers": _people_by_job(crew, lambda c: c.get("department") == "Writing" and c.get("job") in WRITER_JOBS),
        "writing_other": _people_by_job(crew, lambda c: c.get("department") == "Writing"
                                        and c.get("job") not in WRITER_JOBS),
        "dops": _people_by_job(crew, lambda c: c.get("job") in DOP_JOBS),
        "producers": _people_by_job(crew, lambda c: c.get("job") in MOVIE_PRODUCER_JOBS),
        "composers": _people_by_job(crew, lambda c: c.get("job") in COMPOSER_JOBS),
        "cast": [{"tmdb_id": c["id"], **_pname(c), "character": c.get("character") or None,
                  "order": c.get("order")} for c in cast],
    }


def _aggregate_credits(agg: dict) -> dict:
    """TV aggregate_credits (cast[].roles[], crew[].jobs[]) -> same shape as movie credits."""
    crew_rows = []
    for c in (agg or {}).get("crew") or []:
        for j in c.get("jobs") or []:
            crew_rows.append({"id": c["id"], "name": c.get("name"), "original_name": c.get("original_name"),
                              "job": j.get("job"), "department": c.get("department"),
                              "episodes": j.get("episode_count")})

    def grouped(pred):
        out = _people_by_job(crew_rows, pred)
        for p in out:
            p["episodes"] = max((r["episodes"] or 0) for r in crew_rows if r["id"] == p["tmdb_id"] and pred(r))
        return sorted(out, key=lambda p: -p["episodes"])

    cast = sorted((agg or {}).get("cast") or [], key=lambda c: c.get("order", 1e9))
    return {
        "directors": grouped(lambda r: r["job"] in DIRECTOR_JOBS),
        "writers": grouped(lambda r: r["department"] == "Writing" and r["job"] in WRITER_JOBS),
        "writing_other": grouped(lambda r: r["department"] == "Writing" and r["job"] not in WRITER_JOBS),
        "dops": grouped(lambda r: r["job"] in DOP_JOBS),
        "producers": grouped(lambda r: r["job"] in TV_PRODUCER_JOBS),
        "composers": grouped(lambda r: r["job"] in COMPOSER_JOBS),
        "cast": [{"tmdb_id": c["id"], **_pname(c),
                  "character": " / ".join(r["character"] for r in c.get("roles") or [] if r.get("character")) or None,
                  "episodes": c.get("total_episode_count"), "order": c.get("order")} for c in cast],
    }


# ----------------------------------------------------------------------------------------------
# find by IMDb id
# ----------------------------------------------------------------------------------------------

def find_by_imdb(conn, imdb_id: str, max_age_days: float = DEFAULT_MAX_AGE_DAYS) -> dict:
    """{'imdb_id', 'movie': id|None, 'tv': id|None, 'tv_episode': {...}|None, 'tv_season': {...}|None,
    'warnings': [...]} via /find/{imdb_id}?external_source=imdb_id."""
    status, body = get(conn, "find", imdb_id, "", f"/find/{imdb_id}", {"external_source": "imdb_id"},
                       max_age_days=max_age_days)
    body = body or {}
    out = {"imdb_id": imdb_id, "movie": None, "tv": None, "tv_episode": None, "tv_season": None, "warnings": []}
    if status != 200:
        out["warnings"].append(f"HTTP {status}")
        return out
    mv, tv = body.get("movie_results") or [], body.get("tv_results") or []
    if mv:
        out["movie"] = mv[0]["id"]
        out["movie_title"] = mv[0].get("title")
        out["movie_year"] = _year(mv[0].get("release_date"))
    if tv:
        out["tv"] = tv[0]["id"]
        out["tv_title"] = tv[0].get("name")
        out["tv_year"] = _year(tv[0].get("first_air_date"))
    if len(mv) > 1 or len(tv) > 1:
        out["warnings"].append(f"several results: movie={[m['id'] for m in mv]} tv={[t['id'] for t in tv]}")
    if mv and tv:
        out["warnings"].append("both a movie and a tv result")
    ep = body.get("tv_episode_results") or []
    if ep:
        e = ep[0]
        out["tv_episode"] = {"show_id": e.get("show_id"), "season": e.get("season_number"),
                             "episode": e.get("episode_number"), "name": e.get("name"), "id": e.get("id")}
    se = body.get("tv_season_results") or []
    if se:
        s = se[0]
        out["tv_season"] = {"show_id": s.get("show_id"), "season": s.get("season_number"), "id": s.get("id")}
    if not (mv or tv or ep or se):
        out["warnings"].append("not on TMDB (no result for this IMDb id)")
    return out


# ----------------------------------------------------------------------------------------------
# movie
# ----------------------------------------------------------------------------------------------

_RD_TYPES = {1: "premiere", 2: "theatrical_limited", 3: "theatrical", 4: "digital", 5: "physical", 6: "tv"}


def _release_dates(rd_body: dict) -> list[dict]:
    out = []
    for c in (rd_body or {}).get("results") or []:
        for r in c.get("release_dates") or []:
            if r.get("release_date"):
                out.append({"country": c.get("iso_3166_1"), "date": r["release_date"][:10],
                            "type": r.get("type"), "kind": _RD_TYPES.get(r.get("type")), "note": r.get("note") or None})
    out.sort(key=lambda r: r["date"])
    return out


def movie_raw(conn, tmdb_id: int, max_age_days: float = DEFAULT_MAX_AGE_DAYS) -> dict | None:
    params = {"append_to_response": ",".join(MOVIE_APPEND), "include_video_language": VIDEO_LANGS}
    status, body = get(conn, "movie", str(tmdb_id), "ru-RU", f"/movie/{tmdb_id}", params,
                       max_age_days=max_age_days, require=MOVIE_APPEND)
    return body if status == 200 else None


def _titles(conn, body: dict, field: str, orig_field: str) -> dict:
    """title_ru / title_uk / title_en / title_latin with their sources (translations, then alt titles)."""
    tr = ((body.get("translations") or {}).get("translations")) or []
    alts = (body.get("alternative_titles") or {})
    alts = alts.get("titles") or alts.get("results") or []
    orig = body.get(orig_field)
    olang = body.get("original_language")
    out = {"title_original": orig}
    for lang, regions, alt_regions, col in (("ru", ("RU",), ("RU",), "title_ru"),
                                            ("uk", ("UA",), ("UA",), "title_uk"),
                                            ("en", ("US", "GB"), ("US", "GB"), "title_en")):
        if olang == lang and orig:
            out[col], out[col + "_source"] = orig, "original"
            continue
        v, src = _translation(tr, lang, field, regions)
        if v:
            out[col], out[col + "_source"] = v, "translation:" + src
            continue
        v = _alt_title(alts, alt_regions, "title")
        out[col], out[col + "_source"] = (v, "alternative_titles") if v else (None, None)
    if orig and not is_latin(orig):
        if out.get("title_en") and is_latin(out["title_en"]):
            out["title_latin"], out["title_latin_source"] = out["title_en"], out["title_en_source"]
        else:
            rom = next((a.get("title") for a in alts if is_latin(a.get("title"))
                        and re.search(r"roman|romaji|translit|pinyin|latin", a.get("type") or "", re.I)), None)
            if not rom:
                rom = next((a.get("title") for a in alts if a.get("title") and is_latin(a["title"])
                            and a.get("iso_3166_1") in ("US", "GB")), None)
            out["title_latin"], out["title_latin_source"] = (rom, "alternative_titles") if rom else (None, None)
    else:
        out["title_latin"], out["title_latin_source"] = None, "original is Latin"
    return out


def movie(conn, tmdb_id: int, max_age_days: float = DEFAULT_MAX_AGE_DAYS) -> dict | None:
    """Normalized movie dict (see movie_row() for the titles-column mapping).  None if TMDB says 404."""
    b = movie_raw(conn, tmdb_id, max_age_days)
    if b is None:
        return None
    rds = _release_dates(b.get("release_dates"))
    prem = [r for r in rds if r["type"] in (1, 2, 3)]
    world = prem[0] if prem else (rds[0] if rds else None)
    digital = next((r for r in rds if r["type"] == 4), None)
    ru_rel = next((r for r in rds if r["country"] == "RU" and r["type"] in (2, 3)), None)
    genres = b.get("genres") or []
    coll = b.get("belongs_to_collection")
    ext = b.get("external_ids") or {}
    d = {
        "tmdb_id": b["id"], "tmdb_type": "movie", "kind": "movie",
        **_titles(conn, b, "title", "original_title"),
        "release_date": world["date"] if world else (b.get("release_date") or None),
        "release_date_kind": (f"{world['kind']}:{world['country']}" if world else
                              ("primary" if b.get("release_date") else None)),
        "release_date_primary": b.get("release_date") or None,
        "release_date_ru": ru_rel["date"] if ru_rel else None,
        "digital_release_date": digital["date"] if digital else None,
        "release_dates": rds,
        "countries": _countries(conn, [c["iso_3166_1"] for c in b.get("production_countries") or []]),
        "country_codes": [c["iso_3166_1"] for c in b.get("production_countries") or []],
        "origin_country": b.get("origin_country") or [],
        "genres": [g["name"] for g in genres],
        "genre_ids": [g["id"] for g in genres],
        "main_genre": genres[0]["name"] if genres else None,
        "is_animation": any(g["id"] == 16 for g in genres),
        "runtime_min": _nz(b.get("runtime")),
        "studios": [c["name"] for c in b.get("production_companies") or []],
        "studio_ids": [c["id"] for c in b.get("production_companies") or []],
        "budget_usd": _nz(b.get("budget")),
        "box_office_usd": _nz(b.get("revenue")),
        "imdb_id": b.get("imdb_id") or ext.get("imdb_id") or None,
        "external_ids": {k: v for k, v in ext.items() if v and k != "id"},
        "collection": {"id": coll["id"], "name": coll.get("name")} if coll else None,
        "keywords": [k["name"] for k in (b.get("keywords") or {}).get("keywords") or []],
        "original_language": b.get("original_language"),
        "status": b.get("status"),
        "poster_path": b.get("poster_path"),
        **_movie_credits(b.get("credits")),
        **pick_trailers(b.get("videos")),
    }
    d["year"] = _year(d["release_date"])
    d["year_primary"] = _year(d["release_date_primary"])
    return d


def movie_row(m: dict) -> dict:
    """Subset of a movie() dict keyed by `titles` columns (JSON columns already serialized)."""
    return {
        "kind": "movie", "is_animation": int(bool(m["is_animation"])), "title_original": m["title_original"],
        "title_latin": m["title_latin"], "title_ru": m["title_ru"], "title_uk": m["title_uk"],
        "year": m["year"], "release_date": m["release_date"],
        "countries": json.dumps(m["countries"], ensure_ascii=False),
        "genres": json.dumps(m["genres"], ensure_ascii=False), "main_genre": m["main_genre"],
        "runtime_min": m["runtime_min"], "studios": json.dumps(m["studios"], ensure_ascii=False),
        "budget_usd": m["budget_usd"], "box_office_usd": m["box_office_usd"], "imdb_id": m["imdb_id"],
        "tmdb_id": m["tmdb_id"], "tmdb_type": "movie", "trailer_ru_url": m["trailer_ru_url"],
    }


# ----------------------------------------------------------------------------------------------
# TV series / season
# ----------------------------------------------------------------------------------------------

TV_STATUS = {"Returning Series": "returning", "Ended": "ended", "Canceled": "canceled",
             "In Production": "in_production", "Planned": "planned", "Pilot": "pilot"}


def tv_raw(conn, tmdb_id: int, max_age_days: float = DEFAULT_MAX_AGE_DAYS) -> dict | None:
    params = {"append_to_response": ",".join(TV_APPEND), "include_video_language": VIDEO_LANGS}
    status, body = get(conn, "tv", str(tmdb_id), "ru-RU", f"/tv/{tmdb_id}", params,
                       max_age_days=max_age_days, require=TV_APPEND)
    return body if status == 200 else None


def tv(conn, tmdb_id: int, max_age_days: float = DEFAULT_MAX_AGE_DAYS) -> dict | None:
    """Series-level dict.  None if TMDB says 404."""
    b = tv_raw(conn, tmdb_id, max_age_days)
    if b is None:
        return None
    genres = b.get("genres") or []
    ext = b.get("external_ids") or {}
    seasons = [{"season_number": s.get("season_number"), "air_date": s.get("air_date") or None,
                "year": _year(s.get("air_date")), "episode_count": s.get("episode_count"), "name": s.get("name")}
               for s in b.get("seasons") or []]
    d = {
        "tmdb_id": b["id"], "tmdb_type": "tv", "kind": "series",
        **_titles(conn, b, "name", "original_name"),
        "first_air_date": b.get("first_air_date") or None,
        "last_air_date": b.get("last_air_date") or None,
        "year": _year(b.get("first_air_date")),
        "countries": _countries(conn, [c["iso_3166_1"] for c in b.get("production_countries") or []]),
        "country_codes": [c["iso_3166_1"] for c in b.get("production_countries") or []],
        "origin_country": b.get("origin_country") or [],
        "genres": [g["name"] for g in genres],
        "genre_ids": [g["id"] for g in genres],
        "main_genre": genres[0]["name"] if genres else None,
        "is_animation": any(g["id"] == 16 for g in genres),
        "networks": [n["name"] for n in b.get("networks") or []],
        "studios": [c["name"] for c in b.get("production_companies") or []],
        "created_by": [{"tmdb_id": c["id"], **_pname(c)} for c in b.get("created_by") or []],
        "number_of_seasons": b.get("number_of_seasons"),
        "number_of_episodes": b.get("number_of_episodes"),
        "seasons": seasons,
        "status": b.get("status"),
        "series_status": TV_STATUS.get(b.get("status"), (b.get("status") or "").lower() or None),
        "tv_type": b.get("type"),
        "in_production": b.get("in_production"),
        "episode_run_time": b.get("episode_run_time") or [],
        "imdb_id": ext.get("imdb_id") or None,
        "external_ids": {k: v for k, v in ext.items() if v and k != "id"},
        "keywords": [k["name"] for k in (b.get("keywords") or {}).get("results") or []],
        "original_language": b.get("original_language"),
        "poster_path": b.get("poster_path"),
        **pick_trailers(b.get("videos")),
    }
    return d


def season_raw(conn, tv_id: int, season_number: int, max_age_days: float = DEFAULT_MAX_AGE_DAYS) -> dict | None:
    params = {"append_to_response": ",".join(SEASON_APPEND), "include_video_language": VIDEO_LANGS}
    status, body = get(conn, "season", f"{tv_id}/{season_number}", "ru-RU", f"/tv/{tv_id}/season/{season_number}",
                       params, max_age_days=max_age_days, require=SEASON_APPEND)
    return body if status == 200 else None


_SEASON_WORDS = r"(?:сезон|season|сезону|сезона|s)"


def _mentions_season(name: str, n: int) -> bool | None:
    """True if the video name names season n, False if it names only other seasons, None if no season."""
    name = name or ""
    nums = [int(x) for x in re.findall(rf"\b{_SEASON_WORDS}\s*0?(\d{{1,2}})\b", name, re.I)]
    nums += [int(x) for x in re.findall(rf"\b(\d{{1,2}})\s*-?\s*(?:й|ий|ой|го)?\s*{_SEASON_WORDS}\b", name, re.I)]
    if not nums:
        return None
    return n in nums


def _season_trailer(season_videos: dict, series_videos: dict, n: int, air_date: str | None,
                    prev_air: str | None, next_air: str | None) -> dict:
    """Russian trailer for one season, with `trailer_ru_source` saying how it was chosen:
    1. 'season_videos'                     — the season's own videos on TMDB;
    2. 'series_videos:name_mentions_season' — a series-level video whose name names this season;
    3. 'series_videos:published_near_season_premiere' — a series-level video naming no season, published
       after the previous season's premiere + 30 days, no later than this premiere + 60 days, more than
       90 days before the next season's premiere, and at most 400 days (season 1: 730) before this premiere.
    Otherwise None; the unassigned series-level ru trailers are listed for review."""
    own = pick_trailers(season_videos)
    if own["trailer_ru"]:
        own["trailer_ru_source"] = "season_videos"
        return own
    ser = pick_trailers(series_videos)
    named = [v for v in ser["trailers_ru"] if _mentions_season(v["name"], n) and not v["not_trailer"]]
    if named:
        return dict(own, trailer_ru=named[0], trailer_ru_url=named[0]["url"],
                    trailers_ru=named, trailer_ru_source="series_videos:name_mentions_season")
    if air_date:
        a = date.fromisoformat(air_date)
        lo = date.fromisoformat(prev_air) + timedelta(days=30) if prev_air else date.min
        hi = a + timedelta(days=60)
        if next_air:
            hi = min(hi, date.fromisoformat(next_air) - timedelta(days=91))
        max_before = 730 if n == 1 else 400
        dated = []
        for v in ser["trailers_ru"]:
            if _mentions_season(v["name"], n) is not None or not v["published_at"] or v["not_trailer"]:
                continue
            p = date.fromisoformat(v["published_at"][:10])
            if lo < p <= hi and (a - p).days <= max_before:
                dated.append(v)
        if dated:
            return dict(own, trailer_ru=dated[0], trailer_ru_url=dated[0]["url"], trailers_ru=dated,
                        trailer_ru_source="series_videos:published_near_season_premiere")
    own["trailer_ru_source"] = None
    own["series_trailers_ru_unassigned"] = ser["trailers_ru"]
    return own


def season(conn, tv_id: int, season_number: int, max_age_days: float = DEFAULT_MAX_AGE_DAYS,
           series: dict | None = None) -> dict | None:
    """Per-season dict (air date / year, episodes, typical runtime, season credits, season trailer).
    None if TMDB has no such season."""
    b = season_raw(conn, tv_id, season_number, max_age_days)
    if b is None:
        return None
    s = series or tv(conn, tv_id, max_age_days)
    eps = b.get("episodes") or []
    runtimes = [e["runtime"] for e in eps if e.get("runtime")]
    ep_dates = sorted(e["air_date"] for e in eps if e.get("air_date"))
    air = b.get("air_date") or (ep_dates[0] if ep_dates else None)
    today = date.today().isoformat()
    others = sorted((x for x in (s or {}).get("seasons") or [] if x["season_number"] and x["air_date"]),
                    key=lambda x: x["season_number"])
    prev_air = next((x["air_date"] for x in reversed(others) if x["season_number"] < season_number), None)
    next_air = next((x["air_date"] for x in others if x["season_number"] > season_number), None)
    raw_series_videos = (tv_raw(conn, tv_id, max_age_days) or {}).get("videos")
    tr = _season_trailer(b.get("videos"), raw_series_videos, season_number, air, prev_air, next_air)
    d = {
        "series_tmdb_id": tv_id, "season_number": season_number, "season_tmdb_id": b.get("id"),
        "name": b.get("name"),
        "air_date": air, "year": _year(air),
        "last_episode_date": ep_dates[-1] if ep_dates else None,
        "episode_count": len(eps),
        "episodes_aired": sum(1 for x in ep_dates if x <= today),
        "runtime_min": int(statistics.median(runtimes)) if runtimes else None,
        "runtime_min_range": [min(runtimes), max(runtimes)] if runtimes else None,
        "external_ids": {k: v for k, v in (b.get("external_ids") or {}).items() if v and k != "id"},
        "poster_path": b.get("poster_path"),
        **_aggregate_credits(b.get("aggregate_credits")),
        "creators": (s or {}).get("created_by") or [],   # series creators, credited on every season (survey 2026-10-25)
        **tr,
    }
    return d


def season_row(series: dict, s: dict) -> dict:
    """titles-column dict for one season row (series-level fields + this season's)."""
    return {
        "kind": "series", "season_number": s["season_number"],
        "series_key": f"imdb:{series['imdb_id']}" if series.get("imdb_id") else f"tmdb:{series['tmdb_id']}",
        "series_imdb_id": series.get("imdb_id"), "series_tmdb_id": series["tmdb_id"],
        "is_animation": int(bool(series["is_animation"])),
        "title_original": series["title_original"], "title_latin": series["title_latin"],
        "title_ru": series["title_ru"], "title_uk": series["title_uk"],
        "year": s["year"], "release_date": s["air_date"],
        "countries": json.dumps(series["countries"], ensure_ascii=False),
        "genres": json.dumps(series["genres"], ensure_ascii=False), "main_genre": series["main_genre"],
        "runtime_min": s["runtime_min"], "studios": json.dumps(series["studios"], ensure_ascii=False),
        "seasons_total": series["number_of_seasons"], "episodes_total": s["episode_count"],
        "series_status": series["series_status"], "tmdb_id": series["tmdb_id"], "tmdb_type": "tv",
        "trailer_ru_url": s["trailer_ru_url"],
    }


# ----------------------------------------------------------------------------------------------
# collection / person
# ----------------------------------------------------------------------------------------------

def collection(conn, collection_id: int, max_age_days: float = DEFAULT_MAX_AGE_DAYS) -> dict | None:
    """{'id', 'name' (ru-RU, falls back to TMDB default), 'name_en', 'parts': [{tmdb_id, title_ru,
    title_original, release_date, year}] by release date (undated last)}."""
    status, b = get(conn, "collection", str(collection_id), "ru-RU", f"/collection/{collection_id}",
                    {"append_to_response": ",".join(COLLECTION_APPEND)}, max_age_days=max_age_days)
    if status != 200:
        return None
    tr = ((b.get("translations") or {}).get("translations")) or []
    name_en, _ = _translation(tr, "en", "title", ("US", "GB"))
    if not name_en and b.get("original_language") == "en":
        name_en = b.get("original_name")
    name_ru, _ = _translation(tr, "ru", "title", ("RU",))
    name_uk, _ = _translation(tr, "uk", "title", ("UA",))
    parts = [{"tmdb_id": p["id"], "media_type": p.get("media_type"), "title_ru": p.get("title"),
              "title_original": p.get("original_title"), "release_date": p.get("release_date") or None,
              "year": _year(p.get("release_date"))} for p in b.get("parts") or []]
    parts.sort(key=lambda p: (p["release_date"] is None, p["release_date"] or ""))
    return {"id": b["id"], "name": b.get("name"), "name_original": b.get("original_name"), "name_ru": name_ru,
            "name_uk": name_uk, "name_en": name_en, "parts": parts}


def person(conn, person_id: int, max_age_days: float = 180) -> dict | None:
    """{'tmdb_id', 'name' (original / primary), 'name_ru', 'name_ru_source', 'imdb_id',
    'known_for_department'}; one request (+ external_ids, translations)."""
    status, b = get(conn, "person", str(person_id), "ru-RU", f"/person/{person_id}",
                    {"append_to_response": ",".join(PERSON_APPEND)}, max_age_days=max_age_days)
    if status != 200:
        return None
    tr = ((b.get("translations") or {}).get("translations")) or []
    name_ru, src = _translation(tr, "ru", "name", ("RU",))
    if name_ru:
        src = "translation:" + src
    else:
        cyr = [a for a in b.get("also_known_as") or [] if re.search(r"[а-яё]", a, re.I)
               and not re.search(r"[іїєґў]", a, re.I)]
        name_ru, src = (cyr[0], "also_known_as") if cyr else (None, None)
    ext = b.get("external_ids") or {}
    # `name` of a ru-RU response is already the Russian one when it exists; the original is the primary
    # translation's name (TMDB marks it primary: True)
    prim = next((t["data"]["name"] for t in tr if (t.get("data") or {}).get("primary") and t["data"].get("name")), None)
    name_uk, src_uk = person_name_uk(b)
    return {"tmdb_id": b["id"], "name": prim or b.get("name"), "name_ru": name_ru, "name_ru_source": src,
            "name_uk": name_uk, "name_uk_source": src_uk,
            "imdb_id": b.get("imdb_id") or ext.get("imdb_id") or None,
            "known_for_department": b.get("known_for_department")}


_UK_ONLY = re.compile(r"[ІіЇїЄєҐґ]")
_NOT_UK = re.compile(r"[ЫыЭэЁёЪъЎў]")


def person_name_uk(b: dict) -> tuple[str | None, str | None]:
    """Ukrainian spelling of a person's name (survey 2026-10-25: «украинское — хранить»): the uk translation, else an
    also_known_as entry that is unmistakably Ukrainian (і/ї/є/ґ and none of ы/э/ё/ъ/ў)."""
    tr = ((b.get("translations") or {}).get("translations")) or []
    v, src = _translation(tr, "uk", "name", ("UA",))
    if v:
        return v, "translation:" + src
    aka = [a for a in b.get("also_known_as") or [] if _UK_ONLY.search(a) and not _NOT_UK.search(a)]
    return (aka[0], "also_known_as") if aka else (None, None)


def _homepage(b: dict) -> str | None:
    """Official site: TMDB `homepage` is translatable — the original-language translation's, else English, else any
    (a ru-RU body often has it empty: Dune: Part Two)."""
    tr = ((b.get("translations") or {}).get("translations")) or []
    olang = b.get("original_language")
    for lang in [x for x in (olang, "en") if x]:
        v, _ = _translation(tr, lang, "homepage")
        if v:
            return v
    if (b.get("homepage") or "").strip():
        return b["homepage"].strip()
    v = next(((t.get("data") or {}).get("homepage", "").strip() for t in tr
              if ((t.get("data") or {}).get("homepage") or "").strip()), None)
    return v or None


def title_extras(b: dict | None, kind: str, season_body: dict | None = None) -> dict:
    """Survey fields (owner, 2026-10-25) from a cached movie / tv body: keywords, official site, original and spoken
    languages, Wikidata id (a season's own when it has one), miniseries flag.  Pure; unit-tested on fixtures."""
    b = b or {}
    kw = (b.get("keywords") or {})
    kw = kw.get("keywords") if kind == "movie" else kw.get("results")
    wd = ((season_body or {}).get("external_ids") or {}).get("wikidata_id") or (b.get("external_ids") or {}).get("wikidata_id")
    langs = [x.get("iso_639_1") for x in b.get("spoken_languages") or [] if x.get("iso_639_1")]
    return {
        "keywords": [k["name"] for k in kw or [] if k.get("name")],
        "homepage": _homepage(b),
        "original_language": b.get("original_language") or None,
        "spoken_languages": langs,
        "wikidata_id": wd or None,
        "series_wikidata_id": (b.get("external_ids") or {}).get("wikidata_id") or None,
        "is_miniseries": (1 if b.get("type") == "Miniseries" else 0) if kind == "tv" and b.get("type") else None,
    }


# ----------------------------------------------------------------------------------------------
# search
# ----------------------------------------------------------------------------------------------

_ARTICLES = ("the ", "a ", "an ", "le ", "la ", "les ", "l ", "der ", "die ", "das ", "el ", "los ", "las ", "il ")
_SUBSEP = re.compile(r"\s*(?::|\s-\s|\s–\s|\s—\s|\.\s)\s*")


def _strip_article(k: str) -> str:
    for a in _ARTICLES:
        if k.startswith(a):
            return k[len(a):]
    return k


def name_fit(query: str, names: list[str]) -> tuple[float, str, str | None]:
    """Best (fit, how, matched_name) of query against candidate names."""
    q = norm(query)
    qa = _strip_article(q)
    qh = norm(_SUBSEP.split(query, maxsplit=1)[0])
    best = (0.0, "none", None)
    for n in names:
        if not n:
            continue
        k = norm(n)
        if not k:
            continue
        if k == q:
            return 1.0, "exact", n
        cand = []
        if _strip_article(k) == qa:
            cand.append((0.9, "no_article"))
        kh = norm(_SUBSEP.split(n, maxsplit=1)[0])
        if (qh and qh != q and qh == k) or (kh and kh != k and kh == q):
            cand.append((0.75, "before_subtitle"))
        r = difflib.SequenceMatcher(None, qa, _strip_article(k)).ratio()
        if r >= 0.8:
            cand.append((round(0.8 * r, 3), f"fuzzy:{r:.2f}"))
        for f, how in cand:
            if f > best[0]:
                best = (f, how, n)
    return best


def _year_fit(diff: int | None) -> float:
    if diff is None:
        return 1.0
    return {0: 1.0, 1: 0.75, 2: 0.35}.get(diff, 0.0)


def _detail_names(b: dict, kind: str) -> list[str]:
    f, of = ("title", "original_title") if kind == "movie" else ("name", "original_name")
    names = [b.get(f), b.get(of)]
    for t in ((b.get("translations") or {}).get("translations")) or []:
        names.append((t.get("data") or {}).get(f))
    alts = b.get("alternative_titles") or {}
    for a in alts.get("titles") or alts.get("results") or []:
        names.append(a.get("title"))
    return [n for n in dict.fromkeys(names) if n]


def _detail_year(b: dict, kind: str, year: int) -> tuple[float, int | None, str]:
    """Year fit from full details.  movie: primary release year / world-premiere year = full fit; another
    release year (a country's later release, a re-release) only 0.6.  tv: any season's year."""
    if kind == "movie":
        rds = _release_dates(b.get("release_dates"))
        prem = [r for r in rds if r["type"] in (1, 2, 3)]
        main = {_year(b.get("release_date"))} | ({_year(prem[0]["date"])} if prem else set())
        main.discard(None)
        anyy = {_year(r["date"]) for r in rds} - {None}
        if year in main:
            return 1.0, 0, "release year"
        diff = min((abs(y - year) for y in main), default=None)
        if diff is None:
            return (0.6, 0, "a country's release year") if year in anyy else (0.3, None, "no date on TMDB")
        if diff >= 2 and year in anyy:
            return 0.6, diff, "only a country's release / re-release that year"
        return _year_fit(diff), diff, "nearest release year"
    years = {_year(s.get("air_date")) for s in b.get("seasons") or [] if s.get("season_number")} - {None}
    fy, ly = _year(b.get("first_air_date")), _year(b.get("last_air_date"))
    if year in years:
        return 1.0, 0, "a season aired that year"
    if fy and fy <= year <= (ly or fy):
        return 0.9, 0, "within the series' air span"
    allyears = years | ({fy} if fy else set())
    if not allyears:
        return 0.3, None, "no date on TMDB"
    diff = min(abs(y - year) for y in allyears)
    return _year_fit(diff), diff, "nearest season year"


_TRANSLIT = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюяіїєґ",
                     ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r",
                      "s", "t", "u", "f", "kh", "ts", "ch", "sh", "shch", "", "y", "", "e", "yu", "ya", "i", "yi",
                      "ye", "g"]))


def _translit(s: str) -> str:
    return "".join(_TRANSLIT.get(ch, ch) for ch in norm(s))


def _skeleton(tok: str) -> str:
    """Rough phonetic key of a Latin token: digraphs folded, vowels after the first letter and doubled
    letters dropped ('мэдиган' -> medigan -> mdgn == madigan -> mdgn)."""
    t = tok
    for a, b in (("shch", "sh"), ("dzh", "j"), ("kh", "h"), ("zh", "j"), ("ts", "c"), ("tz", "c"), ("ks", "x"),
                 ("ph", "f"), ("ck", "k"), ("w", "v"), ("y", "i"), ("q", "k"), ("ch", "c"), ("sh", "s")):
        t = t.replace(a, b)
    if not t:
        return t
    out = t[0] + "".join(ch for ch in t[1:] if ch not in "aeiou")
    return re.sub(r"(.)\1+", r"\1", out)


def _person_match(want: list[str], people: list[dict]) -> bool | None:
    """Do any of the wanted names (any script) denote one of `people`?  Surname-level, cross-script:
    exact normalized name, or the wanted surname's transliteration has the same skeleton as / is >= 0.8
    similar to a token of the person's name.  None when no comparison is possible (TMDB names only in a
    non-Latin, non-Cyrillic script, e.g. Chinese)."""
    have, comparable = [], False
    for p in people:
        for n in (p.get("name"), p.get("name_ru"), p.get("original_name")):
            if not n:
                continue
            toks = [_translit(t) for t in norm(n).split()]
            if any(re.search(r"[a-z]", t) for t in toks):
                comparable = True
            have.append(toks)
    if not comparable:
        return None
    for w in want:
        wt = [_translit(t) for t in norm(w).split()]
        if not wt:
            continue
        sur = wt[-1]
        for h in have:
            if h == wt:
                return True
            for t in h:
                if len(sur) >= 3 and (_skeleton(t) == _skeleton(sur) and len(_skeleton(sur)) >= 2
                                      or difflib.SequenceMatcher(None, t, sur).ratio() >= 0.8):
                    return True
    return False


# tracker spellings -> the names countries_ru() produces
_COUNTRY_ALIASES = {"Корея Южная": "Южная Корея", "Корея Северная": "Северная Корея",
                    "Доминикана": "Доминиканская Республика", "Малазия": "Малайзия"}


def _hint_factor(conn, b: dict, kind: str, hints: dict) -> tuple[float, list[str]]:
    """Score factor from the caller's hints (directors / countries); notes say what matched or not.
    Tracker data is noisy, so a contradiction costs little (director 0.9, country 0.95); a match is used
    by search() as a tie-breaker."""
    f, notes = 1.0, []
    dirs = hints.get("directors") or []
    if dirs:
        if kind == "movie":
            people = _movie_credits(b.get("credits"))["directors"]
        else:
            people = [dict(_pname(c), tmdb_id=c["id"]) for c in b.get("created_by") or []]
        m = _person_match(dirs, people) if people else None
        if m:
            notes.append("director matches")
        elif m is False and kind == "movie":
            f *= 0.9
            notes.append("director differs")
    ctr = hints.get("countries") or []
    if ctr:
        codes = [c["iso_3166_1"] for c in b.get("production_countries") or []] + list(b.get("origin_country") or [])
        if codes:
            m = countries_ru(conn)
            have = {norm(m.get(c, c)) for c in codes} | {norm(c) for c in codes}
            want = {norm(_COUNTRY_ALIASES.get(c, c)) for c in ctr}
            if have & want:
                notes.append("country matches")
            else:
                f *= 0.95
                notes.append("no common country")
    return f, notes


def search(conn, titles: list[str], year: int | None, kind: str = "movie",
           max_age_days: float = DEFAULT_MAX_AGE_DAYS, accept: float = 0.8, margin: float = 0.15,
           details_for: int = 8, hints: dict | None = None) -> dict:
    """Search TMDB by every given title (ru-RU and en-US) -> {status, best, match, candidates, reason}.

    kind: 'movie' or 'tv' (also accepts 'series').  `year` = tracker year (for a TV season: the season's
    year; the series may have started earlier, so tv queries are also run without a year).  Queries with
    year-1 / year+1 are added when the exact year finds nothing.
    hints (optional): {'directors': [names in any language], 'countries': [Russian names or ISO codes]}
    from the tracker description; they lower the score of candidates that contradict them.

    Scoring: name fit (exact 1.0 / without article 0.9 / before the subtitle 0.75 / fuzzy <= 0.8) x year
    fit (diff 0: 1.0, 1: 0.75, 2: 0.35) x hint factor x coverage (0.7 when another candidate matches more
    of the given titles: a film called just «Отец» matching only the Russian title loses to the one that
    also matches the original title).  Candidates whose name fits (>= 0.75) and TMDB's top-2 hits of every
    query are re-scored from full details (all translations + alternative titles; release dates / every
    season's year); those detail requests are the same cached requests movie()/tv() use.

    Decision (same convention as films.sources.imdb.resolve): 'matched' only when the best score >=
    accept and every runner-up is >= margin lower, or every close runner-up matches a strict subset of
    the titles the best one matches, or only the best one has the hinted director (or the hinted country,
    every close runner-up explicitly lacking it), or the close runner-ups have no TMDB votes at all while
    the best has >= 20; otherwise
    'ambiguous' with the candidates; 'not_found' when nothing (or nothing with a fitting name/year) came back.
    `best` and `match` are the same object (`match` = the imdb.resolve key name).
    """
    kind = "tv" if kind in ("tv", "series") else "movie"
    hints = hints or {}
    titles = [t.strip() for t in dict.fromkeys(titles) if t and t.strip()]
    ep = "/search/movie" if kind == "movie" else "/search/tv"
    yparam = "year" if kind == "movie" else "first_air_date_year"
    tf, otf, df = (("title", "original_title", "release_date") if kind == "movie"
                   else ("name", "original_name", "first_air_date"))
    results: dict[int, dict] = {}
    queries = 0

    def run(q, lang, y):
        nonlocal queries
        p = {"query": q, "include_adult": "false"}
        if y:
            p[yparam] = y
        st, body = get(conn, "search", f"{kind}|{y or ''}|{q}", lang, ep, p, max_age_days=max_age_days)
        queries += 1
        for rank, r in enumerate((body or {}).get("results") or []):
            e = results.setdefault(r["id"], {"tmdb_id": r["id"], "names": set(), "r": r, "rank": rank})
            e["names"].update(x for x in (r.get(tf), r.get(otf)) if x)
            e["rank"] = min(e["rank"], rank)

    years_tried = []
    for y in ([year] if year else []) + ([None] if (kind == "tv" or not year) else []):
        years_tried.append(y)
        for q in titles:
            for lang in ("ru-RU", "en-US"):
                run(q, lang, y)
    if year and not results:
        for y in (year - 1, year + 1):
            years_tried.append(y)
            for q in titles:
                for lang in ("ru-RU", "en-US"):
                    run(q, lang, y)

    def fill(c, names, yf, diff, yhow):
        fits = {q: name_fit(q, names) for q in titles}
        best = max((f + (q,) for q, f in fits.items()), key=lambda x: x[0])
        c.update(name_fit=best[0], matched=best[1], matched_name=best[2], query=best[3], year_fit=yf,
                 year_diff=diff, year_how=yhow, titles_matched=[q for q, f in fits.items() if f[0] >= 0.9])
        c["score"] = round(c["name_fit"] * yf * c.get("hint_factor", 1.0), 4)

    cands = []
    for e in results.values():
        r = e["r"]
        ry = _year(r.get(df))
        if year is None:
            yf, diff, yhow = 1.0, None, "no year given"
        elif kind == "tv" and ry and ry <= year:
            yf, diff, yhow = 0.8, None, "series started earlier (unverified)"
        elif ry:
            diff = abs(ry - year)
            yf, yhow = _year_fit(diff), "search result year"
        else:
            yf, diff, yhow = 0.3, None, "no date"
        c = {"tmdb_id": e["tmdb_id"], "kind": kind, "title": r.get(tf), "original_title": r.get(otf),
             "date": r.get(df) or None, "year": ry, "vote_count": r.get("vote_count") or 0,
             "popularity": r.get("popularity"), "original_language": r.get("original_language"),
             "search_rank": e["rank"], "verified": False}
        fill(c, list(e["names"]), yf, diff, yhow)
        cands.append(c)
    cands.sort(key=lambda c: (-c["score"], -c["name_fit"], -c["vote_count"]))

    # full details for the plausible ones (see docstring)
    for c in [c for c in cands if c["name_fit"] >= 0.75 or c["search_rank"] < 2][:details_for]:
        b = (movie_raw if kind == "movie" else tv_raw)(conn, c["tmdb_id"], max_age_days)
        if b is None:
            continue
        c["hint_factor"], c["hint_notes"] = _hint_factor(conn, b, kind, hints) if hints else (1.0, [])
        c.update(verified=True, vote_count=b.get("vote_count") or c["vote_count"],
                 imdb_id=b.get("imdb_id") or (b.get("external_ids") or {}).get("imdb_id"))
        fill(c, _detail_names(b, kind), *((1.0, None, "no year given") if year is None
                                          else _detail_year(b, kind, year)))

    # coverage: matching fewer of the given titles than the best-covering candidate costs 30%
    top_cov = max((len(c["titles_matched"]) for c in cands), default=0)
    if len(titles) > 1 and top_cov > 1:
        for c in cands:
            if len(c["titles_matched"]) < top_cov:
                c["score"] = round(c["score"] * 0.7, 4)
                c["coverage_penalty"] = True
    cands.sort(key=lambda c: (-c["score"], -c["name_fit"], -c["vote_count"]))
    cands = cands[:10]
    out = {"queries": queries, "years_tried": years_tried, "candidates": cands, "best": None, "match": None}
    if not cands:
        return dict(out, status="not_found", reason="no TMDB results for any title")
    best = cands[0]
    if best["name_fit"] < 0.5:
        return dict(out, status="not_found", reason="only results with unrelated names")
    if best["score"] < 0.3:
        return dict(out, status="not_found", reason="names match only titles from other years")
    close = [c for c in cands[1:] if best["score"] - c["score"] < margin]
    notes = lambda c: c.get("hint_notes") or []
    if best["score"] < accept:
        if (best["year_diff"] == 1 and best["name_fit"] >= 0.9 and "director matches" in notes(best)
                and not any("director matches" in notes(c) for c in close)):
            return dict(out, status="matched", best=best, match=best,
                        reason="year differs by 1, but the name and the tracker's director match")
        return dict(out, status="ambiguous", reason="low score")
    if not close:
        return dict(out, status="matched", best=best, match=best, reason="clear")
    bt = set(best["titles_matched"])
    if all(set(c["titles_matched"]) < bt for c in close):
        return dict(out, status="matched", best=best, match=best,
                    reason="close runner-ups match only some of the titles the best one matches")
    if "director matches" in notes(best) and not any("director matches" in notes(c) for c in close):
        return dict(out, status="matched", best=best, match=best,
                    reason="only the best one has the tracker's director")
    if "country matches" in notes(best) and all("no common country" in notes(c) for c in close):
        return dict(out, status="matched", best=best, match=best,
                    reason="only the best one has the tracker's country")
    if best["vote_count"] >= 20 and all(c["vote_count"] == 0 for c in close):
        return dict(out, status="matched", best=best, match=best,
                    reason="close runner-ups have no TMDB votes at all")
    return dict(out, status="ambiguous", reason="close runner-up")


# ----------------------------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------------------------

def _trim(d, full: bool):
    if full or not isinstance(d, dict):
        return d
    d = dict(d)
    for k in ("cast",):
        if isinstance(d.get(k), list) and len(d[k]) > 12:
            d[k] = d[k][:12] + [f"... {len(d[k]) - 12} more"]
    if isinstance(d.get("release_dates"), list):
        d["release_dates"] = f"{len(d['release_dates'])} dates (--full to show)"
    for k in ("keywords",):
        if isinstance(d.get(k), list) and len(d[k]) > 20:
            d[k] = d[k][:20] + [f"... {len(d[k]) - 20} more"]
    return d


def main(argv=None):
    global OFFLINE
    ap = argparse.ArgumentParser(prog="films.sources.tmdb")
    ap.add_argument("--max-age", type=float, default=DEFAULT_MAX_AGE_DAYS, help="cache freshness, days")
    ap.add_argument("--offline", action="store_true", help="cache only")
    ap.add_argument("--full", action="store_true", help="don't trim long lists")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("find")
    p.add_argument("imdb_id")
    p = sub.add_parser("movie")
    p.add_argument("tmdb_id", type=int)
    p = sub.add_parser("tv")
    p.add_argument("tmdb_id", type=int)
    p.add_argument("--season", type=int)
    p = sub.add_parser("search")
    p.add_argument("title")
    p.add_argument("year", type=int, nargs="?")
    p.add_argument("kind", nargs="?", default="movie", choices=["movie", "tv", "series"])
    p.add_argument("more_titles", nargs="*", help="other titles of the same work")
    p.add_argument("--director", action="append", default=[], help="hint (repeatable)")
    p.add_argument("--country", action="append", default=[], help="hint (repeatable)")
    p = sub.add_parser("collection")
    p.add_argument("collection_id", type=int)
    p = sub.add_parser("person")
    p.add_argument("person_id", type=int)
    a = ap.parse_args(argv)
    OFFLINE = a.offline
    conn = db.connect()
    t0 = time.time()
    try:
        if a.cmd == "find":
            res = find_by_imdb(conn, a.imdb_id, a.max_age)
        elif a.cmd == "movie":
            res = _trim(movie(conn, a.tmdb_id, a.max_age), a.full)
        elif a.cmd == "tv":
            s = tv(conn, a.tmdb_id, a.max_age)
            if a.season is not None and s is not None:
                se = season(conn, a.tmdb_id, a.season, a.max_age, series=s)
                res = {"series": _trim(s, a.full) if a.full else {k: v for k, v in s.items() if k not in (
                    "trailers_ru", "trailers_uk", "videos_ru_other")},
                    "season": _trim(se, a.full)}
            else:
                res = _trim(s, a.full)
        elif a.cmd == "search":
            res = search(conn, [a.title] + a.more_titles, a.year, a.kind, a.max_age,
                         hints={"directors": a.director, "countries": a.country})
            if not a.full:
                keep = ("tmdb_id", "title", "original_title", "year", "score", "matched", "year_how",
                        "titles_matched", "hint_notes", "imdb_id", "vote_count")
                short = lambda c: {k: c[k] for k in keep if c.get(k) not in (None, [])}
                res = {"status": res["status"], "reason": res.get("reason"),
                       "best": short(res["best"]) if res["best"] else None, "queries": res["queries"],
                       "candidates": [short(c) for c in res["candidates"][:5]]}
        elif a.cmd == "collection":
            res = collection(conn, a.collection_id, a.max_age)
        else:
            res = person(conn, a.person_id, a.max_age)
    except TmdbError as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
        sys.exit(1)
    print(json.dumps(res, ensure_ascii=False, indent=1, default=list), flush=True)
    print(f"(requests {STATS['requests']}, cache hits {STATS['cache_hits']}, retries {STATS['retries']}, "
          f"{time.time() - t0:.2f} s)", file=sys.stderr)


if __name__ == "__main__":
    main()
