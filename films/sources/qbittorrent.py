"""qBittorrent: what the owner put on download (R47, 2026-10-23).

Run: python -X utf8 -m films.sources.qbittorrent [sync|report]

Read-only: the client's own state folder %LOCALAPPDATA%\\qBittorrent\\BT_backup — one <infohash>.fastresume
(libtorrent resume data: piece bitmap → progress, paused, added/completed time, save path, trackers) and
<infohash>.torrent (name, size, 'comment' = the tracker topic URL) per torrent currently in the client.
Nothing is written there and the client keeps running.  (E:\\temp holds a copy of every .torrent ever added —
history, not the queue; not used for the status.)

A torrent is linked to a title by, in order: the info-hash = releases.btih (exact), the topic URL = releases.url,
the name (scene names, «Title.2024.2160p…», also Russian written in Latin letters — «PodOgnem» = «Под огнём»)
via the titles in the DB and TMDB search; a film found only on TMDB gets a title (the queue is a base of the DB,
like watched / bought / downloaded).  Queued = in the client and not complete → the sheet shows the title green
with white text, «В очереди» in «У меня»; sorting is unchanged (owner).
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from .. import db

BT_BACKUP = Path(os.path.expandvars(r"%LOCALAPPDATA%\qBittorrent\BT_backup"))


def bdecode(b: bytes):
    """Minimal bencode decoder (iterative over one buffer; dict keys stay bytes)."""
    def dec(i):
        c = b[i]
        if c == 0x69:  # i
            j = b.index(b"e", i)
            return int(b[i + 1:j]), j + 1
        if c == 0x6C:  # l
            i += 1
            out = []
            while b[i] != 0x65:
                v, i = dec(i)
                out.append(v)
            return out, i + 1
        if c == 0x64:  # d
            i += 1
            out = {}
            while b[i] != 0x65:
                k, i = dec(i)
                v, i = dec(i)
                out[k] = v
            return out, i + 1
        j = b.index(b":", i)
        n = int(b[i:j])
        return b[j + 1:j + 1 + n], j + 1 + n
    return dec(0)[0]


def _s(v) -> str:
    return v.decode("utf-8", "replace") if isinstance(v, bytes) else (v or "")


def _ts(v) -> str | None:
    return datetime.fromtimestamp(v, timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if v else None


def read_client() -> list[dict]:
    out = []
    for fr in sorted(BT_BACKUP.glob("*.fastresume")):
        h = fr.stem.lower()
        r = bdecode(fr.read_bytes())
        t = {}
        tf = fr.with_suffix(".torrent")
        if tf.exists():
            t = bdecode(tf.read_bytes())
        info = t.get(b"info") or {}
        size = info.get(b"length") or sum(f.get(b"length", 0) for f in info.get(b"files") or [])
        pieces = r.get(b"pieces") or b""
        progress = (sum(1 for x in pieces if x) / len(pieces)) if pieces else 0.0
        trackers = [_s(x) for tier in r.get(b"trackers") or [] for x in (tier if isinstance(tier, list) else [tier])]
        comment = _s(t.get(b"comment"))
        hay = " ".join(trackers + [comment]).lower()
        tracker = "rutor" if "rutor" in hay else "rutracker" if ("rutracker" in hay or "t-ru.org" in hay) else "other"
        out.append({"infohash": h, "name": _s(r.get(b"qBt-name")) or _s(r.get(b"name")) or _s(info.get(b"name")),
                    "size_bytes": size or None, "progress": round(progress, 4), "paused": int(bool(r.get(b"paused"))),
                    "added_at": _ts(r.get(b"added_time")), "completed_at": _ts(r.get(b"completed_time")),
                    "save_path": _s(r.get(b"qBt-savePath")) or _s(r.get(b"save_path")),
                    "tracker": tracker, "topic_url": comment if comment.startswith("http") else None})
    return out


# ------------------------------------------------------------------------------------------------- names

_SCENE_STOP = re.compile(r"^(?:\d{3,4}p|2160p|1080p|720p|4k|uhd|hdr\d*|dv|bdremux|bdrip|web-?dl|webrip|remux|blu-?ray|"
                         r"hevc|x26[45]|h\.?26[45]|dub|mvo|ukr|eng|rus|imax|open\.?matte|extended|directors|cut|"
                         r"proper|repack|lic|s\d{1,2}(?:e\d{1,3})?)$", re.I)
_TRANSLIT = [("shch", "щ"), ("sch", "щ"), ("zh", "ж"), ("kh", "х"), ("ts", "ц"), ("ch", "ч"), ("sh", "ш"),
             ("yu", "ю"), ("ya", "я"), ("yo", "ё"), ("ye", "е"), ("iy", "ий"), ("yy", "ый"), ("a", "а"), ("b", "б"),
             ("v", "в"), ("g", "г"), ("d", "д"), ("e", "е"), ("z", "з"), ("i", "и"), ("y", "ы"), ("k", "к"),
             ("l", "л"), ("m", "м"), ("n", "н"), ("o", "о"), ("p", "п"), ("r", "р"), ("s", "с"), ("t", "т"),
             ("u", "у"), ("f", "ф"), ("h", "х"), ("c", "к"), ("w", "в"), ("x", "кс"), ("q", "к"), ("j", "й")]


def parse_name(name: str) -> dict:
    """«Conclave.2024.BDREMUX.2160p…mkv» → {'title': 'Conclave', 'year': 2024, 'season': None}."""
    n = re.sub(r"\.(mkv|mp4|avi|m2ts|ts)$", "", name, flags=re.I)
    n = re.sub(r"^[+\s]+|^\d{1,2}\.\s*", "", n)                  # «+++Title», «05.Title»
    n = re.sub(r"\[[^\]]*\]|\([^)]*\b(?:19|20)\d{2}\b[^)]*\)", lambda m: " " + (re.search(r"(?:19|20)\d{2}", m.group(0)) or [""])[0] + " "
               if m.group(0).startswith("(") else " ", n)
    n = re.sub(r"(?<=[A-Za-zА-Яа-яЁё])(?=(?:19|20)\d{2}|BDRip|BDRemux|WEB|2160p|1080p)", " ", n)  # «Dogman2023BDRip2160p»
    n = re.sub(r"(?<=\d)(?=BDRip|BDRemux|WEB|BluRay|Blu-ray)", " ", n)
    toks = re.split(r"[.\s_]+", n)
    title, year, season = [], None, None
    for t in toks:
        if not t:
            continue
        if re.fullmatch(r"(?:19|20)\d{2}", t) and title:
            year = int(t)
            break
        m = re.fullmatch(r"S(\d{1,2})(?:E\d{1,3})?", t, re.I)
        if m and title:
            season = int(m.group(1))
            break
        if _SCENE_STOP.match(t) and title:
            break
        title.append(t)
    if season is None:
        m = re.search(r"\bS(\d{1,2})\b", n, re.I)
        season = int(m.group(1)) if m else None
    title = " ".join(title).replace(" - ", " ").strip(" -")
    title = re.sub(r"\b(?:[A-Z] ){2,}[A-Z]\b", lambda m: m.group(0).replace(" ", "."), title)  # «U N C L E» → «U.N.C.L.E»
    title = re.sub(r"(?<=[a-zа-я])(?=[A-ZА-Я])", " ", title)       # «PodOgnem» → «Pod Ognem»
    m = re.search(r"((?:19|20)\d{2})", n)
    return {"title": title, "year": year or (int(m.group(1)) if m else None), "season": season}


def translit_ru(s: str) -> str:
    """Russian written in Latin letters back to Cyrillic (approximate; good enough for a search query)."""
    out, low, i = [], s.lower(), 0
    while i < len(low):
        for lat, cyr in _TRANSLIT:
            if low.startswith(lat, i):
                out.append(cyr)
                i += len(lat)
                break
        else:
            out.append(low[i])
            i += 1
    return "".join(out)


# ------------------------------------------------------------------------------------------------- link

def link(conn, rows: list[dict]) -> dict:
    from ..link import ensure_movie, ensure_season
    from . import tmdb
    stats = {"linked_btih": 0, "linked_topic": 0, "linked_name": 0, "linked_tmdb": 0, "unlinked": [],
             "queued": 0, "complete": 0}
    by_btih = {(h or "").lower(): rid for h, rid in conn.execute("SELECT btih, id FROM releases WHERE btih IS NOT NULL")}
    by_url = {u: rid for u, rid in conn.execute("SELECT url, id FROM releases WHERE url IS NOT NULL")}
    rel_title = {rid: tid for rid, tid in conn.execute("SELECT release_id, title_id FROM release_titles")}
    for r in rows:
        rid = by_btih.get(r["infohash"])
        how = "btih" if rid else None
        if not rid and r["topic_url"]:
            u = r["topic_url"].replace("https://", "http://").replace("rutor.is", "rutor.info").replace("rutor.org", "rutor.info")
            rid = by_url.get(u) or by_url.get(r["topic_url"])
            how = "topic" if rid else None
        tid = rel_title.get(rid) if rid else None
        if not tid:
            p = parse_name(r["name"])
            kind = "tv" if p["season"] else "movie"
            names = [p["title"]]
            if re.fullmatch(r"[A-Za-z0-9 '\-:!?&]+", p["title"] or ""):
                names.append(translit_ru(p["title"]))
            res = tmdb.search(conn, [n for n in names if n], p["year"], kind) if p["title"] else {"status": "not_found"}
            b = res.get("best") if res.get("status") == "matched" else None
            if not b and res.get("status") == "ambiguous" and p["year"]:
                # the torrent names the year: the only well-named candidate of exactly that year
                same = [c for c in res.get("candidates") or [] if c.get("year") == p["year"] and (c.get("score") or 0) >= 0.75]
                if len(same) > 1:   # namesakes of the same year: the far more popular one (votes x5), as for Plex shows
                    raw = tmdb.movie_raw if kind == "movie" else tmdb.tv_raw
                    same = sorted(((raw(conn, c["tmdb_id"]) or {}).get("vote_count") or 0, i, c) for i, c in enumerate(same))[::-1]
                    same = [same[0][2]] if same[0][0] >= 50 and same[0][0] >= 5 * same[1][0] else []
                b = same[0] if len(same) == 1 else None
            if b:
                if kind == "movie":
                    m = tmdb.movie(conn, b["tmdb_id"])
                    tid = ensure_movie(conn, m["imdb_id"], "qbittorrent") if m and m.get("imdb_id") else None
                else:
                    tid = ensure_season(conn, b["tmdb_id"], p["season"])
                how = "tmdb" if tid else None
            if not tid:
                stats["unlinked"].append({"name": r["name"], "parsed": p, "search": res.get("status")})
        r.update(release_id=rid, title_id=tid, how=how)
        if how:
            stats[f"linked_{'name' if how == 'name' else how}"] += 1
        stats["complete" if r["progress"] >= 0.999 else "queued"] += 1
    return stats


def sync(conn) -> dict:
    rows = read_client()
    stats = link(conn, rows)
    with conn:
        conn.execute("UPDATE qbt_torrents SET present = 0")
        for r in rows:
            conn.execute(
                """INSERT INTO qbt_torrents (infohash, name, size_bytes, progress, paused, added_at, completed_at,
                       save_path, tracker, topic_url, release_id, title_id, how, present, synced_at)
                   VALUES (:infohash, :name, :size_bytes, :progress, :paused, :added_at, :completed_at, :save_path,
                       :tracker, :topic_url, :release_id, :title_id, :how, 1, datetime('now'))
                   ON CONFLICT(infohash) DO UPDATE SET name=excluded.name, size_bytes=excluded.size_bytes,
                       progress=excluded.progress, paused=excluded.paused, added_at=excluded.added_at,
                       completed_at=excluded.completed_at, save_path=excluded.save_path, tracker=excluded.tracker,
                       topic_url=excluded.topic_url, release_id=COALESCE(excluded.release_id, qbt_torrents.release_id),
                       title_id=COALESCE(excluded.title_id, qbt_torrents.title_id),
                       how=COALESCE(excluded.how, qbt_torrents.how), present=1, synced_at=excluded.synced_at""", r)
    stats["torrents"] = len(rows)
    db.mark_sync(conn, "qbittorrent", max((r["added_at"] or "" for r in rows), default=None),
                 {k: v for k, v in stats.items() if k != "unlinked"} | {"unlinked": len(stats["unlinked"])})
    return stats


def queued_titles(conn) -> dict[int, dict]:
    """title_id → the queued torrent (in the client, not complete)."""
    return {r["title_id"]: dict(r) for r in conn.execute(
        "SELECT * FROM qbt_torrents WHERE present = 1 AND progress < 0.999 AND title_id IS NOT NULL")}


if __name__ == "__main__":
    c = db.connect()
    db.migrate(c)
    st = sync(c)
    print(json.dumps(st, ensure_ascii=False, indent=1))
