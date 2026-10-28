"""rustorka.com (R208, source priority 3 after rutor and rutracker; toloka 4, mazepa 5).

Owner, 2026-10-26: «https://rustorka.com/forum/index.php ещё один источник, он должен идти по приоритету после
рутрекера»; «для всех источников достаточно смотреть обновления только с последней даты которая у нас будет об успешной
синхронизации с данными по последнему фильму который там будет по дату».

What a guest can read (checked 2026-10-26, plain HTTP — no browser, no login, no captcha): the film / series / cartoon
forums and their topics are hidden from guests (viewforum / viewtopic answer «… не существует», HTTP 404; index.php lists
only games, software, music, anime, books; tracker.php search asks for a captcha — never solved, never bypassed). The
site's RSS feed for guests, ``rss.php?t=1``, does carry them: the 100 newest topics of all forums (≈ one week), each with
the forum name, the first post's id and date and the whole first post (description + MediaInfo). So this module is an
incremental feed collector (films.parse.rustorka has the details):

  forums — read index.php: forums a guest sees, video forums among them (none today — if some appear, they can be
           walked like mazepa's), changes of the anime forum list the classifier uses; one feed request checks the feed;
  crawl  — read the feed (one request; the feed forgets a topic after a week, so the wanted topics' first posts are kept
           in release_raw and every item's forum / title / verdict in data/cache/rustorka/items.json), classify every item (foreign film / series / cartoon of any country; anime, Russian films and
           series, software … are skipped and counted), and store each wanted topic as a release (source 'rustorka',
           source_id 'p<first post id>', url viewtopic.php?p=…): the first post → release_raw (compact document), the
           rutor page parser's output → the same quality / audio columns as rutor / rutracker, the owner's release rules
           (TS/CAM, 3D, КПК → excluded_releases); DV-Profile-5-only releases are never inserted (excluded_releases).
           Incremental (owner's rule): only items published since the last SUCCESSFUL sync point — the newest video
           topic's date of the last run that finished without a fatal error — minus 2 days of overlap; `--full` takes the
           whole feed. A feed whose oldest item is newer than that point means topics were missed (the feed holds only 100)
           — reported as a gap; run at least every few days;
  parse  — re-parse the stored pages (after a parser change; --reparse = all);
  status — counts and the sync point.

History (R232, owner 2026-10-26 05:30: «везде в хроме вошёл! во всех источниках»): a logged-in Chrome tab reads the
hidden video forums with films/browser/rustorka_collect.js (ONE javascript_tool call with data/cache/rustorka/run.js):

  queries — write the job: queries_run.json (served by the bridge at GET /queries/rustorka) + run.js; per forum the
            sync point (newest last-post date of the last walk that reached its point or its last page) minus 2 days,
            the first-run horizon for forums without one (1 Nov of the year before year_from), the known topics (title
            of the stored page), older-year topics of unwatched DB titles to read; `--full` ignores the points;
  resume  — the tail of the last run (forums not finished continue after their last posted page, extra topics not
            read) as a new run «<base>-rN»;
  ingest  — the bridge files data/cache/ingest/rustorka-*.txt → rows cache (data/cache/rustorka/rows.json), the forum
            tree, the wanted topics as releases (same source_id 'p<first post id>' as the feed, so the feed's row of a
            topic gets the topic page — url viewtopic.php?t=, exact size, btih), the per-forum sync points; `--match`.
  Search (tracker.php) answers a captcha and is not used; the forum walk covers the same.

Polite: one request at a time, ≥ 1.5 s between requests of all processes (films.locks.pace('rustorka_http', 1.5)) plus
jitter, backoff 5 / 15 / 45 s, stop on a captcha or a login page. Nothing is deleted. Then: python -X utf8 -m films match.

Run:  python -X utf8 -m films.sources.rustorka forums
      python -X utf8 -m films.sources.rustorka crawl [--full] [--limit N] [--max-minutes M] [--match]
      python -X utf8 -m films.sources.rustorka queries [--full] [--delay-ms N] [--max-pages N]
      python -X utf8 -m films.sources.rustorka resume [--delay-ms N]
      python -X utf8 -m films.sources.rustorka ingest [file|dir|glob …] [--all] [--match]
      python -X utf8 -m films.sources.rustorka parse [--reparse]
      python -X utf8 -m films.sources.rustorka status
"""
from __future__ import annotations

import glob
import json
import random
import re
import sqlite3
import sys
import time
import zlib
from datetime import datetime, timedelta
from pathlib import Path

from .. import db
from ..parse import rustorka as rk

SOURCE = "rustorka"
CACHE = db.ROOT / "data" / "cache" / "rustorka"
STATE = CACHE / "state.json"
ITEMS = CACHE / "items.json"          # every feed item seen: post id -> forum, title, date, category / why skipped
ROWS = CACHE / "rows.json"            # history: topic id -> forum, title, last post, seeds, size, page_title, verdict, sid
DONE_LIST = CACHE / "ingested.json"   # bridge files already ingested (ingest is idempotent anyway)
INGEST_DIR = db.ROOT / "data" / "cache" / "ingest"
JS = db.ROOT / "films" / "browser" / "rustorka_collect.js"
BRIDGE_QUERIES = "http://127.0.0.1:8765/queries/rustorka"
LOG = db.ROOT / "data" / "logs" / "rustorka_crawl.log"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
PACE_S = 1.5
OVERLAP = timedelta(days=2)


class Blocked(Exception):
    """A captcha, a login page or a «registered users only» answer: stop, never try to get around it."""


class TooManyErrors(Exception):
    pass


class Http:
    """One request at a time with a shared minimum interval (films.locks.pace) and backoff; windows-1251 pages."""

    def __init__(self, pace_s: float = PACE_S, log=print):
        import requests
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept-Language": "ru,en;q=0.8"})
        self.pace_s, self.log = pace_s, log
        self.requests = self.errors = self.fail_streak = 0

    def get(self, path: str, allow_404: bool = False) -> str | None:
        from .. import locks
        url = path if path.startswith("http") else f"{rk.BASE}/{path.lstrip('/')}"
        err = None
        for a in range(4):
            locks.pace("rustorka_http", self.pace_s + random.random() * 0.5)
            self.requests += 1
            try:
                r = self.s.get(url, timeout=90)
                r.encoding = "windows-1251"
                head = r.text[:3000]
                if "login.php" in r.url or r.status_code == 403 and "aptcha" in head or "captcha" in r.url.lower():
                    raise Blocked(f"{url}: HTTP {r.status_code} {r.url} (captcha / login)")
                if r.status_code == 401 or "For registred users only" in head[:200]:
                    raise Blocked(f"{url}: registered users only")
                if r.status_code == 404 and allow_404:
                    self.fail_streak = 0
                    return None
                if r.ok:
                    self.fail_streak = 0
                    return r.text
                err = f"HTTP {r.status_code}"
            except Blocked:
                raise
            except Exception as e:  # noqa: BLE001 — network trouble: backoff and retry
                err = repr(e)
            self.errors += 1
            self.log(f"  ! {url}: {err} (try {a + 1})")
            time.sleep(5 * 3 ** a)
        self.fail_streak += 1
        if self.fail_streak >= 3:
            raise TooManyErrors(f"3 failed requests in a row, last {url}: {err}")
        return None


def _load(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _save(p: Path, v) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(v, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


# --------------------------------------------------------------------------------------------
# DB
# --------------------------------------------------------------------------------------------

def _retry(fn, *a, tries: int = 10):
    """Run a short write transaction, retrying while another collector holds the lock."""
    for i in range(tries):
        try:
            return fn(*a)
        except sqlite3.OperationalError as e:
            if "locked" not in str(e) and "busy" not in str(e) or i == tries - 1:
                raise
            time.sleep(1.5 * (i + 1))


def ensure_source(conn) -> None:
    """releases.source accepts 'rustorka' (the CHECK is widened in place by films.db, no table rebuild)."""
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='releases'").fetchone()[0]
    if "'rustorka'" not in sql:
        _retry(lambda: db._widen_release_sources(conn, (SOURCE,)))


def _js(v):
    return json.dumps(v, ensure_ascii=False) if v is not None else None


def parser_version() -> str:
    from ..parse import rutor as base
    from ..parse import rutracker as rt
    v = max(Path(base.__file__).stat().st_mtime_ns, Path(rt.__file__).stat().st_mtime_ns, Path(rk.__file__).stat().st_mtime_ns)
    return f"rustorka-page:{v}"


def store_parsed(conn, rid: int, d: dict, version: str) -> str | None:
    """The columns films.sources.rutor.parse_pages fills + the owner's release rules (caller holds the transaction).
    Returns the release-rule exclusion reason (cam / is_3d / kpk) or None."""
    from .rutor import h264_equiv, settings, store_rules
    factors = settings()["codec_efficiency"]
    np = d.get("name_parsed") or {}
    conn.execute(
        """UPDATE releases SET season=?, episodes=?, resolution=?, source_type=?, source_detail=?,
               video_codec=?, video_bit_depth=?, video_kbps=?, h264_equiv_kbps=?, hdr=?, dv_profile=?,
               dv_profile_full=?, dv_fallback=?, dv_static_md=?, is_imax_cut=?, edition=?, is_upscale=?, is_ai_upscale=?,
               audio=?, best_voice=?, forced_subs=?, parsed=?, parsed_version=?, details_parsed_at=datetime('now')
           WHERE id=?""",
        (d.get("season"), d.get("episodes"), d.get("resolution"), d.get("source_type"), np.get("source_raw"),
         d.get("video_codec"), d.get("video_bit_depth"), d.get("video_kbps"),
         h264_equiv(d.get("video_codec"), d.get("video_bit_depth"), d.get("video_height") or d.get("resolution"),
                    d.get("video_kbps"), factors),
         _js(d.get("hdr")), d.get("dv_profile"), d.get("dv_profile_full"), d.get("dv_fallback"), d.get("dv_static_md"),
         d.get("is_imax_cut"), _js(d.get("edition")), d.get("is_upscale"), 1 if d.get("is_upscale") else None,
         _js(d.get("audio")), d.get("best_voice"), _js(d.get("forced_subs")), _js(d), version, rid))
    sid, name = conn.execute("SELECT source_id, name FROM releases WHERE id = ?", (rid,)).fetchone()
    return store_rules(conn, rid, SOURCE, sid, name, d)


def raw_page(conn, release_id: int) -> str | None:
    row = conn.execute("SELECT body FROM release_raw WHERE release_id=?", (release_id,)).fetchone()
    return zlib.decompress(row[0]).decode("utf-8") if row else None


def excluded(conn) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT source_id FROM excluded_releases WHERE source = ? AND reason NOT LIKE 'lifted:%'", (SOURCE,))}


def new_stats() -> dict:
    return {"new": 0, "updated": 0, "unchanged": 0, "dv5_excluded": 0, "dv5_already_stored": [], "excluded_skipped": 0,
            "rule_excluded": {}, "parse_failed": 0, "ru_dub": 0, "uk_dub": 0, "uk_voice": 0}


def item_source_id(item: dict) -> str:
    """'p<first post id>' — the same for a feed item and for the topic page (so the two collectors never make two rows of
    one topic); 't<topic id>' only for a topic page whose first post id was not found."""
    return rk.source_id(item["post_id"]) if item.get("post_id") else f"t{int(item['topic_id'])}"


def store_item(conn, item: dict, st: dict, excl: set[str], version: str) -> int | None:
    """One wanted feed item or topic page (item['category'] set) → releases + release_raw + parse. A topic page replaces
    the feed's document of the same topic (it has the size, btih and the forum path); a feed item never replaces a topic
    page's document. Returns the release id."""
    sid = item_source_id(item)
    if sid in excl:
        st["excluded_skipped"] += 1
        return None
    raw = rk.compact_item_html(item)
    try:
        d = rk.parse_item(raw)
    except Exception as e:  # noqa: BLE001 — reported, the item stays in items.json and the raw feed
        st["parse_failed"] += 1
        print(f"  PARSE FAILED rustorka {sid}: {e!r}", file=sys.stderr, flush=True)
        return None
    name = item["title"]
    if d.get("dv5_only"):
        def _ex():
            with conn:
                conn.execute("INSERT OR IGNORE INTO excluded_releases (source, source_id, reason, name) "
                             "VALUES (?, ?, 'dv5_only', ?)", (SOURCE, sid, name))
                return conn.execute("SELECT id FROM releases WHERE source=? AND source_id=?", (SOURCE, sid)).fetchone()
        old = _retry(_ex)
        excl.add(sid)
        st["dv5_excluded"] += 1
        if old:   # stored earlier: never deleted, reported for the owner
            st["dv5_already_stored"].append(old[0])
        return None

    def _write():
        with conn:
            cur = conn.execute(
                "SELECT r.id, r.name, w.body FROM releases r LEFT JOIN release_raw w ON w.release_id = r.id "
                "WHERE r.source=? AND r.source_id=?", (SOURCE, sid)).fetchone()
            old = zlib.decompress(cur[2]).decode("utf-8") if cur and cur[2] is not None else None
            if cur and old is not None and (cur[1] == name and old == raw or
                                            not item.get("topic_id") and rk.split_item(old).get("topic_id")):
                conn.execute("UPDATE releases SET last_seen_at=datetime('now') WHERE id=?", (cur[0],))
                return cur[0], "unchanged", None
            url = rk.topic_url(item["topic_id"]) if item.get("topic_id") else rk.post_url(item["post_id"])
            if cur:
                rid, how = cur[0], "updated"
                conn.execute("""UPDATE releases SET name=?, category=?, url=?, size_bytes=coalesce(?, size_bytes),
                                    seeders=coalesce(?, seeders), leechers=coalesce(?, leechers), btih=coalesce(?, btih),
                                    added_at=coalesce(added_at, ?), last_seen_at=datetime('now') WHERE id=?""",
                             (name, item["category"], url, d.get("size_bytes"), d.get("seeders"), d.get("leechers"),
                              d.get("btih"), d.get("added_at"), rid))
            else:
                rid, how = conn.execute(
                    """INSERT INTO releases (source, source_id, url, btih, name, category, added_at, size_bytes, seeders,
                                             leechers) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (SOURCE, sid, url, d.get("btih"), name, item["category"], d.get("added_at"), d.get("size_bytes"),
                     d.get("seeders"), d.get("leechers"))).lastrowid, "new"
            conn.execute("INSERT OR REPLACE INTO release_raw (release_id, body) VALUES (?, ?)",
                         (rid, zlib.compress(raw.encode("utf-8"), 9)))
            return rid, how, store_parsed(conn, rid, d, version)
    rid, how, rule = _retry(_write)
    st[how] += 1
    if rule:
        st["rule_excluded"][rule] = st["rule_excluded"].get(rule, 0) + 1
    vbl = d.get("voice_by_lang") or {}
    st["ru_dub"] += "dub" in (vbl.get("ru") or [])
    st["uk_dub"] += "dub" in (vbl.get("uk") or [])
    st["uk_voice"] += bool(vbl.get("uk"))
    return rid


# --------------------------------------------------------------------------------------------
# forums / crawl
# --------------------------------------------------------------------------------------------

def forums(http: Http) -> dict:
    page = http.get("index.php") or ""
    fs = rk.parse_index(page)
    anime = {f["name"] for f in fs if f.get("category") and "аниме" in f["category"].casefold()}
    out = {"forums_visible": len(fs), "video_forums_visible": rk.video_forums_visible(fs),
           "anime_forums_new": sorted(anime - rk.ANIME_FORUMS - rk.DORAMA_FORUMS),
           "anime_forums_gone": sorted(rk.ANIME_FORUMS - anime - {"Аниме"}) if anime else None}
    try:
        feed = rk.parse_feed(http.get(rk.FEED) or "")
        out["feed"] = {"guest": feed["guest"], "items": len(feed["items"]), "built": feed["built"]}
    except Blocked as e:
        out["feed"] = {"blocked": str(e)}
    return out


def _sync_point(state: dict) -> datetime | None:
    p = (state.get("last_ok") or {}).get("newest_video_pub")
    return datetime.fromisoformat(p) if p else None


def crawl(conn, full: bool = False, limit: int | None = None, max_minutes: float | None = None, log=print,
          feed_xml: str | None = None) -> dict:
    """One feed read → releases. feed_xml: a saved feed instead of the network (tests)."""
    t0 = time.time()
    deadline = t0 + max_minutes * 60 if max_minutes else None
    ensure_source(conn)
    state, seen = _load(STATE, {}), _load(ITEMS, {})
    http = None
    st: dict = {"fatal": None, "requests": 0, "http_errors": 0}
    now = datetime.now()
    try:
        if feed_xml is None:
            http = Http(log=log)
            feed_xml = http.get(rk.FEED)
            if not feed_xml:
                raise TooManyErrors("feed not read")
        feed = rk.parse_feed(feed_xml)
        if feed["registered_only"] or not feed["items"]:
            raise Blocked("feed has no items (registered users only?)")
    except (Blocked, TooManyErrors) as e:
        st["fatal"] = repr(e)
        log(f"!! crawl stopped: {e!r}")
        feed = {"items": [], "built": None, "guest": None}
    items = feed["items"]
    point = None if full else _sync_point(state)
    since = point - OVERLAP if point else None
    pubs = [it["pub"] for it in items if it.get("pub")]
    st.update({"feed_items": len(items), "feed_built": feed.get("built"), "feed_oldest": min(pubs) if pubs else None,
               "feed_newest": max(pubs) if pubs else None, "sync_point": point.isoformat() if point else None,
               "since": since.isoformat() if since else None, "full": full})
    # owner's rule: only what is newer than the last successful sync point; the feed keeps 100 topics — a gap if its
    # oldest item is already newer than that point
    if point and pubs and datetime.fromisoformat(min(pubs)) > point:
        gap = {"from": point.isoformat(), "to": min(pubs), "found_at": now.isoformat(timespec="seconds")}
        st["gap"] = gap
        state.setdefault("gaps", []).append(gap)
        log(f"!! gap: topics published between {gap['from']} and {gap['to']} may be missed (the guest feed holds 100)")
    why: dict[str, int] = {}
    wanted = []
    for it in items:
        cat, reason = rk.classify(it)
        why[reason] = why.get(reason, 0) + 1
        seen[str(it["post_id"])] = {"forum": it["forum"], "title": it["title"], "pub": it["pub"], "category": cat,
                                    "why": reason, "seen": now.strftime("%Y-%m-%d")}
        if cat and (since is None or not it.get("pub") or datetime.fromisoformat(it["pub"]) >= since):
            wanted.append(dict(it, category=cat, fetched_at=now.isoformat(timespec="seconds")))
    st["classified"] = why
    st["wanted"] = len(wanted)
    st["older_than_sync_point"] = sum(1 for it in items if rk.classify(it)[0]) - len(wanted)
    sp = new_stats()
    version, excl = parser_version(), excluded(conn)
    wanted.sort(key=lambda it: it["post_id"])           # oldest first: a stop leaves the sync point honest
    done_pubs = []
    for i, it in enumerate(wanted[:limit] if limit else wanted, 1):
        if deadline and time.time() > deadline:
            st["stopped"] = "deadline"
            break
        store_item(conn, it, sp, excl, version)
        if it.get("pub"):
            done_pubs.append(it["pub"])
    st["stored"] = sp
    st["left"] = len(wanted) - len(done_pubs) if (limit or st.get("stopped")) else 0
    if http:
        st["requests"], st["http_errors"] = http.requests, http.errors
    st["minutes"] = round((time.time() - t0) / 60, 1)
    _save(ITEMS, seen)
    if not st["fatal"] and sp["parse_failed"] == 0:
        video_pubs = [it["pub"] for it in items if it.get("pub") and seen[str(it["post_id"])]["category"]]
        # the sync point only moves to what was really stored: with --limit / a deadline, to the newest stored item
        newest = max(done_pubs) if st["left"] else max(video_pubs or pubs or [""]) or None
        if newest and (not point or datetime.fromisoformat(newest) > point or full):
            state["last_ok"] = {"at": now.isoformat(timespec="seconds"), "newest_video_pub": newest,
                                "newest_post": max([it["post_id"] for it in items if it.get("pub") == newest] or [0]),
                                "feed_oldest": st["feed_oldest"], "feed_items": len(items)}
        if state.get("last_ok"):
            state["last_ok"]["checked_at"] = now.isoformat(timespec="seconds")   # last successful run, point or not moved
        db.mark_sync(conn, SOURCE, (state.get("last_ok") or {}).get("newest_video_pub"),
                     {k: v for k, v in st.items() if isinstance(v, (int, float, str)) and v is not None})
    runs = state.setdefault("runs", [])
    runs.append({"at": now.isoformat(timespec="seconds"), "ok": not st["fatal"], "feed_items": len(items),
                 "wanted": len(wanted), "new": sp["new"], "updated": sp["updated"], "fatal": st["fatal"]})
    del runs[:-30]
    _save(STATE, state)
    return st


# --------------------------------------------------------------------------------------------
# history: the logged-in browser collector (films/browser/rustorka_collect.js, R232)
# --------------------------------------------------------------------------------------------

def _norm(s: str | None) -> str:
    s = (s or "").casefold().replace("ё", "е").replace("’", "'")
    return re.sub(r"\s+", " ", re.sub(r"[^\w]+", " ", s)).strip()


def _title_years(title: str) -> tuple[int, int] | None:
    m = re.search(r"\[\s*((?:19|20)\d{2})(?:\s*[-–—]\s*((?:19|20)?\d{2}))?", title or "") or \
        re.search(r"\(((?:19|20)\d{2})(?:\s*[-–—]\s*((?:19|20)?\d{2}))?\)", title or "")
    if not m:
        return None
    a = int(m.group(1))
    b = int(m.group(2) if not m.group(2) or len(m.group(2)) == 4 else m.group(1)[:2] + m.group(2)) if m.group(2) else a
    return min(a, b), max(a, b)


def old_title_index(conn, year_from: int) -> dict[str, set[int]]:
    """Names of DB titles older than year_from that nobody has watched and that have tracker releases (franchises,
    prequels, everything encountered — R162 / R180): normalized name -> years. An older rustorka topic is opened only for
    these (as mazepa does)."""
    watched = {r[0] for r in conn.execute("SELECT title_id FROM watched WHERE source NOT LIKE '%partial'")}
    idx: dict[str, set[int]] = {}
    for tid, y, *names in conn.execute(
            """SELECT t.id, t.year, t.title_original, t.title_latin, t.title_uk, t.title_ru FROM titles t
               WHERE t.year < ? AND EXISTS (SELECT 1 FROM release_titles rt WHERE rt.title_id = t.id)
                 AND """ + db.OLD_TITLE_WANTED_SQL, (year_from,)):
        if tid in watched or not y:
            continue
        for n in names:
            k = _norm(n)
            if len(k) >= 3:
                idx.setdefault(k, set()).add(int(y))
    return idx


def old_title_topics(rows: dict, year_from: int, idx: dict[str, set[int]]) -> list[str]:
    """Listing rows of an older year whose name + year is an unwatched DB title, and whose page is not read yet."""
    out = []
    for t, r in rows.items():
        if r.get("page_title") == r.get("title"):
            continue
        yrs = _title_years(r.get("title") or "")
        if not yrs or yrs[1] >= year_from:
            continue
        head = re.sub(r"\([^()]*\)", " ", re.split(r"[\[(]\s*(?:19|20)\d{2}", r["title"])[0])
        if any((ys := idx.get(_norm(n))) and any(yrs[0] - 1 <= y <= yrs[1] + 1 for y in ys) for n in head.split("/")):
            out.append(t)
    return sorted(out, key=int, reverse=True)


def build_job(conn, state: dict, rows: dict, full: bool = False, delay_ms: int = 1500, max_pages: int = 400,
              run_id: str | None = None) -> dict:
    """The collector's job (queries_run.json): per forum the sync point minus 2 days (owner's rule R212; a forum never
    walked to its point starts from the first-run horizon: 1 Nov of the year before year_from — last activity, so every
    topic of a 2024+ film is reached), the known topics (title of the stored page — a changed title, more episodes, is
    read again), and older-year topics of DB titles to read."""
    year_from = min(db.years())
    since = {}
    if not full:
        for f, p in (state.get("forums") or {}).items():
            if p.get("newest"):
                since[f] = (datetime.strptime(p["newest"], "%Y-%m-%d %H:%M") - OVERLAP).strftime("%Y-%m-%d %H:%M")
    known = {t: r["page_title"] for t, r in rows.items() if r.get("page_title")}
    extra = old_title_topics(rows, year_from, old_title_index(conn, year_from))
    return {"v": 1, "run": run_id or datetime.now().strftime("%Y%m%d-%H%M"), "yearFrom": year_from, "since": since,
            "defaultSince": f"{year_from - 1}-11-01 00:00", "known": known, "extra": extra, "delayMs": delay_ms,
            "maxPages": max_pages, "waitPollMs": 120000, "waitHours": 12}


def write_run(job: dict, out: Path | None = None) -> dict:
    """queries_run.json (served by the bridge at GET /queries/rustorka) and run.js (the collector + one call)."""
    out = out or CACHE
    out.mkdir(parents=True, exist_ok=True)
    (out / "queries_run.json").write_text(json.dumps(job, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    src = JS.read_text(encoding="utf-8")
    (out / "run.js").write_text(src + f"\n__rkCollect({{queriesUrl: {json.dumps(BRIDGE_QUERIES)}}});\n", encoding="utf-8")
    return {"run": job["run"], "forums_with_point": len(job["since"]), "default_since": job["defaultSince"],
            "known": len(job["known"]), "extra": len(job["extra"]), "run_js": str(out / "run.js")}


def load_payloads(paths: list[str]) -> list[dict]:
    """Bridge bodies (one JSON object each), or a read-out of __rkChunk (a JSON array of JSON strings / objects)."""
    out = []
    for p in paths:
        try:
            v = json.loads(Path(p).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for x in v if isinstance(v, list) else [v]:
            if isinstance(x, str):
                try:
                    x = json.loads(x)
                except ValueError:
                    continue
            if isinstance(x, dict) and x.get("kind") == SOURCE:
                out.append(x)
    return sorted(out, key=lambda p: (str(p.get("run")), p.get("final") is True, p.get("seq") or 0))


def _topic_item(tp: dict, row: dict, forum: dict | None) -> dict:
    from ..parse.rutracker import parse_reged
    f = forum or {}
    path = tp.get("forum_path") or " / ".join(x for x in (f.get("category"), f.get("parent"), f.get("name")) if x)
    return {"post_id": int(tp["post_id"]) if str(tp.get("post_id") or "").isdigit() else None,
            "topic_id": int(tp["topic_id"]), "title": tp.get("title") or row.get("title"),
            "forum": tp.get("forum") or f.get("name"), "forum_id": tp.get("forum_id") or f.get("f"), "forum_path": path,
            "added": parse_reged(tp.get("reged")), "pub": None, "post_html": tp.get("post_html") or "",
            "size_bytes": tp.get("size_bytes") or row.get("size"), "seeders": row.get("seeds"),
            "leechers": row.get("leech"), "btih": tp.get("btih"), "reply": False}


def ingest(conn, payloads: list[dict], state: dict, rows: dict) -> dict:
    """Collector payloads → rows cache, releases (wanted topics), the forum tree and the per-forum sync points."""
    ensure_source(conn)
    st = {"payloads": len(payloads), "pages": 0, "rows": 0, "topics": 0, "skipped": {}, "errors": 0, "error_samples": [],
          "forums_done": 0, "fatal": [], "finals": 0, "stored": new_stats(), "status_waits": 0}
    version, excl = parser_version(), excluded(conn)
    now = datetime.now().isoformat(timespec="seconds")
    for p in payloads:
        if p.get("final"):
            st["finals"] += 1
            if p.get("fatal"):
                st["fatal"].append(p["fatal"])
            continue
        if p.get("type") == "forums":
            state["forum_tree"] = p.get("forums") or []
            state["video_forums"] = p.get("video") or []
            state["forum_tree_at"] = p.get("at")
            continue
        st["pages"] += p.get("type") == "page"
        forum = p.get("forum") or {}
        by_t = {}
        for r in p.get("rows") or []:
            st["rows"] += 1
            by_t[str(r["t"])] = r
            old = rows.get(str(r["t"]), {})
            rows[str(r["t"])] = dict(old, f=forum.get("f", old.get("f")), title=r.get("title"), last=r.get("last"),
                                     seeds=r.get("seeds"), size=r.get("size"), seen=now[:10])
        for e in p.get("errors") or []:
            st["errors"] += 1
            if len(st["error_samples"]) < 10:
                st["error_samples"].append({k: (str(v)[:200] if k == "snippet" else v) for k, v in e.items()})
        for tp in p.get("topics") or []:
            st["topics"] += 1
            t = str(tp.get("topic_id"))
            row = by_t.get(t) or rows.get(t) or {}
            item = _topic_item(tp, row, forum or None)
            cat, why = rk.classify(item)
            rec = rows.setdefault(t, {"title": item["title"]})
            rec.update(page_title=row.get("title") or item["title"], verdict=why, page_at=now[:10])
            if not cat:
                st["skipped"][why] = st["skipped"].get(why, 0) + 1
                continue
            item["category"] = cat
            rid = store_item(conn, item, st["stored"], excl, version)
            if rid:
                rec["sid"] = item_source_id(item)
        done = p.get("forumDone")
        if done and done.get("stopped") in ("since", "last page"):
            st["forums_done"] += 1
            fs = state.setdefault("forums", {}).setdefault(str(done["f"]), {})
            if done.get("newest") and (not fs.get("newest") or done["newest"] > fs["newest"]):
                fs["newest"] = done["newest"]
            fs.update(name=forum.get("name"), stopped=done["stopped"], run=p.get("run"), at=now)
    st["fatal"] = sorted(set(st["fatal"]))
    return st


def resume_job(base_job: dict, payloads: list[dict], conn, state: dict, rows: dict, n: int) -> tuple[dict, dict]:
    """The tail of a run (stopped, tab closed, relogin): forums of the run that did not finish — each continues after
    its last posted listing page — and the extra topics not read yet; a new run «<base>-rN»."""
    base = str(base_job.get("base") or base_job.get("run"))
    mine = [p for p in payloads if str(p.get("run") or "") == base or str(p.get("run") or "").startswith(base + "-r")]
    video = next((p.get("video") for p in reversed(mine) if p.get("type") == "forums"), None)
    done, start_at, got = set(), {}, set()
    for p in mine:
        if p.get("type") == "page" and p.get("forum"):
            f = str(p["forum"]["f"])
            if p.get("forumDone"):
                done.add(f)
            start_at[f] = max(start_at.get(f, 0), int(p.get("next") or int(p.get("start") or 0) + 50))
        for tp in p.get("topics") or []:
            got.add(str(tp.get("topic_id")))
    job = build_job(conn, state, rows, delay_ms=base_job.get("delayMs", 1500), max_pages=base_job.get("maxPages", 400),
                    run_id=f"{base}-r{n}")
    job["base"] = base
    job["since"] = dict(base_job.get("since") or {}, **job["since"])
    job["defaultSince"] = base_job.get("defaultSince", job["defaultSince"])
    if video is not None:
        only = {str(f) for f in base_job.get("onlyForums") or []}
        job["onlyForums"] = [str(f) for f in video if str(f) not in done and (not only or str(f) in only)]
        job["startAt"] = {f: s for f, s in start_at.items() if f not in done}
    job["extra"] = [t for t in base_job.get("extra") or [] if t not in got]
    return job, {"base": base, "payloads": len(mine), "forums_done": len(done),
                 "forums_left": len(job.get("onlyForums") or []) if video is not None else "all (index not read)",
                 "extra_left": len(job["extra"])}


def _ingest_paths(args: list[str], all_: bool) -> list[str]:
    done = set(_load(DONE_LIST, []))
    paths = []
    for a in args or [str(INGEST_DIR / "rustorka-*.txt")]:
        p = Path(a)
        found = sorted(glob.glob(str(p / "rustorka-*.txt"))) if p.is_dir() else sorted(glob.glob(a)) or ([a] if p.exists() else [])
        paths += [x for x in found if not Path(x).name.startswith("rustorka_status")]
    if not args and not all_:
        paths = [p for p in paths if Path(p).name not in done]
    return list(dict.fromkeys(paths))


def reparse(conn, all_: bool = False) -> dict:
    """Re-run the parser over stored rustorka pages (after a parser change)."""
    v = parser_version()
    rows = conn.execute(
        f"""SELECT r.id, r.source_id, r.name FROM releases r JOIN release_raw w ON w.release_id = r.id
            WHERE r.source = ? {'' if all_ else "AND (r.parsed IS NULL OR r.parsed_version IS NOT ?)"}""",
        (SOURCE,) if all_ else (SOURCE, v)).fetchall()
    st = {"parsed": 0, "failed": 0, "dv5_found": []}
    for rid, sid, name in rows:
        try:
            d = rk.parse_item(raw_page(conn, rid))
        except Exception as e:  # noqa: BLE001 — recorded and reported
            st["failed"] += 1
            print(f"  PARSE FAILED {sid}: {e!r}", file=sys.stderr, flush=True)
            continue

        def _w():
            with conn:
                if d.get("dv5_only"):
                    conn.execute("INSERT OR IGNORE INTO excluded_releases (source, source_id, reason, name) "
                                 "VALUES (?, ?, 'dv5_only', ?)", (SOURCE, sid, name))
                store_parsed(conn, rid, d, v)
        _retry(_w)
        if d.get("dv5_only"):
            st["dv5_found"].append(rid)   # not deleted — reported
        st["parsed"] += 1
    return st


def status(conn) -> dict:
    q = lambda sql: conn.execute(sql, (SOURCE,)).fetchone()[0]  # noqa: E731
    state = _load(STATE, {})
    return {
        "releases": q("SELECT count(*) FROM releases WHERE source=?"),
        "with_page": q("SELECT count(*) FROM releases r JOIN release_raw w ON w.release_id=r.id WHERE r.source=?"),
        "linked": q("SELECT count(DISTINCT r.id) FROM releases r JOIN release_titles t ON t.release_id=r.id WHERE r.source=?"),
        "titles": q("SELECT count(DISTINCT t.title_id) FROM releases r JOIN release_titles t ON t.release_id=r.id WHERE r.source=?"),
        "ru_dub": q("""SELECT count(*) FROM releases WHERE source=? AND json_valid(parsed)
                       AND json_extract(parsed, '$.voice_by_lang.ru') LIKE '%"dub"%'"""),
        "uk_dub": q("""SELECT count(*) FROM releases WHERE source=? AND json_valid(parsed)
                       AND json_extract(parsed, '$.voice_by_lang.uk') LIKE '%"dub"%'"""),
        "excluded": q("SELECT count(*) FROM excluded_releases WHERE source=? AND reason NOT LIKE 'lifted:%'"),
        "items_seen": len(_load(ITEMS, {})),
        "sync_point": state.get("last_ok"),
        "gaps": state.get("gaps", [])[-5:],
        "last_run": (state.get("runs") or [None])[-1],
        "history": {"video_forums": len(state.get("video_forums") or []), "forums_with_point": len(state.get("forums") or {}),
                    "rows_cached": len(_load(ROWS, {})), "bridge_files": len(glob.glob(str(INGEST_DIR / "rustorka-*.txt"))),
                    "ingested_files": len(_load(DONE_LIST, [])), "last_ingest": state.get("last_ingest")},
    }


def main(argv: list[str]) -> None:
    cmd = argv[0] if argv else "status"
    conn = db.connect()
    db.migrate(conn)
    LOG.parent.mkdir(parents=True, exist_ok=True)

    def log(m):
        line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {m}"
        print(line, file=sys.stderr, flush=True)
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    opt = lambda k, d=None, f=int: f(argv[argv.index(k) + 1]) if k in argv else d  # noqa: E731
    if cmd == "forums":
        print(json.dumps(forums(Http(log=log)), ensure_ascii=False, indent=1))
    elif cmd == "crawl":
        st = crawl(conn, full="--full" in argv, limit=opt("--limit"), max_minutes=opt("--max-minutes", None, float), log=log)
        if "--match" in argv and not st.get("fatal"):
            from ..match import link_exact_names, match_releases
            st["match"] = match_releases(conn)
            st["match_exact_names"] = link_exact_names(conn)
        log(f"crawl: {json.dumps({k: st.get(k) for k in ('feed_items', 'wanted', 'stored', 'gap', 'fatal')}, ensure_ascii=False)}")
        print(json.dumps(st, ensure_ascii=False, indent=1, default=str))
        if not st.get("match"):
            log("next: python -X utf8 -m films match   (links the new releases to titles), then rebuild the sheet")
    elif cmd in ("queries", "resume"):
        # they rewrite queries_run.json / run.js that the bridge serves — one at a time
        from .. import locks
        with locks.lock("rustorka_run", timeout=1800):
            state, rows = _load(STATE, {}), _load(ROWS, {})
            if cmd == "queries":
                job = build_job(conn, state, rows, full="--full" in argv, delay_ms=opt("--delay-ms", 1500),
                                max_pages=opt("--max-pages", 400))
                info = {}
            else:
                base_job = _load(CACHE / "queries_run.json", None)
                if not base_job:
                    raise SystemExit("no queries_run.json — run `queries` first")
                base = str(base_job.get("base") or base_job.get("run"))
                n = 1 + sum(1 for _ in CACHE.glob(f"queries_run_{base}-r*.json"))
                saved = CACHE / f"queries_run_{base_job['run']}.json"     # the run being replaced is kept
                if not saved.exists():
                    _save(saved, base_job)
                job, info = resume_job(base_job, load_payloads(sorted(glob.glob(str(INGEST_DIR / "rustorka-*.txt")))),
                                       conn, state, rows, n)
                if "--delay-ms" in argv:
                    job["delayMs"] = opt("--delay-ms")
                _save(CACHE / f"queries_run_{job['run']}.json", job)
            print(json.dumps(dict(info, **write_run(job)), ensure_ascii=False, indent=1))
    elif cmd == "ingest":
        args = [a for a in argv[1:] if not a.startswith("--")]
        paths = _ingest_paths(args, "--all" in argv)
        state, rows = _load(STATE, {}), _load(ROWS, {})
        st = ingest(conn, load_payloads(paths), state, rows)
        st["files"] = len(paths)
        state["last_ingest"] = {"at": datetime.now().isoformat(timespec="seconds"), "files": len(paths),
                                "topics": st["topics"], "new": st["stored"]["new"], "fatal": st["fatal"]}
        _save(ROWS, rows)
        _save(STATE, state)
        done = set(_load(DONE_LIST, [])) | {Path(p).name for p in paths if Path(p).parent == INGEST_DIR}
        _save(DONE_LIST, sorted(done))
        if "--match" in argv:
            from .. import locks
            from ..match import link_exact_names, match_releases
            with locks.lock("match", timeout=6 * 3600):
                st["match"] = match_releases(conn)
                st["match_exact_names"] = link_exact_names(conn)
        if st["topics"]:
            db.mark_sync(conn, "rustorka_history", None, {k: v for k, v in st.items() if isinstance(v, int)})
        log(f"ingest: {json.dumps({k: st.get(k) for k in ('files', 'pages', 'rows', 'topics', 'forums_done', 'fatal')}, ensure_ascii=False)}")
        print(json.dumps(st, ensure_ascii=False, indent=1, default=str))
        if st["fatal"]:
            log(f"!! collector stopped: {st['fatal']} — check the rustorka tab (login / captcha), then `resume`")
        if not st.get("match"):
            log("next: python -X utf8 -m films match   (links the new releases to titles), then rebuild the sheet")
    elif cmd == "parse":
        print(json.dumps(reparse(conn, "--reparse" in argv), ensure_ascii=False, indent=1))
    elif cmd == "status":
        print(json.dumps(status(conn), ensure_ascii=False, indent=1, default=str))
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
