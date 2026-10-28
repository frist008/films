"""mazepa.to (R206, source priority 7 after rutor, rutracker, nnmclub, kinozal, rustorka, toloka): Ukrainian-dubbed films, series and cartoons.

Owner, 2026-10-26: «https://toloka.to/ https://mazepa.to/index.php ещё 2 источника укр мовных … добавь их по приоритету 3
и 4 источником и можешь добавить с них данные если вдруг тут есть что лучше качества». mazepa is open to guests over plain
HTTP (forum listings and topic pages; its search, tracker.php, needs a login — not used), so this module walks the video
forums (films.parse.ua_trackers.MAZEPA_FORUMS: films / series / cartoons / cartoon series, Ukrainian ones too):

  forums  — read index.php and list the forums (collected or not; a new video-looking forum is flagged);
  crawl   — (1) the listing pages of every collected forum: the first run (or --full) walks them all, later runs only
            back to the last SUCCESSFUL sync of that forum minus 2 days (state.json «syncs»: date + newest topic id;
            owner, 2026-10-26: «смотреть обновления только с последней даты … об успешной синхронизации»; topics are
            listed by last activity, so new and re-uploaded topics come first — walk_forum); every row (topic id, title, slugged
            address) is remembered in data/cache/mazepa/rows.json, so later runs can pick old topics without walking again;
            (2) the wanted rows — title year ≥ config year_from (2024, open-ended), or an older year when the name + year is
            a title of the DB nobody has watched yet (franchises, prequels, everything encountered — R162 / R180) — are
            stored as releases from the title (source 'mazepa'); (3) their topic pages (description: voice, studio,
            audio tracks, video, forced subtitles, IMDb link) are read newest year first, only for topics without a stored
            page or whose title changed (more episodes, a new quality), until --limit / --max-minutes;
  parse   — re-parse the stored pages (after a parser change; --reparse = all);
  status  — counts.

Polite: one request at a time, ≥ 1.5 s between requests of all processes (films.locks.pace('mazepa_http', 1.5)) plus
jitter, backoff 5 / 15 / 45 s on errors, stop after 8 failures in a row; robots.txt allows the forum and topic pages.
Nothing is deleted; DV-Profile-5-only releases are never inserted (excluded_releases). Then: python -X utf8 -m films match.

Run:  python -X utf8 -m films.sources.mazepa forums
      python -X utf8 -m films.sources.mazepa crawl [--limit N] [--max-minutes M] [--forums 12,44] [--no-pages] [--full]
            [--progress data/logs/agent_src_mazepa.log] [--match]
      python -X utf8 -m films.sources.mazepa status
"""
from __future__ import annotations

import json
import random
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from .. import db
from ..parse import ua_trackers as uparse
from . import ua_trackers as ua

SOURCE = "mazepa"
BASE = uparse.BASE[SOURCE]
CACHE = db.ROOT / "data" / "cache" / "mazepa"
STATE = CACHE / "state.json"
ROWS = CACHE / "rows.json"
LOG = db.ROOT / "data" / "logs" / "mazepa_crawl.log"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
PACE_S = 1.5
PER_PAGE = 50
OVERLAP_DAYS = 2      # owner, 2026-10-26: re-read from the last successful sync minus two days (edited / late topics)


class Progress:
    """PROGRESS lines for the watchdog (owner, 2026-10-25: «PROGRESS 35% ETA 21:40 — <шаг>», at least every 10 min;
    one log per source — data/logs/agent_src_mazepa.log)."""

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
            fh.write(f"{now:%Y-%m-%d %H:%M:%S} PROGRESS {max(0, min(100, int(pct)))}% ETA {e} — {step}\n")

    def line(self, text: str):
        if self.path:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(text + "\n")


class LoginRequired(Exception):
    pass


class TooManyErrors(Exception):
    pass


class Http:
    """One request at a time with a shared minimum interval (films.locks.pace) and backoff."""

    def __init__(self, pace_s: float = PACE_S, log=print):
        import requests
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept-Language": "uk,en;q=0.8"})
        self.pace_s, self.log = pace_s, log
        self.requests = self.errors = self.fail_streak = 0

    def get(self, path: str) -> str | None:
        from .. import locks
        url = path if path.startswith("http") else f"{BASE}/{path.lstrip('/')}"
        err = None
        for a in range(4):
            locks.pace("mazepa_http", self.pace_s + random.random() * 0.5)
            self.requests += 1
            try:
                r = self.s.get(url, timeout=40)
                if "login.php" in r.url:
                    raise LoginRequired(url)
                if r.status_code == 404:
                    self.fail_streak = 0
                    return None
                if r.ok:
                    r.encoding = "utf-8"
                    self.fail_streak = 0
                    return r.text
                err = f"HTTP {r.status_code}"
            except LoginRequired:
                raise
            except Exception as e:  # noqa: BLE001 — network trouble: backoff and retry
                err = repr(e)
            self.errors += 1
            self.log(f"  ! {url}: {err} (try {a + 1})")
            time.sleep(5 * 3 ** a)
        self.fail_streak += 1
        if self.fail_streak >= 8:
            raise TooManyErrors(f"8 failed requests in a row, last {url}: {err}")
        return None


def _load(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _save(p: Path, v) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(v, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(p)


# --------------------------------------------------------------------------------------------
# forums
# --------------------------------------------------------------------------------------------

def forums(http: Http) -> list[dict]:
    page = http.get("index.php") or ""
    out = uparse.parse_mazepa_index(page)
    for f in out:
        f["new_video_forum"] = f["category"] is None and f["f"] not in uparse.MAZEPA_FORUMS and \
            f["f"] not in uparse.MAZEPA_PARENTS and uparse.category_for_forum(f["name"]) is not None
    return out


def walk_forum(http: Http, f: int, slug: str, state: dict, rows: dict, full: bool = False, log=print,
               deadline: float | None = None) -> dict:
    """Listing pages of one forum, newest activity first.

    Owner, 2026-10-26: «для всех источников достаточно смотреть обновления только с последней даты которая у нас будет об
    успешной синхронизации». The sync point of a forum is ``state['forums'][f]['last_success'] = {'at', 'max_topic'}``,
    written only when the walk ended normally (last page reached, or the point reached) — not on a deadline, a missing
    page or a fatal error; the last 20 points are kept (``syncs``). Next run: walk until the point minus an overlap of
    OVERLAP_DAYS — the boundary is the newest topic id of the latest successful sync at least OVERLAP_DAYS older than the
    last one (else of the first sync) — and stop at the first page with nothing new or renamed and no topic above that
    boundary. Guests see no dates in the listing, hence our own sync dates + topic ids. First walk, an interrupted first
    walk (resumed at resume_start) or --full: every page."""
    fs = state.setdefault("forums", {}).setdefault(str(f), {})
    points = fs.get("syncs") or ([{"at": fs["walked_at"], "max_topic": fs.get("max_topic")}]
                                 if fs.get("complete") and fs.get("walked_at") else [])   # state written before the rule
    incremental = bool(points) and not full
    boundary = None
    if incremental:
        cutoff = (datetime.fromisoformat(points[-1]["at"]) - timedelta(days=OVERLAP_DAYS)).isoformat(timespec="seconds")
        older = [p for p in points if p["at"] <= cutoff]
        boundary = int((older[-1] if older else points[0]).get("max_topic") or 0)
    st = {"pages": 0, "rows": 0, "new": 0, "changed": 0, "stopped": None, "boundary_topic": boundary}
    start = 0 if incremental else int(fs.get("resume_start") or 0)
    today = datetime.now().strftime("%Y-%m-%d")
    ok = False
    while True:
        if deadline and time.time() > deadline:
            st["stopped"] = "deadline"
            fs["resume_start"] = start
            break
        html = http.get(f"{slug}&start={start}.html" if start else f"{slug}.html")
        if html is None:
            st["stopped"] = "page missing"
            fs["resume_start"] = start
            break
        p = uparse.parse_mazepa_listing(html)
        st["pages"] += 1
        fresh = 0
        for r in p["rows"]:
            st["rows"] += 1
            old = rows.get(r["t"])
            if old is None:
                st["new"] += 1
                fresh += 1
            elif old.get("title") != r["title"]:
                st["changed"] += 1
                fresh += 1
            else:
                continue
            rows[r["t"]] = {"f": f, "title": r["title"], "href": r["href"], "seen": today}
        fs["max_topic"] = max([int(fs.get("max_topic") or 0)] + [int(r["t"]) for r in p["rows"]])
        if st["pages"] % 20 == 0:
            _save(ROWS, rows)
            _save(STATE, state)
            log(f"  forum {f}: page {p['page']}/{p['pages']}, rows {len(rows)}")
        last = p["page"] >= p["pages"] or not p["rows"]
        if incremental and not fresh and all(int(r["t"]) <= boundary for r in p["rows"]):
            st["stopped"] = "sync point reached"
            ok = True
            break
        if last:
            st["stopped"] = "last page"
            fs["complete"] = True
            fs["resume_start"] = 0
            ok = True
            break
        start += PER_PAGE
    fs["walked_at"] = datetime.now().isoformat(timespec="seconds")
    if ok:
        fs["last_success"] = {"at": fs["walked_at"], "max_topic": fs.get("max_topic")}
        fs["syncs"] = (points + [fs["last_success"]])[-20:]
    _save(ROWS, rows)
    _save(STATE, state)
    return st


# --------------------------------------------------------------------------------------------
# selection
# --------------------------------------------------------------------------------------------

def _norm(s: str | None) -> str:
    s = (s or "").casefold().replace("ё", "е").replace("ʼ", "'").replace("’", "'")
    return re.sub(r"\s+", " ", re.sub(r"[^\w]+", " ", s)).strip()


def old_title_index(conn, year_from: int) -> dict[str, set[int]]:
    """Names of DB titles older than year_from that nobody has watched and that have tracker releases (franchises,
    prequels, everything encountered): normalized name -> years. An older mazepa topic is read only for these."""
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


def wanted(rows: dict, year_from: int, old_idx: dict[str, set[int]]) -> dict[str, str]:
    """topic id -> why ('year' / 'old_title') for the listing rows worth a release."""
    out = {}
    for t, r in rows.items():
        if int(r["f"]) not in uparse.MAZEPA_FORUMS:
            continue
        yrs = uparse.title_years(r["title"])
        if not yrs:
            continue
        if yrs[1] >= year_from:
            out[t] = "year"
            continue
        head = re.split(r"\((?:19|20)\d{2}", r["title"])[0]
        head = re.sub(r"\([^()]*\)", " ", head)
        for n in head.split("/"):
            ys = old_idx.get(_norm(n))
            if ys and any(yrs[0] - 1 <= y <= yrs[1] + 1 for y in ys):
                out[t] = "old_title"
                break
    return out


# --------------------------------------------------------------------------------------------
# crawl
# --------------------------------------------------------------------------------------------

def crawl(conn, limit: int | None = None, max_minutes: float | None = None, only_forums: list[int] | None = None,
          pages: bool = True, full: bool = False, log=print, progress: Progress | None = None) -> dict:
    t0 = time.time()
    prog = progress or Progress(None)
    end = datetime.fromtimestamp(t0 + max_minutes * 60) if max_minutes else None
    prog(0, "mazepa: crawl started (listing of the video forums)", end, force=True)
    deadline = t0 + max_minutes * 60 if max_minutes else None
    http = Http(log=log)
    state, rows = _load(STATE, {}), _load(ROWS, {})
    st: dict = {"forums": {}, "requests": 0, "http_errors": 0, "fatal": None}
    try:
        idx = {f["f"]: f for f in forums(http)}
        st["new_video_forums"] = [f"{f['f']} {f['name']}" for f in idx.values() if f.get("new_video_forum")]
        for k, f in enumerate(sorted(uparse.MAZEPA_FORUMS), 1):
            prog(15 * k / len(uparse.MAZEPA_FORUMS), f"mazepa: listing forum {k}/{len(uparse.MAZEPA_FORUMS)}, "
                 f"rows {len(rows)}, requests {http.requests}", end)
            if only_forums and f not in only_forums:
                continue
            if f not in idx:
                st["forums"][f] = {"stopped": "forum not on index.php"}
                continue
            st["forums"][f] = walk_forum(http, f, idx[f]["slug"], state, rows, full=full, log=log, deadline=deadline)
            log(f"forum {f} {idx[f]['name']}: {st['forums'][f]}")
            if deadline and time.time() > deadline:
                break
    except (TooManyErrors, LoginRequired) as e:
        st["fatal"] = repr(e)
        log(f"!! listing stopped: {e!r}")

    year_from = min(db.years())
    want = wanted(rows, year_from, old_title_index(conn, year_from))
    have = ua.known(conn, SOURCE)
    excl = ua.excluded(conn, SOURCE)
    st["rows_cached"], st["wanted"] = len(rows), len(want)
    st["wanted_old_titles"] = sum(1 for v in want.values() if v == "old_title")
    # (2) every wanted row as a release from its title (new ones, and renamed ones keep their page until it is re-read)
    sn = ua.new_stats()
    for t, why in want.items():
        r = rows[t]
        h = have.get(t)
        if h and h["name"] == r["title"]:
            continue
        ua.store_topic(conn, SOURCE, t, r["title"], uparse.MAZEPA_FORUMS[int(r["f"])][0], None, sn, excl)
    st["titles_stored"] = sn
    # (3) topic pages: missing or renamed, newest year first
    have = ua.known(conn, SOURCE)
    todo = [t for t in want if t in have and t not in excl
            and (not have[t]["has_raw"] or have[t]["parsed_name"] != rows[t]["title"])]
    st["pages_renamed"] = sum(1 for t in todo if have[t]["has_raw"])
    todo.sort(key=lambda t: ((uparse.title_years(rows[t]["title"]) or (0, 0))[1], int(t)), reverse=True)
    st["pages_todo"] = len(todo)
    sp = ua.new_stats()
    sp["read"] = sp["missing"] = sp["login"] = 0
    if pages and not st["fatal"]:
        try:
            for i, t in enumerate(todo[:limit] if limit else todo, 1):
                if deadline and time.time() > deadline:
                    st["stopped"] = "deadline"
                    break
                r = rows[t]
                try:
                    html = http.get(r.get("href") or f"viewtopic.php?t={t}")
                except LoginRequired:
                    sp["login"] += 1
                    continue
                if html is None:
                    sp["missing"] += 1
                    continue
                topic = uparse.split_mazepa_topic(html)
                topic["fetched_at"] = datetime.now().isoformat(timespec="seconds")
                if not topic["post_html"]:
                    log(f"  no_post_body {t}: {html[:300]!r}")
                name = topic.get("title") or r["title"]
                ua.store_topic(conn, SOURCE, t, name, uparse.MAZEPA_FORUMS[int(r["f"])][0], topic, sp, excl)
                sp["read"] += 1
                n_all = max(1, min(len(todo), limit or len(todo)))
                eta = datetime.now() + timedelta(seconds=(time.time() - t0) / i * (n_all - i))
                prog(15 + 85 * i / n_all, f"mazepa: topic pages {i}/{len(todo)}, UA dubs {sp['ukr_dub']}",
                     min(end, eta) if end else eta)
                if i % 100 == 0:
                    pct = round(100 * i / max(1, min(len(todo), limit or len(todo))))
                    log(f"  pages {i}/{len(todo)} ({pct}%), new dubs {sp['ukr_dub']}, {round((time.time() - t0) / 60)} min")
        except TooManyErrors as e:
            st["fatal"] = repr(e)
            log(f"!! pages stopped: {e!r}")
    st["pages"] = sp
    st["pages_left"] = max(0, len(todo) - sp["read"] - sp["missing"] - sp["login"])
    st["requests"], st["http_errors"] = http.requests, http.errors
    st["minutes"] = round((time.time() - t0) / 60, 1)
    state["last_crawl"] = {"at": datetime.now().isoformat(timespec="seconds"), "minutes": st["minutes"],
                           "pages_left": st["pages_left"]}
    _save(STATE, state)
    cursor = str(max([int(t) for t in rows] or [0]))
    db.mark_sync(conn, SOURCE, cursor, {k: v for k, v in st.items() if isinstance(v, (int, float, str))})
    if st["fatal"]:
        prog(100, f"mazepa: STOPPED — {st['fatal']}", None, force=True)
    else:
        prog(100, f"mazepa: crawl done in {st['minutes']} min — pages read {sp['read']}, left {st['pages_left']}, "
             f"UA dubs {sp['ukr_dub']}; next: films match (under lock 'match')", None, force=True)
        prog.line("AGENT_SRC_MAZEPA_OK")
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
                   [int(x) for x in fl.split(",")] if fl else None, pages="--no-pages" not in argv, full="--full" in argv,
                   log=log, progress=Progress(opt("--progress", None, str)))
        if "--match" in argv:
            from ..match import link_exact_names, match_releases
            st["match"] = match_releases(conn)
            st["match_exact_names"] = link_exact_names(conn)
        print(json.dumps(st, ensure_ascii=False, indent=1, default=str))
        if not st.get("match"):
            log("next: python -X utf8 -m films match   (links the new releases to titles), then rebuild the sheet")
    elif cmd == "parse":
        print(json.dumps(ua.reparse(conn, SOURCE, "--reparse" in argv), ensure_ascii=False, indent=1))
    elif cmd == "status":
        s = ua.status(conn, SOURCE)
        s["rows_cached"] = len(_load(ROWS, {}))
        s["state"] = _load(STATE, {}).get("last_crawl")
        print(json.dumps(s, ensure_ascii=False, indent=1))
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
