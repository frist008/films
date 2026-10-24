"""YouTube: Russian-language trailer for every title (movie / one series season) that has none yet.

Usage:
    python -X utf8 -m films.sources.youtube trailers [--limit N] [--recheck-days D] [--ids 1,2,3]
                                                     [--dry-run] [--no-uk] [--sample N] [--max-hours H]
    python -X utf8 -m films.sources.youtube search "Нежить 2024 трейлер на русском"   # parsed results, JSON
    python -X utf8 -m films.sources.youtube explain <title_id>                        # scored candidates
    python -X utf8 -m films.sources.youtube report                                    # counts from decisions

Access: plain HTTPS GET https://www.youtube.com/results?search_query=…&hl=ru&gl=UA with `requests` (no API key,
no browser), `ytInitialData` parsed for `videoRenderer` items.  Politeness: >= MIN_INTERVAL seconds between
requests of this process (+ random jitter); HTTP 429 / the "unusual traffic" (/sorry/) page / a page without
ytInitialData → back off BACKOFF seconds, and after the last step raise `Blocked` so the run stops (it never
keeps hammering).  The EU consent interstitial (consent.youtube.com) is answered with the "reject all" cookie
SOCS=CAI, which is also preset.

Cache (files, data/cache/youtube/):
  searches.jsonl   one line per fetched search: query, fetched_at, status, parsed videos (trimmed videoRenderer:
                   id, title, channel, length, published text, views, badges, description snippet).  A query
                   younger than SEARCH_MAX_AGE_DAYS is answered from here, so re-scoring never refetches.
  decisions.jsonl  one line per title per run: status, chosen video, scored candidates (top + rejected with
                   reasons), Ukrainian / subtitled findings, queries used.
  ambiguous.jsonl, uk_trailers.jsonl, subtitled_only.jsonl
                   snapshots of the latest decision per title, rewritten at the end of every run / `report`.

DB writes (titles only, short transactions, never over an existing trailer):
  accepted  → trailer_ru_url = https://www.youtube.com/watch?v=<id>, trailer_ru_source = 'youtube_search',
              trailer_checked_at = now
  otherwise → trailer_checked_at = now (so re-runs skip recent misses; --recheck-days decides how recent)
Every UPDATE has `AND trailer_ru_url IS NULL`, so a TMDB trailer (or one written meanwhile) is never replaced.

Verification — a video is accepted only when it is very likely THIS title's Russian-language trailer; the rules
are in `evaluate()` / `decide()` and docs/youtube.md.  Ukrainian-language trailers and Russian-subtitled
original trailers are recorded as separate findings and never written to trailer_ru_url.
"""
from __future__ import annotations

import argparse
import difflib
import json
import math
import random
import re
import sqlite3
import sys
import time
import traceback
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import requests

from films import db

CACHE_DIR = db.ROOT / "data" / "cache" / "youtube"
SEARCH_CACHE = CACHE_DIR / "searches.jsonl"
DECISIONS = CACHE_DIR / "decisions.jsonl"
VIDEO_CACHE = CACHE_DIR / "videos.jsonl"
AUDIO_CHECKS = CACHE_DIR / "audio_checks.jsonl"   # written by films.sources.trailer_lang (speech check)
WATCH_URL = "https://www.youtube.com/watch"

SEARCH_URL = "https://www.youtube.com/results"
MIN_INTERVAL = 1.8          # seconds between requests (task: >= 1.5 s)
JITTER = 0.6                # + uniform(0, JITTER)
BACKOFF = [60, 180, 600]    # seconds; after the last one fails -> Blocked
TIMEOUT = 30
SEARCH_MAX_AGE_DAYS = 30
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/130.0.0.0 Safari/537.36")
PARSER_VERSION = 1

MIN_LEN, MAX_LEN = 30, 300  # trailer duration window, seconds
TEASER_LEN = 90             # shorter = teaser: allowed only when no longer Russian trailer exists (owner, 2026-10-24)
ACCEPT_SCORE = 60           # see decide(); tuned on the hand-verified sample (docs/youtube.md)


class Blocked(Exception):
    """YouTube refuses us (429 / captcha / no data) even after backing off — stop the run and report."""


# ------------------------------------------------------------------------------------------------
# HTTP client + search cache
# ------------------------------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


class YouTube:
    def __init__(self, min_interval: float = MIN_INTERVAL, max_age_days: float = SEARCH_MAX_AGE_DAYS,
                 offline: bool = False, log=None):
        self.min_interval = max(1.5, min_interval)
        self.max_age_days = max_age_days
        self.offline = offline
        self.log = log or (lambda msg: print(msg, file=sys.stderr, flush=True))
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.5"})
        self.s.cookies.set("SOCS", "CAI", domain=".youtube.com")   # consent: "reject all"
        self.last = 0.0
        self.stats = {"requests": 0, "from_cache": 0, "consent_pages": 0, "http_429": 0, "sorry_pages": 0,
                      "no_initial_data": 0, "backoffs": 0, "errors": 0, "backoff_seconds": 0,
                      "watch_requests": 0, "watch_from_cache": 0}
        self.video_cache: dict[str, dict] = {}
        if VIDEO_CACHE.exists():
            with VIDEO_CACHE.open(encoding="utf-8") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                        self.video_cache[rec["id"]] = rec
                    except (json.JSONDecodeError, KeyError):
                        continue
        self.cache: dict[str, dict] = {}
        if SEARCH_CACHE.exists():
            with SEARCH_CACHE.open(encoding="utf-8") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("status") == "ok":
                        self.cache[rec["query"]] = rec

    def _throttle(self):
        wait = self.last + self.min_interval + random.uniform(0, JITTER) - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self.last = time.monotonic()

    def _get(self, url: str, marker: str = "ytInitialData") -> str:
        consent_tries = 0
        step = 0
        while True:
            self._throttle()
            from ..locks import pace
            pace("youtube_http", self.min_interval)   # one shared rate for all parallel trailer runs (semaphore, 2026-10-24)
            self.stats["requests"] += 1
            problem = None
            try:
                r = self.s.get(url, timeout=TIMEOUT)
            except requests.RequestException as e:
                self.stats["errors"] += 1
                problem = f"network: {type(e).__name__}"
                r = None
            if r is not None:
                if "consent.youtube.com" in r.url or 'action="https://consent.youtube.com' in r.text[:200000]:
                    self.stats["consent_pages"] += 1
                    consent_tries += 1
                    if consent_tries > 2:
                        raise Blocked("consent interstitial keeps coming back despite SOCS cookie")
                    self.s.cookies.set("SOCS", "CAI", domain=".youtube.com")
                    self.s.cookies.set("CONSENT", "PENDING+987", domain=".youtube.com")
                    continue
                if r.status_code == 429:
                    self.stats["http_429"] += 1
                    problem = "HTTP 429"
                elif "/sorry/" in r.url or "unusual traffic" in r.text[:50000]:
                    self.stats["sorry_pages"] += 1
                    problem = "captcha /sorry/ page"
                elif r.status_code >= 500:
                    self.stats["errors"] += 1
                    problem = f"HTTP {r.status_code}"
                elif r.status_code != 200:
                    raise Blocked(f"unexpected HTTP {r.status_code} for {url}")
                elif marker not in r.text:
                    self.stats["no_initial_data"] += 1
                    problem = f"page without {marker}"
                else:
                    return r.text
            if step >= len(BACKOFF):
                raise Blocked(f"{problem} persists after backing off {sum(BACKOFF)} s ({url})")
            pause = BACKOFF[step] + random.uniform(0, 10)
            step += 1
            self.stats["backoffs"] += 1
            self.stats["backoff_seconds"] += int(pause)
            self.log(f"  youtube: {problem}; backing off {pause:.0f} s (step {step}/{len(BACKOFF)})")
            time.sleep(pause)

    def watch(self, video_id: str) -> dict:
        """What the watch page says about one video (cached in VIDEO_CACHE): playability from here (gl=UA),
        the video's own title (search pages may show a *translated* title: Moviefone, Prime Video, Melor Film …),
        audio tracks (the original one vs YouTube's automatic AI dubs) and caption tracks (automatic 'asr'
        captions are in the spoken language) → `audio_lang`."""
        rec = self.video_cache.get(video_id)
        if rec and (self.offline or (_now() - datetime.fromisoformat(rec["fetched_at"])).days <= self.max_age_days):
            self.stats["watch_from_cache"] += 1
            return rec
        if self.offline:
            return {"id": video_id, "status": None}
        self.stats["watch_requests"] += 1
        html = self._get(f"{WATCH_URL}?{urlencode({'v': video_id, 'hl': 'ru', 'gl': 'UA'})}",
                         marker="ytInitialPlayerResponse")
        rec = parse_watch(html, video_id)
        rec["fetched_at"] = _now().isoformat(timespec="seconds")
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with VIDEO_CACHE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.video_cache[video_id] = rec
        return rec

    def search(self, query: str) -> dict:
        """{'query', 'fetched_at', 'status', 'videos': [...]} — from the cache when fresh enough."""
        rec = self.cache.get(query)
        if rec:
            age = (_now() - datetime.fromisoformat(rec["fetched_at"])).total_seconds() / 86400
            if self.offline or age <= self.max_age_days:
                self.stats["from_cache"] += 1
                return rec
        if self.offline:
            return {"query": query, "fetched_at": _now().isoformat(timespec="seconds"), "status": "offline",
                    "videos": []}
        html = self._get(f"{SEARCH_URL}?{urlencode({'search_query': query, 'hl': 'ru', 'gl': 'UA'})}")
        videos = parse_results(html)
        rec = {"query": query, "fetched_at": _now().isoformat(timespec="seconds"), "status": "ok",
               "parser": PARSER_VERSION, "videos": videos}
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with SEARCH_CACHE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.cache[query] = rec
        return rec


# ------------------------------------------------------------------------------------------------
# ytInitialData parsing
# ------------------------------------------------------------------------------------------------

def _initial_data(html: str) -> dict | None:
    m = re.search(r"(?:var ytInitialData|window\[\"ytInitialData\"\])\s*=\s*", html)
    if not m:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(html, m.end())
        return obj
    except json.JSONDecodeError:
        return None


def _text(o) -> str:
    if not o:
        return ""
    if "simpleText" in o:
        return o["simpleText"]
    return "".join(r.get("text", "") for r in o.get("runs", []))


def _walk_videos(o):
    if isinstance(o, dict):
        for k, v in o.items():
            if k == "videoRenderer" and isinstance(v, dict):
                yield v
            else:
                yield from _walk_videos(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk_videos(v)


def parse_length(s: str | None) -> int | None:
    if not s or not re.fullmatch(r"\d+(:\d{2}){0,2}", s.strip()):
        return None
    sec = 0
    for p in s.strip().split(":"):
        sec = sec * 60 + int(p)
    return sec


def parse_views(s: str | None) -> int | None:
    if not s:
        return None
    digits = re.sub(r"\D", "", s)
    return int(digits) if digits else (0 if re.search(r"нет просмотров|no views", s, re.I) else None)


_AGO_UNITS = [  # (regex, days per unit)
    (r"сек", 1 / 86400), (r"мин", 1 / 1440), (r"час|ч\.", 1 / 24), (r"дн|день|дня", 1), (r"нед", 7),
    (r"мес", 30.44), (r"г\.|год|лет|л\.|г$|л$", 365.25),
    (r"second", 1 / 86400), (r"minute", 1 / 1440), (r"hour", 1 / 24), (r"day", 1), (r"week", 7),
    (r"month", 30.44), (r"year", 365.25),
]


def parse_ago(s: str | None) -> tuple[float, float] | None:
    """'2 г. назад' -> (min_days, max_days) ago: 2..3 years.  None when not understood."""
    if not s:
        return None
    m = re.search(r"(\d+)\s*([^\d\s]+)", s)
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2).lower()
    for rx, days in _AGO_UNITS:
        if re.match(rx, unit):
            return (n * days, (n + 1) * days)
    return None


def parse_results(html: str) -> list[dict]:
    data = _initial_data(html)
    if data is None:
        return []
    out, seen = [], set()
    for v in _walk_videos(data):
        vid = v.get("videoId")
        if not vid or vid in seen:
            continue
        seen.add(vid)
        owner = (v.get("ownerText") or v.get("longBylineText") or {}).get("runs") or [{}]
        be = ((owner[0].get("navigationEndpoint") or {}).get("browseEndpoint") or {})
        badges = [(b.get("metadataBadgeRenderer") or {}).get("label") or (b.get("metadataBadgeRenderer") or {}).get("style")
                  for b in v.get("badges") or []]
        owner_badges = [(b.get("metadataBadgeRenderer") or {}).get("style") for b in v.get("ownerBadges") or []]
        snippet = " ".join(_text(s.get("snippetText")) for s in v.get("detailedMetadataSnippets") or [])
        length_text = _text(v.get("lengthText"))
        live = "LIVE_NOW" in json.dumps(v.get("badges") or []) or "LIVE" in json.dumps(v.get("thumbnailOverlays") or [])
        out.append({
            "id": vid, "pos": len(out), "title": _text(v.get("title")),
            "channel": owner[0].get("text"), "channel_id": be.get("browseId"),
            "channel_url": be.get("canonicalBaseUrl"),
            "length_text": length_text or None, "length": parse_length(length_text),
            "published": _text(v.get("publishedTimeText")) or None,
            "views": parse_views(_text(v.get("viewCountText"))),
            "badges": [b for b in badges if b], "owner_badges": [b for b in owner_badges if b],
            "snippet": snippet or None, "live": live or None,
        })
    return out


def _player_response(html: str) -> dict | None:
    m = re.search(r"var ytInitialPlayerResponse\s*=\s*", html)
    if not m:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(html, m.end())
        return obj
    except json.JSONDecodeError:
        return None


def _base_lang(code: str | None) -> str | None:
    return code.split(".")[0].split("-")[0].lower() if code else None


def audio_language(tracks: list[dict], caps: list[dict]) -> tuple[str | None, str]:
    """Spoken language of a video: with YouTube's automatic AI dubs present, the original (not auto-dubbed)
    audio track; otherwise the single automatic ('asr') caption track (YouTube transcribes the spoken
    language); otherwise an uploader-declared audio track.  Both signals come from YouTube's language
    metadata and can be wrong (seen once: a "Русский дублированный трейлер" with 'en' asr), so a title that
    says дубляж / озвучка wins over them (see _verify)."""
    originals = {_base_lang(t["id"]) for t in tracks if not t.get("auto_dub")}
    asr = {_base_lang(c["lang"]) for c in caps if c.get("kind") == "asr"}
    if originals and any(t.get("auto_dub") for t in tracks):
        return ("ru" if "ru" in originals else sorted(originals)[0]), "audio_track"
    if len(asr) == 1:
        return next(iter(asr)), "asr_captions"
    if originals:
        return ("ru" if "ru" in originals else sorted(originals)[0]), "audio_track"
    return None, ("several_asr_tracks" if asr else "no_signal")


def parse_watch(html: str, video_id: str) -> dict:
    """Compact facts from a watch page (see YouTube.watch)."""
    pr = _player_response(html) or {}
    ps = pr.get("playabilityStatus") or {}
    vd = pr.get("videoDetails") or {}
    mf = (pr.get("microformat") or {}).get("playerMicroformatRenderer") or {}
    tracks, seen = [], set()
    for f in (pr.get("streamingData") or {}).get("adaptiveFormats") or []:
        t = f.get("audioTrack")
        if t and t.get("id") not in seen:
            seen.add(t.get("id"))
            tracks.append({"id": t.get("id"), "auto_dub": bool(t.get("isAutoDubbed")), "default": t.get("audioIsDefault")})
    caps = [{"lang": c.get("languageCode"), "kind": c.get("kind")}
            for c in ((pr.get("captions") or {}).get("playerCaptionsTracklistRenderer") or {}).get("captionTracks") or []]
    audio_lang, how = audio_language(tracks, caps)
    return {
        "id": video_id, "status": ps.get("status"), "reason": ps.get("reason"),
        "title": vd.get("title"), "author": vd.get("author"), "channel_id": vd.get("channelId"),
        "length": int(vd["lengthSeconds"]) if str(vd.get("lengthSeconds") or "").isdigit() else None,
        "views": int(vd["viewCount"]) if str(vd.get("viewCount") or "").isdigit() else None,
        "publish_date": mf.get("publishDate"), "upload_date": mf.get("uploadDate"),
        "category": mf.get("category"), "is_family_safe": mf.get("isFamilySafe"),
        "available_in_ua": ("UA" in mf["availableCountries"]) if mf.get("availableCountries") else None,
        "description": (vd.get("shortDescription") or "")[:600],
        "audio_tracks": tracks, "captions": caps, "audio_lang": audio_lang, "audio_lang_how": how,
        "ru_manual_captions": any(_base_lang(c["lang"]) == "ru" and c["kind"] != "asr" for c in caps),
    }


# ------------------------------------------------------------------------------------------------
# text helpers
# ------------------------------------------------------------------------------------------------

_PUNCT = re.compile(r"[^\w]+|_")


def norm(s: str | None) -> str:
    """lower-case, ё→е (and other diacritics stripped), punctuation → space."""
    if not s:
        return ""
    s = s.replace("&", " и ").replace("№", " ")
    s = unicodedata.normalize("NFKD", s.casefold())
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return " ".join(_PUNCT.sub(" ", s).split())


def tokens(s: str | None) -> list[str]:
    return norm(s).split()


def find_phrase(hay: list[str], needle: list[str]) -> list[int]:
    if not needle or len(needle) > len(hay):
        return []
    n = len(needle)
    return [i for i in range(len(hay) - n + 1) if hay[i:i + n] == needle]


_CYR = re.compile(r"[а-яё]", re.I)
_UK_LETTERS = re.compile(r"[іїєґ]", re.I)
_UK_WORDS = re.compile(r"українськ|укр\.?\s*мов|\bукр\b|\bua\b|\bукраїн|дубляж\s+укр|озвучк\w*\s+укр", re.I)
_TRAILER_RU = re.compile(r"тр[еэ]йл[еэ]р|тизер", re.I)
_TRAILER_EN = re.compile(r"\btrailer\b|\bteaser\b", re.I)
_RU_HINT = re.compile(r"русск|\bru\b|\brus\b|дубляж|дублир|озвуч", re.I)
_TEASER = re.compile(r"тизер|teaser", re.I)
_SUBTITLED = re.compile(r"субтитр|\bsub(s|bed|titles)?\b|\bсаб\b", re.I)
_ORIGINAL_LANG = re.compile(r"на английском|\beng\b|\bengl?ish\b|оригинал(ьн\w*)?\s+(трейлер|язык|озвуч)|"
                            r"в оригинале|без перевода|original version|мовою оригіналу|в оригіналі|оригінальн\w* (?:трейлер|мов)", re.I)
_OTHER_CYR = re.compile(r"[әғқңөұүһў]", re.I)     # Kazakh / Belarusian letters: not a Ukrainian video
_DUB = re.compile(r"дубляж|дублир", re.I)
# says the audio is Russian: strong = dub / voice-over; weak = "русский трейлер" (also used by subtitled uploads)
_RU_AUDIO_STRONG = re.compile(r"дубл|озвуч|закадров|многоголос|двухголос|одноголос|перевод\w* (?:от|студ)|"
                              r"red head sound|\brhs\b|jaskier|lostfilm|tvshows|newstudio|alexfilm|кубик в кубе", re.I)
_RU_AUDIO_WEAK = re.compile(r"русск|на русском|\brus\b|\bru\b|\bрус\b", re.I)
_NEURO = re.compile(r"нейро", re.I)
_OFFICIAL = re.compile(r"официальн|official", re.I)
# not a trailer of the title (task: review / обзор / разбор / реакция / клип / сцена / отрывок / fan-made ...)
_NOT_TRAILER = re.compile(
    r"обзор|разбор|реакци|reaction|\breview\b|\bклип|\bсцен[аыу]\b|отрыв[оa]к|фрагмент|фанатск|fan[\s-]?made|"
    r"\bфан\b|фан[\s-]?трейлер|\bfan[\s-]?trailer|"
    r"концепт|\bconcept\b|пароди|parody|переозвуч|смешн|прикол|пересказ|теори[ия]|что известно|"
    r"дата выхода|когда выйдет|выйдет ли|новост|\bnews\b|интервью|interview|за кадром|behind the scenes|"
    r"featurette|making of|со съ[её]мок|backstage|саундтрек|soundtrack|\bost\b|music video|песн[яи]|"
    r"фильм целиком|весь фильм|полный фильм|full movie|смотреть онлайн|все серии|\bсери[яи]\b|эпизод|"
    r"\bepisode\b|#shorts|\bshorts\b|отзыв|\bмнение|покадров|пасхалк|easter egg|сравнени|\bvs\b|"
    r"нейросет|\bai\b|\bии\b|сгенерир|deepfake|дипфе[йи]к|рецензи|итоги|топ\s?\d|\btop\s?\d|подборк|"
    r"лучшие фильмы|фильмы недели|трейлеры недели|все трейлеры|новые трейлеры|главные трейлеры|"
    r"трейлеры \w+ (?:20\d\d|года|месяца|недели)|трейлеры фильмов|\bпромокод|reupload|не официальн|неофициальн|"
    r"первый взгляд|first look|спойлер|\bending\b|финал объяснен|объяснени|анализ|"
    r"\bтв[\s-]?спот|\btv[\s-]?spot|ролик о|о съ[её]мках|кастинг",
    re.I)
_KIND_SERIES = re.compile(r"сериал|\bсезон|season|\bсери[ия]\b|мини-сериал|аниме-сериал", re.I)
_KIND_MOVIE = re.compile(r"\bфильм|\bмультфильм|\bкино\b|\bmovie\b|\bfilm\b", re.I)
_YEAR = re.compile(r"(?<!\d)(19[5-9]\d|20[0-3]\d)(?!\d)")

_ORDINALS = {"перв": 1, "втор": 2, "трет": 3, "четверт": 4, "пят": 5, "шест": 6, "седьм": 7, "восьм": 8,
             "девят": 9, "десят": 10}
_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8, "ix": 9, "x": 10}


def season_mentions(title: str) -> set[int]:
    """Season numbers a video title names: '2 сезон', 'сезон 2', '2-й сезон', 'второй сезон', 'Season 2',
    'S2', 'S02E01' (not episode numbers), roman 'сезон II'."""
    t = norm(title).replace("ё", "е")
    nums: set[int] = set()
    for m in re.finditer(r"\b(?:сезон\w*|season|s)\s*0?(\d{1,2})\b", t):
        nums.add(int(m.group(1)))
    for m in re.finditer(r"\b(\d{1,2})\s*(?:и|ии|ои|го|ыи)?\s*(?:сезон\w*|season)\b", t):
        nums.add(int(m.group(1)))
    for m in re.finditer(r"\bs(\d{1,2})e\d{1,3}\b", t):
        nums.add(int(m.group(1)))
    for m in re.finditer(r"\b(перв|втор|трет|четверт|пят|шест|седьм|восьм|девят|десят)\w*\s+сезон", t):
        nums.add(_ORDINALS[m.group(1)])
    for m in re.finditer(r"\bсезон\w*\s+(i|ii|iii|iv|v|vi|vii|viii|ix|x)\b", t):
        nums.add(_ROMAN[m.group(1)])
    return {n for n in nums if 0 < n < 60}


# words that explain a video title besides the film's own name (used for the "leftover" penalty)
_FILLER = set(norm(
    "трейлер трейлеры трейлера трэйлер тизер тизертрейлер тизер-трейлер официальный официальная официальные "
    "русский русская русские русском русской русскоязычный рус ru rus russian на в во с со и к от для по "
    "дубляж дублированный дублированная дубляжом озвучка озвучке озвучкой озвучании многоголосая многоголосый "
    "финальный финальная новый новая второй третий первый основной международный главный полный полная "
    "версия фильм фильма фильму фильмы кинофильм сериал сериала сериалу мультфильм мультфильма мультсериал аниме "
    "анимационный документальный "
    "сезон сезона сезоне season сезоны премьера премьеры премьерный год года г серия выход выходит "
    "hd fullhd uhd 4k 8k 1080p 720p full official trailer teaser tv тв 18 16 12 6 0 "
    "кино кинотеатрах кинотеатре кинотеатры прокат прокате онлайн смотрите смотреть только эксклюзивно "
    "эксклюзив скоро уже январь февраль март апрель май июнь июль август сентябрь октябрь ноябрь декабрь "
    "января февраля марта апреля мая июня июля августа сентября октября ноября декабря "
    "netflix нетфликс amazon prime video apple disney hbo max hulu peacock paramount кинопоиск kinopoisk okko "
    "окко иви ivi wink винк start старт premier премьер kion кион амедиатека amediateka more тв3 тнт стс "
    "ужасы триллер комедия драма боевик фантастика фэнтези мелодрама детектив криминал приключения "
    "военный исторический семейный биография вестерн мюзикл спорт "
    "2 3 4 5 6 7 8 9 10 1 часть"
).split()) | {str(y) for y in range(1950, 2036)}


# ------------------------------------------------------------------------------------------------
# channels
# ------------------------------------------------------------------------------------------------

# Russian-language trailer channels seen posting correct, full-length trailers (distributors, dubbing
# studios, platforms, big trailer aggregators).  Matching is on norm(channel name).  Only a score bonus —
# a channel never makes a wrong title right.  (Candidate for config/ — see docs/youtube.md.)
TRUSTED_CHANNELS = {norm(c) for c in [
    "World Pictures", "Kinoman", "Киноман", "топКино", "TopKino", "Кино Трейлеры", "Все трейлеры",
    "TrailerOK", "Кинопоиск", "КИНОПОИСК", "Okko", "Иви", "Wink", "START", "PREMIER", "KION",
    "Амедиатека", "Amediateka", "Централ Партнершип", "Вольга", "Volga", "Пионер", "Pioneer Film",
    "Каро Премьер", "KARO.ART", "Атмосфера кино", "Экспонента", "Exponenta", "Freedom", "ПРОвзгляд",
    "Kinologistika", "Кинологистика", "Флагман", "Global Film", "Russian Report", "Наше Кино",
    "Red Head Sound", "Jaskier", "TVShows", "LostFilm", "LostFilm.TV", "HDrezka Studio", "Кубик в Кубе",
    "NewStudio", "AlexFilm", "SDI Media", "Novamedia", "КИНОНОВОСТИ", "Trailers Россия",
    "Universal Pictures Russia", "Sony Pictures Russia", "Disney Россия", "WB Россия", "Paramount Россия",
    "Netflix Россия", "Иллюзион", "Mister Star", "Кинотеатр Октябрь", "Синема Парк", "Формула Кино",
    "Плюс Студия", "КиноТВ",
]}


def channel_trusted(name: str | None) -> bool:
    return norm(name) in TRUSTED_CHANNELS


# ------------------------------------------------------------------------------------------------
# title context + candidate evaluation
# ------------------------------------------------------------------------------------------------

def _date(s: str | None) -> date | None:
    try:
        return date.fromisoformat(s[:10]) if s else None
    except ValueError:
        return None


_RU_INDEX: dict[str, list[dict]] | None = None


def _ru_index(conn) -> dict[str, list[dict]]:
    global _RU_INDEX
    if _RU_INDEX is None:
        _RU_INDEX = {}
        for o in conn.execute("SELECT id, year, kind, series_key, title_ru FROM titles"):
            _RU_INDEX.setdefault(norm(o["title_ru"]), []).append(
                {"id": o["id"], "year": o["year"], "kind": o["kind"], "series_key": o["series_key"]})
    return _RU_INDEX


def title_context(conn, row) -> dict:
    """Everything evaluate() needs about one title (dict built from a titles row + DB lookups)."""
    r = dict(row)
    ctx = {k: r.get(k) for k in ("id", "kind", "season_number", "seasons_total", "title_ru", "title_original",
                                 "title_latin", "title_uk", "year", "release_date", "is_animation", "series_key")}
    ctx["year_source"] = "titles.year" if r.get("year") else None
    if not ctx["year"]:   # no TMDB year: tracker years (for a season possibly the series start year)
        ys = [y for (y,) in conn.execute(
            """SELECT DISTINCT CAST(json_extract(rl.parsed, '$.year') AS INTEGER) FROM release_titles rt
               JOIN releases rl ON rl.id = rt.release_id WHERE rt.title_id = ? AND json_extract(rl.parsed, '$.year')
               IS NOT NULL""", (r["id"],)) if y]
        if ys:
            ctx["year"] = max(ys)
            ctx["year_source"] = "releases.parsed.year"
    # other DB titles with the same Russian name (another film or another series)
    nru = norm(r.get("title_ru"))
    ctx["homonyms_db"] = [o for o in _ru_index(conn).get(nru, [])
                          if o["id"] != r["id"] and not (r.get("series_key") and o["series_key"] == r.get("series_key"))]
    # top-billed actors and directors: their names in a video title confirm it (and are not "other words")
    cast_tokens: set[str] = set()
    for (name,) in conn.execute(
            """SELECT p.name FROM credits cr JOIN people p ON p.id = cr.person_id WHERE cr.title_id = ?
               AND (cr.role = 'director' OR (cr.role = 'actor' AND coalesce(cr.ord, 99) < 8))""", (r["id"],)):
        for t in tokens(name):
            if len(t) >= 4:
                cast_tokens.add(t)
                if re.fullmatch(r"[a-z]+", t):
                    cast_tokens.add(translit(t))
    ctx["cast_tokens"] = cast_tokens
    # premieres of the other seasons of the same series (for season trailers that name no season)
    ctx["season_dates"] = {}
    if r.get("series_key"):
        for o in conn.execute("SELECT season_number, release_date, year FROM titles WHERE series_key = ?",
                              (r["series_key"],)):
            ctx["season_dates"][o["season_number"]] = o["release_date"] or (f"{o['year']}-07-01" if o["year"] else None)
    return ctx


def _published_range(v: dict, fetched_at: str) -> tuple[date, date] | None:
    ago = parse_ago(v.get("published"))
    if not ago:
        return None
    base = datetime.fromisoformat(fetched_at).date()
    return (base - timedelta(days=ago[1]), base - timedelta(days=ago[0]))   # (earliest, latest)


_CONJ = {"и", "and", "et", "y", "und", "e"}


def _name_tokens(ctx: dict) -> dict[str, list[str]]:
    """Name variants to look for in a video title: ru, ru_head (before ':' / ' - '), ru_noconj ('Любовь смерть
    роботы' for 'Любовь, смерть и роботы'), orig (original title, or its latin transliteration for non-latin
    originals), orig_noconj, latin (the other one), orig_head, uk."""
    names: dict[str, list[str]] = {}
    ru = tokens(ctx.get("title_ru"))
    if ru:
        names["ru"] = ru
        head = tokens(re.split(r"[:.!?]|\s[-—–]\s", ctx["title_ru"])[0])
        if head and head != ru and len(" ".join(head)) >= 4:
            names["ru_head"] = head
        nc = [t for t in ru if t not in _CONJ]
        if nc != ru and len(nc) >= 2:
            names["ru_noconj"] = nc
    orig_raw = ctx.get("title_original") or ""
    latin_raw = ctx.get("title_latin") or ""
    if latin_raw and not re.search(r"[A-Za-z]", orig_raw):
        orig_raw, latin_raw = latin_raw, orig_raw
    o = tokens(orig_raw)
    if o and o != ru and len(" ".join(o)) >= 3:
        names["orig"] = o
        oh = tokens(re.split(r"[:]|\s[-—–]\s", orig_raw)[0])
        if oh and oh != o and len(" ".join(oh)) >= 4:
            names["orig_head"] = oh
        nc = [t for t in o if t not in _CONJ]
        if nc != o and len(nc) >= 2:
            names["orig_noconj"] = nc
    la = tokens(latin_raw)
    if la and la != ru and la != o and len(" ".join(la)) >= 3:
        names["latin"] = la
    uk = tokens(ctx.get("title_uk"))
    if uk and uk != ru:
        names["uk"] = uk
    return names


# separators that end a "segment" of a video title ("Нежить — Русский трейлер (2024) | Kinoman"); a hyphen
# inside a word ("Эники-Беники") is not one, any emoji / symbol is
_SEP = re.compile(r"\s[-–—]+\s|[-–—]{2,}|[|/\\()\[\]{}«»\"“”„≪≫:;,.!?#•·~+*=<>]|[^\w\s'’\-–—]")


def segmented_tokens(title: str) -> tuple[list[str], list[int]]:
    """Normalized tokens of a video title and, for each, the index of the segment it is in."""
    toks, segs = [], []
    for i, part in enumerate(_SEP.split(title or "")):
        for t in tokens(part):
            toks.append(t)
            segs.append(i)
    return toks, segs


# words that may stand right next to the name inside one segment without making it another name
_ADJ_OK = {t for t in _FILLER if t not in set(norm(
    "на в во с со и к от для по ужасы триллер комедия драма боевик фантастика фэнтези мелодрама детектив "
    "криминал приключения военный исторический семейный биография вестерн мюзикл спорт новый новая второй "
    "третий первый основной главный полный полная часть").split())}


def _sequel_conflict(hay: list[str], start: int, length: int, needle: list[str]) -> str | None:
    """'Нежить 2' for our 'Нежить' (or 'Нежить: Часть вторая')."""
    nxt = hay[start + length] if start + length < len(hay) else None
    nxt2 = hay[start + length + 1] if start + length + 1 < len(hay) else None
    if nxt is None:
        return None
    if re.fullmatch(r"\d{1,2}|ii|iii|iv|vi|vii", nxt) and nxt2 not in ("сезон", "сезона", "season", "серия",
                                                                         "и", "ии", "ои", "ыи", "го", "сезоне"):
        return f"sequel number '{nxt}' after the name"
    if nxt in ("часть", "part", "chapter", "глава") and nxt2 and nxt2 not in needle:
        return f"'{nxt} {nxt2}' after the name"
    return None


def _extension(hay: list[str], segs: list[int], start: int, length: int) -> list[str]:
    """Words glued to the name inside the same segment ('Проклятые воды' for 'Проклятые')."""
    out = []
    end = start + length
    if start > 0 and segs[start - 1] == segs[start] and hay[start - 1] not in _ADJ_OK and not hay[start - 1].isdigit():
        out.append(hay[start - 1])
    if end < len(hay) and segs[end] == segs[end - 1] and hay[end] not in _ADJ_OK and not hay[end].isdigit():
        out.append(hay[end])
    return out


def evaluate(v: dict, ctx: dict, fetched_at: str) -> dict:
    """Score one search result for one title.  Returns {'id', 'score', 'ok', 'lang', 'kind', 'flags',
    'reject' (list of hard reasons), 'why' (list of score parts)}.  `ok` = Russian trailer candidate that
    passed every hard rule; lang 'uk' / 'subtitled' candidates are findings, never ok."""
    title = v.get("title") or ""
    hay, segs = segmented_tokens(title)
    reject, why, flags = [], [], []
    score = 0.0
    names = _name_tokens(ctx)

    # --- name match ---------------------------------------------------------------------------------
    matched: dict[str, tuple[int, int]] = {}
    for key, needle in names.items():
        pos = find_phrase(hay, needle)
        if pos:
            matched[key] = (pos[0], len(needle))
    ru_hit = "ru" in matched or "ru_noconj" in matched
    orig_hit = "orig" in matched or "latin" in matched or "orig_noconj" in matched
    if ru_hit:
        score += 40; why.append("+40 ru name")
    elif "ru_head" in matched:
        score += 22; why.append("+22 ru name before subtitle"); flags.append("partial_name")
    if orig_hit:
        add = 15 if (ru_hit or "ru_head" in matched) else 30
        score += add; why.append(f"+{add} original name"); flags.append("orig_in_title")
    elif "orig_head" in matched and not ru_hit:
        score += 15; why.append("+15 original name before subtitle"); flags.append("partial_name")
    uk_name_only = "uk" in matched and not ru_hit and "ru_head" not in matched and not orig_hit
    if not (ru_hit or orig_hit or "ru_head" in matched or "orig_head" in matched or "uk" in matched):
        reject.append("name not in video title")
    name_pos = set()
    for key, (p, n) in matched.items():
        name_pos |= set(range(p, p + n))
    for key in ("ru", "ru_noconj", "orig", "orig_noconj", "latin", "ru_head", "orig_head"):
        if key in matched:
            p, n = matched[key]
            c = _sequel_conflict(hay, p, n, names[key])
            if c:
                reject.append(c)
            ext = _extension(hay, segs, p, n)
            # a longer name only counts when the full name of the other kind did not match too
            if ext and not (key.startswith("ru") and orig_hit) and not (key.startswith(("orig", "latin")) and ru_hit):
                score -= 30; why.append(f"-30 name continues: {ext}"); flags.append("name_extended")
            break
    rest = " ".join(t for i, t in enumerate(hay) if i not in name_pos)   # the title without the name itself

    # --- leftover words: what in the video title is not the name / filler / channel ------------------
    explained = set(_FILLER) | set(tokens(v.get("channel")))
    for needle in names.values():
        explained |= set(needle)
    left = [t for t in hay if t not in explained and not t.isdigit() and len(t) > 2]
    cast = ctx.get("cast_tokens") or set()
    named = [t for t in left if _name_like(t, cast)]
    exact = [t for t in named if t in cast]
    if not exact and len(named) < 2:    # a single fuzzy hit ('Тайна' ~ 'Tapia') proves nothing
        named = []
    if named:
        flags.append("cast_in_title")
        why.append(f"cast/director names {named[:4]}")
        left = [t for t in left if t not in named]
    if left:
        pen = min(30, 6 * len(left))
        score -= pen; why.append(f"-{pen} other words {left[:6]}")
        if len(left) >= 3:
            flags.append("many_other_words")

    # --- trailer + language -----------------------------------------------------------------------
    lang = "ru"
    channel = v.get("channel") or ""
    if _OTHER_CYR.search(title):
        lang = "other"
    elif _UK_LETTERS.search(title) or _UK_WORDS.search(title):
        lang = "uk"
    elif not _CYR.search(title):
        lang = "other"
    is_trailer = bool(_TRAILER_RU.search(title)) or bool(_TRAILER_EN.search(title) and _RU_HINT.search(title))
    if not is_trailer:
        reject.append("no трейлер/тизер in title")
    kind = "teaser" if _TEASER.search(title) and not re.search(r"тизер[\s-]*трейлер|teaser trailer", title, re.I) \
        else "trailer"
    if is_trailer:
        if kind == "trailer":
            score += 10; why.append("+10 trailer")
        else:
            score += 5; why.append("+5 teaser")
    if lang == "other":
        reject.append("not a Russian-language title (no Cyrillic, or Kazakh / Belarusian letters)")
    if lang == "ru" and (_SUBTITLED.search(title) or _SUBTITLED.search(channel)):
        lang = "subtitled"
    if lang in ("ru", "subtitled", "uk") and _ORIGINAL_LANG.search(title):
        reject.append("original-language trailer")
    if uk_name_only and lang == "ru":
        flags.append("uk_name_only")
    if _DUB.search(title):
        score += 12; why.append("+12 says дубляж"); flags.append("ru_audio_strong")
    elif _RU_AUDIO_STRONG.search(title):
        score += 9; why.append("+9 says voice-over"); flags.append("ru_audio_strong")
    elif _RU_AUDIO_WEAK.search(title):
        score += 6; why.append("+6 says русский"); flags.append("ru_audio_weak")
    elif _OFFICIAL.search(title):
        score += 3; why.append("+3 official")
    if _NEURO.search(title):
        score -= 10; why.append("-10 neural (AI) voice-over"); flags.append("neuro_dub")
    nt = _NOT_TRAILER.search(rest) or (re.search(r"#shorts", title, re.I))
    if nt:
        reject.append(f"not a trailer: '{nt.group(0)}'")

    # --- duration ------------------------------------------------------------------------------------
    ln = v.get("length")
    if ln is None:
        reject.append("no duration (live / upcoming / shorts)")
    elif not (MIN_LEN <= ln <= MAX_LEN):
        reject.append(f"duration {v.get('length_text')} outside 0:30-5:00")
    elif ln < TEASER_LEN:   # owner, 2026-10-24: under 1:30 it is a teaser, not a trailer — take a longer one if any
        score -= 15; why.append(f"-15 short ({v.get('length_text')}): teaser"); flags.append("teaser_short")

    # --- year ----------------------------------------------------------------------------------------
    year = ctx.get("year")
    years_in_title = {int(y) for y in _YEAR.findall(rest)}
    snippet = v.get("snippet") or ""
    if year and years_in_title:
        if year in years_in_title:
            score += 12; why.append("+12 year in title")
            flags.append("year_match")
        elif min(abs(y - year) for y in years_in_title) <= 1:
            score += 4; why.append("+4 year ±1 in title")
            flags.append("year_close")
            if max(years_in_title) < year:
                flags.append("year_before")   # video names the year before: a Russian release is usually later
        elif ctx.get("kind") == "series" and (ctx.get("season_number") or 1) > 1 and min(years_in_title) < year:
            flags.append("older_year_in_title")   # seasons often carry the series start year
        else:
            reject.append(f"year {sorted(years_in_title)} in title, title is {year}")
    if year and str(year) in snippet:
        score += 4; why.append("+4 year in description")
        flags.append("year_in_snippet")
    if "orig" in names and " ".join(names["orig"]) in norm(snippet) and not orig_hit:
        score += 4; why.append("+4 original name in description")
        flags.append("orig_in_snippet")

    # --- published date --------------------------------------------------------------------------------
    pr = _published_range(v, fetched_at)
    if pr is None:
        flags.append("no_publish_date")
        score -= 5; why.append("-5 publish date unknown")
    elif year:
        earliest_ok = date(year - 2, 1, 1)
        if pr[1] < earliest_ok:
            reject.append(f"published {v.get('published')} — before {earliest_ok.year}")
        rd = _date(ctx.get("release_date"))
        if rd and pr[0] > rd + timedelta(days=3 * 365):
            flags.append("published_long_after_release")
            score -= 5; why.append("-5 published > 3 years after release")

    # --- kind (movie / series) ----------------------------------------------------------------------
    is_season = ctx.get("kind") == "series"
    if not is_season and _KIND_SERIES.search(rest) and not re.search(r"сериал\w*\s+фильм", rest, re.I):
        reject.append("video is about a series, title is a movie")
    if is_season and re.search(r"\bфильм\b|\bмультфильм\b", rest, re.I) and not _KIND_SERIES.search(rest):
        score -= 8; why.append("-8 video says фильм, title is a series")
    if not ctx.get("is_animation") and re.search(r"мультфильм|мультсериал", rest, re.I):
        score -= 8; why.append("-8 video says мульт, title is not animation")

    # --- season --------------------------------------------------------------------------------------
    if is_season:
        n = ctx.get("season_number") or 1
        seen = season_mentions(title)
        if seen:
            if n in seen:
                score += 15; why.append("+15 season number")
                flags.append("season_match")
            else:
                reject.append(f"season {sorted(seen)} in title, title is season {n}")
        else:
            multi = (ctx.get("seasons_total") or 0) > 1 or n > 1 or len(ctx.get("season_dates") or {}) > 1
            if n > 1:
                flags.append("season_unstated")
                reject.append(f"no season number in title (season {n})")
            elif multi:
                # season 1 of a multi-season series: only a video published well before season 2
                nxt = _date((ctx.get("season_dates") or {}).get(2))
                if pr and nxt and pr[1] > nxt - timedelta(days=90):
                    reject.append("no season number and possibly published around season 2")
                else:
                    flags.append("season1_unstated")
                    score -= 5; why.append("-5 season 1, video names no season")

    # --- channel / popularity ---------------------------------------------------------------------
    if channel_trusted(channel):
        score += 10; why.append("+10 trusted channel")
        flags.append("trusted_channel")
    if any("VERIFIED" in (b or "") for b in v.get("owner_badges") or []):
        score += 5; why.append("+5 verified channel")
        flags.append("verified_channel")
    views = v.get("views") or 0
    vb = round(min(12.0, 2 * math.log10(views + 1)), 1)
    if vb:
        score += vb; why.append(f"+{vb} views {views}")
    score -= min(5, v.get("pos", 0) * 0.5)

    if not set(flags) & {"ru_audio_strong", "ru_audio_weak", "trusted_channel"}:
        flags.append("lang_unconfirmed")   # Cyrillic title only; nothing says the audio is Russian
    ok = not reject and lang == "ru"
    return {"id": v["id"], "title": title, "channel": channel, "length": v.get("length_text"),
            "published": v.get("published"), "views": views, "score": round(score, 1), "ok": ok,
            "lang": lang, "kind": kind, "flags": sorted(set(flags)), "reject": reject, "why": why}


_TRANSLIT = [("shch", "щ"), ("sch", "ш"), ("tch", "ч"), ("sh", "ш"), ("ch", "ч"), ("zh", "ж"), ("kh", "х"),
             ("ts", "ц"), ("th", "т"), ("ph", "ф"), ("ck", "к"), ("qu", "кв"), ("ee", "и"), ("oo", "у"), ("ou", "у"),
             ("ya", "я"), ("yu", "ю"), ("yo", "йо"), ("ye", "е"), ("j", "дж"), ("x", "кс"), ("w", "у"), ("y", "и"),
             ("a", "а"), ("b", "б"), ("c", "к"), ("d", "д"), ("e", "е"), ("f", "ф"), ("g", "г"), ("h", "х"),
             ("i", "и"), ("k", "к"), ("l", "л"), ("m", "м"), ("n", "н"), ("o", "о"), ("p", "п"), ("q", "к"),
             ("r", "р"), ("s", "с"), ("t", "т"), ("u", "у"), ("v", "в"), ("z", "з")]


def translit(word: str) -> str:
    """Rough Latin → Cyrillic ('daniels' → 'даниелс'), only for fuzzy matching of names in video titles."""
    w, out, i = norm(word), "", 0
    while i < len(w):
        for lat, cyr in _TRANSLIT:
            if w.startswith(lat, i):
                out += cyr
                i += len(lat)
                break
        else:
            out += w[i]
            i += 1
    return out


def _name_like(token: str, cast_tokens: set[str]) -> bool:
    """A video-title token that is (a transliteration of) a cast / director name token."""
    if len(token) < 4:
        return False
    if token in cast_tokens:
        return True
    # fuzzy (transliteration is rough: lopez → лопез vs «Лопес») only for tokens of 5+ letters with the same
    # first letter; evaluate() counts fuzzy hits only together with an exact one or a second fuzzy one
    return len(token) >= 5 and any(
        c[:1] == token[:1] and abs(len(c) - len(token)) <= 2 and
        difflib.SequenceMatcher(None, token, c).ratio() >= 0.75 for c in cast_tokens)


def _corroborated(c: dict) -> bool:
    """Something besides the (Russian) name ties the video to this title."""
    return bool(set(c["flags"]) & {"year_match", "year_close", "year_in_snippet", "season_match", "orig_in_snippet",
                                   "orig_in_title", "orig_in_description", "cast_in_title"})


def decide(cands: list[dict], ctx: dict) -> dict:
    """Pick the trailer among scored candidates.  status: accepted / ambiguous / not_found.
    Accepted needs score >= ACCEPT_SCORE and none of the doubt conditions below; the doubts are listed in
    'doubts' so the owner sees why a plausible candidate was not written."""
    ok = sorted([c for c in cands if c["ok"]], key=lambda c: -c["score"])
    if not ok:
        return {"status": "not_found", "best": None, "doubts": []}
    # other uploads of this very title are marked subtitled → its Russian release may have no dub, so only an
    # upload that says дубляж / озвучка is trusted to have Russian audio
    sub_siblings = [c for c in cands if c["lang"] == "subtitled" and not c["reject"]]
    solid = [c for c in ok if c["score"] >= ACCEPT_SCORE]
    if sub_siblings:
        strong = [c for c in solid if set(c["flags"]) & {"ru_audio_strong", "audio_ru"} and not
                  set(c["flags"]) & {"name_extended", "partial_name", "many_other_words", "uk_name_only"}]
        if strong:
            ok.remove(strong[0])
            ok.insert(0, strong[0])
    # prefer the full trailer over a teaser when both are solid
    best_trailer = next((c for c in ok if c["kind"] == "trailer"), None)
    if best_trailer and ok[0]["kind"] == "teaser" and best_trailer["score"] >= ACCEPT_SCORE \
            and best_trailer["score"] >= ok[0]["score"] - 15 \
            and not (sub_siblings and "ru_audio_strong" in ok[0]["flags"]
                     and "ru_audio_strong" not in best_trailer["flags"]):
        ok.remove(best_trailer)
        ok.insert(0, best_trailer)
    best = ok[0]
    doubts = []
    f = set(best["flags"])
    strong_confirm = bool(f & {"season_match", "year_match"}) and bool(
        f & {"orig_in_title", "orig_in_description", "orig_in_snippet", "cast_in_title"})
    # a longer name ("Джек Ричер" for "Ричер") with season/year + original title / cast confirmed: no penalty
    eff = best["score"] + (30 if "name_extended" in f and strong_confirm else 0)
    if eff < ACCEPT_SCORE:
        doubts.append(f"score {best['score']} < {ACCEPT_SCORE}")
    if sub_siblings and not f & {"ru_audio_strong", "audio_ru"}:
        doubts.append(f"{len(sub_siblings)} other upload(s) of this title are subtitled; this one does not say "
                      f"дубляж/озвучка")
    if "name_extended" in f and not strong_confirm:
        doubts.append("the name continues with other words (maybe another title)")
    if "year_before" in f and not (f & {"year_in_snippet", "season_match", "orig_in_snippet", "orig_in_title",
                                         "orig_in_description"}):
        doubts.append("video names the year before the title's year and nothing else confirms it")
    if "audio_unknown" in f and "lang_unconfirmed" in f:
        doubts.append("audio language unknown and the title does not say русский / дубляж")
    if "neuro_dub" in f:
        doubts.append("neural (AI) voice-over")
    if "partial_name" in f and not (f & {"year_match", "year_in_snippet", "season_match", "orig_in_snippet",
                                         "orig_in_title", "orig_in_description", "cast_in_title"}):
        doubts.append("only part of the name matched, no year/original/season to confirm")
    if "uk_name_only" in f:
        doubts.append("matched only the Ukrainian name")
    if "many_other_words" in f:
        doubts.append("video title has several words not explained by the name")
    if "season1_unstated" in f and not _corroborated(best) and "trusted_channel" not in f:
        doubts.append("season 1 of a multi-season series, video names no season and no year")
    if "published_long_after_release" in f and not _corroborated(best):
        doubts.append("published long after the release, no year")
    # same name, other title: in our DB, or another search result that names a different year
    if ctx.get("homonyms_db") and not _corroborated(best):
        doubts.append(f"{len(ctx['homonyms_db'])} other DB title(s) share the Russian name, video has no year/original")
    year = ctx.get("year")
    if year and not _corroborated(best):
        other_years = set()
        for c in cands:
            if c["id"] == best["id"]:
                continue
            for rj in c["reject"]:
                m = re.match(r"year \[([\d, ]+)\] in title", rj)
                if m and not any(r.startswith(("name not", "sequel")) for r in c["reject"]) and \
                        "name_extended" not in c["flags"] and not any("other words" in w for w in c["why"]):
                    other_years |= {int(x) for x in m.group(1).split(",")}
        if other_years:
            doubts.append(f"same-name videos of other years {sorted(other_years)}; best has no year")
    short = len(" ".join(tokens(ctx.get("title_ru")))) <= 4 or (len(tokens(ctx.get("title_ru"))) == 1 and
                                                                not ctx.get("title_original"))
    if short and not _corroborated(best) and "trusted_channel" not in f:
        doubts.append("very short name without year/original/season confirmation")
    status = "accepted" if not doubts else "ambiguous"
    return {"status": status, "best": best, "doubts": doubts}


# ------------------------------------------------------------------------------------------------
# queries + per-title search
# ------------------------------------------------------------------------------------------------

def queries_for(ctx: dict) -> list[str]:
    ru = (ctx.get("title_ru") or "").strip()
    orig = (ctx.get("title_latin") or ctx.get("title_original") or "").strip()
    if ctx.get("title_original") and re.search(r"[A-Za-z]", ctx["title_original"]):
        orig = ctx["title_original"].strip()
    y = ctx.get("year") or ""
    qs = []
    if ctx.get("kind") == "series":
        n = ctx.get("season_number") or 1
        qs.append(f"{ru} {n} сезон трейлер на русском")
        if n == 1:
            qs.append(f"{ru} {y} трейлер на русском".replace("  ", " "))
        qs.append(f"{ru} сериал {n} сезон трейлер русский")
        if orig and norm(orig) != norm(ru):
            qs.append(f"{orig} {n} сезон русский трейлер")
    else:
        qs.append(f"{ru} {y} трейлер на русском".replace("  ", " "))
        qs.append(f"{ru} трейлер русский")
        if orig and norm(orig) != norm(ru):
            qs.append(f"{orig} {y} русский трейлер".replace("  ", " "))
        qs.append(f"{ru} {y} дублированный трейлер".replace("  ", " "))
    out = []
    for q in qs:
        q = " ".join(q.split())
        if q not in out:
            out.append(q)
    return out


def uk_query(ctx: dict) -> str | None:
    uk = (ctx.get("title_uk") or "").strip()
    if not uk:
        return None
    if ctx.get("kind") == "series":
        return f"{uk} {ctx.get('season_number') or 1} сезон трейлер українською"
    return " ".join(f"{uk} {ctx.get('year') or ''} трейлер українською".split())


MAX_VERIFY = 4   # watch-page checks per title


def _verify(yt: YouTube, c: dict, raw: dict, ctx: dict) -> dict:
    """Re-check a candidate on its watch page; returns the candidate to keep in the pool.
    - not playable from here (deleted / private / blocked in UA) → rejected;
    - the search page showed a translated title → re-evaluated on the video's own title;
    - original audio language: 'ru' confirms; 'uk' → Ukrainian finding; another language → rejected, or a
      'subtitled' finding when it has Russian captions; unknown → stays 'lang_unconfirmed'."""
    w = yt.watch(c["id"])
    if w.get("status") is None:            # offline, nothing known
        return c
    if w["status"] not in ("OK", "LOGIN_REQUIRED"):     # LOGIN_REQUIRED = age-gated, still a real video
        return dict(c, ok=False, flags=sorted(set(c["flags"]) | {"watch_checked"}),
                    reject=c["reject"] + [f"not playable from here: {w['status']} {w.get('reason') or ''}".strip()],
                    watch=_watch_brief(w))
    v, fetched_at = raw[c["id"]]
    if w.get("title") and norm(w["title"]) != norm(v.get("title")):
        c2 = evaluate(dict(v, title=w["title"]), ctx, fetched_at)
        c2["shown_title"] = v.get("title")
        c2["flags"] = sorted(set(c2["flags"]) | {"title_translated"})
        if c2["lang"] != c["lang"]:
            c2["ok"] = False
            c2["reject"] = c2["reject"] + [f"own title is '{c2['lang']}', the shown title was a translation"]
    else:
        c2 = dict(c, flags=list(c["flags"]), reject=list(c["reject"]))
    flags = set(c2["flags"]) | {"watch_checked"}
    al, how = audio_language(w.get("audio_tracks") or [], w.get("captions") or [])
    claims_dub = bool(_DUB.search(c2["title"]) or _RU_AUDIO_STRONG.search(c2["title"]))
    if al and al not in ("ru", "uk") and claims_dub and c2["lang"] in ("ru", "uk"):
        flags.add(f"audio_conflict_{al}")      # the title says дубляж / озвучка: trust the title, keep the flag
        al = c2["lang"]
    # a speech check by films.sources.trailer_lang (Whisper on the audio) beats every metadata guess
    sc = speech_checks().get(c["id"])
    if sc and sc.get("language") and (sc.get("probability") or 0) >= 0.7:
        al, how = sc["language"], "speech_check"
        flags.add("speech_checked")
    if al == "ru":
        flags.add("audio_ru")
        flags.discard("lang_unconfirmed")
    elif al == "uk":
        flags.add("audio_uk")
        if c2["lang"] == "ru":
            if _UK_LETTERS.search(w.get("title") or "") or _UK_WORDS.search(w.get("title") or "") or not claims_dub:
                c2["lang"], c2["ok"] = "uk", False
            else:
                flags.add("audio_conflict_uk")
    elif al:
        flags.add(f"audio_{al}")
        if c2["lang"] in ("ru", "uk"):
            if w.get("ru_manual_captions"):
                c2["lang"] = "subtitled"
            else:
                c2["reject"] = c2["reject"] + [f"original audio is '{al}' ({how})"]
            c2["ok"] = False
    else:
        flags.add("audio_unknown")
    if w.get("available_in_ua") is False:
        c2["ok"] = False
        c2["reject"] = c2["reject"] + ["not available in UA"]
    desc = norm(w.get("description"))
    orig = tokens(ctx.get("title_original"))
    if orig and re.search(r"[a-z]", " ".join(orig)) and len(" ".join(orig)) >= 4 and             find_phrase(desc.split(), orig):
        flags.add("orig_in_description")
    c2["flags"] = sorted(flags)
    c2["watch"] = _watch_brief(w)
    return c2


_SPEECH_CHECKS: dict[str, dict] | None = None


def speech_checks() -> dict[str, dict]:
    """Verdicts of films.sources.trailer_lang (data/cache/youtube/audio_checks.jsonl): video id → {'language',
    'probability', …}.  Read once per process; a video it found not to be Russian is never picked again."""
    global _SPEECH_CHECKS
    if _SPEECH_CHECKS is None:
        _SPEECH_CHECKS = {}
        if AUDIO_CHECKS.exists():
            with AUDIO_CHECKS.open(encoding="utf-8") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                        _SPEECH_CHECKS[r["id"]] = r
                    except (json.JSONDecodeError, KeyError):
                        continue
    return _SPEECH_CHECKS


def _watch_brief(w: dict) -> dict:
    return {k: w.get(k) for k in ("status", "title", "author", "publish_date", "audio_lang", "audio_lang_how",
                                  "available_in_ua")}


def find_trailer(yt: YouTube, ctx: dict, uk: bool = True, verify: bool = True) -> dict:
    """Run the queries in order until one gives an accepted trailer, check the pick on its watch page (and the best
    ambiguous / Ukrainian candidate); returns the decision record."""
    pool: dict[str, dict] = {}
    raw: dict[str, tuple[dict, str]] = {}
    verified: set[str] = set()
    used = []
    dec = {"status": "not_found", "best": None, "doubts": []}

    def add(res, only_new=False):
        for v in res["videos"]:
            if v["id"] in verified or (only_new and v["id"] in pool):
                continue
            c = evaluate(v, ctx, res["fetched_at"])
            if v["id"] not in pool or c["score"] > pool[v["id"]]["score"]:
                pool[v["id"]] = c
                raw[v["id"]] = (v, res["fetched_at"])

    def settle():
        d = decide(list(pool.values()), ctx)
        while verify and d["best"] and d["best"]["id"] not in verified and len(verified) < MAX_VERIFY:
            vid = d["best"]["id"]
            pool[vid] = _verify(yt, pool[vid], raw, ctx)
            verified.add(vid)
            d = decide(list(pool.values()), ctx)
        if verify and d["status"] == "accepted" and d["best"]["id"] not in verified:
            d = dict(d, status="ambiguous", doubts=d["doubts"] + ["watch-page check budget used up"])
        return d

    for q in queries_for(ctx):
        res = yt.search(q)
        used.append(q)
        add(res)
        dec = settle()
        if dec["status"] == "accepted":
            break
    if uk and dec["status"] != "accepted" and not any(c["lang"] == "uk" and _uk_ok(c) for c in pool.values()):
        q = uk_query(ctx)
        if q:
            res = yt.search(q)
            used.append(q)
            add(res, only_new=True)
            dec = settle()
    if verify:   # the Ukrainian finding is checked too, so its counts are honest
        for _ in range(2):
            uk_best = max([c for c in pool.values() if c["lang"] == "uk" and _uk_ok(c)], key=lambda c: c["score"],
                          default=None)
            if not uk_best or uk_best["id"] in verified:
                break
            pool[uk_best["id"]] = _verify(yt, uk_best, raw, ctx)
            verified.add(uk_best["id"])
    cands = sorted(pool.values(), key=lambda c: -c["score"])
    uk_c = [c for c in cands if c["lang"] == "uk" and _uk_ok(c)]
    sub_c = [c for c in cands if c["lang"] == "subtitled" and _uk_ok(c)]
    non_trailer = [c for c in cands if any(r.startswith("not a trailer") for r in c["reject"])
                   and not any(r.startswith(("name not", "year", "season", "sequel")) for r in c["reject"])]
    rec = {
        "title_id": ctx["id"], "at": _now().isoformat(timespec="seconds"), "status": dec["status"],
        "kind": ctx.get("kind"), "season": ctx.get("season_number"), "year": ctx.get("year"),
        "year_source": ctx.get("year_source"), "title_ru": ctx.get("title_ru"),
        "title_original": ctx.get("title_original"),
        "chosen": dec["best"] if dec["status"] == "accepted" else None,
        "best": dec["best"], "doubts": dec["doubts"],
        "runners_up": [c for c in cands if c["ok"] and (not dec["best"] or c["id"] != dec["best"]["id"])][:4],
        "uk": uk_c[:3], "subtitled": sub_c[:3], "non_trailer_same_title": [c["id"] for c in non_trailer[:5]],
        "rejected": [c for c in cands if not c["ok"]][:6],
        "rejected_after_watch_check": [c for c in cands if c["id"] in verified and not c["ok"]],
        "queries": used,
    }
    return rec


def _uk_ok(c: dict) -> bool:
    """A uk / subtitled finding that passed every hard rule except the language one, with no doubt about
    the name."""
    return not c["reject"] and not set(c["flags"]) & {"name_extended", "partial_name", "many_other_words"}


_UK_CLAIM = re.compile(r"українськ|українською|дубльован|\bua\b|укр\.? ?(?:мов|дубл|озв)", re.I)


def uk_confirmed(c: dict) -> bool:
    """A Ukrainian finding whose audio is evidently Ukrainian: audio metadata says so, or the title says
    український / українською / дубльований (a Ukrainian *title* alone can be a translation)."""
    return "audio_uk" in c["flags"] or bool(_UK_CLAIM.search(c.get("title") or ""))


# ------------------------------------------------------------------------------------------------
# run over the DB
# ------------------------------------------------------------------------------------------------

def _todo(conn, recheck_days: float | None, ids: list[int] | None = None) -> list:
    sql = "SELECT * FROM titles WHERE trailer_ru_url IS NULL"
    args: list = []
    if ids:
        sql += f" AND id IN ({','.join('?' * len(ids))})"
        args += ids
    elif recheck_days is not None:
        sql += " AND (trailer_checked_at IS NULL OR trailer_checked_at < datetime('now', ?))"
        args.append(f"-{recheck_days} days")
    sql += " ORDER BY year DESC, id"
    return conn.execute(sql, args).fetchall()


def _write(conn, rec: dict, dry: bool) -> bool:
    if dry:
        return False
    for attempt in range(5):
        try:
            with conn:
                if rec["status"] == "accepted":
                    cur = conn.execute(
                        """UPDATE titles SET trailer_ru_url = ?, trailer_ru_source = 'youtube_search',
                           trailer_checked_at = datetime('now'), updated_at = datetime('now')
                           WHERE id = ? AND trailer_ru_url IS NULL""",
                        (f"https://www.youtube.com/watch?v={rec['chosen']['id']}", rec["title_id"]))
                else:
                    cur = conn.execute("""UPDATE titles SET trailer_checked_at = datetime('now')
                                          WHERE id = ? AND trailer_ru_url IS NULL""", (rec["title_id"],))
            return cur.rowcount == 1
        except sqlite3.OperationalError as e:   # "database is locked" past busy_timeout
            if "locked" not in str(e) or attempt == 4:
                raise
            time.sleep(5 * (attempt + 1))
    return False


def run(limit: int | None = None, recheck_days: float | None = 30, ids: list[int] | None = None,
        dry: bool = False, uk: bool = True, sample: int | None = None, max_hours: float | None = None,
        offline: bool = False) -> dict:
    conn = db.connect()
    rows = _todo(conn, recheck_days, ids)
    if sample:
        random.seed(sample)
        rows = random.sample(rows, min(sample, len(rows)))
    if limit:
        rows = rows[:limit]
    yt = YouTube(offline=offline)
    stats = {"todo": len(rows), "done": 0, "accepted": 0, "ambiguous": 0, "not_found": 0, "written": 0,
             "skipped_trailer_appeared": 0, "uk_found": 0, "subtitled_found": 0, "errors": 0, "stopped": None}
    consecutive_errors = 0
    t0 = time.monotonic()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    log = sys.stderr
    try:
        for i, row in enumerate(rows, 1):
            if max_hours and time.monotonic() - t0 > max_hours * 3600:
                stats["stopped"] = f"max_hours {max_hours} reached"
                break
            try:
                ctx = title_context(conn, row)
                rec = find_trailer(yt, ctx, uk=uk)
            except (Blocked, KeyboardInterrupt):
                raise
            except Exception as e:   # one broken title must not stop the run; it stays unchecked → retried
                stats["errors"] += 1
                consecutive_errors += 1
                print(f"  ERROR title {row['id']}: {type(e).__name__}: {e}", file=log, flush=True)
                with DECISIONS.open("a", encoding="utf-8") as f:
                    f.write(json.dumps({"title_id": row["id"], "at": _now().isoformat(timespec="seconds"),
                                        "status": "error", "error": f"{type(e).__name__}: {e}",
                                        "traceback": traceback.format_exc()[-2000:], "dry_run": dry},
                                       ensure_ascii=False) + "\n")
                if consecutive_errors >= 20:
                    stats["stopped"] = "20 errors in a row"
                    break
                continue
            consecutive_errors = 0
            rec["dry_run"] = dry
            written = _write(conn, rec, dry)
            if not dry and not written:
                stats["skipped_trailer_appeared"] += 1
                rec["note"] = "trailer_ru_url was set meanwhile (TMDB?) — nothing written"
            stats["written"] += int(written and rec["status"] == "accepted")
            with DECISIONS.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            stats[rec["status"]] += 1
            stats["uk_found"] += int(bool(rec["uk"]))
            stats["subtitled_found"] += int(bool(rec["subtitled"]))
            stats["done"] += 1
            if i % 25 == 0 or i == len(rows):
                el = time.monotonic() - t0
                print(f"  {i}/{len(rows)} acc {stats['accepted']} amb {stats['ambiguous']} "
                      f"nf {stats['not_found']} req {yt.stats['requests']} cache {yt.stats['from_cache']} "
                      f"{el / 60:.0f} min", file=log, flush=True)
    except Blocked as e:
        stats["stopped"] = f"YouTube blocked: {e}"
        print(f"STOPPED: {e}", file=log, flush=True)
    except KeyboardInterrupt:
        stats["stopped"] = "interrupted"
    stats["youtube"] = yt.stats
    stats["minutes"] = round((time.monotonic() - t0) / 60, 1)
    write_snapshots()
    return stats


# ------------------------------------------------------------------------------------------------
# decision snapshots / report
# ------------------------------------------------------------------------------------------------

def latest_decisions(include_dry: bool = False) -> dict[int, dict]:
    out: dict[int, dict] = {}
    if not DECISIONS.exists():
        return out
    with DECISIONS.open(encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (rec.get("dry_run") and not include_dry) or rec.get("status") == "error":
                continue
            out[rec["title_id"]] = rec
    return out


def write_snapshots():
    latest = latest_decisions()
    snaps = {"ambiguous.jsonl": [r for r in latest.values() if r["status"] == "ambiguous"],
             "uk_trailers.jsonl": [r for r in latest.values() if r.get("uk")],
             "subtitled_only.jsonl": [r for r in latest.values() if r["status"] != "accepted" and r.get("subtitled")]}
    for name, recs in snaps.items():
        with (CACHE_DIR / name).open("w", encoding="utf-8") as f:
            for r in recs:
                keep = {k: r.get(k) for k in ("title_id", "status", "kind", "season", "year", "title_ru",
                                               "title_original", "best", "doubts", "runners_up", "uk",
                                               "subtitled", "queries")}
                if name == "uk_trailers.jsonl":
                    keep["uk_audio_confirmed"] = uk_confirmed(r["uk"][0])
                f.write(json.dumps(keep, ensure_ascii=False) + "\n")


def audit(dry: bool = False, ids: list[int] | None = None) -> dict:
    """Re-decide every trailer this module wrote (trailer_ru_source = 'youtube_search') with the current rules
    (searches / watch pages come from the cache).  Still accepted with the same video → kept; accepted with
    another video → replaced; not accepted any more → link removed (trailer_checked_at = now, so the miss is
    recorded like any other).  A link that differs from the one this module chose (replaced meanwhile by
    films.sources.trailer_lang) is left alone.  Every change is logged in decisions.jsonl ('audit': true)."""
    conn = db.connect()
    chosen = {}
    for tid, r in latest_decisions().items():
        if r["status"] == "accepted" and r.get("chosen"):
            chosen[tid] = r["chosen"]["id"]
    sql = "SELECT * FROM titles WHERE trailer_ru_source = 'youtube_search' AND trailer_ru_url IS NOT NULL"
    rows = [r for r in conn.execute(sql) if not ids or r["id"] in ids]
    yt = YouTube()
    stats = {"checked": 0, "kept": 0, "replaced": 0, "removed": 0, "changed_elsewhere": 0, "changes": []}
    for row in rows:
        url = row["trailer_ru_url"]
        if chosen.get(row["id"]) and url != f"https://www.youtube.com/watch?v={chosen[row['id']]}":
            stats["changed_elsewhere"] += 1
            continue
        rec = find_trailer(yt, title_context(conn, row))
        rec["audit"] = True
        rec["dry_run"] = dry
        stats["checked"] += 1
        new = f"https://www.youtube.com/watch?v={rec['chosen']['id']}" if rec["status"] == "accepted" else None
        if new == url:
            stats["kept"] += 1
        else:
            stats["replaced" if new else "removed"] += 1
            stats["changes"].append({"title_id": row["id"], "title_ru": row["title_ru"], "old": url, "new": new,
                                     "status": rec["status"], "doubts": rec["doubts"],
                                     "best": (rec.get("best") or {}).get("title")})
            if not dry:
                with conn:
                    conn.execute("""UPDATE titles SET trailer_ru_url = ?, trailer_checked_at = datetime('now'),
                                    updated_at = datetime('now')
                                    WHERE id = ? AND trailer_ru_source = 'youtube_search' AND trailer_ru_url = ?""",
                                 (new, row["id"], url))
        with DECISIONS.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    write_snapshots()
    return stats


def report() -> dict:
    latest = latest_decisions()
    conn = db.connect()
    rows = {r["id"]: r for r in conn.execute(
        "SELECT id, kind, year, trailer_ru_url, trailer_ru_source FROM titles")}
    out = {"titles_with_decision": len(latest), "by_status": {}, "by_kind": {}, "by_year": {},
           "uk_found": {"ru_accepted_too": 0, "uk_only": 0, "uk_only_audio_confirmed": 0,
                        "uk_only_title_only": 0},
           "subtitled_only": 0, "accepted_language_evidence": {}, "accepted_channels": {}, "doubts": {},
           "db": {}}
    for tid, r in latest.items():
        st = r["status"]
        out["by_status"][st] = out["by_status"].get(st, 0) + 1
        k = "season" if r.get("kind") == "series" else "movie"
        out["by_kind"].setdefault(k, {}).setdefault(st, 0)
        out["by_kind"][k][st] += 1
        y = str(rows[tid]["year"] if tid in rows else r.get("year"))
        out["by_year"].setdefault(y, {}).setdefault(st, 0)
        out["by_year"][y][st] += 1
        if r.get("uk"):
            out["uk_found"]["ru_accepted_too" if st == "accepted" else "uk_only"] += 1
            if st != "accepted":
                key = "uk_only_audio_confirmed" if uk_confirmed(r["uk"][0]) else "uk_only_title_only"
                out["uk_found"][key] += 1
        if st == "accepted":
            f = set(r["chosen"]["flags"])
            ev = ("audio_ru" if "audio_ru" in f else
                  "title_says_dub_audio_conflict" if any(x.startswith("audio_conflict") for x in f) else
                  "title_says_dub" if "ru_audio_strong" in f else
                  "title_says_russian" if "ru_audio_weak" in f else
                  "trusted_channel_only" if "trusted_channel" in f else "unconfirmed")
            out["accepted_language_evidence"][ev] = out["accepted_language_evidence"].get(ev, 0) + 1
            ch = r["chosen"]["channel"]
            out["accepted_channels"][ch] = out["accepted_channels"].get(ch, 0) + 1
        if st != "accepted" and r.get("subtitled"):
            out["subtitled_only"] += 1
        for d in r.get("doubts") or []:
            key = re.sub(r"\d+|\[.*?\]", "#", d)
            out["doubts"][key] = out["doubts"].get(key, 0) + 1
    out["accepted_channels"] = dict(sorted(out["accepted_channels"].items(), key=lambda kv: -kv[1])[:30])
    for r in conn.execute("SELECT coalesce(trailer_ru_source, 'none') s, count(*) n FROM titles GROUP BY 1"):
        out["db"][r["s"]] = r["n"]
    write_snapshots()
    return out


# ------------------------------------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------------------------------------

def main(argv=None):
    p = argparse.ArgumentParser(prog="films.sources.youtube")
    sub = p.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("trailers", help="find Russian trailers for titles without one")
    t.add_argument("--limit", type=int)
    t.add_argument("--recheck-days", type=float, default=30,
                   help="skip titles checked less than D days ago (default 30; 0 = recheck all)")
    t.add_argument("--ids", help="comma-separated title ids (ignores --recheck-days)")
    t.add_argument("--dry-run", action="store_true", help="search and decide, write nothing to the DB")
    t.add_argument("--no-uk", action="store_true", help="skip the extra Ukrainian query for misses")
    t.add_argument("--sample", type=int, help="random sample of N titles (seeded with N)")
    t.add_argument("--max-hours", type=float)
    t.add_argument("--offline", action="store_true", help="search cache only")
    s = sub.add_parser("search", help="print parsed search results")
    s.add_argument("query")
    e = sub.add_parser("explain", help="score the cached/fresh search results for one title")
    e.add_argument("title_id", type=int)
    e.add_argument("--offline", action="store_true")
    sub.add_parser("report", help="counts from decisions.jsonl; rewrites the snapshot files")
    au = sub.add_parser("audit", help="re-decide trailers this module wrote, with the current rules")
    au.add_argument("--dry-run", action="store_true")
    au.add_argument("--ids", help="comma-separated title ids")
    a = p.parse_args(argv)
    if a.cmd == "trailers":
        ids = [int(x) for x in a.ids.split(",")] if a.ids else None
        res = run(limit=a.limit, recheck_days=a.recheck_days, ids=ids, dry=a.dry_run, uk=not a.no_uk,
                  sample=a.sample, max_hours=a.max_hours, offline=a.offline)
        print(json.dumps(res, ensure_ascii=False, indent=1))
    elif a.cmd == "search":
        print(json.dumps(YouTube().search(a.query), ensure_ascii=False, indent=1))
    elif a.cmd == "explain":
        conn = db.connect()
        row = conn.execute("SELECT * FROM titles WHERE id = ?", (a.title_id,)).fetchone()
        ctx = title_context(conn, row)
        rec = find_trailer(YouTube(offline=a.offline), ctx)
        print(json.dumps(rec, ensure_ascii=False, indent=1))
    elif a.cmd == "audit":
        ids = [int(x) for x in a.ids.split(",")] if a.ids else None
        print(json.dumps(audit(dry=a.dry_run, ids=ids), ensure_ascii=False, indent=1))
    else:
        print(json.dumps(report(), ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
