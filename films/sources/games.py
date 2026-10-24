"""The owner's games (PlayStation, Steam) -> screen adaptations of those games / their game franchises (R36).

Usage:
    python -X utf8 -m films.sources.games collect [--store playstation|steam] [--max-age DAYS]
    python -X utf8 -m films.sources.games import STORE FILE      # list exported by the owner (see docs/games.md)
    python -X utf8 -m films.sources.games adaptations [--max-age DAYS] [--no-wikipedia]
    python -X utf8 -m films.sources.games report [--json]

Docs: docs/games.md.  The owner's account identifiers come from config/private.toml [games]
(`<games.ps_store_db_url>`, `<games.steam_id>`, `<games.steamdb_url>`) via films.db.private(); they are
never printed, never written into the DB (rows keep only game ids / titles / play stats) and never into
tracked files.  Optional STEAM_API_KEY (environment or .env) enables the full Steam library.

Collection (plain HTTP only, no browser):
  * PlayStation — ps-store-db.live profile page.  The public page is server-rendered and shows only
    highlights (top-9 by hours, first games per console, platinums, badge strips); the full game list
    ("hub": backlog / lists) is served only to the browser that built the page (session cookie,
    /api/hub/me).  So `collect` stores the highlights and says how many games the page reports in total.
  * Steam — official endpoints first: /profiles/<id>/games?tab=all&xml=1 (since 2025 it redirects every
    anonymous visitor to the sign-in page, for any profile); then, if STEAM_API_KEY is set,
    IPlayerService/GetOwnedGames (full library); otherwise the public profile page (library size,
    favourite game, recent activity, achievement showcase) and every page of the public reviews list
    (a review can only be written for a product in the library).  Names/types: store appdetails API.
    SteamDB calculator is tried once as a fallback and reported (Cloudflare challenge).

Adaptations (Wikidata + Wikipedia):
  1. game -> Wikidata item: Steam app id via P1733 (exact; several items with one app id are all used);
     otherwise a title search (EntitySearch run inside WDQS) restricted to items that are video games,
     exact normalized label/alias match, store platform as a tie-breaker.  Still several candidates ->
     'ambiguous' (used only when all candidates are in one game series).  Missing matches are reported.
  2. related nodes: the game, its series (P179, two levels), its media franchise (P8345, also of the
     series), its fictional universe (P1080), sibling games of the same series, and what the game / its
     series is itself based on (P144: novels, toys, films -> relation 'same_source', always 'review').
  3. screen works (films, TV / web series, miniseries, animation; not episodes / seasons / trailers /
     porn parodies) with P144 "based on" -> a node (or a film series based on it), or sharing the
     series / franchise / universe (P179 / P8345 / P1080).  IMDb id = P345; without it the TMDB id
     (P4947 / P4983) is resolved to IMDb through films/sources/tmdb.py; still none -> 'wd:Q…'.
  4. en / ru Wikipedia articles of the game and of its game series: sections about adaptations
     ("Adaptations", "Film", "Television", "In other media", «Экранизации», «В других медиа» …, with
     subsections): linked articles and {{Main|…}} targets that are screen works (film series expanded to
     their parts) are added; only-Wikipedia finds get confidence 'review'.
  A franchise / universe counts as game-originated when Wikidata types it as a video game series or its
  earliest dated member is a video game.  Other franchises (Star Wars, Marvel …) are "shared franchise"
  links: kept with confidence 'review' when small, skipped (and reported) above BIG_FRANCHISE works.

Rows: `games` (store, game_id, title, platform, raw JSON {source…, wikidata…}) and `game_adaptations`
(store, game_id, imdb_id, title, year, kind, sources JSON {relation, via, found_in, confidence, …}).
Rows are upserted, never deleted.  Raw HTTP responses are cached under data/cache/games/ (gitignored).
"""
from __future__ import annotations

import argparse
import hashlib
import html as htmlmod
import json
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

import requests

from films import db
from films.sources.imdb import norm

CACHE = db.ROOT / "data" / "cache" / "games"
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/130.0 Safari/537.36")
BOT_UA = "films-db/0.1 (personal non-commercial film database; python-requests)"
MIN_INTERVAL = {                       # seconds between requests per host (polite)
    "steamcommunity.com": 3.0, "store.steampowered.com": 1.6, "api.steampowered.com": 1.0,
    "ps-store-db.live": 2.0, "steamdb.info": 2.0,
    "query.wikidata.org": 1.0, "www.wikidata.org": 0.3, "en.wikipedia.org": 0.3, "ru.wikipedia.org": 0.3,
}
MAX_ATTEMPTS = 6
DEFAULT_MAX_AGE = 30.0
BIG_FRANCHISE = 25                     # shared (non-game) franchise with more screen works -> skipped

STATS = defaultdict(int)


class BlockedError(RuntimeError):
    """The source answered with a bot challenge / login wall; we do not try to get around it."""


# ----------------------------------------------------------------------------------------------
# HTTP with file cache, per-host rate limit, backoff
# ----------------------------------------------------------------------------------------------

_session = requests.Session()
_last: dict[str, float] = {}


def _wikimedia(host: str) -> bool:
    return host != "query.wikidata.org" and host.endswith(("wikipedia.org", "wikidata.org"))


def _wait(host: str):
    """Per-host gap.  The Wikimedia action APIs (all *.wikipedia.org + www.wikidata.org) are one bucket:
    10 requests/min for a User-Agent without contact, ~200/min with FILMS_CONTACT (see films/franchise.py)."""
    bucket, gap = host, MIN_INTERVAL.get(host, 1.0)
    if _wikimedia(host):
        from films import franchise
        bucket, gap = "wikimedia", (0.6 if franchise.contact() else franchise.WIKIMEDIA_INTERVAL_ANON)
    d = _last.get(bucket, 0) + gap - time.monotonic()
    if d > 0:
        time.sleep(d)
    _last[bucket] = time.monotonic()


def _ua(host: str, browser_ua: bool) -> str:
    if browser_ua:
        return BROWSER_UA
    if host.endswith(("wikipedia.org", "wikidata.org")):
        from films import franchise
        return franchise.user_agent()          # includes FILMS_CONTACT when set (never printed)
    return BOT_UA


def _cache_path(kind: str, key: str, ext: str) -> Path:
    h = hashlib.sha1(key.encode("utf-8")).hexdigest()[:24]
    return CACHE / kind / f"{h}.{ext}"


def fetch(url: str, *, kind: str, params: dict | None = None, data: dict | None = None,
          headers: dict | None = None, max_age: float = DEFAULT_MAX_AGE, as_json: bool = False,
          browser_ua: bool = False, redact: str | None = None) -> dict:
    """Cached GET (POST when `data`).  Returns {'status', 'url' (final, redacted), 'text' | 'json', 'cached'}.
    `redact` = a private value to replace in logged URLs.  Login redirects / Cloudflare challenges raise
    BlockedError (and are cached as such for a day so a re-run does not hammer the site)."""
    key = json.dumps([url, params, data], sort_keys=True, ensure_ascii=False)
    path = _cache_path(kind, key, "json")
    if path.exists() and max_age > 0:
        try:
            c = json.loads(path.read_text(encoding="utf-8"))
            limit = 1.0 if c.get("blocked") else max_age
            if time.time() - c["at"] < limit * 86400:
                STATS["cache_hits"] += 1
                if c.get("blocked"):
                    raise BlockedError(c["blocked"])
                return {**c["resp"], "cached": True}
        except (ValueError, KeyError):
            pass
    host = urlparse(url).netloc
    hdr = {"User-Agent": _ua(host, browser_ua), "Accept-Language": "en-US,en;q=0.8"}
    hdr.update(headers or {})
    shown = url.replace(redact, "<private>") if redact else url
    last = ""
    attempts = MAX_ATTEMPTS + (4 if _wikimedia(host) or "wikidata" in host else 0)
    for attempt in range(attempts):
        _wait(host)
        try:
            # Wikimedia: a fresh connection per request.  A kept-alive session stayed pinned to an edge node
            # that answered every request with 429 «robot policy», while new connections went through.
            http = requests if _wikimedia(host) else _session
            if data is not None:
                r = http.post(url, data=data, headers=hdr, timeout=90)
            else:
                r = http.get(url, params=params, headers=hdr, timeout=60)
        except requests.RequestException as e:
            last = type(e).__name__
            STATS["retries"] += 1
            time.sleep(min(5 * 2 ** attempt, 120))
            continue
        STATS["requests"] += 1
        final = r.url.replace(redact, "<private>") if redact else r.url
        blocked = None
        if r.headers.get("cf-mitigated") == "challenge" or "<title>Just a moment...</title>" in r.text[:3000]:
            blocked = f"Cloudflare challenge (HTTP {r.status_code}) at {host}"
        elif "steamcommunity.com/login" in r.url or "/login/?redir=" in r.url:
            blocked = f"redirected to the Steam sign-in page ({shown})"
        if blocked:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"at": time.time(), "blocked": blocked}), encoding="utf-8")
            raise BlockedError(blocked)
        if r.status_code == 429 or r.status_code >= 500:
            ra = r.headers.get("Retry-After")
            wait = min(int(ra), 600) if ra and ra.isdigit() else min(30 * 2 ** attempt, 300)
            last = f"HTTP {r.status_code}"
            STATS["retries"] += 1
            body = re.sub(r"\s+", " ", r.text[:300])
            print(f"  {host}: {last}, waiting {wait}s ({body})", file=sys.stderr)
            time.sleep(wait)
            continue
        resp = {"status": r.status_code, "url": final}
        if as_json:
            try:
                resp["json"] = r.json()
            except ValueError:
                resp["json"] = None
                resp["text"] = r.text[:2000]
        else:
            resp["text"] = r.text
        if r.status_code in (200, 404):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"at": time.time(), "resp": resp}, ensure_ascii=False), encoding="utf-8")
        return {**resp, "cached": False}
    raise RuntimeError(f"{shown}: giving up after {attempts} attempts ({last})")


def _text(html: str) -> str:
    html = re.sub(r"<script.*?</script>|<style.*?</style>|<svg.*?</svg>", " ", html, flags=re.S)
    return re.sub(r"\s+", " ", htmlmod.unescape(re.sub(r"<[^>]+>", " ", html))).strip()


def _env_key(name: str) -> str | None:
    k = os.environ.get(name)
    if k:
        return k
    env = db.ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith(name + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'") or None
    return None


# ----------------------------------------------------------------------------------------------
# DB helpers
# ----------------------------------------------------------------------------------------------

def _upsert_game(conn, store: str, game_id: str, title: str | None, platform: str | None, raw: dict):
    """Merges `raw['source']` lists with what is already stored (several collection paths)."""
    row = conn.execute("SELECT raw FROM games WHERE store = ? AND game_id = ?", (store, game_id)).fetchone()
    old = json.loads(row["raw"]) if row and row["raw"] else {}
    merged = dict(old)
    for k, v in raw.items():
        if k == "seen_in":
            merged[k] = sorted(set(old.get(k, [])) | set(v))
        elif v is not None:
            merged[k] = v
    conn.execute(
        "INSERT INTO games (store, game_id, title, platform, raw, checked_at) VALUES (?, ?, ?, ?, ?, datetime('now'))"
        " ON CONFLICT(store, game_id) DO UPDATE SET title = COALESCE(excluded.title, games.title),"
        " platform = COALESCE(excluded.platform, games.platform), raw = excluded.raw, checked_at = excluded.checked_at",
        (store, game_id, title, platform, json.dumps(merged, ensure_ascii=False)))


def _set_raw_key(conn, store, game_id, key, value):
    row = conn.execute("SELECT raw FROM games WHERE store = ? AND game_id = ?", (store, game_id)).fetchone()
    raw = json.loads(row["raw"]) if row and row["raw"] else {}
    raw[key] = value
    conn.execute("UPDATE games SET raw = ? WHERE store = ? AND game_id = ?",
                 (json.dumps(raw, ensure_ascii=False), store, game_id))


def _note(conn_notes: dict, store: str, msg: str):
    conn_notes.setdefault(store, []).append(msg)
    print(f"[{store}] {msg}")


NOTES_FILE = CACHE / "collect_notes.json"


# ----------------------------------------------------------------------------------------------
# PlayStation: ps-store-db.live public profile
# ----------------------------------------------------------------------------------------------

_PS_CV = re.compile(r"""<div class="pf-cv[^"]*" style="background-image:url\('([^']*)'\)" role="img" """
                    r"""aria-label="([^"]*)"[^>]*>(.*?)</div>""", re.S)


def parse_ps_profile(page: str) -> tuple[list[dict], dict]:
    """-> (games, summary).  Each game: title, sections (where it appears), hours, image, ids from image url."""
    summary = {}
    txt = _text(page)
    m = re.search(r"([\d,]+) games played", txt)
    summary["games_played_total"] = int(m.group(1).replace(",", "")) if m else None
    m = re.search(r"([\d,]+) hours on PS", txt)
    summary["hours_total"] = int(m.group(1).replace(",", "")) if m else None
    m = re.search(r"([\d,]+) platinums", txt)
    summary["platinums"] = int(m.group(1).replace(",", "")) if m else None
    summary["owner_only_tools"] = "only appear in the browser that built this page" in txt
    # split by <h2> headings to know the section of each tile
    parts = re.split(r"<h2[^>]*>(.*?)</h2>", page, flags=re.S)
    games: dict[str, dict] = {}
    section = "top"
    for i, chunk in enumerate(parts):
        if i % 2 == 1:
            section = _text(chunk)
            continue
        for sub in re.split(r'(?=<details class="pf-card pf-badge">)', chunk):
            badge = re.search(r"<b>([^<]+)</b>\s*</div>\s*</summary>", sub)
            where = f"badge: {htmlmod.unescape(badge.group(1))}" if badge else section
            for img, label, inner in _PS_CV.findall(sub):
                title = htmlmod.unescape(label).strip()
                if not title:
                    continue
                g = games.setdefault(norm(title), {"title": title, "seen_in": [], "ids": []})
                if where not in g["seen_in"]:
                    g["seen_in"].append(where)
                h = re.search(r"pf-pill\">(\d[\d,]*)h<", inner)
                if h:
                    g["hours"] = int(h.group(1).replace(",", ""))
                for pid in re.findall(r"\b((?:CUSA|PPSA|NPWR|NPUA|BCES|BLES)\d{5})", img):
                    if pid not in g["ids"]:
                        g["ids"].append(pid)
                g.setdefault("image", img)
        # "First PS3 game · 2011" tags
    for tag, title in re.findall(r'<span class="pf-tag">([^<]+)</span><b>([^<]+)</b>', page):
        k = norm(htmlmod.unescape(title))
        if k in games:
            games[k]["first_game"] = htmlmod.unescape(tag).replace("\xa0", " ")
    return list(games.values()), summary


def collect_playstation(conn, notes: dict, max_age: float):
    url = db.private()["games"]["ps_store_db_url"]
    try:
        r = fetch(url, kind="ps", browser_ua=True, max_age=max_age, redact=url)
    except BlockedError as e:
        _note(notes, "playstation", f"profile page blocked: {e}")
        return
    if r["status"] != 200:
        _note(notes, "playstation", f"profile page: HTTP {r['status']}")
        return
    games, summary = parse_ps_profile(r["text"])
    # the full list is only behind the owner's session (/api/hub/me); check what an anonymous visitor gets
    try:
        me = fetch("https://ps-store-db.live/api/hub/me", kind="ps", as_json=True, browser_ua=True, max_age=max_age)
        hub = (me.get("json") or {}).get("hub")
    except BlockedError as e:
        hub = f"blocked: {e}"
    _note(notes, "playstation",
          f"public profile: {len(games)} games visible (highlights) of {summary['games_played_total']} games "
          f"played per the page; hours {summary['hours_total']}, platinums {summary['platinums']}; "
          f"/api/hub/me without the owner's session -> hub={hub!r}; full list = owner-only "
          f"(page says: tools only in the browser that built it)")
    with conn:
        for g in games:
            gid = "title:" + norm(g["title"])
            _upsert_game(conn, "playstation", gid, g["title"], "PS4/PS5 (per profile)",
                         {"source": "ps-store-db.live public profile highlights", "seen_in": g["seen_in"],
                          "hours": g.get("hours"), "first_game": g.get("first_game"),
                          "ps_ids": g["ids"] or None, "image": g.get("image"),
                          "note": "played (trophy data), not necessarily purchased"})
    notes.setdefault("playstation_summary", summary)


# ----------------------------------------------------------------------------------------------
# Steam
# ----------------------------------------------------------------------------------------------

def _steam_appdetails(appid: str, max_age: float) -> dict:
    """name / type from the store API; falls back to the community app page title."""
    try:
        r = fetch("https://store.steampowered.com/api/appdetails", kind="steam_app",
                  params={"appids": appid, "filters": "basic", "l": "english"}, as_json=True, max_age=max_age * 6)
        d = (r.get("json") or {}).get(str(appid)) or {}
        if d.get("success") and d.get("data"):
            return {"name": d["data"].get("name"), "type": d["data"].get("type"), "via": "store_api"}
    except (BlockedError, RuntimeError) as e:
        print(f"  appdetails {appid}: {e}", file=sys.stderr)
    try:
        r = fetch(f"https://steamcommunity.com/app/{appid}", kind="steam_app", max_age=max_age * 6, browser_ua=True)
        m = re.search(r"<title>Steam Community :: (.*?)</title>", r.get("text") or "")
        if m and m.group(1).strip() and m.group(1).strip() != "Error":
            return {"name": htmlmod.unescape(m.group(1).strip()), "type": None, "via": "community_page"}
    except (BlockedError, RuntimeError) as e:
        print(f"  community app {appid}: {e}", file=sys.stderr)
    return {"name": None, "type": None, "via": "not_found"}


def parse_steam_profile(page: str) -> tuple[dict, dict[str, list[str]]]:
    """-> (summary, {appid: [where]}).  No personal data is returned."""
    txt = _text(page)
    summary = {}
    m = re.search(r"(\d[\d,]*) games in library", page) or re.search(r"Games\s+(\d[\d,]*)", txt)
    summary["library_size"] = int(m.group(1).replace(",", "")) if m else None
    m = re.search(r"(\d[\d,]*) DLC", txt)
    summary["dlc"] = int(m.group(1).replace(",", "")) if m else None
    m = re.search(r"Reviews\s+(\d[\d,]*)", txt)
    summary["reviews"] = int(m.group(1).replace(",", "")) if m else None
    apps: dict[str, list[str]] = defaultdict(list)
    blocks = {
        "favorite_game": r'class="favoritegame_showcase.*?(?=<div class="profile_customization[ "]|<div class="profile_recentgame_header)',
        "achievement_showcase": r'class="showcase_content_bg showcase_stats_row.*?(?=<div class="profile_customization[ "])',
        "recent_activity": r'class="recent_games".*?(?=<div class="profile_rightcol|<div class="recentgame_quicklinks)',
    }
    for where, rx in blocks.items():
        for b in re.findall(rx, page, flags=re.S):
            for a in re.findall(r"(?:steamcommunity\.com|store\.steampowered\.com)/app/(\d+)", b):
                if where not in apps[a]:
                    apps[a].append(where)
    for a in re.findall(r"(?:steamcommunity\.com|store\.steampowered\.com)/app/(\d+)", page):
        if a not in apps:
            apps[a].append("profile_page")
    return summary, apps


def parse_steam_reviews(page: str) -> tuple[list[dict], bool]:
    out = []
    for box in re.split(r'class="review_box"', page)[1:]:
        m = re.search(r"steamcommunity\.com/app/(\d+)", box)
        if not m:
            continue
        hrs = re.search(r"([\d,.]+) hrs on record", box)
        out.append({"appid": m.group(1), "hours": float(hrs.group(1).replace(",", "")) if hrs else None,
                    "counted_purchase": "icon_review_counted" in box,
                    "recommended": "icon_thumbsUp" in box})
    nxt = re.search(r"""pagebtn['"]\s+href="\?p=\d+">&gt;""", page) is not None
    return out, nxt


def collect_steam(conn, notes: dict, max_age: float):
    g = db.private()["games"]
    sid = str(g["steam_id"])
    base = f"https://steamcommunity.com/profiles/{sid}"
    apps: dict[str, dict] = {}

    def add(appid, where, **extra):
        a = apps.setdefault(str(appid), {"seen_in": []})
        if where not in a["seen_in"]:
            a["seen_in"].append(where)
        for k, v in extra.items():
            if v is not None:
                a[k] = v

    full = False
    # 1. official XML games list
    try:
        r = fetch(f"{base}/games", kind="steam", params={"tab": "all", "xml": "1"}, max_age=max_age,
                  browser_ua=True, redact=sid)
        found = re.findall(r"<appID>(\d+)</appID>\s*<name><!\[CDATA\[(.*?)\]\]></name>", r["text"])
        for appid, name in found:
            add(appid, "games_xml", name=name)
        full = bool(found)
        _note(notes, "steam", f"games?tab=all&xml=1: {len(found)} games")
    except BlockedError as e:
        _note(notes, "steam", f"games?tab=all&xml=1 -> {e} (Steam requires sign-in for game lists of any profile)")
    # 2. Web API (needs the owner's key)
    key = _env_key("STEAM_API_KEY")
    if not full and key:
        try:
            r = fetch("https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/", kind="steam_api",
                      params={"key": key, "steamid": sid, "include_appinfo": 1, "include_played_free_games": 1},
                      as_json=True, max_age=max_age, redact=key)
            gl = ((r.get("json") or {}).get("response") or {}).get("games") or []
            for x in gl:
                add(x["appid"], "web_api", name=x.get("name"),
                    hours=round(x.get("playtime_forever", 0) / 60, 1))
            full = bool(gl)
            _note(notes, "steam", f"Web API GetOwnedGames: HTTP {r['status']}, {len(gl)} games")
        except (BlockedError, RuntimeError) as e:
            _note(notes, "steam", f"Web API GetOwnedGames failed: {str(e).replace(key, '***')}")
    elif not full:
        _note(notes, "steam", "STEAM_API_KEY not set -> full library unavailable; collecting the public subset")
    # 3. public profile page + reviews (subset)
    summary = {}
    try:
        r = fetch(base + "/", kind="steam", max_age=max_age, browser_ua=True, redact=sid)
        summary, papps = parse_steam_profile(r["text"])
        for a, where in papps.items():
            for w in where:
                add(a, w)
        _note(notes, "steam", f"public profile: library {summary.get('library_size')} games, "
                              f"{summary.get('dlc')} DLC, {summary.get('reviews')} reviews; "
                              f"{len(papps)} apps linked on the page")
    except (BlockedError, RuntimeError) as e:
        _note(notes, "steam", f"public profile failed: {e}")
    n_rev, p = 0, 1
    while True:
        try:
            r = fetch(base + "/recommended/", kind="steam", params={"p": p}, max_age=max_age,
                      browser_ua=True, redact=sid)
        except (BlockedError, RuntimeError) as e:
            _note(notes, "steam", f"reviews page {p} failed: {e}")
            break
        rows, more = parse_steam_reviews(r["text"])
        for x in rows:
            add(x["appid"], "reviews", hours=x["hours"], counted_purchase=x["counted_purchase"])
        n_rev += len(rows)
        if not more or not rows or p > 200:
            break
        p += 1
    _note(notes, "steam", f"public reviews: {n_rev} reviews on {p} pages")
    # 4. SteamDB calculator (fallback, usually Cloudflare)
    if not full and g.get("steamdb_url"):
        try:
            r = fetch(g["steamdb_url"], kind="steam", max_age=max_age, browser_ua=True, redact=sid)
            _note(notes, "steam", f"SteamDB calculator: HTTP {r['status']} (not parsed: page without a challenge "
                                  "was not expected; inspect the cache)")
        except BlockedError as e:
            _note(notes, "steam", f"SteamDB calculator: {e} -> not bypassed")
    notes["steam_summary"] = {**summary, "full_library": full, "collected": len(apps)}
    # names / types
    for appid, a in apps.items():
        if not a.get("name") or not a.get("type"):
            d = _steam_appdetails(appid, max_age)
            a.setdefault("name", d["name"])
            if not a.get("name"):
                a["name"] = d["name"]
            a["type"] = d["type"]
            a["name_via"] = d["via"]
    with conn:
        for appid, a in apps.items():
            _upsert_game(conn, "steam", appid, a.get("name"), "pc",
                         {"source": "steam " + ("full library" if full else "public subset"),
                          "seen_in": a["seen_in"], "hours": a.get("hours"), "app_type": a.get("type"),
                          "counted_purchase": a.get("counted_purchase"), "name_via": a.get("name_via")})
    _note(notes, "steam", f"stored {len(apps)} apps ({'full library' if full else 'public subset only'}, "
                          f"library size per profile: {summary.get('library_size')})")


# ----------------------------------------------------------------------------------------------
# import of an owner-provided list
# ----------------------------------------------------------------------------------------------

def import_list(conn, store: str, path: Path) -> int:
    """JSON: GetOwnedGames response / [{appid|id, name|title}] / ["title", …]; or text: one title per line."""
    text = path.read_text(encoding="utf-8-sig")
    items: list[tuple[str | None, str]] = []
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            data = (data.get("response") or data).get("games") or data.get("titles") or []
        for x in data:
            if isinstance(x, str):
                items.append((None, x))
            else:
                items.append((str(x.get("appid") or x.get("id") or "") or None, x.get("name") or x.get("title")))
    except ValueError:
        items = [(None, line.strip()) for line in text.splitlines() if line.strip()]
    with conn:
        for gid, title in items:
            if not title and not gid:
                continue
            _upsert_game(conn, store, gid or "title:" + norm(title), title, None,
                         {"source": f"import:{path.name}", "seen_in": ["import"]})
    return len(items)


# ----------------------------------------------------------------------------------------------
# Wikidata / Wikipedia
# ----------------------------------------------------------------------------------------------

WD = "http://www.wikidata.org/entity/"
SCREEN_ROOTS = "wd:Q11424 wd:Q5398426 wd:Q526877 wd:Q1259759 wd:Q15416 wd:Q63952888 wd:Q581714 wd:Q20650540"
EXCLUDE_TYPES = {"Q21191270", "Q3464665", "Q24856", "Q7889", "Q7058673", "Q196600", "Q1983062"}
PLATFORM = {"playstation": {"Q5014725", "Q63184502", "Q10683", "Q1065000"},   # PS4, PS5, PS3, PS Vita
            "steam": {"Q1406", "Q14116", "Q388"}}                              # Windows, macOS, Linux


def sparql(query: str, max_age: float) -> list[dict]:
    r = fetch("https://query.wikidata.org/sparql", kind="wd_sparql", data={"query": query, "format": "json"},
              headers={"Accept": "application/sparql-results+json"}, as_json=True, max_age=max_age)
    if r["status"] != 200 or not r.get("json"):
        raise RuntimeError(f"SPARQL HTTP {r['status']}: {(r.get('text') or '')[:300]}")
    out = []
    for b in r["json"]["results"]["bindings"]:
        out.append({k: v["value"].replace(WD, "") for k, v in b.items()})
    return out


def wp_api(lang: str, params: dict, max_age: float) -> dict:
    r = fetch(f"https://{lang}.wikipedia.org/w/api.php", kind=f"wp_{lang}",
              params={**params, "format": "json", "formatversion": 2}, as_json=True, max_age=max_age)
    return r.get("json") or {}


_EDITION = re.compile(
    r"\s*(?:[-–—:]\s*)?\b(?:\d+(?:st|nd|rd|th) anniversary|game of the year edition|goty edition|"
    r"(?:digital |ultimate |deluxe |complete |definitive |enhanced |gold |standard |special |collector'?s |"
    r"director'?s cut |legendary |premium |anniversary )+edition|remastered|ps4 (?:&|and) ps5|"
    r"for ps4|ps4™?|ps5™?|director'?s cut)\b.*$", re.I)


_SUFFIXES = re.compile(r"\s*\b(?:hd collection|collection|trilogy|n\. ?sane trilogy|remaster(?:ed)?|redux|"
                       r"definitive|complete edition|enhanced edition|game of the year)\b.*$", re.I)


def clean_title(t: str) -> str:
    t = re.sub(r"[™®©]", "", t or "").strip()
    t2 = _EDITION.sub("", t).strip(" -–—:")
    return t2 or t


def _chunks(xs, n):
    xs = list(xs)
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


def map_steam_qids(appids: list[str], max_age: float) -> dict[str, list[str]]:
    out: dict[str, list[str]] = defaultdict(list)
    for ch in _chunks(appids, 150):
        vals = " ".join(f'"{a}"' for a in ch)
        for b in sparql(f"SELECT ?game ?app WHERE {{ VALUES ?app {{ {vals} }} ?game wdt:P1733 ?app . }}", max_age):
            if b["game"] not in out[b["app"]]:
                out[b["app"]].append(b["game"])
    return out


def _video_games_info(qids: list[str], max_age: float) -> dict[str, dict]:
    """qid -> {is_game, platforms, labels(normalized label+aliases), series, label, year}"""
    info: dict[str, dict] = {}
    for ch in _chunks(qids, 80):
        vals = " ".join(f"wd:{q}" for q in ch)
        q = f"""SELECT ?c ?isgame ?plat ?ser ?name ?date ?label WHERE {{
          VALUES ?c {{ {vals} }}
          BIND(EXISTS {{ ?c wdt:P31/wdt:P279* wd:Q7889 }} AS ?isgame)
          OPTIONAL {{ ?c wdt:P400 ?plat }}
          OPTIONAL {{ ?c wdt:P179 ?ser }}
          OPTIONAL {{ ?c wdt:P577 ?date }}
          OPTIONAL {{ ?c rdfs:label ?label . FILTER(LANG(?label) IN ("en", "mul")) }}
          OPTIONAL {{ {{ ?c rdfs:label ?name }} UNION {{ ?c skos:altLabel ?name }}
                     FILTER(LANG(?name) IN ("en", "mul", "ru", "ja")) }}
        }}"""
        for b in sparql(q, max_age):
            d = info.setdefault(b["c"], {"is_game": False, "platforms": set(), "names": set(), "series": set(),
                                         "years": set(), "label": None})
            d["is_game"] = d["is_game"] or b.get("isgame") in ("true", "1")
            if b.get("plat"):
                d["platforms"].add(b["plat"])
            if b.get("ser"):
                d["series"].add(b["ser"])
            if b.get("name"):
                d["names"].add(norm(b["name"]))
            if b.get("date"):
                d["years"].add(int(b["date"][:4]) if b["date"][:4].isdigit() else None)
            if b.get("label"):
                d["label"] = b["label"]
    return info


def _lit(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)          # a valid SPARQL string literal


def entity_search(text: str, max_age: float, limit: int = 20) -> list[dict]:
    """Wikidata full-text entity search (label + aliases, like wbsearchentities) run inside WDQS
    (mwapi EntitySearch), which avoids the 10 req/min anonymous Wikimedia action-API bucket."""
    q = f"""SELECT ?item WHERE {{ SERVICE wikibase:mwapi {{
        bd:serviceParam wikibase:endpoint "www.wikidata.org"; wikibase:api "EntitySearch";
                        mwapi:search {_lit(text)}; mwapi:language "en"; mwapi:limit "{limit}" .
        ?item wikibase:apiOutputItem mwapi:item . }} }}"""
    return sparql(q, max_age)


def match_title(title: str, store: str, max_age: float) -> dict:
    """Wikidata item of a video game by title.  -> {'status': matched|ambiguous|none, 'qid', 'candidates', 'how'}"""
    cleaned = clean_title(title)
    keys = {norm(cleaned), norm(title)}
    cand: list[str] = []
    for q in dict.fromkeys([cleaned, title]):
        for b in entity_search(q, max_age):
            if b["item"] not in cand:
                cand.append(b["item"])
    if not cand:
        return {"status": "none", "qid": None, "candidates": [], "how": "no search results", "query": cleaned}
    info = _video_games_info(cand, max_age)
    games = [c for c in cand if info.get(c, {}).get("is_game")]
    exact = [c for c in games if keys & info[c]["names"]]
    how = "exact title"
    if not exact:
        # 'Title: Subtitle' stored without the subtitle or with different punctuation
        loose = {k.replace(" ", "") for k in keys}
        exact = [c for c in games if loose & {n.replace(" ", "") for n in info[c]["names"]}]
        how = "title without spaces/punctuation"
    if not exact:
        return {"status": "none", "qid": None, "how": "no video game with this exact title",
                "candidates": [{"qid": c, "label": info[c]["label"]} for c in games[:8]], "query": cleaned}
    if len(exact) > 1:
        # the store's own title (before edition stripping) names exactly one item: "God of War III Remastered"
        verbatim = [c for c in exact if norm(title) in info[c]["names"]]
        if norm(title) != norm(cleaned) and len(verbatim) == 1:
            return {"status": "matched", "qid": verbatim[0], "how": how + " (store title verbatim)",
                    "candidates": [{"qid": c, "label": info[c]["label"]} for c in exact]}
        plat = PLATFORM.get(store, set())
        on_plat = [c for c in exact if info[c]["platforms"] & plat]
        if len(on_plat) == 1:
            return {"status": "matched", "qid": on_plat[0], "how": how + " + platform",
                    "candidates": [{"qid": c, "label": info[c]["label"]} for c in exact]}
        pool = on_plat or exact
        series = {frozenset(info[c]["series"]) for c in pool}
        return {"status": "ambiguous", "qid": None, "how": how + f", {len(pool)} candidates",
                "same_series": len(series) == 1 and bool(next(iter(series))),
                "candidates": [{"qid": c, "label": info[c]["label"],
                                "years": sorted(y for y in info[c]["years"] if y)} for c in pool]}
    return {"status": "matched", "qid": exact[0], "how": how, "candidates": []}


def resolve_games(conn, max_age: float) -> dict[tuple[str, str], dict]:
    rows = conn.execute("SELECT store, game_id, title, raw FROM games ORDER BY store, title").fetchall()
    steam_ids = [r["game_id"] for r in rows if r["store"] == "steam" and r["game_id"].isdigit()]
    by_app = map_steam_qids(steam_ids, max_age) if steam_ids else {}
    out = {}
    for r in rows:
        raw = json.loads(r["raw"]) if r["raw"] else {}
        if raw.get("app_type") and raw["app_type"] not in ("game",):
            res = {"status": "skipped", "qid": None, "how": f"steam app type '{raw['app_type']}'"}
        elif r["store"] == "steam" and by_app.get(r["game_id"]):
            q = by_app[r["game_id"]]
            # several items carry the same app id (original + remaster/edition item): all are this app
            res = {"status": "matched" if len(q) == 1 else "matched_multi", "qid": q[0] if len(q) == 1 else None,
                   "how": "Steam app id (P1733)" + (f", {len(q)} items share it" if len(q) > 1 else ""),
                   "candidates": [{"qid": x} for x in q] if len(q) > 1 else []}
        elif r["title"]:
            res = match_title(r["title"], r["store"], max_age)
        else:
            res = {"status": "none", "qid": None, "how": "no title"}
        out[(r["store"], r["game_id"])] = res
        with conn:
            _set_raw_key(conn, r["store"], r["game_id"], "wikidata", res)
    return out


def _years(b: dict) -> int | None:
    for k in ("date", "start"):
        v = b.get(k)
        if v and v[:4].lstrip("+-").isdigit():
            return int(v[:4].lstrip("+"))
    return None


def screen_works_for(game_qids: list[str], max_age: float) -> list[dict]:
    """All (game, node, nodeKind, rel, work) links from Wikidata.  Three cheap steps instead of one query
    (the combined query times out on WDQS): nodes of each game -> items linked to the nodes -> type filter."""
    nodes: list[dict] = [{"game": g, "node": g, "nodeKind": "game"} for g in game_qids]
    for ch in _chunks(game_qids, 40):
        vals = " ".join(f"wd:{q}" for q in ch)
        q = f"""SELECT DISTINCT ?game ?node ?nodeKind WHERE {{
          VALUES ?game {{ {vals} }}
          {{ ?game wdt:P179 ?node . BIND("series" AS ?nodeKind) }}
          UNION {{ ?game wdt:P179/wdt:P179 ?node . BIND("series2" AS ?nodeKind) }}
          UNION {{ ?game wdt:P8345 ?node . BIND("franchise" AS ?nodeKind) }}
          UNION {{ ?game wdt:P179/wdt:P8345 ?node . BIND("franchise" AS ?nodeKind) }}
          UNION {{ ?game wdt:P1080 ?node . BIND("universe" AS ?nodeKind) }}
          UNION {{ ?game wdt:P144 ?node . BIND("source" AS ?nodeKind) }}
          UNION {{ ?game wdt:P179/wdt:P144 ?node . BIND("source" AS ?nodeKind) }}
          UNION {{ ?game wdt:P179 ?s . ?node wdt:P179 ?s . FILTER(?node != ?game) BIND("sibling_game" AS ?nodeKind) }}
        }}"""
        nodes.extend(sparql(q, max_age))
    by_node: dict[str, list[dict]] = defaultdict(list)
    for n in nodes:
        by_node[n["node"]].append(n)
    linked: list[dict] = []
    for ch in _chunks(sorted(by_node), 60):
        vals = " ".join(f"wd:{q}" for q in ch)
        q = f"""SELECT DISTINCT ?node ?rel ?work WHERE {{
          VALUES ?node {{ {vals} }}
          {{ ?work wdt:P144 ?node . BIND("based_on" AS ?rel) }}
          UNION {{ ?fs wdt:P144 ?node . ?work wdt:P179 ?fs . BIND("based_on" AS ?rel) }}
          UNION {{ ?work wdt:P8345 ?node . BIND("franchise" AS ?rel) }}
          UNION {{ ?work wdt:P179 ?node . BIND("part_of_series" AS ?rel) }}
          UNION {{ ?work wdt:P1080 ?node . BIND("universe" AS ?rel) }}
        }}"""
        linked.extend(sparql(q, max_age))
    screen = is_screen_work(sorted({x["work"] for x in linked}), max_age) if linked else set()
    out = []
    for x in linked:
        if x["work"] not in screen:
            continue
        for n in by_node[x["node"]]:
            out.append({"game": n["game"], "node": x["node"], "nodeKind": n["nodeKind"], "rel": x["rel"],
                        "work": x["work"]})
    return out


def work_details(qids: list[str], max_age: float) -> dict[str, dict]:
    det: dict[str, dict] = {}
    for ch in _chunks(qids, 60):
        vals = " ".join(f"wd:{q}" for q in ch)
        q = f"""SELECT ?w ?imdb ?tmdbm ?tmdbt ?type ?typeLabel ?date ?start ?en ?mul ?ru ?genre WHERE {{
          VALUES ?w {{ {vals} }}
          OPTIONAL {{ ?w wdt:P345 ?imdb }}
          OPTIONAL {{ ?w wdt:P4947 ?tmdbm }}
          OPTIONAL {{ ?w wdt:P4983 ?tmdbt }}
          OPTIONAL {{ ?w wdt:P31 ?type . ?type rdfs:label ?typeLabel . FILTER(LANG(?typeLabel) = "en") }}
          OPTIONAL {{ ?w wdt:P577 ?date }}
          OPTIONAL {{ ?w wdt:P580 ?start }}
          OPTIONAL {{ ?w wdt:P136 ?genre }}
          OPTIONAL {{ ?w rdfs:label ?en . FILTER(LANG(?en) = "en") }}
          OPTIONAL {{ ?w rdfs:label ?mul . FILTER(LANG(?mul) = "mul") }}
          OPTIONAL {{ ?w rdfs:label ?ru . FILTER(LANG(?ru) = "ru") }}
        }}"""
        for b in sparql(q, max_age):
            d = det.setdefault(b["w"], {"imdb": set(), "tmdb_movie": set(), "tmdb_tv": set(), "types": {},
                                        "years": set(), "en": None, "ru": None, "genres": set()})
            for k, f in (("imdb", "imdb"), ("tmdbm", "tmdb_movie"), ("tmdbt", "tmdb_tv")):
                if b.get(k):
                    d[f].add(b[k])
            if b.get("type"):
                d["types"][b["type"]] = b.get("typeLabel") or b["type"]
            y = _years(b)
            if y:
                d["years"].add(y)
            if b.get("genre"):
                d["genres"].add(b["genre"])
            d["en"] = d["en"] or b.get("en") or b.get("mul")
            d["ru"] = d["ru"] or b.get("ru")
    return det


ADULT_GENRES = {"Q185529", "Q16254232"}          # pornographic film, pornographic parody film
ANIMATION_GENRES = {"Q202866", "Q581714", "Q1107", "Q20650540", "Q63952888", "Q11425", "Q28968511"}


def work_kind(d: dict) -> str:
    labels = " | ".join(d["types"].values()).lower()
    series = any(w in labels for w in ("series", "television program", "miniseries", "original net animation",
                                       "original video animation", "anime television"))
    animated = any(w in labels for w in ("animat", "anime", "cartoon")) or bool(d["genres"] & ANIMATION_GENRES)
    if "documentary" in labels:
        base = "documentary series" if series else "documentary"
    elif "short" in labels and not series:
        base = "short"
    elif "miniseries" in labels:
        base = "miniseries"
    elif "web series" in labels:
        base = "web series"
    elif series:
        base = "series"
    elif "television film" in labels:
        base = "TV film"
    else:
        base = "film"
    return ("animated " + base) if animated else base


def node_info(qids: list[str], max_age: float) -> dict[str, dict]:
    """label, P31 labels, game-originated?, number of screen works linked to it, wiki sitelinks."""
    out: dict[str, dict] = {}
    for ch in _chunks(qids, 60):
        vals = " ".join(f"wd:{q}" for q in ch)
        q = f"""SELECT ?n ?en ?mul ?ru ?typeLabel ?enwiki ?ruwiki WHERE {{
          VALUES ?n {{ {vals} }}
          OPTIONAL {{ ?n rdfs:label ?en . FILTER(LANG(?en) = "en") }}
          OPTIONAL {{ ?n rdfs:label ?mul . FILTER(LANG(?mul) = "mul") }}
          OPTIONAL {{ ?n rdfs:label ?ru . FILTER(LANG(?ru) = "ru") }}
          OPTIONAL {{ ?n wdt:P31 ?type . ?type rdfs:label ?typeLabel . FILTER(LANG(?typeLabel) = "en") }}
          OPTIONAL {{ ?enwiki schema:about ?n ; schema:isPartOf <https://en.wikipedia.org/> }}
          OPTIONAL {{ ?ruwiki schema:about ?n ; schema:isPartOf <https://ru.wikipedia.org/> }}
        }}"""
        for b in sparql(q, max_age):
            d = out.setdefault(b["n"], {"en": None, "ru": None, "types": set(), "enwiki": None, "ruwiki": None})
            d["en"] = d["en"] or b.get("en") or b.get("mul")
            d["ru"] = d["ru"] or b.get("ru")
            if b.get("typeLabel"):
                d["types"].add(b["typeLabel"])
            for w in ("enwiki", "ruwiki"):
                if b.get(w):
                    d[w] = requests.utils.unquote(b[w].rsplit("/wiki/", 1)[1]).replace("_", " ")
    for d in out.values():
        d["game_origin"] = any("video game" in t for t in d["types"])
        d["origin_how"] = "P31 video game series/franchise" if d["game_origin"] else None
    return out


def franchise_origin(qids: list[str], max_age: float) -> dict[str, dict]:
    """Franchise / universe item -> earliest publication year of its video-game members vs. of its other
    members (P8345 / P1080 / P179 pointing at it).  Game-originated = a game came first."""
    out = {}
    for q in qids:
        query = f"""SELECT ?isgame (MIN(?d) AS ?first) WHERE {{
          ?m wdt:P8345|wdt:P1080|wdt:P179 wd:{q} . ?m wdt:P577 ?d .
          BIND(EXISTS {{ ?m wdt:P31/wdt:P279* wd:Q7889 }} AS ?isgame) }} GROUP BY ?isgame"""
        try:
            rows = sparql(query, max_age)
        except RuntimeError as e:
            out[q] = {"error": str(e)[:200]}
            continue
        first = {("game" if r.get("isgame") in ("true", "1") else "other"): int(r["first"][:4])
                 for r in rows if r.get("first") and r["first"][:4].isdigit()}
        out[q] = first
    return out


def _resolve_imdb(conn, d: dict) -> tuple[str | None, str]:
    if d["imdb"]:
        ids = sorted(i for i in d["imdb"] if i.startswith("tt"))
        if ids:
            return ids[0], "wikidata P345" + (f" (several: {ids})" if len(ids) > 1 else "")
    from films.sources import tmdb
    for tid in sorted(d["tmdb_movie"]):
        b = tmdb.movie_raw(conn, int(tid)) if tid.isdigit() else None
        imdb = b and (b.get("imdb_id") or (b.get("external_ids") or {}).get("imdb_id"))
        if imdb:
            return imdb, f"TMDB movie {tid} external_ids"
    for tid in sorted(d["tmdb_tv"]):
        b = tmdb.tv_raw(conn, int(tid)) if tid.isdigit() else None
        imdb = b and (b.get("external_ids") or {}).get("imdb_id")
        if imdb:
            return imdb, f"TMDB tv {tid} external_ids"
    return None, "no IMDb id on Wikidata / TMDB"


SECTION_RX = {
    "en": re.compile(r"adaptation|film|television|tv series|in other media|animated|anime|live[- ]action|"
                     r"cinema|movie|media franchise|other media", re.I),
    "ru": re.compile(r"экраниз|адаптац|фильм|сериал|в других медиа|кино|мульт|аниме|телевид|другие медиа", re.I),
}


_HEAD = re.compile(r"^(={2,6})\s*(.*?)\s*\1\s*$", re.M)
_LINK = re.compile(r"\[\[([^\]\|#{}]+)(?:#[^\]\|]*)?(?:\|[^\]]*)?\]\]")
_TPL_LINK = re.compile(r"\{\{\s*(?:main|see also|further|details|main article|основная статья|см\. также|"
                       r"подробнее|основная)\s*\|([^}]+)\}\}", re.I)


def _wtitle(t: str) -> str:
    t = re.sub(r"\s+", " ", t.replace("_", " ")).strip()
    return t[:1].upper() + t[1:] if t else t


def section_wikitexts(lang: str, page: str, max_age: float) -> list[tuple[str, str]]:
    """[(heading, wikitext)] of the adaptation-like sections of `page` (subsections included).
    Two small requests (section list, then each matching top section) instead of the whole article:
    full-article batches were answered with Varnish 429 «robot policy» while other clients shared the
    anonymous Wikimedia bucket.  Raises RuntimeError when rate limited for good."""
    res = wp_api(lang, {"action": "parse", "page": page, "prop": "sections", "redirects": 1}, max_age)
    secs = (res.get("parse") or {}).get("sections") or []
    out = []
    taken_until = None                     # skip subsections of a section already taken
    for sec in secs:
        level = int(sec.get("toclevel") or 1)
        if taken_until is not None and level > taken_until:
            continue
        taken_until = None
        name = _text(sec.get("line") or "")
        if not SECTION_RX[lang].search(name) or not str(sec.get("index", "")).isdigit():
            continue
        r2 = wp_api(lang, {"action": "parse", "page": page, "section": sec["index"], "prop": "wikitext",
                           "redirects": 1}, max_age)
        out.append((name, (r2.get("parse") or {}).get("wikitext") or ""))
        taken_until = level
    return out


def adaptation_links(lang: str, wikitext: str) -> list[tuple[str, str]]:
    """-> [(section heading, linked title)] from adaptation-like sections (with their subsections)."""
    heads = [(m.start(), len(m.group(1)), _text(m.group(2))) for m in _HEAD.finditer(wikitext)]
    out = []
    for i, (pos, level, name) in enumerate(heads):
        if not SECTION_RX[lang].search(name):
            continue
        end = len(wikitext)
        for pos2, level2, _ in heads[i + 1:]:
            if level2 <= level:
                end = pos2
                break
        body = wikitext[pos:end]
        for t in _LINK.findall(body):
            if ":" in t and t.split(":", 1)[0].strip().lower() in (
                    "file", "image", "category", "файл", "категория", "изображение", "wikt", "s", "commons"):
                continue
            out.append((name, _wtitle(t)))
        for args in _TPL_LINK.findall(body):
            for t in args.split("|"):
                if t.strip() and "=" not in t:
                    out.append((name, _wtitle(t)))
    return out


def titles_to_qids(lang: str, titles: list[str], max_age: float) -> dict[str, str]:
    """Wikipedia titles -> QIDs: exact sitelinks via WDQS first, the rest (redirects) via the action API."""
    out: dict[str, str] = {}
    titles = sorted(set(titles))
    for ch in _chunks(titles, 150):
        vals = " ".join(f"{_lit(t)}@{lang}" for t in ch)
        q = f"""SELECT ?t ?item WHERE {{ VALUES ?t {{ {vals} }}
          ?a schema:about ?item ; schema:isPartOf <https://{lang}.wikipedia.org/> ; schema:name ?t . }}"""
        for b in sparql(q, max_age):
            out[b["t"]] = b["item"]
    rest = [t for t in titles if t not in out]
    for ch in _chunks(rest, 50):
        res = wp_api(lang, {"action": "query", "titles": "|".join(ch), "prop": "pageprops",
                            "ppprop": "wikibase_item", "redirects": 1}, max_age)
        q = res.get("query") or {}
        back = {}
        for n in q.get("normalized", []) + q.get("redirects", []):
            back[n["to"]] = back.get(n["from"], n["from"])
        for p in q.get("pages", []):
            qid = (p.get("pageprops") or {}).get("wikibase_item")
            if qid:
                out[back.get(p["title"], p["title"])] = qid
    return out


def expand_lists(qids: list[str], max_age: float) -> dict[str, set[str]]:
    """Film series / franchise / list items linked from Wikipedia -> their screen-work parts
    (P527 has part, or works with P179 part of the series)."""
    out: dict[str, set[str]] = defaultdict(set)
    for ch in _chunks(qids, 80):
        vals = " ".join(f"wd:{q}" for q in ch)
        q = f"""SELECT DISTINCT ?l ?w WHERE {{ VALUES ?l {{ {vals} }}
          ?l wdt:P31/wdt:P279* ?lt . VALUES ?lt {{ wd:Q24856 wd:Q196600 wd:Q1667921 }}
          {{ ?l wdt:P527 ?w }} UNION {{ ?w wdt:P179 ?l }}
          ?w wdt:P31/wdt:P279* ?root . VALUES ?root {{ {SCREEN_ROOTS} }} }}"""
        for b in sparql(q, max_age):
            out[b["l"]].add(b["w"])
    return out


def is_screen_work(qids: list[str], max_age: float) -> set[str]:
    ok = set()
    for ch in _chunks(qids, 40):
        vals = " ".join(f"wd:{q}" for q in ch)
        excl = " ".join(f"wd:{q}" for q in EXCLUDE_TYPES)
        q = f"""SELECT DISTINCT ?w WHERE {{ VALUES ?w {{ {vals} }}
          ?w wdt:P31/wdt:P279* ?root . VALUES ?root {{ {SCREEN_ROOTS} }}
          FILTER NOT EXISTS {{ ?w wdt:P31 ?x . VALUES ?x {{ {excl} }} }} }}"""
        ok |= {b["w"] for b in sparql(q, max_age)}
    return ok


def _shared(node_kind: str, ni: dict) -> bool:
    """A link through something that did not start as a game: a franchise / universe that is not a video
    game series, or the work the game itself is based on (novels, films …) -> 'review', capped by size."""
    return node_kind == "source" or (node_kind in ("franchise", "universe") and not ni.get("game_origin"))


def find_adaptations(conn, max_age: float, use_wikipedia: bool = True) -> dict:
    res = resolve_games(conn, max_age)
    game_titles = {(r["store"], r["game_id"]): r["title"]
                   for r in conn.execute("SELECT store, game_id, title FROM games")}
    qid_games: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for key, r in res.items():
        if r.get("qid"):
            qid_games[r["qid"]].append(key)
        elif r.get("status") == "matched_multi" or (r.get("status") == "ambiguous" and r.get("same_series")):
            # every candidate belongs to the same game series -> adaptations do not depend on the choice
            for c in r["candidates"]:
                qid_games[c["qid"]].append(key)
    print(f"games: {len(res)}; with a Wikidata item: {len(qid_games)} items")
    links = screen_works_for(sorted(qid_games), max_age)
    # Wikipedia sections of the game and its game series
    wiki_links: list[dict] = []
    notes_wp: list[str] = []
    wp_checked: dict[str, int] = {}
    nodes_for_wiki: dict[str, set] = defaultdict(set)      # node qid -> games
    ser = sparql("SELECT ?g ?s WHERE { VALUES ?g { %s } ?g wdt:P179 ?s }"
                 % " ".join(f"wd:{q}" for q in qid_games), max_age) if qid_games else []
    for g in qid_games:
        nodes_for_wiki[g].add(g)
    for b in ser:
        nodes_for_wiki[b["s"]].add(b["g"])
    all_nodes = sorted({b["node"] for b in links} | set(nodes_for_wiki))
    ninfo = node_info(all_nodes, max_age)
    fr_nodes = sorted({b["node"] for b in links if b["nodeKind"] in ("franchise", "universe")
                       and not ninfo.get(b["node"], {}).get("game_origin")})
    for n, first in franchise_origin(fr_nodes, max_age).items():
        ni = ninfo.setdefault(n, {"en": None, "ru": None, "types": set()})
        ni["first_members"] = first
        if "game" in first and first["game"] <= first.get("other", 9999):
            ni["game_origin"] = True
            ni["origin_how"] = f"earliest member is a video game ({first})"
    if use_wikipedia:
        found: list[tuple[str, str, str, str]] = []     # (node, lang, section, page title)
        for lang in ("en", "ru"):
            pages: dict[str, list[str]] = defaultdict(list)
            for node in sorted(nodes_for_wiki):
                ni = ninfo.get(node, {})
                if node not in qid_games and not ni.get("game_origin"):
                    continue                              # series item that is not a game series
                if ni.get(f"{lang}wiki"):
                    pages[ni[f"{lang}wiki"]].append(node)
            missing = []
            for page in sorted(pages):
                try:
                    sections = section_wikitexts(lang, page, max_age)
                except RuntimeError as e:
                    print(f"  {lang}.wikipedia {page}: {e}", file=sys.stderr)
                    missing.append(page)
                    continue
                for head, txt in sections:
                    for sec, t in adaptation_links(lang, txt):
                        for node in pages[page]:
                            found.append((node, lang, sec if sec == head else f"{head} / {sec}", t))
            wp_checked[lang] = len(pages) - len(missing)
            if missing:
                notes_wp.append(f"{lang}.wikipedia: NOT checked (rate limited) {missing}")
        by_lang = defaultdict(list)
        for node, lang, sec, t in found:
            by_lang[lang].append(t)
        t2q = {lang: titles_to_qids(lang, ts, max_age) for lang, ts in by_lang.items()}
        cand = {t2q[lang].get(t) for node, lang, sec, t in found} - {None}
        screen = is_screen_work(sorted(cand), max_age) if cand else set()
        parts = expand_lists(sorted(cand - screen), max_age) if cand - screen else {}
        for node, lang, sec, t in found:
            w = t2q[lang].get(t)
            ws = [w] if w in screen else sorted(parts.get(w, ()))
            for x in ws:
                for g in nodes_for_wiki[node]:
                    wiki_links.append({"game": g, "node": node, "nodeKind": "game" if node == g else "series",
                                       "rel": f"wikipedia:{lang}", "work": x,
                                       "section": sec if x == w else f"{sec} -> {t}"})
    all_works = sorted({b["work"] for b in links} | {b["work"] for b in wiki_links})
    det = work_details(all_works, max_age)
    # drop episodes / seasons / film-series items
    drop = {w for w, d in det.items() if set(d["types"]) & EXCLUDE_TYPES
            or any("trailer" in t.lower() for t in d["types"].values())}
    adult = {w: d["en"] for w, d in det.items() if d["genres"] & ADULT_GENRES}   # porn parodies: not adaptations
    drop |= set(adult)
    # size of non-game franchises / universes
    shared_size: dict[str, set] = defaultdict(set)
    for b in links:
        if _shared(b["nodeKind"], ninfo.get(b["node"], {})):
            shared_size[b["node"]].add(b["work"])
    skipped_big = {n: len(ws) for n, ws in shared_size.items() if len(ws) > BIG_FRANCHISE}
    # Wikipedia-only links: "Other media" sections also link the director's / writer's other films, cameo
    # films, look-alikes ("Saw", "Pacific Rim" from God of War).  Such a link is kept only when the work's
    # title contains the name of the game, its series or game franchise; the rest are listed for the owner.
    wd_pairs = {(b["game"], b["work"]) for b in links}
    game_names: dict[str, set] = defaultdict(set)
    for g in qid_games:
        titles = [ninfo.get(g, {}).get("en")] + [r_title for (st, gid) in qid_games[g]
                                                 for r_title in [game_titles.get((st, gid))]]
        for t in titles:
            if t:
                game_names[g] |= {norm(clean_title(t)), norm(_SUFFIXES.sub("", clean_title(t)))}
        for x in ser:
            if x["g"] == g:
                game_names[g].add(norm(ninfo.get(x["s"], {}).get("en") or ""))
    for b in links:
        if b["nodeKind"] in ("series", "series2") or (b["nodeKind"] == "franchise"
                                                      and ninfo.get(b["node"], {}).get("game_origin")):
            game_names[b["game"]].add(norm(ninfo.get(b["node"], {}).get("en") or ""))
    unconfirmed: dict[tuple[str, str], dict] = {}
    kept_wiki = []
    for b in wiki_links:
        if (b["game"], b["work"]) in wd_pairs or b["work"] in drop:
            kept_wiki.append(b)
            continue
        d = det.get(b["work"]) or {}
        label = norm(d.get("en") or "")
        if any(len(n) >= 4 and n in label for n in game_names[b["game"]]):
            kept_wiki.append(b)
        else:
            u = unconfirmed.setdefault((b["game"], b["work"]), {"work": d.get("en") or b["work"], "qid": b["work"],
                                                                 "imdb": sorted(d.get("imdb") or []),
                                                                 "game": ninfo.get(b["game"], {}).get("en") or b["game"],
                                                                 "sections": set()})
            u["sections"].add(f"{b['rel']}: {b.get('section')}")
    wiki_links = kept_wiki
    # aggregate per (game qid, work)
    agg: dict[tuple[str, str], dict] = {}
    for b in links + wiki_links:
        if b["work"] in drop:
            continue
        node, nk = b["node"], b["nodeKind"]
        ni = ninfo.get(node, {})
        shared = _shared(nk, ni)
        if shared and node in skipped_big:
            continue
        a = agg.setdefault((b["game"], b["work"]), {"via": [], "found_in": set(), "relations": set(),
                                                    "sections": set()})
        a["found_in"].add(b["rel"] if b["rel"].startswith("wikipedia") else "wikidata")
        if b["rel"].startswith("wikipedia"):
            a["sections"].add(f"{b['rel']}: {b.get('section')}")
            rel = "wikipedia_section"
        elif b["rel"] == "based_on":
            rel = {"game": "based_on_game", "series": "based_on_series", "series2": "based_on_series",
                   "sibling_game": "based_on_series_game",
                   "franchise": "based_on_franchise", "universe": "based_on_universe",
                   "source": "same_source"}[nk]
            if shared and nk != "source":
                rel = "based_on_shared_franchise"
        else:
            rel = ("shared_franchise" if shared else "game_franchise") if nk in ("franchise", "universe", "source") \
                else f"{b['rel']}:{nk}"
        a["relations"].add(rel)
        v = {"qid": node, "label": ni.get("en"), "kind": nk}
        if v not in a["via"]:
            a["via"].append(v)
    strong = {"based_on_game", "based_on_series", "based_on_series_game", "based_on_franchise", "game_franchise",
              "based_on_universe", "part_of_series:series", "part_of_series:game", "franchise:series",
              "franchise:game", "universe:series", "universe:game", "part_of_series:sibling_game",
              "part_of_series:series2", "franchise:series2", "universe:series2"}
    game_group: dict[str, dict] = {}
    for g in qid_games:
        mine = [b for b in links if b["game"] == g]
        cand = sorted({b["node"] for b in mine if b["nodeKind"] == "series"},
                      key=lambda n: (ninfo.get(n, {}).get("en") or n))
        cand = cand or [x["s"] for x in ser if x["g"] == g]
        cand = cand or sorted({b["node"] for b in mine if b["nodeKind"] == "franchise"
                               and ninfo.get(b["node"], {}).get("game_origin")})
        node = cand[0] if cand else g
        game_group[g] = {"qid": node, "label": ninfo.get(node, {}).get("en") or node}
    written = 0
    no_imdb = []
    rows = []
    for (gq, w), a in agg.items():
        d = det.get(w)
        if not d:
            continue
        imdb, imdb_how = _resolve_imdb(conn, d)
        if not imdb:
            no_imdb.append(w)
        conf = "confirmed" if a["relations"] & strong else "review"
        group = game_group[gq]
        for v in a["via"]:
            o = ninfo.get(v["qid"], {})
            if o.get("origin_how") and v["kind"] in ("franchise", "universe"):
                v["origin"] = o["origin_how"]
        src = {"relation": sorted(a["relations"]), "confidence": conf, "via": a["via"],
               "found_in": sorted(a["found_in"]), "wikipedia_sections": sorted(a["sections"]) or None,
               "work_qid": w, "game_qid": gq, "title_ru": d["ru"], "group": group.get("label") or group["qid"],
               "group_qid": group["qid"], "imdb_how": imdb_how, "types": sorted(d["types"].values())}
        year = min(d["years"]) if d["years"] else None
        for store, gid in qid_games[gq]:
            rows.append((store, gid, imdb or f"wd:{w}", d["en"] or d["ru"] or w, year, work_kind(d),
                         json.dumps(src, ensure_ascii=False)))
    current = {(r[0], r[1], r[2]) for r in rows}
    stale = 0
    with conn:
        for r in rows:
            conn.execute(
                "INSERT INTO game_adaptations (store, game_id, imdb_id, title, year, kind, sources)"
                " VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(store, game_id, imdb_id) DO UPDATE SET"
                " title = excluded.title, year = excluded.year, kind = excluded.kind, sources = excluded.sources", r)
            written += 1
        # rows not confirmed by this run are kept (nothing is deleted) but flagged; the report skips them
        for old in conn.execute("SELECT store, game_id, imdb_id, sources FROM game_adaptations").fetchall():
            if (old[0], old[1], old[2]) in current:
                continue
            src = json.loads(old[3] or "{}")
            if not src.get("stale"):
                src["stale"] = time.strftime("%Y-%m-%d")
                conn.execute("UPDATE game_adaptations SET sources = ? WHERE store = ? AND game_id = ? AND imdb_id = ?",
                             (json.dumps(src, ensure_ascii=False), old[0], old[1], old[2]))
            stale += 1
    summary = {"games": len(res),
               "matched": sum(1 for r in res.values() if r.get("qid") or r.get("status") == "matched_multi"),
               "ambiguous_same_series_used": sum(1 for r in res.values()
                                                 if r.get("status") == "ambiguous" and r.get("same_series")),
               "ambiguous": sum(1 for r in res.values() if r.get("status") == "ambiguous"),
               "unmatched": sum(1 for r in res.values() if r.get("status") == "none"),
               "skipped_non_games": sum(1 for r in res.values() if r.get("status") == "skipped"),
               "links": written, "stale_rows": stale,
               "wikipedia_unconfirmed": sorted(
                   ({**u, "sections": sorted(u["sections"])} for u in unconfirmed.values()),
                   key=lambda u: (u["game"], u["work"])),
               "skipped_adult_parodies": sorted(f"{t} ({w})" for w, t in adult.items()),
               "wikipedia": "checked" if use_wikipedia else "skipped (--no-wikipedia)",
               "franchise_origin": {f"{ninfo.get(n, {}).get('en') or n} ({n})": ninfo.get(n, {}).get("first_members")
                                    for n in fr_nodes}, "works": len({r[2] for r in rows}), "no_imdb": len(set(no_imdb)),
               "wikipedia_pages_checked": wp_checked,
               "wikipedia_notes": notes_wp,
               "skipped_big_franchises": {f"{ninfo.get(n, {}).get('en') or n} ({n})": c
                                          for n, c in skipped_big.items()}}
    (CACHE / "adaptations_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1),
                                                    encoding="utf-8")
    return summary


# ----------------------------------------------------------------------------------------------
# report
# ----------------------------------------------------------------------------------------------

def report(conn, as_json: bool = False) -> dict:
    out: dict = {}
    out["games_per_store"] = {r[0]: r[1] for r in conn.execute("SELECT store, COUNT(*) FROM games GROUP BY store")}
    try:
        out["collect_notes"] = json.loads(NOTES_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        out["collect_notes"] = {}
    try:
        out["adaptations_summary"] = json.loads((CACHE / "adaptations_summary.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        out["adaptations_summary"] = {}
    wd_status = defaultdict(int)
    unresolved = []
    for r in conn.execute("SELECT store, game_id, title, raw FROM games ORDER BY store, title"):
        w = (json.loads(r["raw"] or "{}")).get("wikidata") or {}
        st = w.get("status", "not resolved yet")
        wd_status[f"{r['store']}:{st}"] += 1
        if st in ("none", "ambiguous"):
            unresolved.append({"store": r["store"], "title": r["title"], "status": st, "how": w.get("how"),
                               "candidates": w.get("candidates"), "same_series": w.get("same_series")})
    out["wikidata_match"] = dict(wd_status)
    out["unresolved_games"] = unresolved
    rows = conn.execute("""
        SELECT a.*, g.title AS game_title FROM game_adaptations a
        JOIN games g ON g.store = a.store AND g.game_id = a.game_id ORDER BY a.year""").fetchall()
    rows = [r for r in rows if not json.loads(r["sources"] or "{}").get("stale")]
    out["games_with_adaptations"] = len({(r["store"], r["game_id"]) for r in rows})
    # presence in titles / watched
    def in_db(imdb):
        if not imdb.startswith("tt"):
            return [], False
        t = conn.execute("SELECT id FROM titles WHERE imdb_id = ? OR series_imdb_id = ?", (imdb, imdb)).fetchall()
        ids = [x[0] for x in t]
        watched = bool(ids) and conn.execute(
            f"SELECT 1 FROM watched WHERE title_id IN ({','.join('?' * len(ids))}) LIMIT 1", ids).fetchone() is not None
        return ids, watched
    groups: dict[str, dict] = {}
    for r in rows:
        s = json.loads(r["sources"] or "{}")
        g = groups.setdefault(s.get("group") or "?", {"games": set(), "works": {}})
        g["games"].add(f"{r['game_title']} [{r['store']}]")
        w = g["works"].get(r["imdb_id"])
        if not w:
            ids, watched = in_db(r["imdb_id"])
            w = g["works"][r["imdb_id"]] = {
                "imdb_id": r["imdb_id"], "title": r["title"], "title_ru": s.get("title_ru"), "year": r["year"],
                "kind": r["kind"], "confidence": s.get("confidence"), "relations": set(), "found_in": set(),
                "in_titles": len(ids), "watched": watched, "via": set()}
        w["relations"] |= set(s.get("relation") or [])
        w["found_in"] |= set(s.get("found_in") or [])
        w["via"] |= {f"{v.get('label') or v['qid']} ({v['kind']})" for v in s.get("via") or []}
        if s.get("confidence") == "confirmed":
            w["confidence"] = "confirmed"
    out["groups"] = {}
    for name in sorted(groups, key=lambda k: k.lower()):
        g = groups[name]
        out["groups"][name] = {"games": sorted(g["games"]),
                               "works": [{**w, "relations": sorted(w["relations"]), "found_in": sorted(w["found_in"]),
                                          "via": sorted(w["via"])}
                                         for w in sorted(g["works"].values(), key=lambda x: (x["year"] or 9999))]}
    if as_json:
        print(json.dumps(out, ensure_ascii=False, indent=1, default=list))
        return out
    print("== Games per store:", out["games_per_store"])
    for store, ns in out["collect_notes"].items():
        if isinstance(ns, list):
            for n in ns:
                print(f"   [{store}] {n}")
    print("== Wikidata match:", out["wikidata_match"])
    print("== Adaptation run:", out["adaptations_summary"])
    print(f"== Games with adaptations: {out['games_with_adaptations']}")
    for name, g in out["groups"].items():
        print(f"\n## {name}   <- {'; '.join(g['games'])}")
        for w in g["works"]:
            flag = "WATCHED" if w["watched"] else ("in titles" if w["in_titles"] else "")
            conf = "" if w["confidence"] == "confirmed" else "  [REVIEW]"
            ru = f" / {w['title_ru']}" if w["title_ru"] and w["title_ru"] != w["title"] else ""
            print(f"  {w['year'] or '----'}  {w['title']}{ru}  ({w['kind']})  {w['imdb_id']}  {flag}{conf}"
                  f"  rel={','.join(w['relations'])}")
    unc = (out["adaptations_summary"] or {}).get("wikipedia_unconfirmed") or []
    if unc:
        by_work: dict[str, dict] = {}
        for u in unc:
            w = by_work.setdefault(u["qid"], {"work": u["work"], "ids": ",".join(u["imdb"]) or u["qid"],
                                              "games": set(), "sections": set()})
            w["games"].add(u["game"])
            w["sections"] |= {x.split(": ", 1)[-1].split(" -> ")[0] for x in u["sections"]}
        print(f"\n== Wikipedia links NOT stored (title does not name the game/series): {len(by_work)} works; "
              f"check by hand:")
        for w in sorted(by_work.values(), key=lambda x: sorted(x["games"])[0]):
            print(f"  {w['work']} {w['ids']}  <- {'; '.join(sorted(w['games']))}  [{'; '.join(sorted(w['sections']))}]")
    if out["unresolved_games"]:
        print(f"\n== Games without a single Wikidata match ({len(out['unresolved_games'])}):")
        for u in out["unresolved_games"]:
            c = ", ".join(f"{x.get('label') or ''} {x['qid']}" for x in (u.get("candidates") or [])[:5])
            used = "  [all candidates in one game series -> used]" if u.get("same_series") else ""
            print(f"  [{u['store']}] {u['title']} -> {u['status']} ({u['how']}) {c}{used}")
    return out


# ----------------------------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m films.sources.games")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect")
    c.add_argument("--store", choices=["playstation", "steam"])
    c.add_argument("--max-age", type=float, default=DEFAULT_MAX_AGE)
    i = sub.add_parser("import")
    i.add_argument("store", choices=["playstation", "steam"])
    i.add_argument("file", type=Path)
    a = sub.add_parser("adaptations")
    a.add_argument("--max-age", type=float, default=DEFAULT_MAX_AGE)
    a.add_argument("--no-wikipedia", action="store_true")
    r = sub.add_parser("report")
    r.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    conn = db.connect()
    if args.cmd == "collect":
        notes: dict = {}
        try:
            notes = json.loads(NOTES_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        for store, fn in (("playstation", collect_playstation), ("steam", collect_steam)):
            if args.store in (None, store):
                notes[store] = []
                fn(conn, notes, args.max_age)
        NOTES_FILE.parent.mkdir(parents=True, exist_ok=True)
        NOTES_FILE.write_text(json.dumps(notes, ensure_ascii=False, indent=1), encoding="utf-8")
    elif args.cmd == "import":
        print(f"imported {import_list(conn, args.store, args.file)} rows into games ({args.store})")
    elif args.cmd == "adaptations":
        print(json.dumps(find_adaptations(conn, args.max_age, not args.no_wikipedia), ensure_ascii=False, indent=1))
    elif args.cmd == "report":
        report(conn, args.json)
    print(f"http: {dict(STATS)}", file=sys.stderr)


if __name__ == "__main__":
    main()
