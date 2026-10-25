"""Plex source: library scan (what is downloaded, in which quality) and the owner's watch history.

Run: python -X utf8 -m films.sources.plex

Read-only toward Plex: only GET requests to the local server, plex.tv and the Plex metadata providers
(plus one GraphQL *query* POST to community.plex.tv), and a read-only (mode=ro) open of the Plex Media
Server SQLite database for history rows the HTTP API does not return. Nothing is ever marked or edited.
How to mark something watched is documented in docs/plex-api.md and lives in the mark-watched skill.

The token is read from the Windows registry at runtime and sent only in the X-Plex-Token header;
it is never put into URLs, files or log output.

Storage notes (until the schema gets dedicated columns — see the proposal in docs/plex-api.md):

plex_items
  kind         'movie'        — matched by the Plex Movie agent (plex://movie/...)
               'episode-like' — a series/anime episode stored in a "movie" library (episode number parsed)
               'unmatched'    — no agent match and no episode number (home video, specials, unknown)
  guids        JSON array: the item's own Plex guid (plex://movie/..., local://..., com.plexapp.agents.none://...)
               followed by external ids (imdb://, tmdb://, tvdb://)
  plex_year    for agent-matched items Plex's year; for unmatched/episode-like items the year parsed from
               the file name (Plex's own value there is just the file date — kept in the "item" entry below)
  hdr          JSON array, best first: DV, HDR10+, HDR10, HLG (empty = SDR)
  resolution / source_type / hdr / dv_profile describe the best media version of the item
  audio        JSON array of tagged entries. Entries with "stream": "audio" are the audio tracks
               ({lang, language, codec, profile, channels, layout, kbps, title, kind, studio, default, media}).
               The same array temporarily holds the rest of the file description, each tagged:
                 "video"    — codec, profile, bit_depth, width, height, kbps, frame_rate, colour info, DV fields
                 "subtitle" — FORCED subtitles only (owner's decision: other subtitles are not kept)
                 "part"     — every file of every media version: file, size, container, duration_ms, media_kbps
                 "item"     — plex_guid, library_id, agent, plex_year_raw, original_title, edition, added_at,
                              episode {show, season, episode}, media_count
               Use audio_streams(row) / plex_extras(row) below instead of reading the column directly.

plex_history
  source       'library' — this server's history for account 1 (<plex.user>) + watch state kept by the server
               'plextv'  — plex.tv watch history of <plex.user> (community.plex.tv GraphQL), incl. titles
                           that were never in the local libraries
  plex_guid    plex://movie|show|episode/..., or the local guid for unmatched videos
  guids        JSON array of external ids (imdb://, tmdb://, tvdb://) plus one bookkeeping marker that makes
               re-runs idempotent: pms-history://<id>, pms-settings://<guid>, plextv-activity://<uuid>,
               plextv-episode://<id>; for server rows also pms-view://played|marked
  viewed_at    'YYYY-MM-DD HH:MM:SS' UTC
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
import winreg
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath

import requests

PLEX_URL = "http://127.0.0.1:32400"
OWNER_ACCOUNT_ID = 1          # account id of <plex.user> on this server
from ..db import private  # noqa: E402

OWNER_USERNAME = private()["plex"]["user"]
PLEXTV = "https://plex.tv"
COMMUNITY_GQL = "https://community.plex.tv/api"
METADATA_PROVIDER = "https://metadata.provider.plex.tv"
DISCOVER_PROVIDER = "https://discover.provider.plex.tv"
CLIENT_ID = "films-db-plex-reader"

REG_KEY = r"Software\Plex, Inc.\Plex Media Server"


# --------------------------------------------------------------------------------------------- HTTP

def _token() -> str:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_KEY) as k:
        return winreg.QueryValueEx(k, "PlexOnlineToken")[0]


class Plex:
    """Thin read-only client. All responses are decoded as UTF-8 with errors='replace' before json."""

    def __init__(self):
        from .plex_guard import GuardedSession   # every write to the owner's watch history is checked (plex_guard)
        self.s = GuardedSession(PLEX_URL, DISCOVER_PROVIDER)
        self.s.headers.update({
            "Accept": "application/json",
            "X-Plex-Token": _token(),
            "X-Plex-Client-Identifier": CLIENT_ID,
            "X-Plex-Product": "films-db",
        })
        self.requests = 0
        # semaphore (owner, 2026-10-24): writes from all parallel jobs go one at a time, ≥ 1 s apart (parallel runs got
        # 429 / 503 from Plex and lost marks); the guarded session still decides whether a write is allowed at all
        inner = self.s.request

        def paced(method, url, *a, **kw):
            if method.upper() in ("PUT", "POST", "DELETE") or "/actions/" in url or "/:/" in url:
                from ..locks import lock, pace
                with lock("plex_write", timeout=1800):
                    pace("plex_write_rate", 1.0)
                    return inner(method, url, *a, **kw)
            return inner(method, url, *a, **kw)
        self.s.request = paced

    def _json(self, resp: requests.Response):
        resp.raise_for_status()
        return json.loads(resp.content.decode("utf-8", errors="replace"))

    def get(self, url: str, params: dict | None = None, retries: int = 4):
        if not url.startswith("http"):
            url = PLEX_URL + url
        for attempt in range(retries):
            try:
                self.requests += 1
                r = self.s.get(url, params=params, timeout=120)
                if r.status_code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                    time.sleep(2 * (attempt + 1))
                    continue
                return self._json(r)
            except (requests.ConnectionError, requests.Timeout):
                if attempt == retries - 1:
                    raise
                time.sleep(2 * (attempt + 1))

    def graphql(self, query: str, variables: dict, op: str, retries: int = 4):
        body = {"query": query, "variables": variables, "operationName": op}
        for attempt in range(retries):
            self.requests += 1
            r = self.s.post(COMMUNITY_GQL, json=body, timeout=120)
            if r.status_code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
                continue
            data = self._json(r)
            if data.get("errors"):
                raise RuntimeError(f"GraphQL {op}: {data['errors']}")
            return data["data"]


# ------------------------------------------------------------------------------------------ parsing

def ts_to_sql(ts) -> str | None:
    if ts in (None, "", 0):
        return None
    return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def iso_to_sql(s: str | None) -> str | None:
    if not s:
        return None
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


_SOURCE_PATTERNS = [  # order matters: first match wins
    ("BDRemux", r"remux"),
    ("BDRip", r"bd-?rip|b[dr]-?rip|hd-?dvd-?rip"),
    ("WEB-DL", r"web-?dl|webdl"),
    ("WEBRip", r"web-?rip"),
    ("HDTVRip", r"hdtv-?rip"),
    ("HDTV", r"hdtv"),
    ("HDRip", r"hd-?rip"),
    ("DVDRip", r"dvd-?rip"),
    ("DVD", r"dvd-?[59]|\bdvd\b"),
    ("BluRay", r"blu-?ray|\bbd\b|\buhd\b"),     # disc/encode without Rip/Remux marker
    ("WEB", r"(?<![a-z])web(?![a-z])|\bnf\b|\bamzn\b|\bdsnp\b|\batvp\b|\bhmax\b"),
    ("CAMRip", r"cam-?rip|\bcam\b"),
    ("TS", r"\btelesync\b|\bts\b|\bhdts\b"),
    ("TC", r"\btelecine\b|\btc\b"),
]


def _first_token(text: str) -> str:
    m = re.search(r"[^\W_]{3,}", re.sub(r"\[[^\]]*\]", " ", text).lower())
    return m.group(0) if m else ""


def guess_source_type(path: str) -> str | None:
    """Release type from the file name; the parent folder is used only when it is the same release
    (same first word) — the owner reuses folders, e.g. X-Men '97 files inside a Punisher folder."""
    p = PureWindowsPath(path)
    texts = [p.stem]
    if _first_token(p.parent.name) and _first_token(p.parent.name) == _first_token(p.stem):
        texts.append(p.parent.name)
    for text in texts:
        low = text.lower().replace("_", ".")
        for name, rx in _SOURCE_PATTERNS:
            if re.search(rx, low):
                return name
    return None


_RES_WORDS = {"480", "576", "720", "1080", "2160", "1440"}


def _year_in(text: str) -> int | None:
    for m in re.finditer(r"(?<!\d)(19[2-9]\d|20[0-4]\d)(?!\d)", text):
        return int(m.group(1))
    return None


_EP_STRICT = [re.compile(r"(?i)(?<![a-z0-9])S(\d{1,2})[ ._-]?E(\d{1,4})(?!\d)")]
_EP_LOOSE = [
    re.compile(r"(?i)(?<![a-z0-9])(\d{1,2})x(\d{2,3})(?!\d)"),                       # 1x05
    re.compile(r"\[(\d{1,4}(?:\.\d)?)(?:_of_\d+)?\]"),                                # [287] [01_of_12] [09.5]
    re.compile(r"(?i)(?<![a-zа-я])(?:episode|ep|e)[ ._-]?(\d{1,4})(?!\d)"),             # Episode 01
    re.compile(r"(?i)(\d{1,4})[ ._-]*(?:серия|series)"),                              # 1 серия
    re.compile(r"(?i)(?:серия|глава|часть|case)[ ._-]*(\d{1,4})(?!\d)"),              # серия 5 / Case.1
    re.compile(r"[ ._]-[ ._](\d{1,4})(?:[ ._\[(v-]|$)"),                              # Show - 05 [..] / - 01-1
    re.compile(r"\.(\d{2,3})\.(?=[A-Za-z])"),                                         # OverLord.03.HDTV
    re.compile(r"[ ._](\d{1,4})$"),                                                   # Berserk 26
]


def _clean_show(text: str) -> str | None:
    t = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", text)
    t = re.sub(r"[._]+", " ", t)
    t = re.sub(r"\s+-\s*$|\s+", " ", t).strip(" -")
    return t or None


def parse_episode(path: str, strict_only: bool) -> dict | None:
    """Episode info from a file name. strict_only=True accepts only SxxEyy (used for agent-matched
    movies, where 'Justice League - War 3' must stay a movie)."""
    p = PureWindowsPath(path)
    stem = p.stem
    pats = _EP_STRICT if strict_only else _EP_STRICT + _EP_LOOSE
    for i, rx in enumerate(pats):
        for m in rx.finditer(stem):
            groups = [g for g in m.groups() if g is not None]
            nums = [float(g) if "." in g else int(g) for g in groups]
            if i == 0:
                season, episode = nums[0], nums[1]
            elif i == 1 and not strict_only:
                season, episode = nums[0], nums[1]
            else:
                season, episode = None, nums[-1]
                g = groups[-1]
                bracketed = i == 2  # "[720]" is an episode number; "720p" never reaches here
                if (g in _RES_WORDS and not bracketed) or (len(g) == 4 and 1920 <= int(float(g)) <= 2049):
                    continue  # resolution or a year, not an episode number
            show = _clean_show(stem[:m.start()]) or _clean_show(p.parent.name)
            if season is None:
                sm = (re.search(r"(?i)(?<![a-z])(?:TV|S|Season|Сезон)[ ._-]?(\d{1,2})(?!\d)", stem)
                      or re.search(r"(?i)(?<![a-z])(?:TV|S|Season|Сезон)[ ._-]?(\d{1,2})(?!\d)", p.parent.name))
                if sm:
                    season = int(sm.group(1))
            return {"show": show, "season": season, "episode": episode}
    return None


_DUB = re.compile(r"(?i)дубл|dubbing|\bdub\b|\bdubbed\b|^d$|\(d\)|\bD\b(?=[\s\]|,)-]|$)")
_MVO = re.compile(r"(?i)\bmvo\b|многоголос|\bп\b|профессиональный \(многоголосый")
_DVO = re.compile(r"(?i)\bdvo\b|двухголос|\bп2\b")
_AVO = re.compile(r"(?i)\bavo\b|авторск|одноголос|\bvo\b|\bа\b")
_ORIG = re.compile(r"(?i)original|оригинал|\borig\b")
_KIND_WORDS = re.compile(
    r"(?i)\b(?:mvo|dvo|avo|vo|dub|dubbed|eng|rus|ukr|ua|ru|en|jp|jpn|original|orig|d|п|п2|а|"
    r"english|russian|ukrainian|japanese|korean|chinese|french|german|spanish|"
    r"дубляж|дублированный|многоголосый|многоголосая|двухголосый|авторский|одноголосый|оригинал|"
    r"профессиональный|любительский|закадровый|"
    r"ac3|eac3|e-ac3|dts(?:-hd)?(?:\s*ma)?|truehd|atmos|aac|flac|ddp?\+?|dd|mp3|opus|pcm|lpcm|"
    r"\d\.\d|\d+\s*kbps|\d+\s*ch)(?!\w)")


def classify_voice(title: str | None, is_original: bool) -> tuple[str | None, str | None]:
    """(kind, studio) guess from an audio stream title; the raw title is always kept as well."""
    if not title:
        return ("original" if is_original else None), None
    t = title.strip()
    if _DUB.search(t):
        kind = "dub"
    elif _DVO.search(t):
        kind = "dvo"
    elif _MVO.search(t):
        kind = "mvo"
    elif _AVO.search(t):
        kind = "avo"
    elif _ORIG.search(t) or is_original:
        kind = "original"
    else:
        kind = None
    studio = _KIND_WORDS.sub(" ", t)
    studio = re.sub(r"[\[\](){}|,;:/\\]+", " ", studio)
    studio = re.sub(r"^\s*[-–—.]+|[-–—.]+\s*$", " ", studio)
    studio = re.sub(r"\s+", " ", studio).strip(" -–—.")
    if not re.search(r"[^\W\d_]", studio):  # only punctuation/digits left ("LPCM -> flac", "DD+ 7.1 @")
        studio = ""
    return kind, (studio or None)


def resolution_class(width: int | None, height: int | None, plex_res: str | None) -> int | None:
    w, h = width or 0, height or 0
    if w or h:
        if w >= 3200 or h >= 1800:
            return 2160
        if w >= 2400 or h >= 1300:
            return 1440
        if w >= 1700 or h >= 900:
            return 1080
        if w >= 1200 or h >= 650:
            return 720
        if h >= 540 or w >= 960:
            return 576
        return 480
    return {"4k": 2160, "1080": 1080, "720": 720, "576": 576, "480": 480, "sd": 480}.get(str(plex_res).lower())


_HDR_RANK = {"DV": 4, "HDR10+": 3, "HDR10": 2, "HLG": 1}


def hdr_of(video: dict) -> tuple[list[str], int | None]:
    """HDR formats (best first) and DV profile from a Plex video stream."""
    out, profile = [], None
    trc = (video.get("colorTrc") or "").lower()
    if video.get("DOVIPresent"):
        out.append("DV")
        profile = video.get("DOVIProfile")
    if video.get("HDR10PlusPresent"):
        out.append("HDR10+")
    compat = video.get("DOVIBLCompatID")
    if trc == "smpte2084" or (video.get("DOVIPresent") and compat in (1, 6)):
        out.append("HDR10")
    if trc in ("arib-std-b67", "hlg") or (video.get("DOVIPresent") and compat == 4):
        out.append("HLG")
    disp = (video.get("displayTitle") or "") + " " + (video.get("extendedDisplayTitle") or "")
    if "HDR10+" in disp and "HDR10+" not in out:
        out.append("HDR10+")
    seen, res = set(), []
    for x in sorted(out, key=lambda x: -_HDR_RANK[x]):
        if x not in seen:
            seen.add(x)
            res.append(x)
    return res, profile


def _lang(s: dict) -> str | None:
    return s.get("languageTag") or s.get("languageCode")


def describe_media(meta: dict) -> dict:
    """All media versions of a /library/metadata item → best-version summary + tagged stream list."""
    entries, versions = [], []
    for mi, media in enumerate(meta.get("Media") or []):
        best_video, audio_count = None, 0
        for part in media.get("Part") or []:
            entries.append({
                "stream": "part", "media": mi, "file": part.get("file"), "size": part.get("size"),
                "container": part.get("container") or media.get("container"),
                "duration_ms": part.get("duration"), "media_kbps": media.get("bitrate"),
            })
            for s in part.get("Stream") or []:
                st = s.get("streamType")
                if st == 1:
                    hdr, prof = hdr_of(s)
                    v = {
                        "stream": "video", "media": mi, "codec": s.get("codec"), "profile": s.get("profile"),
                        "bit_depth": s.get("bitDepth"), "width": s.get("width"), "height": s.get("height"),
                        "kbps": s.get("bitrate"), "frame_rate": s.get("frameRate"),
                        "color_trc": s.get("colorTrc"), "color_primaries": s.get("colorPrimaries"),
                        "color_space": s.get("colorSpace"), "hdr": hdr, "dv_profile": prof,
                        "dv_level": s.get("DOVILevel"), "dv_bl_compat_id": s.get("DOVIBLCompatID"),
                        "dv_el": s.get("DOVIELPresent"), "title": s.get("title"),
                        "display": s.get("extendedDisplayTitle") or s.get("displayTitle"),
                    }
                    entries.append(v)
                    if best_video is None:
                        best_video = v
                elif st == 2:
                    kind, studio = classify_voice(s.get("title"), bool(s.get("original")))
                    audio_count += 1
                    entries.append({
                        "stream": "audio", "media": mi, "lang": _lang(s), "language": s.get("language"),
                        "codec": s.get("codec"), "profile": s.get("profile"), "channels": s.get("channels"),
                        "layout": s.get("audioChannelLayout"), "kbps": s.get("bitrate"),
                        "title": s.get("title"), "kind": kind, "studio": studio,
                        "default": bool(s.get("default")), "original": bool(s.get("original")),
                        "display": s.get("extendedDisplayTitle") or s.get("displayTitle"),
                    })
                elif st == 3:
                    title = s.get("title") or ""
                    forced = bool(s.get("forced")) or bool(re.search(r"(?i)forced|форс|надпис|\bsigns\b", title))
                    if forced:
                        entries.append({"stream": "subtitle", "media": mi, "lang": _lang(s),
                                        "language": s.get("language"), "codec": s.get("codec"),
                                        "title": s.get("title"), "forced": True})
        if best_video is None:  # no stream details (should not happen after the detail fetch)
            best_video = {"codec": media.get("videoCodec"), "width": media.get("width"),
                          "height": media.get("height"), "hdr": [], "dv_profile": None}
        res = resolution_class(best_video.get("width"), best_video.get("height"), media.get("videoResolution"))
        versions.append({"media": mi, "resolution": res, "hdr": best_video.get("hdr") or [],
                         "dv_profile": best_video.get("dv_profile"), "audio_count": audio_count})
    best = max(versions, key=lambda v: ((v["resolution"] or 0),
                                        max([_HDR_RANK[h] for h in v["hdr"]] or [0])), default=None)
    # audio first so audio_streams() readers see them in order; then the rest
    entries.sort(key=lambda e: {"audio": 0, "video": 1, "subtitle": 2, "part": 3}[e["stream"]])
    return {"best": best, "entries": entries}


def audio_streams(row) -> list[dict]:
    """Audio tracks from a plex_items row (the column temporarily also holds other tagged entries)."""
    return [e for e in json.loads(row["audio"] or "[]") if e.get("stream") == "audio"]


def plex_extras(row, kind: str) -> list[dict]:
    """Non-audio entries of plex_items.audio: kind in video / subtitle / part / item."""
    return [e for e in json.loads(row["audio"] or "[]") if e.get("stream") == kind]


# --------------------------------------------------------------------------------------------- scan

def _sections(px: Plex) -> list[dict]:
    return px.get("/library/sections")["MediaContainer"].get("Directory") or []


def _list_section(px: Plex, key: str) -> list[dict]:
    out, start, page = [], 0, 200
    while True:
        mc = px.get(f"/library/sections/{key}/all", {"includeGuids": 1, "X-Plex-Container-Start": start,
                                                     "X-Plex-Container-Size": page})["MediaContainer"]
        md = mc.get("Metadata") or []
        out += md
        start += len(md)
        total = mc.get("totalSize", len(out))
        if not md or start >= total:
            break
    if len(out) != total:
        raise RuntimeError(f"section {key}: listed {len(out)} of totalSize {total}")
    return out


def _details(px: Plex, keys: list[str]) -> dict[str, dict]:
    """Full metadata (streams) for rating keys; batched, with single-key fallback for anything missing."""
    out: dict[str, dict] = {}
    for i in range(0, len(keys), 40):
        chunk = keys[i:i + 40]
        try:
            md = px.get("/library/metadata/" + ",".join(chunk), {"includeGuids": 1})["MediaContainer"].get("Metadata") or []
            for m in md:
                out[str(m["ratingKey"])] = m
        except requests.HTTPError:
            pass
    for k in keys:
        if k not in out:
            md = px.get(f"/library/metadata/{k}", {"includeGuids": 1})["MediaContainer"].get("Metadata") or []
            if md:
                out[k] = md[0]
    return out


def _link_title(conn, guids: list[str]) -> int | None:
    imdb = [g.split("://", 1)[1] for g in guids if g.startswith("imdb://")]
    for t in imdb:
        r = conn.execute("SELECT id FROM titles WHERE imdb_id = ?", (t,)).fetchone()
        if r:
            return r[0]
    return None


def build_item(lib: dict, listed: dict, meta: dict) -> dict:
    agent = lib.get("agent") or ""
    plex_guid = meta.get("guid") or listed.get("guid")
    ext = [g["id"] for g in (meta.get("Guid") or listed.get("Guid") or []) if g.get("id")]
    guids = ([plex_guid] if plex_guid else []) + [g for g in ext if g != plex_guid]
    media = describe_media(meta)
    parts = [e for e in media["entries"] if e["stream"] == "part"]
    first_file = parts[0]["file"] if parts else None
    # matched = Plex agent match (plex://) or at least external ids (e.g. a tv.plex.agents.none item with imdb guid)
    agent_matched = bool(plex_guid and plex_guid.startswith("plex://")) or any(
        g.startswith(("imdb://", "tmdb://", "tvdb://")) for g in ext)
    episode = None
    for p in parts:
        episode = parse_episode(p["file"] or "", strict_only=agent_matched)
        if episode:
            break
    if episode:
        kind = "episode-like"
    elif agent_matched:
        kind = "movie"
    else:
        kind = "unmatched"
    if agent_matched:
        year = meta.get("year")
    else:
        year = _year_in(PureWindowsPath(first_file).stem) if first_file else None
        if year is None and first_file:
            year = _year_in(PureWindowsPath(first_file).parent.name)
    item_entry = {
        "stream": "item", "plex_guid": plex_guid, "library_id": int(lib["key"]), "agent": agent,
        "plex_year_raw": meta.get("year"), "original_title": meta.get("originalTitle"),
        "edition": meta.get("editionTitle"), "added_at": ts_to_sql(meta.get("addedAt")),
        "originally_available_at": meta.get("originallyAvailableAt"),
        "duration_ms": meta.get("duration"), "episode": episode, "media_count": len(meta.get("Media") or []),
        "subtype": meta.get("subtype"),
    }
    best = media["best"] or {}
    return {
        "rating_key": str(meta.get("ratingKey") or listed["ratingKey"]),
        "library": lib["title"],
        "kind": kind,
        "plex_title": meta.get("title") or listed.get("title"),
        "plex_year": year,
        "guids": guids,
        "file_path": first_file,
        "resolution": best.get("resolution"),
        "source_type": guess_source_type(first_file) if first_file else None,
        "hdr": best.get("hdr") or [],
        "dv_profile": best.get("dv_profile"),
        "audio": media["entries"] + [item_entry],
        "view_count": int(listed.get("viewCount") or meta.get("viewCount") or 0),
        "last_viewed_at": ts_to_sql(listed.get("lastViewedAt") or meta.get("lastViewedAt")),
        # Plex «date added» (survey 2026-10-25: shown only on «Обновить качество»)
        "added_at": ts_to_sql(meta.get("addedAt") or listed.get("addedAt")),
    }


UPSERT_ITEM = """
INSERT INTO plex_items (rating_key, title_id, library, kind, plex_title, plex_year, guids, file_path,
                        resolution, source_type, hdr, dv_profile, audio, view_count, last_viewed_at, synced_at, added_at)
VALUES (:rating_key, :title_id, :library, :kind, :plex_title, :plex_year, :guids, :file_path,
        :resolution, :source_type, :hdr, :dv_profile, :audio, :view_count, :last_viewed_at, :synced_at, :added_at)
ON CONFLICT(rating_key) DO UPDATE SET
    added_at = COALESCE(excluded.added_at, plex_items.added_at), present = 1, removed_at = NULL,
    title_id = COALESCE(plex_items.title_id, excluded.title_id),
    library = excluded.library, kind = excluded.kind, plex_title = excluded.plex_title,
    plex_year = excluded.plex_year, guids = excluded.guids, file_path = excluded.file_path,
    resolution = excluded.resolution, source_type = excluded.source_type, hdr = excluded.hdr,
    dv_profile = excluded.dv_profile, audio = excluded.audio, view_count = excluded.view_count,
    last_viewed_at = excluded.last_viewed_at, synced_at = excluded.synced_at
"""


def scan_libraries(conn: sqlite3.Connection, px: Plex | None = None, log=print) -> dict:
    """Scan every library and every item into plex_items. Returns stats for the summary."""
    px = px or Plex()
    run_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    stats = {"libraries": [], "items": 0, "errors": [], "run_at": run_at}
    for lib in _sections(px):
        listed = _list_section(px, lib["key"])
        keys = [str(m["ratingKey"]) for m in listed]
        details = _details(px, keys)
        rows = []
        for m in listed:
            k = str(m["ratingKey"])
            meta = details.get(k)
            if meta is None:
                stats["errors"].append(f"{lib['title']}: no details for ratingKey {k} ({m.get('title')})")
                meta = m  # still record what the listing gave
            item = build_item(lib, m, meta)
            item["title_id"] = _link_title(conn, item["guids"])
            item["synced_at"] = run_at
            for col in ("guids", "hdr", "audio"):
                item[col] = json.dumps(item[col], ensure_ascii=False)
            rows.append(item)
        with conn:  # one short transaction per library
            conn.executemany(UPSERT_ITEM, rows)
        stats["libraries"].append((lib["key"], lib["title"], lib.get("agent"), len(rows)))
        stats["items"] += len(rows)
        log(f"  [{lib['key']:>2}] {lib['title']}: {len(rows)} items")
    stats["stale"] = conn.execute("SELECT count(*) FROM plex_items WHERE synced_at < ?", (run_at,)).fetchone()[0]
    # an item no library lists any more is kept (never deleted) but marked gone — the duplicate check
    # (films.duplicates, survey 2026-10-25) and «what is downloaded» must not count a removed copy
    if not stats["errors"]:
        with conn:
            stats["marked_removed"] = conn.execute(
                "UPDATE plex_items SET present = 0, removed_at = ? WHERE synced_at < ? AND present = 1",
                (run_at, run_at)).rowcount
    return stats


# ------------------------------------------------------------------------------------------ history

def _pms_db_path() -> Path:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_KEY) as k:
            base = Path(winreg.QueryValueEx(k, "LocalAppDataPath")[0])
    except OSError:
        base = Path(os.environ["LOCALAPPDATA"])
    return base / "Plex Media Server" / "Plug-in Support" / "Databases" / "com.plexapp.plugins.library.db"


def _decode_text(b) -> str | None:
    if b is None or isinstance(b, str):
        return b
    for enc in ("utf-8", "cp1251"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            pass
    return b.decode("utf-8", errors="replace")


def _read_pms_db(account_id: int) -> tuple[dict, list] | None:
    """Read-only view of the server's own history table (gives the guid of every history row, and rows the
    HTTP API hides: 'marked as watched' views and watch state without a play). None if unavailable."""
    path = _pms_db_path()
    if not path.exists():
        return None
    con = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=30)
    con.text_factory = _decode_text
    try:
        views = {r[0]: r for r in con.execute(
            "SELECT id, guid, metadata_type, library_section_id, grandparent_title, parent_index, \"index\", "
            "title, viewed_at, grandparent_guid, originally_available_at, view_type "
            "FROM metadata_item_views WHERE account_id = ?", (account_id,))}
        settings = con.execute(
            "SELECT guid, view_count, last_viewed_at FROM metadata_item_settings "
            "WHERE account_id = ? AND view_count > 0", (account_id,)).fetchall()
    finally:
        con.close()
    return views, settings


_MT = {1: "movie", 2: "show", 3: "season", 4: "episode", 12: "trailer"}


def _resolve_plex_guids(px: Plex, guids: set[str]) -> dict[str, dict]:
    """plex://<type>/<id> → {guids, title, year, type, grandparent_title} from the Plex metadata provider."""
    ids = {}
    for g in guids:
        m = re.match(r"plex://(\w+)/([0-9a-f]+)$", g or "")
        if m:
            ids[m.group(2)] = g
    out: dict[str, dict] = {}
    keys = sorted(ids)
    for i in range(0, len(keys), 20):
        chunk = keys[i:i + 20]
        try:
            md = px.get(f"{METADATA_PROVIDER}/library/metadata/" + ",".join(chunk))["MediaContainer"].get("Metadata") or []
        except requests.HTTPError:
            md = []
            for k in chunk:  # one bad id must not lose the rest of the batch
                try:
                    md += px.get(f"{METADATA_PROVIDER}/library/metadata/{k}")["MediaContainer"].get("Metadata") or []
                except requests.HTTPError:
                    pass
        for m in md:
            out[m.get("guid") or ids.get(m.get("ratingKey"))] = {
                "guids": [x["id"] for x in m.get("Guid") or []],
                "title": m.get("title"), "year": m.get("year"), "type": m.get("type"),
                "grandparent_title": m.get("grandparentTitle"),
                "original_title": m.get("originalTitle"),
            }
    return out


GQL_HISTORY = """query GetWatchHistoryHub($uuid: ID = "", $first: PaginationInt!, $after: String) {
  user(id: $uuid) {
    watchHistory(first: $first, after: $after) {
      nodes {
        id
        date
        metadataItem {
          id title type index year originallyAvailableAt publicPagesURL
          userState { viewCount viewedLeafCount }
          parent { id title index }
          grandparent { id title }
        }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}"""

GQL_STATS = """query GetUserStats($username: ID!) {
  userByUsername(username: $username) { watchStats { movieAmount episodeAmount showAmount } }
}"""


def _plextv_history(px: Plex) -> tuple[list[dict], dict]:
    user = px.get(f"{PLEXTV}/api/v2/user")
    if user.get("username") != OWNER_USERNAME:
        raise RuntimeError(f"token belongs to {user.get('username')!r}, expected {OWNER_USERNAME}")
    nodes, after = [], None
    while True:
        wh = px.graphql(GQL_HISTORY, {"uuid": user["uuid"], "first": 100, "after": after},
                        "GetWatchHistoryHub")["user"]["watchHistory"]
        nodes += wh["nodes"]
        if not wh["pageInfo"]["hasNextPage"]:
            break
        after = wh["pageInfo"]["endCursor"]
    stats = px.graphql(GQL_STATS, {"username": OWNER_USERNAME}, "GetUserStats")["userByUsername"]["watchStats"]
    return nodes, stats


def discover_children(px: Plex, key: str, params: dict | None = None) -> list[dict]:
    """All children of a Discover item. Discover pages children by 20 when no size is given and refuses
    (403) sizes far above 200 — so page by 200 until totalSize (South Park, Pokémon: 25+ seasons;
    an anime season: 37+ episodes)."""
    out, start = [], 0
    while True:
        mc = px.get(f"{DISCOVER_PROVIDER}/library/metadata/{key}/children",
                    dict(params or {}, **{"X-Plex-Container-Start": start, "X-Plex-Container-Size": 200}))["MediaContainer"]
        page = mc.get("Metadata") or []
        out += page
        start += len(page)
        if not page or start >= (mc.get("totalSize") or 0):
            return out


def _show_watched_episodes(px: Plex, show_id: str) -> list[dict]:
    """Watched episodes of a plex.tv show (the history hub lists a show once; episodes carry the dates)."""
    out = []
    seasons = discover_children(px, show_id, {"includeUserState": 1})
    for s in seasons:
        if not (s.get("viewedLeafCount") or 0):
            continue
        eps = discover_children(px, s["ratingKey"], {"includeUserState": 1})
        for e in eps:
            if (e.get("viewCount") or 0) > 0:
                out.append(e)
    return out


INSERT_HIST = """
INSERT INTO plex_history (source, plex_guid, guids, kind, title, year, grandparent_title, viewed_at, title_id)
VALUES (:source, :plex_guid, :guids, :kind, :title, :year, :grandparent_title, :viewed_at, :title_id)
"""
UPDATE_HIST = """
UPDATE plex_history SET source = :source, plex_guid = :plex_guid, guids = :guids, kind = :kind, title = :title,
    year = :year, grandparent_title = :grandparent_title, viewed_at = :viewed_at,
    title_id = COALESCE(title_id, :title_id)
WHERE id = :id
"""


def _store_history(conn, rows: list[dict]) -> dict:
    """Idempotent write. Each row carries a marker guid (pms-history://, plextv-activity://, ...) that
    identifies it across runs; a row whose (source, plex_guid, viewed_at) already exists is merged into
    that row (guid lists united) instead of being duplicated."""
    by_marker, by_key, guids_of = {}, {}, {}
    for r in conn.execute("SELECT id, source, plex_guid, viewed_at, guids FROM plex_history"):
        g = json.loads(r["guids"] or "[]")
        guids_of[r["id"]] = g
        by_key[(r["source"], r["plex_guid"], r["viewed_at"])] = r["id"]
        for x in g:
            if x.split("://", 1)[0] in ("pms-history", "pms-settings", "plextv-activity", "plextv-episode"):
                by_marker[x] = r["id"]
    res = {"new": 0, "updated": 0, "merged": 0}
    with conn:
        for row in rows:
            row = dict(row)
            row["title_id"] = _link_title(conn, row["guids"])
            marker = row.pop("marker")
            key = (row["source"], row["plex_guid"], row["viewed_at"])
            rid = by_marker.get(marker)
            key_owner = by_key.get(key)
            if rid is not None and key_owner not in (None, rid):
                rid = key_owner  # the view moved onto another row's key: merge there
            if rid is None:
                rid = key_owner
                if rid is not None:
                    res["merged"] += 1
            else:
                res["updated"] += 1
            if rid is None:
                row["guids"] = json.dumps(row["guids"], ensure_ascii=False)
                cur = conn.execute(INSERT_HIST, row)
                rid = cur.lastrowid
                guids_of[rid] = json.loads(row["guids"])
                res["new"] += 1
            else:
                merged = list(dict.fromkeys(row["guids"] + guids_of.get(rid, [])))
                row["guids"] = json.dumps(merged, ensure_ascii=False)
                row["id"] = rid
                conn.execute(UPDATE_HIST, row)
                guids_of[rid] = merged
            by_key[key] = rid
            by_marker[marker] = rid
    return res


def fetch_history(conn: sqlite3.Connection, px: Plex | None = None, log=print) -> dict:
    """Owner's full watch history: this server (API + server DB, account 1) and plex.tv (<plex.user>)."""
    px = px or Plex()
    st: dict = {"warnings": []}

    # ---- (a) server history, account 1 ---------------------------------------------------------
    api_rows, start = [], 0
    while True:
        mc = px.get("/status/sessions/history/all", {"accountID": OWNER_ACCOUNT_ID, "sort": "viewedAt:desc",
                                                     "X-Plex-Container-Start": start,
                                                     "X-Plex-Container-Size": 500})["MediaContainer"]
        md = mc.get("Metadata") or []
        api_rows += md
        start += len(md)
        if not md or start >= mc.get("totalSize", 0):
            break
    st["api_total"] = mc.get("totalSize")
    api_rows = [r for r in api_rows if int(r.get("accountID", OWNER_ACCOUNT_ID)) == OWNER_ACCOUNT_ID]
    st["api_rows"] = len(api_rows)
    if st["api_total"] is not None and len(api_rows) != st["api_total"]:
        st["warnings"].append(f"server history: got {len(api_rows)} rows of totalSize {st['api_total']}")

    pms = None
    try:
        pms = _read_pms_db(OWNER_ACCOUNT_ID)
    except sqlite3.Error as e:
        st["warnings"].append(f"server DB not readable ({e}); history rows have no guid and 'marked as "
                              f"watched' views are missing")
    views, settings = pms if pms else ({}, [])

    # current library items by their own guid → parsed info (episode-like show/season/episode, guids)
    items_by_guid = {}
    for r in conn.execute("SELECT guids, kind, plex_title, plex_year, audio FROM plex_items"):
        g = json.loads(r["guids"] or "[]")
        if g:
            extra = next((e for e in json.loads(r["audio"] or "[]") if e.get("stream") == "item"), {})
            items_by_guid.setdefault(g[0], {"guids": g[1:], "kind": r["kind"], "year": r["plex_year"],
                                            "episode": extra.get("episode")})

    lib_rows, skipped_trailers = [], Counter()
    seen_ids = set()
    for r in api_rows:
        hid = int(r["historyKey"].rsplit("/", 1)[1])
        seen_ids.add(hid)
        v = views.get(hid)
        lib_rows.append({"hid": hid, "title": r.get("title"), "viewed_at": r.get("viewedAt"),
                         "guid": v[1] if v else None, "mtype": v[2] if v else None,
                         "gp_title": v[4] if v else None, "view_type": v[11] if v else None,
                         "oaa": r.get("originallyAvailableAt"), "section": r.get("librarySectionID")})
    for hid, v in views.items():
        if hid in seen_ids:
            continue
        if v[2] == 12:  # trailer/extra views (iva://) — not a watched film; counted, not stored
            skipped_trailers[v[1].split(":", 1)[0]] += 1
            continue
        lib_rows.append({"hid": hid, "title": v[7], "viewed_at": v[8], "guid": v[1], "mtype": v[2],
                         "gp_title": v[4], "view_type": v[11], "oaa": None, "section": v[3]})
    st["db_only_rows"] = sum(1 for x in lib_rows if x["hid"] not in seen_ids)
    st["skipped_trailer_views"] = sum(skipped_trailers.values())

    # watch state without any history row (e.g. synced from plex.tv, or marked before history existed)
    view_guids = {v[1] for v in views.values()}
    settings_only = [s for s in settings if s[0] not in view_guids and not s[0].startswith("plex://clip/")]
    st["skipped_trailer_views"] += sum(1 for s in settings if s[0] not in view_guids and s[0].startswith("plex://clip/"))

    # plex.tv history -------------------------------------------------------------------------------
    nodes, tv_stats = _plextv_history(px)
    st["plextv_stats"] = tv_stats
    st["plextv_nodes"] = len(nodes)
    show_eps = {}
    for n in nodes:
        mi = n.get("metadataItem") or {}
        if (mi.get("type") or "").upper() == "SHOW" and mi["id"] not in show_eps:
            show_eps[mi["id"]] = _show_watched_episodes(px, mi["id"])

    # resolve every plex:// guid in one pass
    all_guids = {x["guid"] for x in lib_rows if x["guid"]} | {s[0] for s in settings_only}
    for n in nodes:
        mi = n.get("metadataItem") or {}
        if mi.get("id"):
            all_guids.add(f"plex://{(mi.get('type') or 'movie').lower()}/{mi['id']}")
    for eps in show_eps.values():
        all_guids |= {e["guid"] for e in eps if e.get("guid")}
    resolved = _resolve_plex_guids(px, all_guids)
    st["unresolved_plex_guids"] = sorted(g for g in all_guids if g and g.startswith("plex://") and g not in resolved)

    rows = []
    for x in lib_rows:
        g = x["guid"]
        res = resolved.get(g) or {}
        local = items_by_guid.get(g) or {}
        ext = res.get("guids") or local.get("guids") or []
        strict = bool(g and g.startswith("plex://"))
        ep = local.get("episode") or parse_episode((x["title"] or "") + ".x", strict_only=strict)
        mtype = _MT.get(x["mtype"]) if x["mtype"] is not None else None
        if mtype == "movie" and (ep or local.get("kind") == "episode-like"):
            kind = "episode-like"
        elif mtype == "movie" and g and not g.startswith("plex://"):
            kind = "unmatched"
        else:
            kind = mtype or ("episode-like" if ep else None)
        year = res.get("year") or (local.get("year") if local else None)
        if year is None and strict and x["oaa"]:
            year = int(str(x["oaa"])[:4])
        vt = {0: "played", 1: "marked"}.get(x["view_type"])
        rows.append({
            "source": "library", "plex_guid": g or f"pms-history://{x['hid']}",
            "guids": ext + [f"pms-history://{x['hid']}"] + ([f"pms-view://{vt}"] if vt else []),
            "kind": kind, "title": x["title"], "year": year,
            "grandparent_title": x["gp_title"] or res.get("grandparent_title") or (ep or {}).get("show"),
            "viewed_at": ts_to_sql(x["viewed_at"]), "marker": f"pms-history://{x['hid']}",
        })
    for guid, _cnt, last in settings_only:
        res = resolved.get(guid) or {}
        local = items_by_guid.get(guid) or {}
        m = re.match(r"plex://(\w+)/", guid)
        rows.append({
            "source": "library", "plex_guid": guid,
            "guids": (res.get("guids") or local.get("guids") or []) + [f"pms-settings://{guid}"],
            "kind": res.get("type") or (m.group(1) if m else (local.get("kind") or "unmatched")),
            "title": res.get("title"), "year": res.get("year"),
            "grandparent_title": res.get("grandparent_title"),
            "viewed_at": ts_to_sql(last), "marker": f"pms-settings://{guid}",
        })
    st["settings_only_rows"] = len(settings_only)

    for n in nodes:
        mi = n.get("metadataItem") or {}
        typ = (mi.get("type") or "").lower()
        g = f"plex://{typ}/{mi.get('id')}"
        res = resolved.get(g) or {}
        rows.append({
            "source": "plextv", "plex_guid": g,
            "guids": (res.get("guids") or []) + [f"plextv-activity://{n['id']}"],
            "kind": typ, "title": mi.get("title"), "year": mi.get("year") or res.get("year"),
            "grandparent_title": (mi.get("grandparent") or {}).get("title"),
            "viewed_at": iso_to_sql(n.get("date")), "marker": f"plextv-activity://{n['id']}",
        })
    st["plextv_episode_rows"] = 0
    for show_id, eps in show_eps.items():
        for e in eps:
            res = resolved.get(e.get("guid")) or {}
            rows.append({
                "source": "plextv", "plex_guid": e.get("guid") or f"plex://episode/{e['ratingKey']}",
                "guids": (res.get("guids") or []) + [f"plextv-episode://{e['ratingKey']}"],
                "kind": "episode", "title": e.get("title"), "year": e.get("year") or res.get("year"),
                "grandparent_title": e.get("grandparentTitle"),
                "viewed_at": ts_to_sql(e.get("lastViewedAt")), "marker": f"plextv-episode://{e['ratingKey']}",
            })
            st["plextv_episode_rows"] += 1

    st.update(_store_history(conn, rows))
    st["rows"] = len(rows)
    return st


# ------------------------------------------------------------------------------------------ summary

def print_summary(conn, scan: dict | None, hist: dict | None):
    p = print
    if scan:
        p("\n== Libraries ==")
        for key, title, agent, n in scan["libraries"]:
            p(f"  [{key:>2}] {title:28} {n:5}   ({'Plex Movie agent' if 'movie' in (agent or '') else 'no agent'})")
        p(f"  total items: {scan['items']} in {len(scan['libraries'])} libraries")
        if scan["errors"]:
            p("  ERRORS:")
            for e in scan["errors"]:
                p("   ", e)
        if scan["stale"]:
            p(f"  rows in plex_items not seen in this scan (file/library removed from Plex): {scan['stale']}")
    q = lambda sql, *a: conn.execute(sql, a).fetchall()  # noqa: E731
    p("\n== Kind ==")
    for r in q("SELECT kind, count(*) n FROM plex_items GROUP BY kind ORDER BY n DESC"):
        p(f"  {r['kind']}: {r['n']}")
    p("\n== Quality (resolution x HDR) ==")
    for r in q("SELECT resolution, hdr, count(*) n FROM plex_items GROUP BY resolution, hdr ORDER BY resolution DESC, n DESC"):
        p(f"  {r['resolution'] or '?':>5}  {', '.join(json.loads(r['hdr'] or '[]')) or 'SDR':20} {r['n']}")
    p("\n== DV profiles ==")
    for r in q("SELECT dv_profile, count(*) n FROM plex_items WHERE dv_profile IS NOT NULL GROUP BY dv_profile"):
        p(f"  Profile {r['dv_profile']}: {r['n']}")
    p("\n== Source type (from file name) ==")
    for r in q("SELECT COALESCE(source_type,'(unknown)') s, count(*) n FROM plex_items GROUP BY s ORDER BY n DESC"):
        p(f"  {r['s']}: {r['n']}")
    codecs = Counter()
    for r in q("SELECT audio FROM plex_items"):
        v = next((e for e in json.loads(r["audio"] or "[]") if e.get("stream") == "video"), None)
        codecs[(v or {}).get("codec")] += 1
    p("\n== Video codec ==\n  " + ", ".join(f"{k}: {v}" for k, v in codecs.most_common()))
    p("\n== Without imdb guid ==")
    for r in q("SELECT kind, count(*) n FROM plex_items WHERE guids NOT LIKE '%imdb://%' GROUP BY kind"):
        p(f"  {r['kind']}: {r['n']}")
    for r in q("SELECT library, plex_title FROM plex_items WHERE kind='movie' AND guids NOT LIKE '%imdb://%'"):
        p(f"    movie without imdb: [{r['library']}] {r['plex_title']}")
    dup = q("SELECT file_path, count(*) n, group_concat(library, ' | ') libs FROM plex_items "
            "GROUP BY lower(file_path) HAVING n > 1")
    p(f"\n== Same file in several libraries: {len(dup)} files ==")
    p(f"  watched in library (owner viewCount>0): "
      f"{q('SELECT count(*) c FROM plex_items WHERE view_count > 0')[0]['c']}")
    p(f"  linked to titles (imdb match): {q('SELECT count(*) c FROM plex_items WHERE title_id IS NOT NULL')[0]['c']}")
    if hist:
        p("\n== History ==")
        p(f"  server API rows (account {OWNER_ACCOUNT_ID}): {hist['api_rows']} (totalSize {hist['api_total']})")
        p(f"  + rows only in the server DB ('marked as watched' etc.): {hist['db_only_rows']}")
        p(f"  + watch state without a history row: {hist['settings_only_rows']}")
        p(f"  trailer views skipped: {hist['skipped_trailer_views']}")
        p(f"  plex.tv history nodes: {hist['plextv_nodes']} (+ {hist['plextv_episode_rows']} episodes of watched shows); "
          f"profile stats: {hist['plextv_stats']}")
        p(f"  written: {hist['new']} new, {hist['updated']} updated, {hist['merged']} merged into an "
          f"existing row with the same (source, guid, time), of {hist['rows']}")
        if hist["unresolved_plex_guids"]:
            p(f"  plex guids the metadata provider did not resolve: {len(hist['unresolved_plex_guids'])}")
        for w in hist["warnings"]:
            p("  WARNING:", w)
    p("")
    for r in q("SELECT source, kind, count(*) n, sum(guids LIKE '%imdb://%') imdb FROM plex_history "
               "GROUP BY source, kind ORDER BY source, n DESC"):
        p(f"  plex_history {r['source']:8} {str(r['kind']):13} {r['n']:5}  with imdb: {r['imdb']}")
    lib_guids = set()
    for r in q("SELECT guids FROM plex_items"):
        lib_guids.update(json.loads(r["guids"]))
    tv_only = set()
    for r in q("SELECT guids, plex_guid FROM plex_history WHERE source='plextv'"):
        ids = [g for g in json.loads(r["guids"]) if g.split("://")[0] in ("imdb", "tmdb", "tvdb")]
        if r["plex_guid"] not in lib_guids and not any(g in lib_guids for g in ids):
            tv_only.add(r["plex_guid"])
    p(f"  plex.tv titles not present in current libraries: {len(tv_only)}")


def main():
    from films import db
    conn = db.connect()
    db.migrate(conn)
    px = Plex()
    print("Scanning Plex libraries…")
    scan = scan_libraries(conn, px)
    print("Fetching watch history…")
    hist = fetch_history(conn, px)
    print_summary(conn, scan, hist)
    print(f"HTTP requests: {px.requests}")


if __name__ == "__main__":
    sys.exit(main())
