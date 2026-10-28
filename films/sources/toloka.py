"""toloka.to («Гуртом», R206, source priority 6 after rutor, rutracker, nnmclub, kinozal, rustorka): Ukrainian dubs and better quality.

Owner, 2026-10-26: «https://toloka.to/ … ещё 2 источника укр мовных … добавь их по приоритету 3 и 4 источником и можешь
добавить с них данные если вдруг тут есть что лучше качества». toloka shows nothing without an account (every forum, search
and topic redirects to login.php); accounts and passwords are the owner's alone, so — as for rutracker — the pages are
read in his logged-in Chrome by ONE script he runs himself from the DevTools console (films/browser/toloka_collect.js;
it posts to the local bridge `python -X utf8 -m films bridge`). This module is the Python side:

  queries — the search list, most useful first (owner: «дубляж важнее качества всегда»):
              (1) seasons of followed / «хочу» series with no release at all, the «хочу» list (R48, as for rutracker);
              (2) visible titles whose releases (rutor, rutracker, mazepa, toloka) have no dub in any language — a
                  Ukrainian dub would become the best release;
              (3) visible titles without 4K or without HDR / DV in any release;
              (4) visible titles with a Russian dub only — no Ukrainian dub yet (lowest: rank treats a dub as a dub);
            titles at «максимальное качество» (rank.max_quality, R168(в)) are skipped, as rutracker does; watched titles too.
            Names: original, latin, Ukrainian as SEPARATE queries (R151; toloka titles are «Українська назва /
            Original»), the Russian name only when there is no Ukrainian one. Writes data/cache/toloka/queries.json (with
            reasons and title ids, for people), queries_run.json (served by the bridge at GET /queries/toloka), run.js
            (collector + `__tlCollect({queriesUrl})` — THE file the owner copies) and run_inline_N.js (queries inline,
            only if the page cannot reach the bridge). Each query already synced successfully gets `since` = the newest
            registration date that sync saw minus 2 days (data/cache/toloka/state.json, written by `ingest`; owner,
            2026-10-26: «смотреть обновления только с последней даты … об успешной синхронизации»): results come newest
            first, paging stops below it and older rows are not opened. `--full` = everything;
  ingest  — what the collector posted (data/cache/ingest/toloka-*.txt) → `releases` (source='toloka'), the first post →
            `release_raw`, parsed by films.parse.ua_trackers (the rutor parser on the adapted page); a selected search row
            without its page → stored from its title; other rows only refresh seeds of releases already stored. The forum
            list of the first payload → data/cache/toloka/forums.json. DV-Profile-5-only releases are never inserted;
  parse   — re-parse the stored pages (after a parser change; --reparse = all);
  status  — counts.

The logged-in markup has not been seen yet: the first live run must be checked — `ingest` prints the collector's
'row_parse_failed' / 'no_post_body' samples. Nothing is deleted.
Run:  python -X utf8 -m films.sources.toloka queries [--limit N] [--extra "Dune"]…
      python -X utf8 -m films.sources.toloka ingest [file|dir|glob …] [--all] [--match]
"""
from __future__ import annotations

import glob
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from .. import db
from ..parse import ua_trackers as uparse
from . import ua_trackers as ua

SOURCE = "toloka"
CACHE = db.ROOT / "data" / "cache" / "toloka"
INGEST_DIR = db.ROOT / "data" / "cache" / "ingest"
JS = db.ROOT / "films" / "browser" / "toloka_collect.js"
BRIDGE_QUERIES = "http://127.0.0.1:8765/queries/toloka"
DONE_LIST = CACHE / "ingested.json"
STATE = CACHE / "state.json"     # last successful sync per query (remember_syncs)


# --------------------------------------------------------------------------------------------
# queries
# --------------------------------------------------------------------------------------------

def _norm(s: str | None) -> str:
    s = (s or "").casefold().replace("ё", "е")
    return re.sub(r"\s+", " ", re.sub(r"[^\w]+", " ", s)).strip()


def _names(t: dict) -> list[str]:
    """Original, latin, Ukrainian (separate queries, R151). Never Russian (owner, 2026-10-26: «гуртом и мазепа … искать
    там выйдет только по англ или укр и ру названия бестолку искать», «а то окажется что ты будешь искать то чего нет»):
    toloka titles are Ukrainian + English, a Russian name finds nothing or a wrong film."""
    cands = [t.get("title_original"), t.get("title_latin"), t.get("title_uk")]
    out = []
    for n in cands:
        n = re.sub(r"\s+", " ", (n or "")).strip()
        if not re.search(r"[A-Za-zА-Яа-яІіЇїЄєҐґ]", n):
            continue                 # «ポケットモンスター»: toloka titles are Ukrainian + Latin, a CJK name finds nothing
        if n and len(_norm(n)) >= 2 and _norm(n) not in {_norm(x) for x in out}:
            out.append(n)
    return out


class _Queries:
    def __init__(self):
        self.items: dict[str, dict] = {}

    def add(self, name: str, lo, hi, need: list[str], why: str, prio: int, title_id=None):
        q = name
        if len(_norm(name)) < 4 and lo and hi == lo:
            q = f"{name} {lo}"   # «F1», «Up»: the year keeps a short name from returning everything
        k = _norm(q)
        if not k:
            return
        it = self.items.get(k)
        if not it:
            self.items[k] = {"q": q, "lo": lo, "hi": hi, "need": list(need), "why": [why], "prio": prio,
                             "titles": [title_id] if title_id else []}
            return
        it["lo"] = None if it["lo"] is None or lo is None else min(it["lo"], lo)
        it["hi"] = None if it["hi"] is None or hi is None else max(it["hi"], hi)
        it["need"] = ["any"] if "any" in it["need"] or "any" in need else sorted(set(it["need"]) | set(need))
        it["prio"] = min(it["prio"], prio)
        if why not in it["why"]:
            it["why"].append(why)
        if title_id and title_id not in it["titles"]:
            it["titles"].append(title_id)

    def add_title(self, t: dict, need: list[str], why: str, prio: int, open_end: bool = False):
        y = t.get("year")
        for i, n in enumerate(_names(t)):
            # the Russian fallback name and later spellings come after every title's main name
            self.add(n, y, None if open_end else y, need, why, prio + (0.5 if i else 0), t.get("id"))


def title_coverage(conn) -> dict[int, dict]:
    """title id -> what its stored releases (all trackers, without excluded ones) already give:
    {'dub': any dub, 'uk_dub': Ukrainian dub, '4k', 'hdr'}."""
    excl = {(r[0], r[1]) for r in conn.execute(
        "SELECT source, source_id FROM excluded_releases WHERE reason NOT LIKE 'lifted:%'")}
    out: dict[int, dict] = {}
    for tid, src, sid, res, hdr, audio in conn.execute(
            """SELECT rt.title_id, r.source, r.source_id, r.resolution, r.hdr, r.audio FROM releases r
               JOIN release_titles rt ON rt.release_id = r.id
               WHERE coalesce(rt.how, '') NOT LIKE 'wrong_link%'"""):
        if (src, str(sid)) in excl:
            continue
        c = out.setdefault(tid, {"dub": False, "uk_dub": False, "4k": False, "hdr": False})
        c["4k"] |= (res or 0) >= 2160
        try:
            h = json.loads(hdr) if hdr else []
        except ValueError:
            h = []
        c["hdr"] |= any(x in ("DV", "HDR10", "HDR10+", "HLG") for x in h)
        try:
            tracks = json.loads(audio) if audio else []
        except ValueError:
            tracks = []
        for a in tracks or []:
            if isinstance(a, dict) and a.get("kind") == "dub" and a.get("lang") in ("ru", "uk", None):
                c["dub"] = True
                c["uk_dub"] |= a.get("lang") == "uk"
    return out


def build_queries(conn, extras: list[str] = (), limit: int | None = None, log=print) -> list[dict]:
    from .. import watchlist
    qs = _Queries()
    watched = {r[0] for r in conn.execute("SELECT title_id FROM watched WHERE source NOT LIKE '%partial'")}
    for e in extras:
        qs.add(e, None, None, ["any"], "extra", 0)

    # (1) as for rutracker: seasons of followed / «хочу» series without any release, the «хочу» list
    from ..rank import franchise_cfg
    from ..franchise_seen import _too_old
    frs = {f["key"]: f for f in franchise_cfg() if f.get("scope") == "all"}
    followed = set()
    for key, imdb, kind, year in conn.execute(
            "SELECT franchise_key, imdb_id, kind, year FROM franchise_members WHERE confidence != 'review' AND imdb_id IS NOT NULL"):
        if key in frs and not _too_old(frs[key], {"kind": kind, "year": year, "imdb_id": imdb}):
            followed.add(imdb)
    wanted = watchlist.title_ids()
    today = date.today().isoformat()
    n_gap = 0
    for t in conn.execute(
            """SELECT t.* FROM titles t WHERE t.kind = 'series' AND t.season_number > 0
                 AND NOT EXISTS (SELECT 1 FROM release_titles rt WHERE rt.title_id = t.id)
                 AND coalesce(t.release_date, t.year || '-12-31') <= ?""", (today,)):
        t = dict(t)
        if t["id"] in watched or not (t.get("series_imdb_id") in followed or t["id"] in wanted):
            continue
        qs.add_title(t, ["any"], f"season_gap:{t['id']}", 1)
        n_gap += 1

    rank_rows = []
    try:
        from ..rank import compute
        rank_rows = compute(conn)
    except Exception as e:  # noqa: BLE001 — the query list must still be written; reported
        log(f"  rank.compute failed ({e!r}): visible-title part skipped")
    cov = title_coverage(conn)
    best = {r["title"]["id"]: r for r in rank_rows if r["variant"] == "Лучшее"}
    n_want = 0
    for tid in sorted(wanted):
        if tid in watched:
            continue
        t = conn.execute("SELECT * FROM titles WHERE id = ?", (tid,)).fetchone()
        if not t:
            continue
        row = best.get(tid)
        if row and row.get("max_quality"):
            continue
        c = cov.get(tid)
        need = ["any"] if not c else [n for n, ok in (("dub", c["dub"]), ("4k", c["4k"]), ("hdr", c["hdr"]),
                                                     ("ukdub", c["uk_dub"])) if not ok]
        if need:
            qs.add_title(dict(t), need, "watchlist", 1, open_end=t["kind"] == "series")
            n_want += 1

    # (2)–(4) visible titles in table order (the most wanted first)
    n = {"no_dub": 0, "picture": 0, "no_uk_dub": 0}
    for r in rank_rows:
        if r["variant"] != "Лучшее":
            continue
        if set(r["hidden_reasons"]) - {"нет озвучки"} or r["title"]["id"] in watched or r.get("max_quality"):
            continue                       # «нет озвучки»: toloka may have the Ukrainian dub the filter misses
        c = cov.get(r["title"]["id"]) or {"dub": False, "uk_dub": False, "4k": False, "hdr": False}
        pic = [x for x in ("4k", "hdr") if not c[x]]
        if not c["dub"]:
            qs.add_title(r["title"], ["dub"] + pic, "no_dub" + ("," + ",".join(pic) if pic else ""), 2)
            n["no_dub"] += 1
        elif pic:
            qs.add_title(r["title"], pic + ([] if c["uk_dub"] else ["ukdub"]), "picture:" + ",".join(pic), 3)
            n["picture"] += 1
        elif not c["uk_dub"]:
            qs.add_title(r["title"], ["ukdub"], "no_uk_dub", 4)
            n["no_uk_dub"] += 1

    out = sorted(qs.items.values(), key=lambda q: q["prio"])     # stable: table order inside a priority
    log(f"  queries: {len(out)} (season gaps {n_gap}, watchlist {n_want}, no dub {n['no_dub']}, "
        f"no 4K/HDR {n['picture']}, no UA dub {n['no_uk_dub']}, extra {len(extras)})")
    return out[:limit] if limit else out


def known_topics(conn) -> list[int]:
    """Topics whose page is stored: the collector does not open them again (search rows still refresh seeds)."""
    return [int(r[0]) for r in conn.execute(
        "SELECT r.source_id FROM releases r JOIN release_raw w ON w.release_id = r.id WHERE r.source = 'toloka'")
        if str(r[0]).isdigit()]


OVERLAP_DAYS = 2   # owner, 2026-10-26: from the last successful sync minus two days (edited / late topics)


def load_state(path: Path | None = None) -> dict:
    try:
        return json.loads((path or STATE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def since_for(state: dict, q: str) -> str | None:
    """'YYYY-MM-DD' the collector reads results back to for query q: the newest registration date its last SUCCESSFUL
    run saw (else that run's date) minus OVERLAP_DAYS; None = never synced → everything."""
    s = (state.get("queries") or {}).get(_norm(q))
    if not s:
        return None
    base = (s.get("newest_added") or s.get("at") or "")[:10]
    try:
        return (date.fromisoformat(base) - timedelta(days=OVERLAP_DAYS)).isoformat()
    except ValueError:
        return None


def remember_syncs(state: dict, payloads: list[dict]) -> int:
    """Owner, 2026-10-26: «смотреть обновления только с последней даты … об успешной синхронизации с данными по последнему
    фильму». A query payload without a query-level error and without a fatal stop is a successful sync of that query:
    state['queries'][norm(q)] = {at, newest_added (newest registration date among its rows, kept if older), newest_topic}."""
    n = 0
    qs = state.setdefault("queries", {})
    for p in payloads:
        q = (p.get("query") or {}).get("q")
        errs = p.get("errors") or []
        if not q or p.get("final") or any(e.get("q") or e.get("err") in ("not_logged_in", "cloudflare") for e in errs):
            continue
        rows = [r for s in p.get("searches") or [] for r in s.get("rows") or []]
        added = [str(r["added"])[:10] for r in rows if r.get("added")]
        ids = [int(r["t"]) for r in rows if str(r.get("t", "")).isdigit()]
        old = qs.get(_norm(q)) or {}
        qs[_norm(q)] = {"q": q, "at": p.get("at") or datetime.now().isoformat(timespec="seconds"),
                        "newest_added": max(added + ([old["newest_added"]] if old.get("newest_added") else [])) if added or old.get("newest_added") else None,
                        "newest_topic": max(ids + ([old["newest_topic"]] if old.get("newest_topic") else [])) if ids or old.get("newest_topic") else None,
                        "run": p.get("run")}
        n += 1
    return n


def write_run(queries: list[dict], known: list[int], max_topics: int = 12, delay_ms: int = 1500, max_pages: int = 10,
              part_size: int = 800, out: Path = CACHE, state: dict | None = None) -> dict:
    """state: load_state() (each query gets `since` from its last successful sync); None or {} = everything (--full)."""
    out.mkdir(parents=True, exist_ok=True)
    run = datetime.now().strftime("%Y%m%d-%H%M")
    (out / "queries.json").write_text(json.dumps({"run": run, "queries": queries}, ensure_ascii=False, indent=1),
                                      encoding="utf-8")
    compact = []
    for q in queries:
        c = {k: q[k] for k in ("q", "lo", "hi", "need")}
        since = since_for(state or {}, q["q"])
        if since:
            c["since"] = since
        compact.append(c)
    cfg = {"run": run, "queries": compact, "known": known, "maxTopics": max_topics, "delayMs": delay_ms,
           "maxPages": max_pages}
    (out / "queries_run.json").write_text(json.dumps(cfg, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    src = JS.read_text(encoding="utf-8")
    (out / "run.js").write_text(src + f"\n__tlCollect({{queriesUrl: {json.dumps(BRIDGE_QUERIES)}}});\n", encoding="utf-8")
    parts = []
    for i in range(0, len(compact), part_size):
        n = i // part_size + 1
        p = out / f"run_inline_{n}.js"
        c = dict(cfg, run=f"{run}-p{n}", queries=compact[i:i + part_size])
        p.write_text(src + f"\n__tlCollect({json.dumps(c, ensure_ascii=False, separators=(',', ':'))});\n", encoding="utf-8")
        parts.append(p.name)
    for old in out.glob("run_inline_*.js"):            # stale parts of an earlier, longer list: emptied, not deleted
        if old.name not in parts:
            old.write_text("// superseded by a newer `queries` run\n", encoding="utf-8")
    return {"run": run, "queries": len(compact), "known": len(known), "run_js": str(out / "run.js"),
            "inline_parts": parts}


# --------------------------------------------------------------------------------------------
# ingest
# --------------------------------------------------------------------------------------------

def load_payloads(paths: list[str]) -> list[dict]:
    """Bridge bodies (one JSON object each), or a read-out of __tlChunk (JSON array of JSON strings / objects)."""
    out = []
    for p in paths:
        txt = Path(p).read_text(encoding="utf-8").strip()
        if not txt:
            continue
        try:
            v = json.loads(txt)
        except json.JSONDecodeError as e:
            print(f"  BAD JSON {p}: {e}", file=sys.stderr, flush=True)
            continue
        for it in v if isinstance(v, list) else [v]:
            if isinstance(it, str):
                try:
                    it = json.loads(it)
                except json.JSONDecodeError:
                    continue
            if isinstance(it, dict) and it.get("kind", SOURCE) == SOURCE:
                it["_file"] = str(p)
                out.append(it)
    return out


def ingest(conn, payloads: list[dict], today: date | None = None) -> dict:
    st = {"payloads": 0, "queries": 0, "rows": 0, "selected": 0, "topics": 0, "seeds_refreshed": 0, "errors": 0,
          "error_samples": [], "capped": [], "fatal": [], "finals": 0, "forums": 0}
    st.update(ua.new_stats())
    excl = ua.excluded(conn, SOURCE)
    forum_names: dict[str, str] = {}
    for p in payloads:
        st["payloads"] += 1
        if p.get("forums"):
            forum_names.update({str(f["f"]): f["name"] for f in p["forums"] if f.get("f")})
            CACHE.mkdir(parents=True, exist_ok=True)
            (CACHE / "forums.json").write_text(json.dumps(p["forums"], ensure_ascii=False, indent=1), encoding="utf-8")
            st["forums"] = len(p["forums"])
            continue
        if p.get("final"):
            st["finals"] += 1
            if p.get("fatal"):
                st["fatal"].append(p["fatal"])
            continue
        st["queries"] += 1
        for e in p.get("errors") or []:
            st["errors"] += 1
            if len(st["error_samples"]) < 10:
                st["error_samples"].append({k: (str(v)[:300] if k == "snippet" else v) for k, v in e.items()})
            if e.get("err") in ("not_logged_in", "cloudflare"):
                st["fatal"].append(e["err"])
        rows: dict[str, dict] = {}
        for s in p.get("searches") or []:
            if s.get("capped"):
                st["capped"].append(s.get("q"))
            for r in s.get("rows") or []:
                st["rows"] += 1
                t = str(r.get("t"))
                if t not in rows or (r.get("sel") and not rows[t].get("sel")):
                    rows[t] = r
        topics = {str(tp.get("topic_id")): tp for tp in p.get("topics") or [] if tp.get("topic_id")}
        st["topics"] += len(topics)
        for t in list(dict.fromkeys([t for t, r in rows.items() if r.get("sel")] + list(topics))):
            st["selected"] += 1
            row, topic = rows.get(t, {}), topics.get(t)
            forum = (topic or {}).get("forum") or row.get("forum") or forum_names.get(str((topic or {}).get("forum_id") or row.get("fid")))
            category = uparse.category_for_forum(forum)
            name = (topic or {}).get("title") or row.get("title")
            if not name:
                continue
            if topic is not None:
                topic = dict(topic, size_bytes=topic.get("size_bytes") or row.get("size"),
                             seeders=topic.get("seeders") if topic.get("seeders") is not None else row.get("seeds"),
                             leechers=topic.get("leechers") if topic.get("leechers") is not None else row.get("leech"),
                             reged=topic.get("reged") or row.get("added"), forum=forum)
            else:
                topic = {"size_bytes": row.get("size"), "seeders": row.get("seeds"), "leechers": row.get("leech"),
                         "reged": row.get("added"), "post_html": ""}
            ua.store_topic(conn, SOURCE, t, name, category, topic, st, excl, today)

        def _refresh():
            n = 0
            with conn:
                for t, r in rows.items():
                    if r.get("sel") or t in topics:
                        continue
                    n += conn.execute(
                        """UPDATE releases SET seeders=coalesce(?, seeders), leechers=coalesce(?, leechers),
                               size_bytes=coalesce(?, size_bytes), last_seen_at=datetime('now')
                           WHERE source='toloka' AND source_id=?""",
                        (r.get("seeds"), r.get("leech"), r.get("size"), t)).rowcount
            return n
        st["seeds_refreshed"] += ua._retry(_refresh)
    st["capped"] = sorted(set(q for q in st["capped"] if q))
    st["fatal"] = sorted(set(st["fatal"]))
    return st


PROGRESS_LOG = db.ROOT / "data" / "logs" / "agent_src_toloka.log"   # one log per source (owner, 2026-10-26)


def _progress_line(st: dict) -> None:
    """A PROGRESS line for the watchdog after each ingest: how far the collector got (queries synced of the run's list)."""
    try:
        total = len(json.loads((CACHE / "queries_run.json").read_text(encoding="utf-8")).get("queries") or [])
    except (OSError, ValueError):
        total = 0
    done = len((load_state().get("queries") or {}))
    pct = int(100 * min(done, total) / total) if total else 0
    state = "STOPPED: " + ", ".join(st["fatal"]) if st.get("fatal") else "running"
    PROGRESS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(PROGRESS_LOG, "a", encoding="utf-8") as fh:
        fh.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} PROGRESS {min(pct, 99) if not st.get('finals') else 100}% ETA ? — "
                 f"toloka {state}: queries synced {done}/{total}, pages stored {st.get('pages_stored', 0)}, "
                 f"UA dubs {st.get('ukr_dub', 0)}, errors {st.get('errors', 0)}\n")


def _ingest_paths(args: list[str], all_: bool) -> list[str]:
    done = set(json.loads(DONE_LIST.read_text(encoding="utf-8"))) if DONE_LIST.exists() else set()
    paths = []
    for a in args or [str(INGEST_DIR / "toloka-*.txt")]:
        p = Path(a)
        found = sorted(glob.glob(str(p / "toloka-*.txt"))) if p.is_dir() else sorted(glob.glob(a)) or ([a] if p.exists() else [])
        paths += found
    if not args and not all_:
        paths = [p for p in paths if Path(p).name not in done]
    return list(dict.fromkeys(paths))


def main(argv: list[str]) -> None:
    cmd = argv[0] if argv else "status"
    conn = db.connect()
    db.migrate(conn)
    log = lambda m: print(m, file=sys.stderr, flush=True)  # noqa: E731
    opt = lambda k, d: int(argv[argv.index(k) + 1]) if k in argv else d  # noqa: E731
    if cmd == "queries":
        extras = [argv[i + 1] for i, a in enumerate(argv) if a == "--extra" and i + 1 < len(argv)]
        qs = build_queries(conn, extras, opt("--limit", None), log=log)
        state = {} if "--full" in argv else load_state()
        res = write_run(qs, known_topics(conn), max_topics=opt("--max-topics", 12), delay_ms=opt("--delay-ms", 1500),
                        max_pages=opt("--max-pages", 10), part_size=opt("--part-size", 800), state=state)
        res["incremental_queries"] = sum(1 for q in qs if since_for(state, q["q"]))
        print(json.dumps(res, ensure_ascii=False, indent=1))
    elif cmd == "ingest":
        args = [a for a in argv[1:] if not a.startswith("--")]
        paths = _ingest_paths(args, "--all" in argv)
        payloads = load_payloads(paths)
        st = ingest(conn, payloads)
        st["files"] = len(paths)
        CACHE.mkdir(parents=True, exist_ok=True)
        state = load_state()
        st["queries_synced"] = remember_syncs(state, payloads)
        STATE.write_text(json.dumps(state, ensure_ascii=False, indent=0), encoding="utf-8")
        _progress_line(st)
        done = set(json.loads(DONE_LIST.read_text(encoding="utf-8"))) if DONE_LIST.exists() else set()
        done |= {Path(p).name for p in paths if Path(p).parent == INGEST_DIR}
        DONE_LIST.write_text(json.dumps(sorted(done), ensure_ascii=False, indent=0), encoding="utf-8")
        if "--match" in argv:
            from ..match import link_exact_names, match_releases
            st["match"] = match_releases(conn)
            st["match_exact_names"] = link_exact_names(conn)
        if st["pages_stored"] or st["name_only"]:
            db.mark_sync(conn, SOURCE, None, {k: v for k, v in st.items() if isinstance(v, int)})
        print(json.dumps(st, ensure_ascii=False, indent=1))
        if st["fatal"]:
            log(f"!! collector stopped: {st['fatal']} — the owner must log in to toloka.to in Chrome")
        if not st.get("match"):
            log("next: python -X utf8 -m films match   (links the new releases to titles), then rebuild the sheet")
    elif cmd == "parse":
        print(json.dumps(ua.reparse(conn, SOURCE, "--reparse" in argv), ensure_ascii=False, indent=1))
    elif cmd == "status":
        s = ua.status(conn, SOURCE)
        s["bridge_files"] = len(glob.glob(str(INGEST_DIR / "toloka-*.txt")))
        s["ingested_files"] = len(json.loads(DONE_LIST.read_text(encoding="utf-8"))) if DONE_LIST.exists() else 0
        print(json.dumps(s, ensure_ascii=False, indent=1))
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
