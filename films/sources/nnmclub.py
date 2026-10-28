"""nnmclub.to (source priority 3: rutor 1, rutracker 2, nnmclub 3, kinozal 4, rustorka 5, toloka 6, mazepa 7).

Owner, 2026-10-26: «и вот ещё источники после ру трекера которые должны идти https://nnmclub.to и https://kinozal.guru»;
«для всех источников достаточно смотреть обновления только с последней даты которая у нас будет об успешной
синхронизации с данными по последнему фильму который там будет по дату». nnmclub answers guests over plain HTTP
(windows-1251): forum listings, tracker.php search (200 results at most, 50 a page) and topic pages (description;
the MediaInfo spoiler is for members only). No login, no browser. robots.txt asks for «Crawl-delay: 3» — kept:

  forums  — index.php: the collected forums (films.parse.nnmclub.FORUMS) and any new video-looking forum to add;
  crawl   — (1) the listing of every collected forum, newest activity first. Incremental: per forum the sync point is
            the date of the newest row of the last SUCCESSFUL walk (state.json «forums».<f>.last_success.newest); the
            next walk stops at the first page whose rows are all older than that point minus OVERLAP_DAYS (2). The first
            walk of a forum (and --full) goes back to the floor: 1 January of config year_from (2024) by last activity
            — every topic created since then — or to the last page with --full. Rows (topic id, title, seeds, size, last
            post) are kept in data/cache/nnmclub/rows.json; (2) searches for the titles of films.sources.rutracker
            .build_queries (original and Russian names as SEPARATE queries — R151) that the listing cannot cover: older
            titles (franchises, prequels, the «хочу» list, season gaps, franchise anime in the anime forums); R9
            queries of titles from year_from on are covered by the listing and skipped. A query with 200 results is split
            by year; (3) the wanted rows — listing rows of year ≥ year_from or of an older unwatched DB title, search rows
            inside the year window that show what the title lacks — become releases from their title (source
            'nnmclub'); (4) their topic pages are read (search hits first, then newest year first) for topics without a
            stored page or whose title changed, until --limit / --max-minutes; the rest waits for the next run.
            --sheet-priority orders the pages by how much they can change the sheet: (1) titles shown in the sheet
            whose best release (any tracker) lacks a dub / 4K / HDR-DV, (2) other shown titles, (3) the rest; the log
            says «PRIORITY GROUP 1 DONE» when (1) is read (the sheet can be rebuilt then);
  parse   — re-parse stored pages (after a parser change; --reparse = all);
  status  — counts and the sync state.

Polite: one request at a time, ≥ PACE_S (3 s, robots.txt Crawl-delay) between requests of all processes
(films.locks.pace('nnmclub_http')) plus jitter, backoff 5 / 15 / 45 s, stop after 8 failures in a row. A captcha,
Cloudflare challenge or login page stops the run (never bypassed) — state is saved, the sync point is not moved.
Nothing is deleted; DV-Profile-5-only releases are never inserted (excluded_releases). Then: python -X utf8 -m films match.

Run:  python -X utf8 -m films.sources.nnmclub forums
      python -X utf8 -m films.sources.nnmclub crawl [--max-minutes M] [--limit N] [--forums 954,227] [--no-search]
                                                    [--no-pages] [--full] [--sheet-priority]
                                                    [--progress data/logs/agent_src_nnmclub.log] [--match]
      python -X utf8 -m films.sources.nnmclub status
"""
from __future__ import annotations

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
from ..parse import nnmclub as nparse

SOURCE = "nnmclub"
BASE = nparse.BASE
CACHE = db.ROOT / "data" / "cache" / "nnmclub"
STATE = CACHE / "state.json"
ROWS = CACHE / "rows.json"
LOG = db.ROOT / "data" / "logs" / "nnmclub_crawl.log"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
PACE_S = 3.0          # robots.txt: Crawl-delay: 3
PER_PAGE = 50
SEARCH_CAP = 200
OVERLAP_DAYS = 2      # owner, 2026-10-26: re-read from the last successful sync minus two days (edited / late topics)
SOURCES_NEW = ("nnmclub", "kinozal")


class Blocked(Exception):
    """A captcha / Cloudflare challenge / login page instead of the content — never bypassed: the run stops."""


class TooManyErrors(Exception):
    pass


# --------------------------------------------------------------------------------------------
# DB side (shared with films/sources/kinozal.py)
# --------------------------------------------------------------------------------------------

def retry(fn, *a, tries: int = 8):
    """Run a short write transaction, retrying while another collector holds the lock."""
    for i in range(tries):
        try:
            return fn(*a)
        except sqlite3.OperationalError as e:
            if "locked" not in str(e) and "busy" not in str(e) or i == tries - 1:
                raise
            time.sleep(1.5 * (i + 1))


def ensure_sources(conn) -> None:
    """releases.source must accept 'nnmclub' / 'kinozal': the CHECK is widened in place by films.db (no table rebuild),
    as the toloka / mazepa / rustorka collectors do. Until films/db.py lists them in a migration this runs here."""
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'releases'").fetchone()[0]
    if all(f"'{s}'" in sql for s in SOURCES_NEW):
        return
    widen = getattr(db, "_widen_release_sources", None)
    if widen is None:
        raise RuntimeError("releases.source does not accept nnmclub/kinozal and films.db has no _widen_release_sources")
    retry(lambda: widen(conn, SOURCES_NEW))


def _js(v):
    return json.dumps(v, ensure_ascii=False) if v is not None else None


def parser_version(kind: str) -> str:
    from ..parse import kinozal as kp
    from ..parse import rutor as base
    from ..parse import rutracker as rt
    files = [Path(base.__file__), Path(rt.__file__), Path(nparse.__file__ if kind.startswith("nnmclub") else kp.__file__)]
    return f"{kind}:{max(p.stat().st_mtime_ns for p in files)}"


def store_parsed(conn, rid: int, d: dict, version: str, source: str) -> str | None:
    """The columns films.sources.rutor.parse_pages fills + the release rules (caller holds the transaction).
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
    return store_rules(conn, rid, source, sid, name, d)


def raw_page(conn, release_id: int) -> str | None:
    row = conn.execute("SELECT body FROM release_raw WHERE release_id=?", (release_id,)).fetchone()
    return zlib.decompress(row[0]).decode("utf-8") if row else None


def excluded(conn, source: str) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT source_id FROM excluded_releases WHERE source = ? AND reason NOT LIKE 'lifted:%'", (source,))}


def known(conn, source: str) -> dict[str, dict]:
    """source_id -> {'id', 'name', 'has_raw', 'parsed_name'} of the releases already stored for this tracker."""
    return {str(r[0]): {"id": r[1], "name": r[2], "has_raw": bool(r[3]), "parsed_name": r[4]} for r in conn.execute(
        """SELECT r.source_id, r.id, r.name, w.release_id,
                  CASE WHEN json_valid(r.parsed) THEN json_extract(r.parsed, '$.name') END
           FROM releases r LEFT JOIN release_raw w ON w.release_id = r.id WHERE r.source = ?""", (source,))}


def _upsert(conn, source: str, sid: str, url: str, name: str, category: str, meta: dict) -> tuple[int, bool]:
    cur = conn.execute(
        """UPDATE releases SET name=?, category=coalesce(?, category), size_bytes=coalesce(?, size_bytes),
               seeders=coalesce(?, seeders), leechers=coalesce(?, leechers), btih=coalesce(?, btih),
               added_at=coalesce(?, added_at), last_seen_at=datetime('now')
           WHERE source=? AND source_id=?""",
        (name, category, meta.get("size_bytes"), meta.get("seeders"), meta.get("leechers"), meta.get("btih"),
         meta.get("added_at"), source, sid))
    if cur.rowcount:
        return conn.execute("SELECT id FROM releases WHERE source=? AND source_id=?", (source, sid)).fetchone()[0], False
    cur = conn.execute(
        """INSERT INTO releases (source, source_id, url, btih, name, category, added_at, size_bytes, seeders, leechers)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (source, sid, url, meta.get("btih"), name, category, meta.get("added_at"), meta.get("size_bytes"),
         meta.get("seeders"), meta.get("leechers")))
    return cur.lastrowid, True


def _exclude_dv5(conn, source: str, sid: str, name: str) -> int | None:
    def _ex():
        with conn:
            conn.execute("INSERT OR IGNORE INTO excluded_releases (source, source_id, reason, name) VALUES (?, ?, 'dv5_only', ?)",
                         (source, sid, name))
            row = conn.execute("SELECT id FROM releases WHERE source=? AND source_id=?", (source, sid)).fetchone()
            return row[0] if row else None
    return retry(_ex)


def new_stats() -> dict:
    return {"new": 0, "updated": 0, "pages_stored": 0, "name_only": 0, "meta_refreshed": 0, "dv5_excluded": 0,
            "dv5_already_stored": [], "excluded_skipped": 0, "rule_excluded": {}, "skipped_category": 0,
            "ru_dub": 0, "uk_dub": 0, "parse_failed": 0}


def store_topic(conn, source: str, sid: str, name: str, category: str | None, url: str, st: dict, excl: set[str], *,
                raw: str | None = None, d: dict | None = None, meta: dict | None = None,
                title_parse=None) -> int | None:
    """One topic. With its page (raw = the compact document for release_raw, d = its parse) → release_raw + full parse;
    without → the title parse (title_parse(name, category)) unless a page is already stored (then only the meta).
    Returns the release id (None when not stored)."""
    sid = str(sid)
    if sid in excl:
        st["excluded_skipped"] += 1
        return None
    category = category or (d or {}).get("category")
    if not category:
        st["skipped_category"] += 1
        return None
    d_name = None
    if d is None:
        d_name = title_parse(name, category)
        if d_name.get("dv5_only"):
            old = _exclude_dv5(conn, source, sid, name)
            excl.add(sid)
            st["dv5_excluded"] += 1
            if old:
                st["dv5_already_stored"].append(old)
            return None
    elif d.get("dv5_only"):
        old = _exclude_dv5(conn, source, sid, name)
        excl.add(sid)
        st["dv5_excluded"] += 1
        if old:   # stored earlier from its title only: never deleted, reported for the owner
            st["dv5_already_stored"].append(old)
        return None
    meta = dict(meta or {})
    if d is not None and not meta.get("added_at"):
        meta["added_at"] = d.get("added_at")

    def _write():
        with conn:
            rid, new = _upsert(conn, source, sid, url, name, category, meta)
            if raw is not None:
                conn.execute("INSERT OR REPLACE INTO release_raw (release_id, body) VALUES (?, ?)",
                             (rid, zlib.compress(raw.encode("utf-8"), 9)))
                return rid, new, "page", store_parsed(conn, rid, d, parser_version(f"{source}-page"), source)
            has_raw = conn.execute("SELECT 1 FROM release_raw WHERE release_id=?", (rid,)).fetchone()
            if not has_raw:
                return rid, new, "name", store_parsed(conn, rid, d_name, parser_version(f"{source}-title"), source)
            return rid, new, "meta", None
    rid, new, how, rule = retry(_write)
    st["new" if new else "updated"] += 1
    st[{"page": "pages_stored", "name": "name_only", "meta": "meta_refreshed"}[how]] += 1
    if rule:
        st["rule_excluded"][rule] = st["rule_excluded"].get(rule, 0) + 1
    if how == "page":
        vbl = d.get("voice_by_lang") or {}
        st["ru_dub"] += "dub" in (vbl.get("ru") or [])
        st["uk_dub"] += "dub" in (vbl.get("uk") or [])
    return rid


def reparse(conn, source: str, parse_page, all_: bool = False) -> dict:
    """Re-run the parser over stored pages of this tracker (after a parser change)."""
    v = parser_version(f"{source}-page")
    rows = conn.execute(
        f"""SELECT r.id, r.source_id, r.name FROM releases r JOIN release_raw w ON w.release_id = r.id
            WHERE r.source = ? {'' if all_ else "AND (r.parsed IS NULL OR r.parsed_version IS NOT ?)"}""",
        (source,) if all_ else (source, v)).fetchall()
    st = {"parsed": 0, "failed": 0, "dv5_found": []}
    for rid, sid, name in rows:
        try:
            d = parse_page(raw_page(conn, rid))
        except Exception as e:  # noqa: BLE001 — recorded and reported
            st["failed"] += 1
            print(f"  PARSE FAILED {sid}: {e!r}", file=sys.stderr, flush=True)
            continue

        def _w():
            with conn:
                if d.get("dv5_only"):
                    conn.execute("INSERT OR IGNORE INTO excluded_releases (source, source_id, reason, name) "
                                 "VALUES (?, ?, 'dv5_only', ?)", (source, sid, name))
                store_parsed(conn, rid, d, v, source)
        retry(_w)
        if d.get("dv5_only"):
            st["dv5_found"].append(rid)   # not deleted — reported
        st["parsed"] += 1
    return st


def reparse_titles(conn, source: str, title_parse) -> dict:
    """Re-parse the releases stored from their titles only (no page yet) — after a title-parser change. A release that
    turns out DV-Profile-5-only goes to excluded_releases and is reported (never deleted)."""
    v = parser_version(f"{source}-title")
    rows = conn.execute("""SELECT r.id, r.source_id, r.name, r.category FROM releases r
                           WHERE r.source = ? AND NOT EXISTS (SELECT 1 FROM release_raw w WHERE w.release_id = r.id)""",
                        (source,)).fetchall()
    st = {"parsed": 0, "dv5_found": [], "rule_excluded": {}}
    for i in range(0, len(rows), 500):
        def _w(chunk=rows[i:i + 500]):
            with conn:
                for rid, sid, name, cat in chunk:
                    d = title_parse(name, cat)
                    if d.get("dv5_only"):
                        conn.execute("INSERT OR IGNORE INTO excluded_releases (source, source_id, reason, name) "
                                     "VALUES (?, ?, 'dv5_only', ?)", (source, sid, name))
                        st["dv5_found"].append(rid)
                    rule = store_parsed(conn, rid, d, v, source)
                    if rule:
                        st["rule_excluded"][rule] = st["rule_excluded"].get(rule, 0) + 1
                    st["parsed"] += 1
        retry(_w)
    return st


def status(conn, source: str) -> dict:
    q = lambda sql: conn.execute(sql, (source,)).fetchone()[0]  # noqa: E731
    return {
        "releases": q("SELECT count(*) FROM releases WHERE source=?"),
        "with_page": q("SELECT count(*) FROM releases r JOIN release_raw w ON w.release_id=r.id WHERE r.source=?"),
        "parsed": q("SELECT count(*) FROM releases WHERE source=? AND parsed IS NOT NULL"),
        "linked": q("SELECT count(DISTINCT r.id) FROM releases r JOIN release_titles t ON t.release_id=r.id WHERE r.source=?"),
        "titles": q("SELECT count(DISTINCT t.title_id) FROM releases r JOIN release_titles t ON t.release_id=r.id WHERE r.source=?"),
        "ru_dub": q("""SELECT count(*) FROM releases WHERE source=? AND json_valid(parsed)
                       AND json_extract(parsed, '$.voice_by_lang.ru') LIKE '%"dub"%'"""),
        "uk_dub": q("""SELECT count(*) FROM releases WHERE source=? AND json_valid(parsed)
                       AND json_extract(parsed, '$.voice_by_lang.uk') LIKE '%"dub"%'"""),
        "excluded": q("SELECT count(*) FROM excluded_releases WHERE source=? AND reason NOT LIKE 'lifted:%'"),
    }


def norm(s: str | None) -> str:
    s = (s or "").casefold().replace("ё", "е").replace("ʼ", "'").replace("’", "'")
    return re.sub(r"\s+", " ", re.sub(r"[^\w]+", " ", s)).strip()


def old_title_index(conn, year_from: int) -> dict[str, set[int]]:
    """Names of DB titles older than year_from that nobody has watched and that have tracker releases (franchises,
    prequels, everything encountered): normalized name -> years. An older topic is taken only for these."""
    watched = {r[0] for r in conn.execute("SELECT title_id FROM watched WHERE source NOT LIKE '%partial'")}
    idx: dict[str, set[int]] = {}
    for tid, y, *names in conn.execute(
            """SELECT t.id, t.year, t.title_original, t.title_latin, t.title_uk, t.title_ru FROM titles t
               WHERE t.year < ? AND EXISTS (SELECT 1 FROM release_titles rt WHERE rt.title_id = t.id)
                 AND """ + db.OLD_TITLE_WANTED_SQL, (year_from,)):
        if tid in watched or not y:
            continue
        for n in names:
            k = norm(n)
            if len(k) >= 3:
                idx.setdefault(k, set()).add(int(y))
    return idx


def old_title_hit(title: str, yrs: tuple[int, int], old_idx: dict[str, set[int]]) -> bool:
    head = re.split(r"[(\[](?:19|20)\d{2}", title or "")[0]
    head = re.sub(r"\([^()]*\)|\[[^\]]*\]", " ", head)
    for n in re.split(r"\s*/\s*", head):
        ys = old_idx.get(norm(n))
        if ys and any(yrs[0] - 1 <= y <= yrs[1] + 1 for y in ys):
            return True
    return False


NEED_RE = {
    "4k": re.compile(r"(?i)(?<![\w])(2160p|4K|UHD)(?![\w])"),
    "hdr": re.compile(r"(?i)(?<![\w])(HDR10\+?|HDR|Dolby\s*Vision|DV|DoVi|HLG)(?![\w])"),
    "flarrow": re.compile(r"(?i)flarrow"),
}


def needs_match(title: str, need: list[str]) -> bool:
    """A search row is worth its page when it may have what the title lacks; titles rarely name the voice, so a needed
    dub (or 'any') takes every row of the year window."""
    if not need or "any" in need or "dub" in need:
        return True
    return any(NEED_RE[n].search(title or "") for n in need if n in NEED_RE)


def load(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def save(p: Path, v) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(v, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(p)


class Progress:
    """PROGRESS lines for the watchdog (owner, 2026-10-25: «PROGRESS 35% ETA 21:40 — <шаг>», at least every 10 min)."""

    def __init__(self, path: str | None, every_s: float = 300):
        self.path, self.every, self.last = (Path(path) if path else None), every_s, 0.0

    def __call__(self, pct: float, step: str, eta: datetime | None = None, force: bool = False):
        if not self.path or (not force and time.time() - self.last < self.every):
            return
        self.last = time.time()
        now = datetime.now()
        e = "?" if eta is None else (f"{eta:%H:%M}" if eta.date() == now.date() else f"{eta:%d.%m_%H:%M}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(f"{now:%Y-%m-%d %H:%M:%S} PROGRESS {max(0, min(99, int(pct)))}% ETA {e} — {step}\n")


# --------------------------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------------------------

class Http:
    """One request at a time with a shared minimum interval (films.locks.pace) and backoff."""

    def __init__(self, pace_s: float = PACE_S, log=print):
        import requests
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept-Language": "ru,en;q=0.8"})
        self.pace_s, self.log = pace_s, log
        self.requests = self.errors = self.fail_streak = 0

    def get(self, path: str, params: dict | None = None) -> str | None:
        from .. import locks
        url = path if path.startswith("http") else f"{BASE}/{path.lstrip('/')}"
        err = None
        for a in range(4):
            locks.pace("nnmclub_http", self.pace_s + random.random() * 0.7)
            self.requests += 1
            try:
                r = self.s.get(url, params=params, timeout=40)
                if r.status_code == 404:
                    self.fail_streak = 0
                    return None
                if r.ok:
                    html = r.content.decode("cp1251", errors="replace")
                    prob = nparse.page_problem(html)
                    if prob in ("captcha", "login", "cloudflare"):
                        raise Blocked(f"{prob} at {r.url}")
                    if prob == "maintenance":
                        err = "maintenance page"
                    else:
                        self.fail_streak = 0
                        return html
                else:
                    err = f"HTTP {r.status_code}"
                    if r.status_code in (403, 429, 503) and "cf-" in (r.headers.get("server", "") + str(r.headers)).lower():
                        err += " (Cloudflare)"
            except Blocked:
                raise
            except Exception as e:  # noqa: BLE001 — network trouble: backoff and retry
                err = repr(e)
            self.errors += 1
            self.log(f"  ! {url} {params or ''}: {err} (try {a + 1})")
            time.sleep(5 * 3 ** a)
        self.fail_streak += 1
        if self.fail_streak >= 8:
            raise TooManyErrors(f"8 failed requests in a row, last {url}: {err}")
        return None


# --------------------------------------------------------------------------------------------
# forums
# --------------------------------------------------------------------------------------------

def forums(http: Http) -> list[dict]:
    out = nparse.parse_index(http.get("index.php") or "")
    for f in out:
        f["collected"] = f["f"] in nparse.FORUMS
        f["search_only"] = f["f"] in nparse.SEARCH_ONLY
        f["new_video_forum"] = not f["collected"] and not f["search_only"] and f["f"] not in nparse.NOT_COLLECTED \
            and nparse.looks_like_video_forum(f["name"])
    return out


def walk_forum(http: Http, f: int, state: dict, rows: dict, floor: str, full: bool = False, log=print,
               deadline: float | None = None, on_page=None) -> dict:
    """Listing pages of one forum, newest activity first (see the module docstring for the sync point).
    on_page(page, pages) is called after every page (progress lines of a long forum)."""
    fs = state.setdefault("forums", {}).setdefault(str(f), {})
    last = fs.get("last_success")
    incremental = bool(last and last.get("newest")) and not full
    if incremental:
        stop_at = (datetime.fromisoformat(last["newest"]) - timedelta(days=OVERLAP_DAYS)).isoformat(timespec="seconds")
    else:
        stop_at = None if full else floor
    st = {"pages": 0, "rows": 0, "new": 0, "changed": 0, "stopped": None, "stop_at": stop_at,
          "mode": "incremental" if incremental else "full" if full else "first"}
    start = int(fs.get("resume_start") or 0) if not incremental else 0
    newest = fs.get("walk_newest") if start else None
    today = datetime.now().strftime("%Y-%m-%d")
    ok = False
    while True:
        if deadline and time.time() > deadline:
            st["stopped"] = "deadline"
            break
        html = http.get(f"viewforum.php?f={f}&start={start}" if start else f"viewforum.php?f={f}")
        if html is None:
            st["stopped"] = "page missing"
            break
        p = nparse.parse_listing(html)
        st["pages"] += 1
        dated = [r for r in p["rows"] if r.get("last_post")]
        for r in p["rows"]:
            st["rows"] += 1
            old = rows.get(r["t"])
            if old is None:
                st["new"] += 1
            elif old.get("title") != r["title"]:
                st["changed"] += 1
            rows[r["t"]] = {"f": f, "title": r["title"], "seeds": r["seeds"], "leech": r["leech"], "size": r["size"],
                            "last_post": r["last_post"], "torrent": r["torrent"],
                            "seen": today, "first_seen": (old or {}).get("first_seen") or today}
        if dated:
            top = max(r["last_post"] for r in dated)
            newest = max(newest or top, top)
        fs["resume_start"] = start + PER_PAGE
        fs["walk_newest"] = newest
        if on_page:
            on_page(p["page"], p["pages"])
        if st["pages"] % 20 == 0:
            save(ROWS, rows)
            save(STATE, state)
            log(f"  forum {f}: page {p['page']}/{p['pages']}, rows cached {len(rows)}")
        if stop_at and dated and all(r["last_post"] < stop_at for r in dated):
            st["stopped"] = "sync point reached" if incremental else "floor reached"
            ok = True
            break
        if p["page"] >= p["pages"] or not p["rows"]:
            st["stopped"] = "last page"
            ok = True
            break
        start += PER_PAGE
    fs["walked_at"] = datetime.now().isoformat(timespec="seconds")
    st["newest"] = newest
    if ok:
        point = {"at": fs["walked_at"], "newest": newest or (last or {}).get("newest"), "mode": st["mode"],
                 "pages": st["pages"]}
        if st["mode"] == "first":
            point["floor"] = floor
        elif st["mode"] == "full":
            point["complete"] = True
        fs["last_success"] = point
        fs["syncs"] = (fs.get("syncs") or [])[-19:] + [point]
        fs["resume_start"] = 0
        fs["walk_newest"] = None
    save(ROWS, rows)
    save(STATE, state)
    return st


# --------------------------------------------------------------------------------------------
# searches
# --------------------------------------------------------------------------------------------

def search_queries(conn, year_from: int, log=print) -> list[dict]:
    """films.sources.rutracker.build_queries minus what the listing covers: R9-only queries of titles from year_from on."""
    from .rutracker import build_queries
    qs = build_queries(conn, log=log)
    out = [q for q in qs if not (all(w.startswith("r9:") for w in q.get("why") or [])
                                 and q.get("lo") is not None and q["lo"] >= year_from)]
    log(f"  nnmclub searches: {len(out)} of {len(qs)} rutracker queries (R9 queries of {year_from}+ titles are covered "
        f"by the listing)")
    return out


def sheet_groups(conn, log=print) -> dict[int, int]:
    """title id -> 1 (shown in the sheet, its best release — any tracker — lacks a dub / 4K / HDR-DV) or 2 (shown)."""
    from ..rank import compute
    from .rutracker import _needs
    out: dict[int, int] = {}
    for r in compute(conn):
        if r["variant"] != "Лучшее":
            continue
        if set(r["hidden_reasons"]) - {"нет озвучки"}:      # «нет озвучки» may be what a page fixes
            continue
        tid = r["title"]["id"]
        out[tid] = min(out.get(tid, 2), 1 if _needs(r["release"], r) else 2)
    log(f"  sheet groups: {sum(1 for g in out.values() if g == 1)} visible titles lacking dub/4K/HDR, "
        f"{sum(1 for g in out.values() if g == 2)} other visible")
    return out


def _search_all(http: Http, q: str, fids: str) -> dict:
    first = http.get("tracker.php", {"nm": q, "f": fids})
    p = nparse.parse_search(first or "")
    res = {"q": q, "total": p["total"], "capped": p["total"] >= SEARCH_CAP, "rows": list(p["rows"]), "pages": 1}
    if p["total"] and not p["rows"]:
        res["error"] = "row_parse_failed"
        return res
    m = re.search(r"tracker\.php\?search_id=(\d+)", first or "")
    for start in range(PER_PAGE, min(p["total"], SEARCH_CAP), PER_PAGE):
        if not m:
            break
        pp = nparse.parse_search(http.get("tracker.php", {"search_id": m.group(1), "start": start}) or "")
        res["pages"] += 1
        if not pp["rows"]:
            break
        res["rows"] += pp["rows"]
    return res


def _split(q: dict) -> list[str]:
    now = datetime.now().year
    if q.get("lo") is not None and q.get("hi") is not None and min(q["hi"] + 1, now + 1) - (q["lo"] - 1) <= 8:
        return [f"{q['q']} {y}" for y in range(q["lo"] - 1, min(q["hi"] + 1, now + 1) + 1)]
    words = {"4k": "2160p", "hdr": "HDR"}
    return [f"{q['q']} {words[n]}" for n in q.get("need") or [] if n in words]


def select_row(r: dict, q: dict) -> bool:
    if nparse.category_for_forum(r.get("fid")) is None:
        return False
    y = nparse.title_years(r.get("title") or "")
    if not y:
        return False
    lo = -1 if q.get("lo") is None else q["lo"]
    hi = 9999 if q.get("hi") is None else q["hi"]
    if y[1] < lo - 1 or y[0] > hi + 1:
        return False
    return needs_match(r.get("title") or "", q.get("need") or [])


# --------------------------------------------------------------------------------------------
# crawl
# --------------------------------------------------------------------------------------------

def crawl(conn, limit: int | None = None, max_minutes: float | None = None, only_forums: list[int] | None = None,
          do_search: bool = True, pages: bool = True, full: bool = False, log=print, progress: Progress | None = None,
          max_topics: int = 40, sheet_priority: bool = False) -> dict:
    t0 = time.time()
    deadline = t0 + max_minutes * 60 if max_minutes else None
    prog = progress or Progress(None)
    http = Http(log=log)
    state, rows = load(STATE, {}), load(ROWS, {})
    ensure_sources(conn)
    year_from = min(db.years())
    floor = f"{year_from}-01-01T00:00:00"
    st: dict = {"forums": {}, "fatal": None, "floor": floor}
    plan = [f for f in sorted(nparse.FORUMS) if not only_forums or f in only_forums]
    # the listing first; it may take most of the budget on the first run, searches and pages get the rest
    listing_deadline = (t0 + max_minutes * 60 * 0.45) if max_minutes else None
    try:
        idx = {f["f"]: f for f in forums(http)}
        st["new_video_forums"] = [f"{f['f']} {f['name']}" for f in idx.values() if f.get("new_video_forum")]
        st["forums_missing"] = [f for f in plan if f not in idx]
        for i, f in enumerate(plan, 1):
            st["forums"][f] = walk_forum(
                http, f, state, rows, floor, full=full, log=log, deadline=listing_deadline,
                on_page=lambda pg, pgs, i=i: prog(30 * (i - 1) / len(plan), f"listing forum {f} ({i}/{len(plan)}), page "
                                                  f"{pg}/{pgs}, rows {len(rows)}, requests {http.requests}"))
            log(f"forum {f} {nparse.FORUMS[f][1]}: {st['forums'][f]}")
            el = time.time() - t0
            prog(30 * i / len(plan), f"listing {i}/{len(plan)} forums, rows {len(rows)}, requests {http.requests}",
                 datetime.now() + timedelta(seconds=el / i * (len(plan) - i)) if i else None)
            if listing_deadline and time.time() > listing_deadline:
                st["listing_stopped"] = f"deadline after {i}/{len(plan)} forums"
                break
    except (TooManyErrors, Blocked) as e:
        st["fatal"] = repr(e)
        log(f"!! listing stopped: {e!r}")

    # (2) searches
    hits: dict[str, dict] = {}            # topic id -> search row (+ query)
    sq = {"queries": 0, "done": 0, "capped": [], "row_parse_failed": [], "rows": 0, "selected": 0}
    if do_search and not st["fatal"]:
        fids = ",".join(str(f) for f in sorted(set(nparse.FORUMS) | set(nparse.SEARCH_ONLY)))
        qs = search_queries(conn, year_from, log=log)
        done_q = set(state.get("search_done", {}).get("keys") or []) if state.get("search_done", {}).get("day") == \
            datetime.now().strftime("%Y-%m-%d") else set()
        sq["queries"] = len(qs)
        search_deadline = (t0 + max_minutes * 60 * 0.7) if max_minutes else None
        try:
            for i, q in enumerate(qs, 1):
                key = f"{q['q']}|{q.get('lo')}|{q.get('hi')}"
                if key in done_q:
                    continue
                if search_deadline and time.time() > search_deadline:
                    sq["stopped"] = f"deadline after {i - 1}/{len(qs)}"
                    break
                res = [_search_all(http, q["q"], fids)]
                if res[0].get("error"):
                    sq["row_parse_failed"].append(q["q"])
                if res[0]["capped"]:
                    sq["capped"].append(q["q"])
                    res += [_search_all(http, s, fids) for s in _split(q)]
                picked = []
                for s in res:
                    for r in s["rows"]:
                        sq["rows"] += 1
                        if select_row(r, q) and r["t"] not in hits:
                            picked.append(r)
                picked.sort(key=lambda r: -(r.get("seeds") or 0))
                for r in picked[:max_topics]:
                    hits[r["t"]] = dict(r, why=q.get("why"))
                sq["done"] += 1
                done_q.add(key)
                if i % 25 == 0:
                    state["search_done"] = {"day": datetime.now().strftime("%Y-%m-%d"), "keys": sorted(done_q)}
                    save(STATE, state)
                    el = time.time() - t0
                    prog(30 + 30 * i / len(qs), f"searches {i}/{len(qs)}, hits {len(hits)}, requests {http.requests}")
        except (TooManyErrors, Blocked) as e:
            st["fatal"] = repr(e)
            log(f"!! searches stopped: {e!r}")
        state["search_done"] = {"day": datetime.now().strftime("%Y-%m-%d"), "keys": sorted(done_q)}
        sq["selected"] = len(hits)
    st["search"] = sq
    # search hits are kept (state «hits»): a later run without searches (--no-search) still reads their pages
    kept = state.setdefault("hits", {})
    for t, r in hits.items():
        kept[t] = {k: r.get(k) for k in ("t", "fid", "title", "size", "seeds", "leech", "added")}
    for t, r in kept.items():
        hits.setdefault(t, r)

    # (3) wanted rows → releases from their titles
    old_idx = old_title_index(conn, year_from)
    want: dict[str, dict] = {}
    for t, r in rows.items():
        cat = nparse.category_for_forum(r.get("f"))
        yrs = nparse.title_years(r.get("title") or "")
        if not cat or not yrs or not r.get("torrent", True):
            continue
        if yrs[1] >= year_from:
            want[t] = dict(r, why="year", cat=cat)
        elif old_title_hit(r["title"], yrs, old_idx):
            want[t] = dict(r, why="old_title", cat=cat)
    for t, r in hits.items():
        cat = nparse.category_for_forum(r.get("fid"))
        want[t] = dict(want.get(t) or {}, title=r["title"], f=r.get("fid"), seeds=r.get("seeds"), leech=r.get("leech"),
                       size=r.get("size"), why="search", cat=cat, added=r.get("added"))
    have = known(conn, SOURCE)
    excl = excluded(conn, SOURCE)
    # releases stored earlier (e.g. search hits of a run before «hits» were kept) that are neither in the listing
    # cache nor in this run's hits: their pages are still wanted
    for sid, cat, name, seeds, size in conn.execute(
            """SELECT r.source_id, r.category, r.name, r.seeders, r.size_bytes FROM releases r
               WHERE r.source = ? AND NOT EXISTS (SELECT 1 FROM release_raw w WHERE w.release_id = r.id)""", (SOURCE,)):
        if sid not in want and sid not in excl and cat:
            want[sid] = {"title": name, "cat": cat, "seeds": seeds, "size": size, "why": "stored"}
    sn = new_stats()
    for t, r in want.items():
        meta = {"size_bytes": r.get("size"), "seeders": r.get("seeds"), "leechers": r.get("leech"),
                "added_at": r.get("added")}
        h = have.get(t)
        if h and h["name"] == r["title"]:
            # only seeds / size refresh
            def _m(t=t, meta=meta):
                with conn:
                    conn.execute("""UPDATE releases SET seeders=coalesce(?, seeders), leechers=coalesce(?, leechers),
                                        size_bytes=coalesce(size_bytes, ?), last_seen_at=datetime('now')
                                    WHERE source=? AND source_id=?""",
                                 (meta["seeders"], meta["leechers"], meta["size_bytes"], SOURCE, t))
            retry(_m)
            sn["meta_refreshed"] += 1
            continue
        store_topic(conn, SOURCE, t, r["title"], r["cat"], nparse.topic_url(t), sn, excl, meta=meta,
                    title_parse=nparse.parse_title_only)
    st["rows_cached"], st["wanted"] = len(rows), len(want)
    st["wanted_by"] = {k: sum(1 for r in want.values() if r["why"] == k) for k in ("year", "old_title", "search", "stored")}
    st["titles_stored"] = sn
    save(ROWS, rows)
    save(STATE, state)

    # (4) topic pages: missing or renamed — search hits first, then newest year, newest activity
    have = known(conn, SOURCE)
    todo = [t for t in want if t in have and t not in excl
            and (not have[t]["has_raw"] or have[t]["parsed_name"] != want[t]["title"])]
    st["pages_renamed"] = sum(1 for t in todo if have[t]["has_raw"])

    grp: dict[str, int] = {}
    if sheet_priority and todo:
        # coordinator, 2026-10-26: pages by how much they can change the sheet — (1) visible titles whose best release
        # (any tracker) lacks a dub / 4K / HDR-DV, (2) other visible titles, (3) the rest
        by_title = sheet_groups(conn, log=log)
        rel_ids = {have[t]["id"]: t for t in todo}
        for rid, tid in conn.execute(f"SELECT release_id, title_id FROM release_titles WHERE release_id IN "
                                     f"({','.join('?' * len(rel_ids))})", list(rel_ids)) if len(rel_ids) < 30000 else []:
            g = by_title.get(tid)
            if g:
                t = rel_ids[rid]
                grp[t] = min(grp.get(t, 3), g)
        st["page_groups"] = {g: sum(1 for t in todo if grp.get(t, 3) == g) for g in (1, 2, 3)}
        log(f"  page groups (1 visible + lacks dub/4K/HDR, 2 visible, 3 rest): {st['page_groups']}")

    def _prio(t):
        r = want[t]
        y = (nparse.title_years(r["title"]) or (0, 0))[1]
        return (-grp.get(t, 3), r["why"] == "search", y, r.get("last_post") or "", int(t))
    todo.sort(key=_prio, reverse=True)
    st["pages_todo"] = len(todo)
    n_g1 = sum(1 for t in todo if grp.get(t, 3) == 1) if grp else 0
    sp = new_stats()
    sp.update({"read": 0, "missing": 0, "no_post_body": 0})
    if pages and not st["fatal"]:
        t1 = time.time()
        n_all = min(len(todo), limit or len(todo))
        try:
            for i, t in enumerate(todo[:n_all], 1):
                if deadline and time.time() > deadline:
                    st["stopped"] = "deadline"
                    break
                r = want[t]
                html = http.get(f"viewtopic.php?t={t}")
                if html is None:
                    sp["missing"] += 1
                    continue
                topic = nparse.split_topic(html)
                topic["topic_id"] = t
                topic["fetched_at"] = datetime.now().isoformat(timespec="seconds")
                topic["seeders"] = topic["seeders"] if topic["seeders"] is not None else r.get("seeds")
                topic["leechers"] = topic["leechers"] if topic["leechers"] is not None else r.get("leech")
                if not topic["post_html"]:
                    sp["no_post_body"] += 1
                    snippet = re.sub(r"\s+", " ", html[:300])
                    log(f"  no_post_body {t}: {snippet!r}")
                    store_topic(conn, SOURCE, t, topic.get("title") or r["title"], r["cat"], nparse.topic_url(t), sp,
                                excl, meta={"btih": topic.get("btih")}, title_parse=nparse.parse_title_only)
                    continue
                name = topic.get("title") or r["title"]
                raw = nparse.compact_topic_html(dict(topic, title=name))
                try:
                    d = nparse.parse_topic(raw)
                except Exception as e:  # noqa: BLE001 — reported, stored by name
                    sp["parse_failed"] += 1
                    log(f"  PARSE FAILED {t}: {e!r}")
                    store_topic(conn, SOURCE, t, name, r["cat"], nparse.topic_url(t), sp, excl,
                                title_parse=nparse.parse_title_only)
                    continue
                meta = {"size_bytes": topic.get("size_bytes") or r.get("size"), "seeders": topic.get("seeders"),
                        "leechers": topic.get("leechers"), "btih": topic.get("btih"), "added_at": d.get("added_at")}
                store_topic(conn, SOURCE, t, name, r["cat"] or d.get("category"), nparse.topic_url(t), sp, excl,
                            raw=raw, d=d, meta=meta)
                sp["read"] += 1
                if n_g1 and i == n_g1:
                    st["group1_done_at"] = datetime.now().isoformat(timespec="seconds")
                    log(f"PRIORITY GROUP 1 DONE: {n_g1} pages of visible titles lacking dub / 4K / HDR-DV read")
                    prog(60 + 39 * i / n_all, f"GROUP 1 DONE ({n_g1} pages: visible titles lacking dub/4K/HDR) — "
                         f"topic pages {i}/{n_all}", None, force=True)
                if i % 50 == 0:
                    el = time.time() - t1
                    left = (n_all - i) * el / i
                    if deadline:
                        left = min(left, max(0, deadline - time.time()))
                    g1 = f", group 1 {min(i, n_g1)}/{n_g1}" if n_g1 else ""
                    log(f"  pages {i}/{n_all}{g1}, ru dub {sp['ru_dub']}, uk dub {sp['uk_dub']}, {round((time.time() - t0) / 60)} min")
                    prog(60 + 39 * i / n_all, f"topic pages {i}/{n_all}{g1}, dub ru {sp['ru_dub']} / uk {sp['uk_dub']}, "
                         f"requests {http.requests}", datetime.now() + timedelta(seconds=left))
        except (TooManyErrors, Blocked) as e:
            st["fatal"] = repr(e)
            log(f"!! pages stopped: {e!r}")
    st["pages"] = sp
    st["pages_left"] = max(0, len(todo) - sp["read"] - sp["missing"])
    st["requests"], st["http_errors"] = http.requests, http.errors
    st["minutes"] = round((time.time() - t0) / 60, 1)
    state["last_crawl"] = {"at": datetime.now().isoformat(timespec="seconds"), "minutes": st["minutes"],
                           "pages_left": st["pages_left"], "fatal": st["fatal"], "requests": st["requests"]}
    save(STATE, state)
    points = [fs.get("last_success", {}).get("newest") for fs in state.get("forums", {}).values()]
    cursor = max([p for p in points if p] or [None]) if any(points) else None
    db.mark_sync(conn, SOURCE, cursor, {k: v for k, v in st.items() if isinstance(v, (int, float, str))})
    return st


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
        fl = opt("--forums", None, str)
        st = crawl(conn, opt("--limit"), opt("--max-minutes", None, float),
                   [int(x) for x in fl.split(",")] if fl else None, do_search="--no-search" not in argv,
                   pages="--no-pages" not in argv, full="--full" in argv, log=log,
                   progress=Progress(opt("--progress", None, str)), sheet_priority="--sheet-priority" in argv)
        if "--match" in argv:
            from ..match import link_exact_names, match_releases
            st["match"] = match_releases(conn)
            st["match_exact_names"] = link_exact_names(conn)
        print(json.dumps(st, ensure_ascii=False, indent=1, default=str))
        if not st.get("match"):
            log("next: python -X utf8 -m films match   (links the new releases to titles), then rebuild the sheet")
    elif cmd == "parse":
        ensure_sources(conn)
        if "--titles" in argv:      # releases stored from their titles only (after a title-parser change)
            print(json.dumps(reparse_titles(conn, SOURCE, nparse.parse_title_only), ensure_ascii=False, indent=1))
        print(json.dumps(reparse(conn, SOURCE, nparse.parse_topic, "--reparse" in argv), ensure_ascii=False, indent=1))
    elif cmd == "status":
        s = status(conn, SOURCE)
        s["rows_cached"] = len(load(ROWS, {}))
        stt = load(STATE, {})
        s["last_crawl"] = stt.get("last_crawl")
        s["sync_points"] = {f: (v.get("last_success") or {}).get("newest") for f, v in (stt.get("forums") or {}).items()}
        print(json.dumps(s, ensure_ascii=False, indent=1))
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
