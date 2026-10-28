"""DB side shared by the Ukrainian trackers (R206): toloka.to (priority 6, films/sources/toloka.py) and mazepa.to
(priority 7, films/sources/mazepa.py; before them: rutor, rutracker, nnmclub, kinozal, rustorka).

A topic becomes a `releases` row (source 'toloka' / 'mazepa', source_id = topic id, url = the topic) exactly like a
rutor / rutracker release: the first post (description + MediaInfo) goes to `release_raw` as the compact document of
films.parse.ua_trackers.compact_topic_html, the rutor parser's output fills the same quality / audio columns and the
release rules of the owner's survey (store_rules: 3D / TS-CAM / КПК → excluded_releases). A topic seen only in a listing
or a search row is stored from its title (parse_title_only) and gets its page later. Releases with only Dolby Vision
Profile 5 (no HDR10 fallback) are never inserted (excluded_releases, reason 'dv5_only'); one found DV5-only later by its
page is removed after the sync (db.purge_dv5). Nothing else is deleted.
Matching to titles is the normal step: `python -X utf8 -m films match`.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
import zlib
from pathlib import Path

from ..parse import ua_trackers as uparse

SOURCES = ("toloka", "mazepa")


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


def parser_version(kind: str) -> str:
    from ..parse import rutor as base
    from ..parse import rutracker as rt
    v = max(Path(base.__file__).stat().st_mtime_ns, Path(rt.__file__).stat().st_mtime_ns,
            Path(uparse.__file__).stat().st_mtime_ns)
    return f"ua-{kind}:{v}"


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
    """source_id -> {'id', 'name', 'has_raw', 'parsed_name'} of the releases already stored for this tracker
    (parsed_name = the title the stored parse was made from: the page's title when a page is stored)."""
    return {str(r[0]): {"id": r[1], "name": r[2], "has_raw": bool(r[3]), "parsed_name": r[4]} for r in conn.execute(
        """SELECT r.source_id, r.id, r.name, w.release_id,
                  CASE WHEN json_valid(r.parsed) THEN json_extract(r.parsed, '$.name') END
           FROM releases r LEFT JOIN release_raw w ON w.release_id = r.id WHERE r.source = ?""", (source,))}


def _upsert(conn, source: str, sid: str, name: str, category: str, meta: dict) -> tuple[int, bool]:
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
        (source, sid, uparse.topic_url(source, sid), meta.get("btih"), name, category, meta.get("added_at"),
         meta.get("size_bytes"), meta.get("seeders"), meta.get("leechers")))
    return cur.lastrowid, True


def _exclude_dv5(conn, source: str, sid: str, name: str) -> int | None:
    def _ex():
        with conn:
            conn.execute("INSERT OR IGNORE INTO excluded_releases (source, source_id, reason, name) VALUES (?, ?, 'dv5_only', ?)",
                         (source, sid, name))
            row = conn.execute("SELECT id FROM releases WHERE source=? AND source_id=?", (source, sid)).fetchone()
            return row[0] if row else None
    return _retry(_ex)


def new_stats() -> dict:
    return {"new": 0, "updated": 0, "pages_stored": 0, "name_only": 0, "meta_refreshed": 0, "dv5_excluded": 0,
            "dv5_already_stored": [], "excluded_skipped": 0, "rule_excluded": {}, "skipped_category": 0,
            "ukr_dub": 0, "parse_failed": 0}


def store_topic(conn, source: str, sid: str, name: str, category: str | None, topic: dict | None, st: dict,
                excl: set[str], today=None) -> int | None:
    """One topic: with its page (topic = split_topic output + fetched_at) → release_raw + full parse; without → name only
    (an existing parse is kept). Returns the release id (None when not stored)."""
    sid = str(sid)
    if sid in excl:
        st["excluded_skipped"] += 1
        return None
    raw, d = None, None
    if topic and topic.get("post_html"):
        tp = dict(topic, title=name, topic_id=sid)
        raw = uparse.compact_topic_html(source, tp)
        try:
            d = uparse.parse_topic(source, raw, today)
        except Exception as e:  # noqa: BLE001 — reported, the topic is stored by name
            st["parse_failed"] += 1
            print(f"  PARSE FAILED {source} {sid}: {e!r}", file=sys.stderr, flush=True)
            raw, d = None, None
        category = category or (d or {}).get("category")
    if not category:
        st["skipped_category"] += 1
        return None
    if d is None:
        d_name = uparse.parse_title_only(name, category)
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
    meta = {}
    if topic:
        meta = {"size_bytes": topic.get("size_bytes"), "seeders": topic.get("seeders"), "leechers": topic.get("leechers"),
                "btih": topic.get("btih"), "added_at": (d or {}).get("added_at") or uparse.parse_ua_date(topic.get("reged"), today)}

    def _write():
        with conn:
            rid, new = _upsert(conn, source, sid, name, category, meta)
            if raw is not None:
                conn.execute("INSERT OR REPLACE INTO release_raw (release_id, body) VALUES (?, ?)",
                             (rid, zlib.compress(raw.encode("utf-8"), 9)))
                return rid, new, "page", store_parsed(conn, rid, d, parser_version("page"), source)
            has_raw = conn.execute("SELECT 1 FROM release_raw WHERE release_id=?", (rid,)).fetchone()
            if not has_raw:
                return rid, new, "name", store_parsed(conn, rid, d_name, parser_version("title"), source)
            return rid, new, "meta", None
    rid, new, how, rule = _retry(_write)
    st["new" if new else "updated"] += 1
    st[{"page": "pages_stored", "name": "name_only", "meta": "meta_refreshed"}[how]] += 1
    if rule:
        st["rule_excluded"][rule] = st["rule_excluded"].get(rule, 0) + 1
    if how == "page" and "dub" in (d.get("voice_by_lang") or {}).get("uk", []):
        st["ukr_dub"] += 1
    return rid


def reparse(conn, source: str, all_: bool = False) -> dict:
    """Re-run the parser over stored pages of this tracker (after a parser change)."""
    v = parser_version("page")
    rows = conn.execute(
        f"""SELECT r.id, r.source_id, r.name FROM releases r JOIN release_raw w ON w.release_id = r.id
            WHERE r.source = ? {'' if all_ else "AND (r.parsed IS NULL OR r.parsed_version IS NOT ?)"}""",
        (source,) if all_ else (source, v)).fetchall()
    st = {"parsed": 0, "failed": 0, "dv5_found": []}
    for rid, sid, name in rows:
        try:
            d = uparse.parse_topic(source, raw_page(conn, rid))
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
        _retry(_w)
        if d.get("dv5_only"):
            st["dv5_found"].append(rid)   # not deleted — reported
        st["parsed"] += 1
    return st


def status(conn, source: str) -> dict:
    q = lambda sql: conn.execute(sql, (source,)).fetchone()[0]  # noqa: E731
    return {
        "releases": q("SELECT count(*) FROM releases WHERE source=?"),
        "with_page": q("SELECT count(*) FROM releases r JOIN release_raw w ON w.release_id=r.id WHERE r.source=?"),
        "parsed": q("SELECT count(*) FROM releases WHERE source=? AND parsed IS NOT NULL"),
        "linked": q("SELECT count(DISTINCT r.id) FROM releases r JOIN release_titles t ON t.release_id=r.id WHERE r.source=?"),
        "titles": q("SELECT count(DISTINCT t.title_id) FROM releases r JOIN release_titles t ON t.release_id=r.id WHERE r.source=?"),
        "ukr_dub": q("""SELECT count(*) FROM releases WHERE source=? AND parsed IS NOT NULL AND json_valid(parsed)
                        AND json_extract(parsed, '$.voice_by_lang.uk') LIKE '%"dub"%'"""),
        "excluded": q("SELECT count(*) FROM excluded_releases WHERE source=? AND reason NOT LIKE 'lifted:%'"),
    }
