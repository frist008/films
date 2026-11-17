"""rutracker.org (R9): searched only for what rutor lacks — 4K, HDR/DV or a dub — plus franchise anime (R44), the owner's
«хочу» series (R48) and seasons of followed series with no rutor release (R91: «Южный парк» S27–S29).

rutracker is behind Cloudflare and needs the owner's login, so the pages are read in his Chrome by ONE javascript call
(films/browser/rutracker_collect.js; every JS call on rutracker shows him an «Allow» window). This module is the Python
side of that run:

  queries   — build the query list (original and Russian names as SEPARATE queries — R151), write
              data/cache/rutracker/queries.json (with reasons and title ids, for people), queries_run.json (served by the
              bridge at GET /queries/rutracker), run.js (collector + `__rtCollect({queriesUrl})`, the one call to paste)
              and run_inline_N.js (the same with the queries inline — only if the page cannot reach the bridge).
              R322 (owner, 2026-11-13: «ищем только всё новое для новых дат и новых фильмов и не трогаем ничего старого»):
              a query searched cleanly before (data/cache/rutracker/state.json, with the titles and needs it covered) is
              NOT sent again — only new queries, queries with a new title / season / need (`pending`), `--extra` names;
              `--again` = all (owner's request only). New releases of the searched titles come from the job's `listing`:
              the collector first walks the video forums (LISTING_FORUMS) newest first back to each forum's sync point
              and keeps rows of tracked titles (every built query, `listing_index`) or of year_from and later;
  ingest    — read what the collector posted to the bridge (data/cache/ingest/rutracker-*.txt, or a file assembled from
              __rtChunk read-outs) into `releases` (source='rutracker') and the first post's HTML into `release_raw`,
              parse it with films.parse.rutracker (= the rutor page parser on the adapted page) and fill the same
              quality/audio columns rutor releases get. Matching to titles is the normal step: `films match`
              (or `--match` here). DV-Profile-5-only releases are never inserted (excluded_releases; one found
              DV5-only later by its page is removed after the sync — db.purge_dv5);
  parse     — re-parse stored rutracker pages (after a parser change; `--reparse` = all, otherwise unparsed only);
  status    — counts.

Nothing is deleted. Run:  python -X utf8 -m films.sources.rutracker queries [--limit N] [--extra "South Park"]… [--again]
                                [--full] [--no-listing] [--listing-topics N]
                          python -X utf8 -m films.sources.rutracker ingest [file|dir|glob …] [--all] [--match]
"""
from __future__ import annotations

import glob
import json
import re
import sqlite3
import sys
import time
import zlib
from datetime import date, datetime
from pathlib import Path

from .. import db
from ..parse import rutracker as rparse

BASE = rparse.BASE
CACHE = db.ROOT / "data" / "cache" / "rutracker"
INGEST_DIR = db.ROOT / "data" / "cache" / "ingest"
JS = db.ROOT / "films" / "browser" / "rutracker_collect.js"
BRIDGE_QUERIES = "http://127.0.0.1:8765/queries/rutracker"
DONE_LIST = CACHE / "ingested.json"        # bridge files already ingested (ingest is idempotent anyway)


def topic_url(topic_id) -> str:
    return f"{BASE}/viewtopic.php?t={topic_id}"


# --------------------------------------------------------------------------------------------
# DB helpers
# --------------------------------------------------------------------------------------------

def _retry(fn, *a, tries: int = 8):
    """Run a short write transaction, retrying while another collector holds the lock."""
    for i in range(tries):
        try:
            return fn(*a)
        except sqlite3.OperationalError as e:
            if "locked" not in str(e) and "busy" not in str(e) or i == tries - 1:
                raise
            time.sleep(1.5 * (i + 1))


def _js(v):
    return json.dumps(v, ensure_ascii=False) if v is not None else None


def store_parsed(conn, rid: int, d: dict, version: str) -> None:
    """Same columns films.sources.rutor.parse_pages fills (caller holds the transaction)."""
    from .rutor import h264_equiv, settings
    factors = settings()["codec_efficiency"]
    np = d.get("name_parsed") or {}
    conn.execute(
        """UPDATE releases SET season=?, episodes=?, resolution=?, source_type=?, source_detail=?,
               video_codec=?, video_bit_depth=?, video_kbps=?, h264_equiv_kbps=?, hdr=?, dv_profile=?,
               dv_profile_full=?, dv_fallback=?, is_imax_cut=?, edition=?, is_upscale=?, is_ai_upscale=?,
               best_voice=?, forced_subs=?, parsed=?, parsed_version=?, details_parsed_at=datetime('now')
           WHERE id=?""",
        (d.get("season"), d.get("episodes"), d.get("resolution"), d.get("source_type"), np.get("source_raw"),
         d.get("video_codec"), d.get("video_bit_depth"), d.get("video_kbps"),
         h264_equiv(d.get("video_codec"), d.get("video_bit_depth"), d.get("video_height") or d.get("resolution"),
                    d.get("video_kbps"), factors),
         _js(d.get("hdr")), d.get("dv_profile"), d.get("dv_profile_full"), d.get("dv_fallback"), d.get("is_imax_cut"),
         _js(d.get("edition")), d.get("is_upscale"), 1 if d.get("is_upscale") else None,
         d.get("best_voice"), _js(d.get("forced_subs")), db.dump_parsed(d), version, rid))
    # release rules of the owner's survey (2026-10-25): ads / localized / 3D / TS-CAM / remaster / dub / colour / fps / КПК
    from .rutor import store_rules
    sid, name = conn.execute("SELECT source_id, name FROM releases WHERE id = ?", (rid,)).fetchone()
    store_rules(conn, rid, "rutracker", sid, name, d)


def _parser_version(kind: str) -> str:
    from ..parse import rutor as base
    v = max(Path(base.__file__).stat().st_mtime_ns, Path(rparse.__file__).stat().st_mtime_ns)
    return f"rutracker-{kind}:{v}"


def raw_page(conn, release_id: int) -> str | None:
    row = conn.execute("SELECT body FROM release_raw WHERE release_id=?", (release_id,)).fetchone()
    return zlib.decompress(row[0]).decode("utf-8") if row else None


# --------------------------------------------------------------------------------------------
# queries
# --------------------------------------------------------------------------------------------

def _norm(s: str | None) -> str:
    s = (s or "").casefold().replace("ё", "е")
    return re.sub(r"\s+", " ", re.sub(r"[^\w]+", " ", s)).strip()


def _watched_ids(conn) -> set[int]:
    return {r[0] for r in conn.execute("SELECT title_id FROM watched WHERE source NOT LIKE '%partial'")}


def _names(t: dict) -> list[str]:
    """Original first, then Russian (separate queries, R151); latin spelling only when it differs."""
    out = []
    for n in (t.get("title_original"), t.get("title_latin"), t.get("title_ru")):
        n = re.sub(r"\s+", " ", (n or "")).strip()
        if not n or len(_norm(n)) < 2 or _norm(n) in {_norm(x) for x in out}:
            continue
        try:
            n.encode("cp1251")       # rutracker searches in windows-1251: «名探偵ホームズ» cannot be searched at all
        except UnicodeEncodeError:
            continue
        out.append(n)
    return out


def _needs(rel: dict | None, row: dict | None = None) -> list[str]:
    """What the best rutor release lacks (R9). No release at all -> anything rutracker has. A title already at
    «максимальное качество» (rank.max_quality, R168(в): a dub + the best picture it was ever released in) needs nothing —
    no alternative is searched; a Marvel / DC series whose best release is not a complete FlarrowFilms one needs 'flarrow'."""
    if not rel:
        return ["any"]
    if row is not None and row.get("max_quality"):
        return []
    from ..rank import hdr_class, _j
    need = []
    if row is not None and row.get("voice_pref_all_episodes") and (row.get("episodes_note") or not row.get("voice_pref_hit")):
        need.append("flarrow")
    if (rel.get("resolution") or 0) < 2160:
        need.append("4k")
    if hdr_class(_j(rel.get("hdr"), []), rel.get("dv_static_md")) in ("none", "", None, "SDR"):
        need.append("hdr")
    if (rel.get("best_voice") or "none") != "dub":
        need.append("dub")
    return need


class _Queries:
    def __init__(self):
        self.items: dict[str, dict] = {}

    def add(self, name: str, lo, hi, need: list[str], why: str, title_id=None):
        q = name
        if len(_norm(name)) < 4 and lo and (hi == lo):
            q = f"{name} {lo}"   # «F1», «Up»: the year keeps a short name from returning 500 foreign rows
        k = _norm(q)
        if not k:
            return
        it = self.items.get(k)
        if not it:
            self.items[k] = {"q": q, "name": name, "lo": lo, "hi": hi, "need": list(need), "why": [why],
                             "titles": [title_id] if title_id else []}
            return
        it["lo"] = None if it["lo"] is None or lo is None else min(it["lo"], lo)
        it["hi"] = None if it["hi"] is None or hi is None else max(it["hi"], hi)
        it["need"] = ["any"] if "any" in it["need"] or "any" in need else sorted(set(it["need"]) | set(need))
        if why not in it["why"]:
            it["why"].append(why)
        if title_id and title_id not in it["titles"]:
            it["titles"].append(title_id)

    def add_title(self, t: dict, need: list[str], why: str, open_end: bool = False):
        y = t.get("year")
        for n in _names(t):
            self.add(n, y, None if open_end else y, need, why, t.get("id"))


def build_queries(conn, extras: list[str] = (), limit: int | None = None, log=print) -> list[dict]:
    from .. import watchlist
    qs = _Queries()
    watched = _watched_ids(conn)
    for e in extras:                                   # owner / agent: explicit names
        qs.add(e, None, None, ["any"], "extra")

    # (1) seasons of followed series (franchise «все годы» members, «хочу» list) without any release, already out (R91)
    from ..rank import franchise_cfg
    from ..franchise_seen import _too_old
    frs = {f["key"]: f for f in franchise_cfg() if f.get("scope") == "all"}
    followed = set()
    for key, imdb, kind, year in conn.execute(
            "SELECT franchise_key, imdb_id, kind, year FROM franchise_members WHERE confidence != 'review' AND imdb_id IS NOT NULL"):
        # the owner's per-franchise cut-offs (hide_series_before, animation rules …) as for franchise_releases
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
        qs.add_title(t, ["any"], f"season_gap:{t['id']}")
        n_gap += 1

    # (2) owner's «хочу» list (R48): every unseen title of it — no release → anything, else what the best one lacks
    rank_rows = None
    try:
        from ..rank import compute
        rank_rows = compute(conn)
    except Exception as e:  # noqa: BLE001 — the query list must still be written; reported
        log(f"  rank.compute failed ({e!r}): R9 part skipped")
    best_by_title = {r["title"]["id"]: r for r in (rank_rows or []) if r["variant"] == "Лучшее"}
    n_want = 0
    for tid in sorted(wanted):
        if tid in watched:
            continue
        t = conn.execute("SELECT * FROM titles WHERE id = ?", (tid,)).fetchone()
        if not t:
            continue
        row = best_by_title.get(tid)
        need = _needs(row["release"] if row else None, row)
        if need:
            qs.add_title(dict(t), need, "watchlist", open_end=t["kind"] == "series")
            n_want += 1

    # (3) R44: anime of the owner's franchises without any release yet
    n_anime = 0
    try:
        from ..franchise_releases import todo
        import tomllib
        with open(db.ROOT / "config" / "franchises.toml", "rb") as f:
            anime_keys = {fr["key"] for fr in tomllib.load(f)["franchise"] if fr.get("anime")}
        for w in todo(conn):
            if w["franchise"] not in anime_keys:
                continue
            t = {"id": None, "title_original": w["title"], "title_ru": w["title_ru"], "year": w["year"]}
            qs.add_title(t, ["any"], f"anime:{w['franchise']}:{w['imdb']}", open_end=w["kind"] in ("series", "animated series"))
            n_anime += 1
    except Exception as e:  # noqa: BLE001
        log(f"  franchise anime part failed ({e!r})")

    # (4) R9: visible titles whose best rutor release lacks 4K / HDR-DV / dub, in table order (most wanted first)
    n_r9 = 0
    for r in rank_rows or []:
        if r["variant"] != "Лучшее":
            continue
        hidden = set(r["hidden_reasons"]) - {"нет озвучки"}   # rutracker may have the dub the filter misses
        if hidden or r["title"]["id"] in watched:
            continue
        rel = r["release"]
        if rel.get("source") not in (None, "rutor"):
            continue                                        # already a rutracker / queued release
        need = _needs(rel, r)
        if need:
            qs.add_title(r["title"], need, "r9:" + ",".join(need))
            n_r9 += 1

    out = list(qs.items.values())
    log(f"  queries: {len(out)} (season gaps {n_gap}, watchlist {n_want}, R9 {n_r9}, anime {n_anime}, extra {len(extras)})")
    return out[:limit] if limit else out


def known_topics(conn) -> list[int]:
    """Topics whose page is already stored: the collector does not open them again (search rows still refresh seeds)."""
    return [int(r[0]) for r in conn.execute(
        """SELECT r.source_id FROM releases r JOIN release_raw w ON w.release_id = r.id WHERE r.source = 'rutracker'""")
        if str(r[0]).isdigit()]


def write_run(queries: list[dict], known: list[int], max_topics: int = 40, delay_ms: int = 1500,
              part_size: int = 800, out: Path = CACHE, run_id: str | None = None, base: str | None = None,
              listing: dict | None = None) -> dict:
    """queries.json (full), queries_run.json (for the bridge), run.js (bridge mode), run_inline_N.js (fallback).
    A resume (`run_id` «<base>-rN») keeps queries.json of the base run (it has the reasons) and records `base`.
    listing (listing_job): the date-ordered forum listing the collector walks before the searches (R322)."""
    out.mkdir(parents=True, exist_ok=True)
    run = run_id or datetime.now().strftime("%Y%m%d-%H%M")
    if not base:
        (out / "queries.json").write_text(json.dumps({"run": run, "queries": queries}, ensure_ascii=False, indent=1),
                                          encoding="utf-8")
    # `titles` rides along so that record_sync knows what a clean search covered (R322: a query is sent again only for a
    # new title / season / need)
    compact = [{k: q.get(k) for k in ("q", "lo", "hi", "need", "titles", "since")
                if (k != "since" or q.get("since")) and (k != "titles" or q.get("titles"))}
               for q in queries]
    cfg = {"run": run, "queries": compact, "known": known, "maxTopics": max_topics, "delayMs": delay_ms}
    if base:
        cfg["base"] = base
    if listing:
        cfg["listing"] = listing
    (out / "queries_run.json").write_text(json.dumps(cfg, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    src = JS.read_text(encoding="utf-8")
    (out / "run.js").write_text(src + f"\n__rtCollect({{queriesUrl: {json.dumps(BRIDGE_QUERIES)}}});\n", encoding="utf-8")
    parts = []
    for i in range(0, len(compact), part_size):
        n = i // part_size + 1
        p = out / f"run_inline_{n}.js"
        c = dict(cfg, run=f"{run}-p{n}", queries=compact[i:i + part_size])
        if n > 1:
            c.pop("listing", None)                      # the listing runs once, with the first part
        p.write_text(src + f"\n__rtCollect({json.dumps(c, ensure_ascii=False, separators=(',', ':'))});\n", encoding="utf-8")
        parts.append(p.name)
    if not compact and listing:                         # nothing to search, only the listing: one inline part with it
        p = out / "run_inline_1.js"
        p.write_text(src + f"\n__rtCollect({json.dumps(dict(cfg, run=f'{run}-p1'), ensure_ascii=False, separators=(',', ':'))});\n",
                     encoding="utf-8")
        parts.append(p.name)
    for old in out.glob("run_inline_*.js"):            # stale parts of an earlier, longer list: emptied, not deleted
        if old.name not in parts:
            old.write_text("// superseded by a newer `queries` run\n", encoding="utf-8")
    return {"run": run, "queries": len(compact), "known": len(known), "run_js": str(out / "run.js"),
            "inline_parts": parts, "listing_forums": len((listing or {}).get("forums") or [])}


def resume_queries(run_cfg: dict, payloads: list[dict], retry_empty: bool = True) -> tuple[list[dict], dict]:
    """The tail of a run (owner, 2026-10-26: «надеюсь ру трекер скрипт работает чтобы если сбился … мы смогли и хвосты
    доказать»): queries of `run_cfg` (queries_run.json) not finished cleanly by the payloads of that run and of its earlier
    resumes (run ids `<base>`, `<base>-r1`, …) — never posted (stopped / closed tab), posted with a query-level error
    (site down, fatal), or (retry_empty) with zero results in every search, since a maintenance page parsed as an empty
    search before the collector knew it (2026-10-26)."""
    base = str(run_cfg.get("base") or run_cfg.get("run") or "")
    mine = [p for p in payloads if str(p.get("run") or "") == base or str(p.get("run") or "").startswith(base + "-r")]
    ok = set()
    for p in mine:
        q = p.get("query") or {}
        if p.get("final") or not q:
            continue
        failed = any(e.get("q") for e in p.get("errors") or [])
        empty = bool(p.get("searches")) and all(not s.get("total") for s in p["searches"])
        if not failed and not (retry_empty and empty):
            ok.add((q.get("q"), q.get("lo"), q.get("hi")))
    left = [q for q in run_cfg.get("queries") or [] if (q.get("q"), q.get("lo"), q.get("hi")) not in ok]
    return left, {"base": base, "queries": len(run_cfg.get("queries") or []), "done_ok": len(ok), "left": len(left)}


# --------------------------------------------------------------------------------------------
# ingest
# --------------------------------------------------------------------------------------------

def load_payloads(paths: list[str]) -> list[dict]:
    """Bridge bodies (one JSON object each), or a read-out of __rtChunk (JSON array of JSON strings / objects)."""
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
        items = v if isinstance(v, list) else [v]
        for it in items:
            if isinstance(it, str):
                try:
                    it = json.loads(it)
                except json.JSONDecodeError:
                    continue
            if isinstance(it, dict) and it.get("kind", "rutracker") == "rutracker":
                it["_file"] = str(p)
                out.append(it)
    return out


def _excluded(conn) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT source_id FROM excluded_releases WHERE source='rutracker' AND reason NOT LIKE 'lifted:%'")}


def _upsert(conn, t: str, name: str, category: str, row: dict, topic: dict | None) -> tuple[int, bool]:
    size = (topic or {}).get("size_bytes") or row.get("size")
    seeds = (topic or {}).get("seeders") if (topic or {}).get("seeders") is not None else row.get("seeds")
    leech = (topic or {}).get("leechers") if (topic or {}).get("leechers") is not None else row.get("leech")
    btih = (topic or {}).get("btih")
    added = rparse.parse_reged((topic or {}).get("reged")) or rparse.parse_reged(row.get("added"))
    cur = conn.execute(
        """UPDATE releases SET name=?, size_bytes=coalesce(?, size_bytes), seeders=coalesce(?, seeders),
               leechers=coalesce(?, leechers), btih=coalesce(?, btih), added_at=coalesce(added_at, ?),
               last_seen_at=datetime('now')
           WHERE source='rutracker' AND source_id=?""", (name, size, seeds, leech, btih, added, t))
    if cur.rowcount:
        return conn.execute("SELECT id FROM releases WHERE source='rutracker' AND source_id=?", (t,)).fetchone()[0], False
    cur = conn.execute(
        """INSERT INTO releases (source, source_id, url, btih, name, category, added_at, size_bytes, seeders, leechers)
           VALUES ('rutracker', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (t, topic_url(t), btih, name, category, added, size, seeds, leech))
    return cur.lastrowid, True


STATE = CACHE / "state.json"
SINCE_OVERLAP = 2 * 86400


def _qkey(q: dict) -> str:
    return f"{_norm(q.get('q'))}|{q.get('lo')}|{q.get('hi')}"


def record_sync(payloads: list[dict], path: Path = STATE) -> int:
    """The last successful sync of every query (R207 / R212, owner 2026-10-26: «поставим фильтр на сканер только на новые
    от даты последнего сканирования», «для всех источников достаточно смотреть обновления только с последней даты … об
    успешной синхронизации»): a payload without a query-level error moves its point to the newest registration time it saw
    (rutracker's `added`, unix seconds). A failed query keeps the old point."""
    try:
        st = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = {}
    pts = st.setdefault("queries", {})
    n = 0
    for p in payloads:
        q = p.get("query") or {}
        if p.get("final") or not q or any(e.get("q") for e in p.get("errors") or []):
            continue
        newest = max((int(r["added"]) for s in p.get("searches") or [] for r in s.get("rows") or [] if r.get("added")), default=0)
        k = _qkey(q)
        old = pts.get(k) or {}
        ent = {"at": p.get("at"), "newest": max(newest, int(old.get("newest") or 0))}
        # what this clean search covered (R322): titles and needs; a payload of an older collector (no titles) keeps the
        # old coverage, an entry without any is adopted by the next `queries` (pending)
        if q.get("titles") is not None or "titles" in old:
            ent["titles"] = sorted(set(old.get("titles") or []) | set(q.get("titles") or []))
            ent["need"] = _need_union(old.get("need"), q.get("need"))
        pts[k] = ent
        n += 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(st, ensure_ascii=False, indent=0), encoding="utf-8")
    return n


def with_since(queries: list[dict], path: Path = STATE, full: bool = False) -> list[dict]:
    """Queries with a sync point get `since` = its newest registration time minus 2 days: the collector then searches
    newest first and stops below it (a query never synced, or `--full`, is searched in full). A query sent again
    because it now covers a new title / season / need (pending: `send` = 'changed') is searched in full too — the new
    title's releases may be older than the sync point."""
    try:
        pts = json.loads(path.read_text(encoding="utf-8")).get("queries", {})
    except (OSError, ValueError):
        pts = {}
    out = []
    for q in queries:
        q = dict(q)
        p = pts.get(_qkey(q))
        if not full and q.get("send") != "changed" and p and p.get("newest"):
            q["since"] = int(p["newest"]) - SINCE_OVERLAP
        else:
            q.pop("since", None)
        out.append(q)
    return out


# --------------------------------------------------------------------------------------------
# R322: each query once; new releases of searched titles come from the forum listing
# --------------------------------------------------------------------------------------------

def _need_union(a, b) -> list[str]:
    a, b = list(a or []), list(b or [])
    return ["any"] if "any" in a or "any" in b else sorted(set(a) | set(b))


def need_covered(new, old) -> bool:
    """A search done for needs `old` already selected every row a search for `new` would ('any' covers everything)."""
    old, new = set(old or []), set(new or [])
    if "any" in old:
        return True
    return "any" not in new and new <= old


def load_state(path: Path = STATE) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(st: dict, path: Path = STATE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(st, ensure_ascii=False, indent=0), encoding="utf-8")


def pending(queries: list[dict], st: dict, again: bool = False) -> tuple[list[dict], dict]:
    """R212 / R322 (owner, 2026-11-13: «ищем только всё новое для новых дат и новых фильмов и не трогаем ничего старого на
    что материал уже найден»): the queries to SEND this run. A query with a clean search in the state (record_sync) is not
    sent again; new releases of its titles come from the forum listing (listing_job). Sent: never searched ('new'), or now
    covering a title / season or a need its last clean search did not ('changed' — searched in full), the owner's / agent's
    explicit `--extra` names ('extra'), everything with `again` (only on the owner's request; 'again' keeps `since`).
    A state entry of the time before R322 (no titles / need) is adopted as covering what the query has now — mutates `st`
    (the caller saves it). Returns (queries with `send`, counts)."""
    pts = st.setdefault("queries", {})
    info = {"built": len(queries), "new": 0, "changed": 0, "extra": 0, "again": 0, "skipped": 0, "adopted": 0}
    out = []
    for q in queries:
        k, ent = _qkey(q), pts.get(_qkey(q))
        if "extra" in (q.get("why") or []):
            why = "extra"
        elif not ent:
            why = "new"
        else:
            if "titles" not in ent:
                ent["titles"] = sorted(set(q.get("titles") or []))
                ent["need"] = list(q.get("need") or [])
                info["adopted"] += 1
            covered = set(q.get("titles") or []) <= set(ent.get("titles") or []) and need_covered(q.get("need"), ent.get("need"))
            why = "changed" if not covered else "again" if again else None
        if why is None:
            info["skipped"] += 1
            continue
        info[why] += 1
        out.append(dict(q, send=why))
    return out, info


# The video forums rutracker's listing walks (R322). Taken from where the stored rutracker releases live (the DB, 2026-11-13:
# forum ids of `parsed.rutracker.forum_id` with a category, without Russian / mobile / DVD-only / 3D ones). ANIME: rows only
# for tracked titles (anime is collected for the owner's franchises only, R44).
LISTING_FORUMS: list[tuple[int, str]] = [
    (313, "Зарубежное кино (HD Video)"), (1457, "Зарубежное кино (UHD Video)"), (252, "Фильмы 2026"),
    (1950, "Фильмы 2021-2025"), (2200, "Фильмы 2016-2020"), (2201, "Азиатские фильмы (HD Video)"),
    (272, "Азиатские фильмы (UHD Video)"), (934, "Азиатские фильмы"), (2339, "Арт-хаус и авторское кино (HD Video)"),
    (271, "Арт-хаус и авторское кино (UHD Video)"), (775, "Классика мирового кинематографа (UHD Video)"),
    (1669, "Сериалы США и Канады (UHD Video)"), (266, "Сериалы США и Канады (HD Video)"), (235, "Сериалы США и Канады"),
    (1171, "Новинки и сериалы в стадии показа (UHD Video)"), (1803, "Новинки и сериалы в стадии показа (HD Video)"),
    (842, "Новинки и сериалы в стадии показа"), (173, "Сериалы совместного производства нескольких стран (UHD Video)"),
    (1288, "Сериалы совместного производства нескольких стран (HD Video)"),
    (387, "Сериалы совместного производства нескольких стран"), (2393, "Сериалы Великобритании и Ирландии (UHD Video)"),
    (193, "Сериалы Великобритании и Ирландии (HD Video)"), (242, "Сериалы Великобритании и Ирландии"),
    (625, "Европейские сериалы (UHD Video)"), (1459, "Европейские сериалы (HD Video)"), (820, "Азиатские сериалы (UHD Video)"),
    (1242, "Корейские сериалы (HD Video)"), (1949, "Сериалы Австралии и Новой Зеландии (UHD Video)"),
    (119, "Зарубежные сериалы (UHD Video)"), (2366, "Зарубежные сериалы (HD Video)"),
    (930, "Иностранные мультфильмы (HD Video)"), (209, "Иностранные мультфильмы"), (84, "Мультфильмы (UHD Video)"),
    (4, "Мультфильмы"), (2365, "Иностранные короткометражные мультфильмы (HD Video)"), (1460, "Мультсериалы (HD Video)"),
    (921, "Мультсериалы"), (498, "Мультсериалы (UHD Video)"), (815, "Мультсериалы (SD Video)"),
    (1105, "Аниме (HD Video)"), (1389, "Аниме (SD Video)"), (2491, "Аниме (QC подраздел)"),
]
LISTING_DEFAULT_DAYS = 30      # a forum never listed, and no «обнови базу» finished yet: this far back


def listing_names(title: str | None) -> list[str]:
    """Normalized names of a listing row (Python twin of titleNames() in rutracker_collect.js and of
    nnmclub.old_title_hit): the part before the year, bracketed bits dropped, split by «/»."""
    head = re.split(r"[(\[](?:19|20)\d{2}", title or "")[0]
    head = re.sub(r"\([^()]*\)|\[[^\]]*\]", " ", head)
    return [k for k in (_norm(n) for n in re.split(r"\s*/\s*", head)) if k]


def listing_index(queries: list[dict]) -> dict[str, list]:
    """Every built query — sent or not — by name: normalized name -> [[lo, hi, need], …]. A listing row of one of these
    names in its year window (±1) showing a needed feature is a release of a tracked title (as a search row would be)."""
    idx: dict[str, list] = {}
    for q in queries:
        k = _norm(q.get("name") or q.get("q"))
        if len(k) < 2:
            continue
        e = [q.get("lo"), q.get("hi"), list(q.get("need") or [])]
        if e not in idx.setdefault(k, []):
            idx[k].append(e)
    return idx


_NEED_PY = {
    "4k": re.compile(r"(?i)(^|[^\w])(2160p|4K|UHD)([^\w]|$)"),
    "hdr": re.compile(r"(?i)(^|[^\w])(HDR10\+?|HDR|Dolby\s*Vision|DV|DoVi|HLG)([^\w]|$)"),
    "dub": re.compile(r"(^|[^\w])(Dub|DUB)([^\w]|$)|Дубл"),
    "flarrow": re.compile(r"(?i)Flarrow"),
}
_ROW_YEARS = (re.compile(r"\[\s*((?:19|20)\d{2})(?:\s*[-–—]\s*((?:19|20)?\d{2}))?"),
              re.compile(r"\(((?:19|20)\d{2})(?:\s*[-–—]\s*((?:19|20)?\d{2}))?\)"))


def row_years(title: str | None) -> tuple[int, int] | None:
    """Python twin of rowYears() in rutracker_collect.js."""
    m = _ROW_YEARS[0].search(title or "") or _ROW_YEARS[1].search(title or "")
    if not m:
        return None
    a = int(m.group(1))
    b2 = m.group(2)
    b = int(m.group(1)[:2] + b2 if b2 and len(b2) == 2 else b2) if b2 else a
    return min(a, b), max(a, b)


def _needs_match(title: str, need) -> bool:
    if not need or "any" in need:
        return True
    return any(_NEED_PY[n].search(title or "") for n in need if n in _NEED_PY)


def listing_match(title: str | None, forum: str | None, index: dict, year_from: int | None,
                  anime_only_tracked: bool = False) -> str | None:
    """Python twin of listingMatch() in rutracker_collect.js: 'tracked' (a built query's name, year window ±1, a needed
    feature in the title — the topic page is opened), 'year' (any video row of year_from or later — stored from its
    title, as a selected search row without its page), None (not taken). Anime (an anime forum or «[ANIME] …») — tracked
    titles only (R44)."""
    f = (forum or "").strip()
    if not f or rparse._NOT_VIDEO.search(f) or not rparse._VIDEO.search(f):     # = videoForum() in the collector
        return None
    y = row_years(title)
    if not y:
        return None
    for n in listing_names(title):
        for lo, hi, need in index.get(n) or []:
            lo_, hi_ = 0 if lo is None else lo, 9999 if hi is None else hi
            if y[1] < lo_ - 1 or y[0] > hi_ + 1:
                continue
            if _needs_match(title, need):
                return "tracked"
    if anime_only_tracked or re.search(r"аниме|anime", forum or "", re.I) or re.match(r"\s*\[ANIME\]", title or "", re.I):
        return None
    return "year" if year_from and y[1] >= year_from else None


def _unix(s: str) -> int:
    from datetime import timezone
    return int(datetime.fromisoformat(s.replace(" ", "T")).replace(tzinfo=timezone.utc).timestamp())


def listing_since(st: dict, fid, conn=None, now: float | None = None) -> int:
    """Unix seconds the listing of forum fid reads back to: its last clean listing's newest registration minus 2 days;
    else the last «обнови базу» (db.last_update_since, already minus 2 days); else LISTING_DEFAULT_DAYS back."""
    p = (st.get("listing") or {}).get(str(fid)) or {}
    if p.get("newest"):
        return int(p["newest"]) - SINCE_OVERLAP
    s = db.last_update_since(conn) if conn is not None else None
    if s:
        return _unix(s)
    return int((now or time.time()) - LISTING_DEFAULT_DAYS * 86400)


def listing_job(conn, queries: list[dict], st: dict, year_from: int | None = None, forums=None,
                max_topics: int = 40, max_pages: int = 10) -> dict:
    """The `listing` section of the job (R322): forums (id, name, since, anime → tracked titles only), the tracked-title
    index of ALL built queries, year_from (rows of it and later are stored from their title), the per-forum topic cap
    and page cap (50 rows a page, rutracker's 500-result cap = 10 pages)."""
    year_from = year_from or min(db.years())
    fs = []
    for fid, name in forums or LISTING_FORUMS:
        fs.append({"f": int(fid), "name": name, "since": listing_since(st, fid, conn),
                   "trackedOnly": bool(re.search(r"аниме|anime", name, re.I))})
    return {"forums": fs, "titles": listing_index(queries), "yearFrom": year_from, "maxTopics": max_topics,
            "maxPages": max_pages}


def record_listing(payloads: list[dict], path: Path = STATE) -> int:
    """A clean listing payload (no forum-level error) moves its forum's point to the newest registration it saw."""
    st = load_state(path)
    pts = st.setdefault("listing", {})
    n = 0
    for p in payloads:
        lst = p.get("listing") or {}
        if p.get("final") or not lst.get("f") or any(e.get("q") for e in p.get("errors") or []):
            continue
        newest = max((int(r["added"]) for s in p.get("searches") or [] for r in s.get("rows") or [] if r.get("added")), default=0)
        old = pts.get(str(lst["f"])) or {}
        pts[str(lst["f"])] = {"at": p.get("at"), "newest": max(newest, int(old.get("newest") or 0)) or None,
                              "incomplete": bool(p.get("incomplete"))}   # incomplete: the 500 cap came before `since`
        n += 1
    save_state(st, path)
    return n


def ingest(conn, payloads: list[dict]) -> dict:
    st = {"payloads": 0, "queries": 0, "rows": 0, "selected": 0, "topics": 0, "new": 0, "updated": 0,
          "pages_stored": 0, "name_only": 0, "seeds_refreshed": 0, "skipped_forum": 0, "dv5_excluded": 0,
          "dv5_already_stored": [], "excluded_skipped": 0, "errors": 0, "error_samples": [], "capped": [],
          "fatal": [], "finals": 0, "listing_forums": 0, "listing_rows": 0, "listing_incomplete": []}
    v_page, v_name = _parser_version("page"), _parser_version("title")
    excluded = _excluded(conn)
    for p in payloads:
        st["payloads"] += 1
        if p.get("final"):
            st["finals"] += 1
            if p.get("fatal"):
                st["fatal"].append(p["fatal"])
            continue
        if p.get("listing"):          # R322: a forum of the date-ordered listing — its rows are taken like search rows
            st["listing_forums"] += 1
            st["listing_rows"] += sum(len(s.get("rows") or []) for s in p.get("searches") or [])
            if p.get("incomplete"):
                st["listing_incomplete"].append(p["listing"].get("f"))
        else:
            st["queries"] += 1
        for e in p.get("errors") or []:
            st["errors"] += 1
            if len(st["error_samples"]) < 10:
                st["error_samples"].append({k: (str(v)[:200] if k == "snippet" else v) for k, v in e.items()})
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
            if t in excluded:
                st["excluded_skipped"] += 1
                continue
            row, topic = rows.get(t, {}), topics.get(t)
            forum = (topic or {}).get("forum") or row.get("forum")
            category = rparse.category_for(forum, (topic or {}).get("title") or row.get("title"))
            if not category:
                st["skipped_forum"] += 1
                continue
            name = (topic or {}).get("title") or row.get("title")
            if not name:
                continue
            raw, d = None, None
            if topic and topic.get("post_html"):
                tp = dict(topic, title=name, size_bytes=topic.get("size_bytes") or row.get("size"),
                          seeders=topic.get("seeders") if topic.get("seeders") is not None else row.get("seeds"))
                raw = rparse.compact_topic_html(tp)
                d = rparse.parse_topic(raw)
            # DV5-only by the page, or by the title alone («WEB-DL 2160p, Dolby Vision TV»: 27 slipped into the table
            # before 2026-10-28) — never stored
            if (d if d is not None else rparse.parse_title_only(name, forum)).get("dv5_only"):
                def _ex():
                    with conn:
                        conn.execute("INSERT OR IGNORE INTO excluded_releases (source, source_id, reason, name) "
                                     "VALUES ('rutracker', ?, 'dv5_only', ?)", (t, name))
                        return conn.execute("SELECT id FROM releases WHERE source='rutracker' AND source_id=?",
                                            (t,)).fetchone()
                old = _retry(_ex)
                excluded.add(t)
                st["dv5_excluded"] += 1
                if old:   # stored earlier from a search row only: never deleted, reported for the owner
                    st["dv5_already_stored"].append(old[0])
                continue

            def _write():
                with conn:
                    rid, new = _upsert(conn, t, name, category, row, topic)
                    if raw is not None:
                        conn.execute("INSERT OR REPLACE INTO release_raw (release_id, body) VALUES (?, ?)",
                                     (rid, zlib.compress(raw.encode("utf-8"), 9)))
                        store_parsed(conn, rid, d, v_page)
                        return new, "page"
                    has_raw = conn.execute("SELECT 1 FROM release_raw WHERE release_id=?", (rid,)).fetchone()
                    parsed = conn.execute("SELECT parsed FROM releases WHERE id=?", (rid,)).fetchone()[0]
                    if not has_raw and not parsed:
                        store_parsed(conn, rid, rparse.parse_title_only(name, forum), v_name)
                        return new, "name"
                    return new, "seeds"
            new, how = _retry(_write)
            st["new" if new else "updated"] += 1
            st[{"page": "pages_stored", "name": "name_only", "seeds": "seeds_refreshed"}[how]] += 1
        # rows the collector did not select: only refresh releases we already have (no new rows from them)
        def _refresh():
            n = 0
            with conn:
                for t, r in rows.items():
                    if r.get("sel") or t in topics:
                        continue
                    n += conn.execute(
                        """UPDATE releases SET seeders=coalesce(?, seeders), leechers=coalesce(?, leechers),
                               size_bytes=coalesce(?, size_bytes), last_seen_at=datetime('now')
                           WHERE source='rutracker' AND source_id=?""",
                        (r.get("seeds"), r.get("leech"), r.get("size"), t)).rowcount
            return n
        st["seeds_refreshed"] += _retry(_refresh)
    st["capped"] = sorted(set(q for q in st["capped"] if q))
    st["fatal"] = sorted(set(st["fatal"]))
    return st


def reparse(conn, all_: bool = False) -> dict:
    """Re-run the parser over stored rutracker pages (after a parser change)."""
    v = _parser_version("page")
    rows = conn.execute(
        f"""SELECT r.id, r.source_id, r.name FROM releases r JOIN release_raw w ON w.release_id = r.id
            WHERE r.source='rutracker' {'' if all_ else "AND (r.parsed IS NULL OR r.parsed_version NOT LIKE 'rutracker-page:%')"}"""
    ).fetchall()
    st = {"parsed": 0, "failed": 0, "dv5_found": []}
    for rid, sid, name in rows:
        try:
            d = rparse.parse_topic(raw_page(conn, rid))
        except Exception as e:  # noqa: BLE001 — recorded and reported
            st["failed"] += 1
            print(f"  PARSE FAILED {sid}: {e!r}", file=sys.stderr, flush=True)
            continue

        def _w():
            with conn:
                if d.get("dv5_only"):
                    conn.execute("INSERT OR IGNORE INTO excluded_releases (source, source_id, reason, name) "
                                 "VALUES ('rutracker', ?, 'dv5_only', ?)", (sid, name))
                store_parsed(conn, rid, d, v)
        _retry(_w)
        if d.get("dv5_only"):
            st["dv5_found"].append(rid)   # not deleted (no DELETE in this module) — reported
        st["parsed"] += 1
    return st


def status(conn) -> dict:
    q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    return {
        "releases": q("SELECT count(*) FROM releases WHERE source='rutracker'"),
        "with_page": q("SELECT count(*) FROM releases r JOIN release_raw w ON w.release_id=r.id WHERE r.source='rutracker'"),
        "parsed": q("SELECT count(*) FROM releases WHERE source='rutracker' AND parsed IS NOT NULL"),
        "linked": q("SELECT count(DISTINCT r.id) FROM releases r JOIN release_titles t ON t.release_id=r.id WHERE r.source='rutracker'"),
        "excluded_dv5": q("SELECT count(*) FROM excluded_releases WHERE source='rutracker'"),
        "bridge_files": len(glob.glob(str(INGEST_DIR / "rutracker-*.txt"))),
        "ingested_files": len(json.loads(DONE_LIST.read_text(encoding="utf-8"))) if DONE_LIST.exists() else 0,
    }


def _ingest_paths(args: list[str], all_: bool) -> list[str]:
    done = set(json.loads(DONE_LIST.read_text(encoding="utf-8"))) if DONE_LIST.exists() else set()
    paths = []
    for a in args or [str(INGEST_DIR / "rutracker-*.txt")]:
        p = Path(a)
        found = sorted(glob.glob(str(p / "rutracker-*.txt"))) if p.is_dir() else sorted(glob.glob(a)) or ([a] if p.exists() else [])
        paths += found
    if not args and not all_:
        paths = [p for p in paths if Path(p).name not in done]
    return list(dict.fromkeys(paths))


def main(argv: list[str]) -> None:
    cmd = argv[0] if argv else "status"
    if cmd in ("queries", "resume"):
        # they rewrite queries_run.json / run.js that the bridge serves (2026-10-26: an agent's `resume` at 02:39 rewrote
        # them while mine was planned) — one at a time
        from .. import locks
        with locks.lock("rutracker_run", timeout=1800):
            return _main(argv, cmd)
    return _main(argv, cmd)


def _main(argv: list[str], cmd: str) -> None:
    conn = db.connect()
    db.migrate(conn)
    log = lambda m: print(m, file=sys.stderr, flush=True)  # noqa: E731
    if cmd == "queries":
        extras = [argv[i + 1] for i, a in enumerate(argv) if a == "--extra" and i + 1 < len(argv)]
        opt = lambda k, d: int(argv[argv.index(k) + 1]) if k in argv else d  # noqa: E731
        built = build_queries(conn, extras, opt("--limit", None), log=log)
        # R322: a query searched cleanly before is not sent again (its new releases come from the listing); `--again` —
        # all of them, only on the owner's request; `--no-listing` — searches only
        state = load_state()
        send, info = pending(built, state, again="--again" in argv)
        if info["adopted"]:
            save_state(state)
        qs = with_since(send, full="--full" in argv)
        log(f"  R322: built {info['built']}, sent {len(qs)} (new {info['new']}, changed {info['changed']}, "
            f"extra {info['extra']}, again {info['again']}), not sent again {info['skipped']}"
            f"{', adopted ' + str(info['adopted']) if info['adopted'] else ''}; "
            f"incremental: {sum(1 for q in qs if q.get('since'))} with a sync point")
        listing = None if "--no-listing" in argv else listing_job(conn, built, state,
                                                                   max_topics=opt("--listing-topics", 40))
        res = write_run(qs, known_topics(conn), max_topics=opt("--max-topics", 40), delay_ms=opt("--delay-ms", 1500),
                        part_size=opt("--part-size", 800), listing=listing)
        res["r322"] = info
        print(json.dumps(res, ensure_ascii=False, indent=1))
    elif cmd == "resume":
        # tail of the last run: what it did not finish cleanly → a new run «<base>-rN» with fresh known topics; the
        # owner pastes the new run.js (after relogin / maintenance). `--keep-empty`: do not re-ask zero-result queries.
        cfg = json.loads((CACHE / "queries_run.json").read_text(encoding="utf-8"))
        left, info = resume_queries(cfg, load_payloads(sorted(glob.glob(str(INGEST_DIR / "rutracker-*.txt")))),
                                    retry_empty="--keep-empty" not in argv)
        base = info["base"]
        n = 1 + sum(1 for p in CACHE.glob(f"queries_run_{base}-r*.json"))
        saved = CACHE / f"queries_run_{cfg.get('run')}.json"   # the run being replaced is kept, not overwritten
        if not saved.exists():
            saved.write_text(json.dumps(cfg, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        # `--delay-ms N`: a slower pace for the tail (R231: Cloudflare checks repeated → slow down)
        delay = int(argv[argv.index("--delay-ms") + 1]) if "--delay-ms" in argv else cfg.get("delayMs", 1500)
        res = write_run(left, known_topics(conn), max_topics=cfg.get("maxTopics", 40), delay_ms=delay,
                        run_id=f"{base}-r{n}", base=base)
        (CACHE / f"queries_run_{base}-r{n}.json").write_text((CACHE / "queries_run.json").read_text(encoding="utf-8"),
                                                              encoding="utf-8")
        print(json.dumps(dict(info, **res), ensure_ascii=False, indent=1))
    elif cmd == "ingest":
        args = [a for a in argv[1:] if not a.startswith("--")]
        paths = _ingest_paths(args, "--all" in argv)
        pays = load_payloads(paths)
        st = ingest(conn, pays)
        st["sync_points"] = record_sync(pays)
        st["listing_points"] = record_listing(pays)
        if st["listing_incomplete"]:
            log(f"!! listing hit rutracker's 500-result cap before the sync point in forums {st['listing_incomplete']}: "
                f"older new topics of these forums were not read (split the forum or run the listing more often)")
        st["files"] = len(paths)
        CACHE.mkdir(parents=True, exist_ok=True)
        done = set(json.loads(DONE_LIST.read_text(encoding="utf-8"))) if DONE_LIST.exists() else set()
        done |= {Path(p).name for p in paths if Path(p).parent == INGEST_DIR}
        DONE_LIST.write_text(json.dumps(sorted(done), ensure_ascii=False, indent=0), encoding="utf-8")
        if "--match" in argv:
            from ..match import link_exact_names, match_releases
            st["match"] = match_releases(conn)
            st["match_exact_names"] = link_exact_names(conn)
        if st["pages_stored"] or st["name_only"]:
            db.mark_sync(conn, "rutracker", None, {k: v for k, v in st.items() if isinstance(v, int)})
        print(json.dumps(st, ensure_ascii=False, indent=1))
        if st["fatal"]:
            log(f"!! collector stopped: {st['fatal']} — the owner must log in to rutracker in Chrome / pass Cloudflare")
        if not st.get("match"):
            log("next: python -X utf8 -m films match   (links the new releases to titles), then rebuild the sheet")
    elif cmd == "parse":
        print(json.dumps(reparse(conn, "--reparse" in argv), ensure_ascii=False, indent=1))
    elif cmd == "status":
        print(json.dumps(status(conn), ensure_ascii=False, indent=1))
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
