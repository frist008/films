"""kinozal.guru (source priority 4: rutor 1, rutracker 2, nnmclub 3, kinozal 4, rustorka 5, toloka 6, mazepa 7).

Owner, 2026-10-26: «и вот ещё источники после ру трекера которые должны идти https://nnmclub.to и https://kinozal.guru»;
«для всех источников достаточно смотреть обновления только с последней даты которая у нас будет об успешной
синхронизации». kinozal answers curl with a Cloudflare challenge (403), so — like rutracker — the pages are read in the
owner's Chrome by films/browser/kinozal_collect.js, which the OWNER pastes into the DevTools console of a kinozal.guru
tab (he opens data/cache/kinozal/run.js in Notepad and copies it himself; nothing here touches the clipboard). This module
is the Python side:

  queries — the job: (1) the listing, one slice per collected category and release year from year_from on
            (browse.php?c=<cat>&d=<year>, newest uploads first — kinozal shows at most 100 pages of a list, so the
            groups «Все фильмы» … are too big) back to the slice's sync point = the newest upload date of its last
            SUCCESSFUL walk minus OVERLAP_DAYS (state.json «groups», key «cat:year»), or to 1 January of config
            year_from on the first walk; (2) searches for the titles of films.sources.rutracker.build_queries the listing cannot cover (older
            titles, season gaps, the «хочу» list, franchise anime; R9 queries of year_from+ titles are skipped — the
            listing has them); (3) topic pages of wanted rows still without a page. Writes queries.json (with reasons),
            queries_run.json (served by the bridge: GET /queries/kinozal) and run.js (the file the owner pastes);
  resume  — the tail of the last run (owner, 2026-10-26: «если сбился … мы смогли и хвосты»): listing groups not finished
            (from the next page), searches not posted cleanly, topics still without a page → a new run «<base>-rN»;
  ingest  — the bridge files data/cache/ingest/kinozal-*.txt → rows cache, releases (source 'kinozal') from the row titles
            (kinozal titles carry the voice codes ДБ / ПМ / …, the quality and the resolution), topic pages → release_raw
            + the rutor parser (films.parse.kinozal), sync points of finished listing walks. DV-Profile-5-only releases
            are never inserted (excluded_releases). Idempotent; nothing is deleted;
  parse   — re-parse stored pages (after a parser change); status — counts, sync points, the last run's problems.

Then: python -X utf8 -m films match.

Run:  python -X utf8 -m films.sources.kinozal queries [--max-topics 1500] [--max-pages 100] [--full] [--sheet-priority] [--no-search]
      python -X utf8 -m films.sources.kinozal resume [--sheet-priority]   (topics of shown titles lacking dub/4K/HDR first)
      python -X utf8 -m films.sources.kinozal ingest [file|glob …] [--all] [--match]
      python -X utf8 -m films.sources.kinozal status
"""
from __future__ import annotations

import glob
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

from .. import db
from ..parse import kinozal as kparse
from . import nnmclub as common

SOURCE = "kinozal"
CACHE = db.ROOT / "data" / "cache" / "kinozal"
STATE = CACHE / "state.json"
ROWS = CACHE / "rows.json"
INGEST_DIR = db.ROOT / "data" / "cache" / "ingest"
DONE_LIST = CACHE / "ingested.json"
JS = db.ROOT / "films" / "browser" / "kinozal_collect.js"
BRIDGE_QUERIES = "http://127.0.0.1:8765/queries/kinozal"
OVERLAP_DAYS = 2
PAGE_CAP = 100        # kinozal lists at most 100 pages (5000 rows); page 100+ repeats page 99 (seen 2026-10-26)


def collected_cats() -> list[int]:
    return sorted(c for c, (cat, _) in kparse.CATEGORIES.items() if cat)


def listing_cats() -> list[int]:
    """Categories walked by the listing (anime only through the franchise searches, R44)."""
    return sorted(c for c, (cat, _) in kparse.CATEGORIES.items() if cat in ("foreign_films", "foreign_series", "animation"))


def skey(c, d=None) -> str:
    return str(c) if d in (None, "") else f"{c}:{d}"


def _since(state: dict, key: str, floor: str, full: bool) -> str | None:
    g = (state.get("groups") or {}).get(str(key)) or {}
    last = g.get("last_success")
    if full:
        return None
    if last and last.get("newest"):
        return (datetime.fromisoformat(last["newest"]) - timedelta(days=OVERLAP_DAYS)).isoformat(timespec="seconds")
    return floor


def known_topics(conn) -> list[str]:
    return [str(r[0]) for r in conn.execute(
        "SELECT r.source_id FROM releases r JOIN release_raw w ON w.release_id = r.id WHERE r.source = ?", (SOURCE,))]


def wanted_rows(conn, rows: dict, year_from: int) -> dict[str, str]:
    """topic id -> why ('year' / 'old_title' / 'search') of the cached rows worth a release."""
    old_idx = common.old_title_index(conn, year_from)
    out = {}
    for t, r in rows.items():
        if not kparse.category_for(r.get("cat")) or kparse.russian_original(r.get("title") or ""):
            continue
        if r.get("sel"):
            out[t] = "search"
            continue
        yrs = kparse.title_years(r.get("title") or "")
        if not yrs:
            continue
        if yrs[1] >= year_from:
            out[t] = "year"
        elif _old_hit(r["title"], yrs, old_idx):
            out[t] = "old_title"
    return out


def _old_hit(title: str, yrs: tuple[int, int], old_idx: dict[str, set[int]]) -> bool:
    """kinozal names come before the « / 2001 / » part: «Шрек / Shrek / 2001 / ДБ / BDRip (1080p)»."""
    names = []
    for p in kparse._split_slash(title or ""):
        if kparse.title_years(p):
            break
        names.append(p)
    for n in names:
        n = n.split("(")[0]
        ys = old_idx.get(common.norm(n))
        if ys and any(yrs[0] - 1 <= y <= yrs[1] + 1 for y in ys):
            return True
    return False


def order_topics(conn, topics: list[str], want: dict, rows: dict, sheet_priority: bool = False, log=print) -> list[str]:
    """Search hits first, then the newest uploads; with sheet_priority (as nnmclub --sheet-priority) first the topics of
    titles shown in the sheet whose best release lacks a dub / 4K / HDR-DV, then other shown titles, then the rest."""
    grp: dict[str, int] = {}
    if sheet_priority and topics:
        by_title = common.sheet_groups(conn, log=log)
        ids = {r[0]: r[1] for r in conn.execute("SELECT source_id, id FROM releases WHERE source = ?", (SOURCE,))}
        rid_t = {ids[t]: t for t in topics if t in ids}
        for rid, tid in conn.execute("SELECT r.release_id, r.title_id FROM release_titles r JOIN releases x ON x.id = r.release_id "
                                     "WHERE x.source = ?", (SOURCE,)):
            t = rid_t.get(rid)
            if t and by_title.get(tid):
                grp[t] = min(grp.get(t, 3), by_title[tid])
        log(f"  kinozal topic groups: { {g: sum(1 for t in topics if grp.get(t, 3) == g) for g in (1, 2, 3)} }")
    return sorted(topics, key=lambda t: (-grp.get(t, 3), want.get(t) == "search", (rows.get(t) or {}).get("added") or ""),
                  reverse=True)


def write_run(cfg: dict, queries_full: list[dict] | None = None, out: Path = CACHE) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    if queries_full is not None:
        (out / "queries.json").write_text(json.dumps({"run": cfg["run"], "queries": queries_full}, ensure_ascii=False,
                                                     indent=1), encoding="utf-8")
    (out / "queries_run.json").write_text(json.dumps(cfg, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (out / f"queries_run_{cfg['run']}.json").write_text(json.dumps(cfg, ensure_ascii=False, separators=(",", ":")),
                                                        encoding="utf-8")
    src = JS.read_text(encoding="utf-8")
    (out / "run.js").write_text(src + f"\n__kzCollect({{queriesUrl: {json.dumps(BRIDGE_QUERIES)}}});\n", encoding="utf-8")
    return {"run": cfg["run"], "listing": cfg["listing"], "queries": len(cfg["queries"]), "topics": len(cfg["topics"]),
            "known": len(cfg["known"]), "run_js": str(out / "run.js")}


def build_job(conn, max_topics: int = 1500, max_pages: int = PAGE_CAP, full: bool = False, log=print,
              sheet_priority: bool = False, searches: bool = True) -> tuple[dict, list]:
    year_from = min(db.years())
    floor = f"{year_from}-01-01T00:00:00"
    state, rows = common.load(STATE, {}), common.load(ROWS, {})
    qs = common.search_queries(conn, year_from, log=log) if searches else []   # --no-search: listing + topic pages only
    known = known_topics(conn)
    have = set(known)
    want = wanted_rows(conn, rows, year_from)
    topics = order_topics(conn, [t for t in want if t not in have], want, rows, sheet_priority, log)
    topics = topics[:max_topics] if max_topics else topics           # --max-topics 0: all
    # one slice per category and release year (browse.php?c=<cat>&d=<year>): a slice stays under the 100-page cap
    listing = []
    for c in listing_cats():
        for y in [y for y in db.years() if y <= datetime.now().year]:   # the site's year select has no future years
            k = skey(c, y)
            g = (state.get("groups") or {}).get(k) or {}
            listing.append({"c": c, "d": y, "since": _since(state, k, floor, full),
                            "startPage": 0 if (g.get("last_success") and not full) else int(g.get("resume_page") or 0),
                            "maxPages": max_pages})
    cfg = {"run": datetime.now().strftime("%Y%m%d-%H%M"), "listing": listing,
           "queries": [{k: q.get(k) for k in ("q", "lo", "hi", "need")} for q in qs], "topics": topics,
           "known": known, "cats": collected_cats(), "yearFrom": year_from, "maxTopics": len(topics) + 5000,
           "delayMs": 1500, "searchPages": 4,
           # fewer requests per topic (coordinator, 2026-10-26): the info hash is not used by ranking / matching (no
           # magnet on kinozal pages, so it would cost one request per topic); the «Тех. данные» tab only when the
           # description lacks video / audio lines or the forced-subtitle detail
           "hash": False, "tabs": "needed"}
    return cfg, qs


# --------------------------------------------------------------------------------------------
# ingest
# --------------------------------------------------------------------------------------------

def load_payloads(paths: list[str]) -> list[dict]:
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


def _row_store(conn, t: str, r: dict, why: str, have: dict, excl: set, st: dict) -> None:
    cat = kparse.category_for(r.get("cat"))
    meta = {"size_bytes": r.get("size"), "seeders": r.get("seeds"), "leechers": r.get("leech"), "added_at": r.get("added")}
    h = have.get(t)
    if h and h["name"] == r.get("title"):
        def _m():
            with conn:
                conn.execute("""UPDATE releases SET seeders=coalesce(?, seeders), leechers=coalesce(?, leechers),
                                    size_bytes=coalesce(size_bytes, ?), last_seen_at=datetime('now')
                                WHERE source=? AND source_id=?""",
                             (meta["seeders"], meta["leechers"], meta["size_bytes"], SOURCE, t))
        common.retry(_m)
        st["meta_refreshed"] += 1
        return
    rid = common.store_topic(conn, SOURCE, t, r["title"], cat, kparse.topic_url(t), st, excl, meta=meta,
                             title_parse=kparse.parse_title_only)
    if rid:
        have[t] = {"id": rid, "name": r["title"], "has_raw": bool(h and h["has_raw"]), "parsed_name": None}


def ingest(conn, payloads: list[dict], today=None) -> dict:
    common.ensure_sources(conn)
    year_from = min(db.years())
    state, rows = common.load(STATE, {}), common.load(ROWS, {})
    st = {"payloads": len(payloads), "listing_pages": 0, "search_queries": 0, "rows": 0, "topics": 0,
          "categories": None, "unknown_categories": [], "errors": 0, "error_samples": [], "fatal": [], "finals": 0,
          "groups_synced": [], "probe": [], "rows_stored": common.new_stats(), "pages": common.new_stats()}
    have = common.known(conn, SOURCE)
    excl = common.excluded(conn, SOURCE)
    topics: dict[str, dict] = {}
    for p in sorted(payloads, key=lambda x: (x.get("at") or "", x.get("_file") or "")):
        typ = p.get("type") or ("final" if p.get("final") else None)
        for e in p.get("errors") or []:
            st["errors"] += 1
            if len(st["error_samples"]) < 12:
                st["error_samples"].append({k: (str(v)[:300] if k == "snippet" else v) for k, v in e.items()})
        if typ == "categories":
            cats = p.get("categories") or []
            st["categories"] = len(cats)
            (CACHE / "categories.json").parent.mkdir(parents=True, exist_ok=True)
            (CACHE / "categories.json").write_text(json.dumps(cats, ensure_ascii=False, indent=1), encoding="utf-8")
            for c in cats:
                if c.get("id") and c["id"] not in kparse.CATEGORIES and c["id"] not in kparse.GROUPS and c["id"]:
                    st["unknown_categories"].append(f"{c['id']} {c.get('name')} → {kparse.category_from_name(c.get('name'))}")
        elif typ == "listing":
            for pg in p.get("pages") or []:
                if pg.get("err"):
                    continue
                st["listing_pages"] += 1
                for r in pg.get("rows") or []:
                    st["rows"] += 1
                    old = rows.get(r["t"]) or {}
                    rows[r["t"]] = dict(old, **{k: r.get(k) for k in ("title", "cat", "size", "seeds", "leech", "added")},
                                        group=skey(p.get("c"), p.get("d")))
            k = skey(p.get("c"), p.get("d"))
            gs = state.setdefault("groups", {}).setdefault(k, {})
            if p.get("newest") and (not gs.get("walk_newest") or p["newest"] > gs["walk_newest"]):
                gs["walk_newest"] = p["newest"]
            if p.get("next") is not None:
                gs["resume_page"] = p["next"] if not p.get("done") else 0
            # «page cap»: the slice hit kinozal's 100-page limit — the newest rows are in, older ones only via searches
            if p.get("done") and p.get("reason") in ("sync point reached", "last page", "page cap"):
                point = {"at": p.get("at"), "newest": gs.get("walk_newest") or p.get("newest"), "since": p.get("since"),
                         "run": p.get("run"), "reason": p.get("reason")}
                gs["last_success"] = point
                gs["syncs"] = (gs.get("syncs") or [])[-19:] + [point]
                gs["walk_newest"] = None
                st["groups_synced"].append(k)
                if p.get("reason") == "page cap":
                    st.setdefault("slices_capped", []).append(k)
        elif typ == "search":
            st["search_queries"] += 1
            for s in p.get("searches") or []:
                for r in s.get("rows") or []:
                    st["rows"] += 1
                    old = rows.get(r["t"]) or {}
                    rows[r["t"]] = dict(old, **{k: r.get(k) for k in ("title", "cat", "size", "seeds", "leech", "added")},
                                        sel=1 if (r.get("sel") or old.get("sel")) else 0)
        elif typ == "topics":
            for tp in p.get("topics") or []:
                if tp.get("topic_id"):
                    topics[str(tp["topic_id"])] = tp
                    if tp.get("probe") and len(st["probe"]) < 3:
                        st["probe"].append({k: v for k, v in tp["probe"].items() if k != "snippet"})
        elif typ == "final":
            st["finals"] += 1
            if p.get("fatal"):
                st["fatal"].append(p["fatal"])
    common.save(ROWS, rows)
    common.save(STATE, state)
    # rows → releases from their titles
    want = wanted_rows(conn, rows, year_from)
    for t, why in want.items():
        if t not in topics:
            _row_store(conn, t, rows[t], why, have, excl, st["rows_stored"])
    # topic pages → release_raw + parse
    sp = st["pages"]
    for t, tp in topics.items():
        st["topics"] += 1
        r = rows.get(t) or {}
        tp = dict(tp, title=tp.get("title") or r.get("title"), cat=tp.get("cat") or r.get("cat"),
                  seeders=tp.get("seeders") if tp.get("seeders") is not None else r.get("seeds"),
                  leechers=tp.get("leechers") if tp.get("leechers") is not None else r.get("leech"),
                  size_bytes=tp.get("size_bytes") or r.get("size"))
        if not tp.get("title"):
            continue
        if kparse.russian_original(tp["title"]):
            sp["russian_skipped"] = sp.get("russian_skipped", 0) + 1
            continue
        cat = kparse.category_for(tp.get("cat"))
        if not (tp.get("post_html") or "").strip() and not tp.get("tabs"):
            _row_store(conn, t, {"title": tp["title"], "cat": tp.get("cat"), "size": tp.get("size_bytes"),
                                 "seeds": tp.get("seeders"), "leech": tp.get("leechers"), "added": tp.get("reged")},
                       "page_empty", have, excl, sp)
            continue
        raw = kparse.compact_topic_html(tp)
        try:
            d = kparse.parse_topic(raw, today)
        except Exception as e:  # noqa: BLE001 — reported, stored by name
            sp["parse_failed"] += 1
            print(f"  PARSE FAILED kinozal {t}: {e!r}", file=sys.stderr, flush=True)
            continue
        meta = {"size_bytes": tp.get("size_bytes"), "seeders": tp.get("seeders"), "leechers": tp.get("leechers"),
                "btih": tp.get("btih"), "added_at": d.get("added_at") or r.get("added")}
        common.store_topic(conn, SOURCE, t, tp["title"], cat, kparse.topic_url(t), sp, excl, raw=raw, d=d, meta=meta)
    st["rows_cached"], st["wanted"] = len(rows), len(want)
    st["fatal"] = sorted(set(st["fatal"]))
    return st


def resume_job(cfg: dict, payloads: list[dict], conn, sheet_priority: bool = False) -> tuple[dict, dict]:
    """The tail of `cfg`'s run (and its earlier resumes): listing groups not finished, searches not posted cleanly."""
    base = str(cfg.get("base") or cfg.get("run") or "")
    mine = [p for p in payloads if str(p.get("run") or "") == base or str(p.get("run") or "").startswith(base + "-r")]
    ok_q = set()
    groups: dict[str, dict] = {}
    for p in mine:
        if p.get("type") == "search":
            q = p.get("query") or {}
            failed = any(e.get("q") for e in p.get("errors") or [])
            if not failed:
                ok_q.add((q.get("q"), q.get("lo"), q.get("hi")))
        elif p.get("type") == "listing" and p.get("next") is not None:
            groups[skey(p["c"], p.get("d"))] = p
    listing = []
    for g in cfg.get("listing") or []:
        p = groups.get(skey(g["c"], g.get("d")))
        if p and p.get("done"):
            continue
        listing.append(dict(g, startPage=int(p["next"]) if p else int(g.get("startPage") or 0)))
    queries = [q for q in cfg.get("queries") or [] if (q.get("q"), q.get("lo"), q.get("hi")) not in ok_q]
    rows = common.load(ROWS, {})
    have = set(known_topics(conn))
    want = wanted_rows(conn, rows, cfg.get("yearFrom") or min(db.years()))
    topics = order_topics(conn, [t for t in want if t not in have], want, rows, sheet_priority)[: cfg.get("maxTopics") or 1500]
    n = 1 + len(list(CACHE.glob(f"queries_run_{base}-r*.json")))
    new = dict(cfg, run=f"{base}-r{n}", base=base, listing=listing, queries=queries, topics=topics,
               known=sorted(have), categories=False)
    return new, {"base": base, "listing_left": [g["c"] for g in listing], "queries_left": len(queries),
                 "queries_done": len(ok_q), "topics_left": len(topics)}


def status(conn) -> dict:
    s = common.status(conn, SOURCE)
    s["rows_cached"] = len(common.load(ROWS, {}))
    stt = common.load(STATE, {})
    s["sync_points"] = {c: (g.get("last_success") or {}).get("newest") for c, g in (stt.get("groups") or {}).items()}
    s["bridge_files"] = len(glob.glob(str(INGEST_DIR / "kinozal-*.txt")))
    s["run_js"] = str(CACHE / "run.js") if (CACHE / "run.js").exists() else None
    return s


def _ingest_paths(args: list[str], all_: bool) -> list[str]:
    done = set(json.loads(DONE_LIST.read_text(encoding="utf-8"))) if DONE_LIST.exists() else set()
    paths = []
    for a in args or [str(INGEST_DIR / "kinozal-*.txt")]:
        p = Path(a)
        paths += sorted(glob.glob(str(p / "kinozal-*.txt"))) if p.is_dir() else sorted(glob.glob(a)) or ([a] if p.exists() else [])
    if not args and not all_:
        paths = [p for p in paths if Path(p).name not in done]
    return list(dict.fromkeys(paths))


def main(argv: list[str]) -> None:
    cmd = argv[0] if argv else "status"
    conn = db.connect()
    db.migrate(conn)
    log = lambda m: print(m, file=sys.stderr, flush=True)  # noqa: E731
    opt = lambda k, d: int(argv[argv.index(k) + 1]) if k in argv else d  # noqa: E731
    if cmd in ("queries", "resume"):
        from .. import locks
        with locks.lock("kinozal_run", timeout=1800):      # both rewrite queries_run.json / run.js the bridge serves
            if cmd == "queries":
                cfg, qs = build_job(conn, opt("--max-topics", 1500), opt("--max-pages", PAGE_CAP), "--full" in argv, log=log,
                                    sheet_priority="--sheet-priority" in argv, searches="--no-search" not in argv)
                print(json.dumps(write_run(cfg, qs), ensure_ascii=False, indent=1))
            else:
                cfg = json.loads((CACHE / "queries_run.json").read_text(encoding="utf-8"))
                new, info = resume_job(cfg, load_payloads(sorted(glob.glob(str(INGEST_DIR / "kinozal-*.txt")))), conn,
                                       "--sheet-priority" in argv)
                print(json.dumps(dict(info, **write_run(new)), ensure_ascii=False, indent=1))
    elif cmd == "ingest":
        args = [a for a in argv[1:] if not a.startswith("--")]
        paths = _ingest_paths(args, "--all" in argv)
        st = ingest(conn, load_payloads(paths))
        st["files"] = len(paths)
        done = set(json.loads(DONE_LIST.read_text(encoding="utf-8"))) if DONE_LIST.exists() else set()
        done |= {Path(p).name for p in paths if Path(p).parent == INGEST_DIR}
        CACHE.mkdir(parents=True, exist_ok=True)
        DONE_LIST.write_text(json.dumps(sorted(done), ensure_ascii=False, indent=0), encoding="utf-8")
        if "--match" in argv:
            from ..match import link_exact_names, match_releases
            st["match"] = match_releases(conn)
            st["match_exact_names"] = link_exact_names(conn)
        if paths:
            pts = [g.get("last_success", {}).get("newest") for g in (common.load(STATE, {}).get("groups") or {}).values()]
            db.mark_sync(conn, SOURCE, max([x for x in pts if x] or [None]) if any(pts) else None,
                         {k: v for k, v in st.items() if isinstance(v, (int, str))})
        print(json.dumps(st, ensure_ascii=False, indent=1, default=str))
        if st["fatal"]:
            log(f"!! collector stopped: {st['fatal']} — the owner passes Cloudflare in the kinozal tab, then `resume`")
    elif cmd == "parse":
        common.ensure_sources(conn)
        if "--titles" in argv:      # releases stored from their titles only (after a title-parser change)
            print(json.dumps(common.reparse_titles(conn, SOURCE, kparse.parse_title_only), ensure_ascii=False, indent=1))
        print(json.dumps(common.reparse(conn, SOURCE, kparse.parse_topic, "--reparse" in argv), ensure_ascii=False, indent=1))
    elif cmd == "status":
        print(json.dumps(status(conn), ensure_ascii=False, indent=1))
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
