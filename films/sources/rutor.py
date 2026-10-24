"""rutor.info collector: listing pages -> releases, release pages -> release_raw.

Coverage strategy: (1) walk each category's browse listing (sorted by date added/updated,
no depth limit) back to CUTOFF and keep releases whose title year/range overlaps the configured
years; (2) exhaustive year searches, recursively split while a query hits the 2000-result cap.
Both are required: browse hides some releases that search finds (seen 2026-10-23), and search
only matches year tokens in titles.

Plain http: rutor's https certificate is expired.
"""
import html
import re
import sys
import time
import tomllib
import zlib
from datetime import date
from pathlib import Path
from urllib.parse import quote

import requests

from .. import db

BASE = "http://rutor.info"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130 Safari/537.36"
DELAY = 0.8          # seconds between requests
CUTOFF = date(2023, 6, 1)  # nothing for 2024+ titles can be older than this
MONTHS = {"Янв": 1, "Фев": 2, "Мар": 3, "Апр": 4, "Май": 5, "Июн": 6,
          "Июл": 7, "Авг": 8, "Сен": 9, "Окт": 10, "Ноя": 11, "Дек": 12}
SIZE_UNITS = {"B": 1, "kB": 1024, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4}

ROW_RE = re.compile(r'<tr class="(?:gai|tum)">(.*?)</tr>', re.S)
YEAR_RE = re.compile(r"\((\d{4})(?:\s*[-–]\s*(\d{4}))?\)")

_session = requests.Session()
_session.headers["User-Agent"] = UA
_last = 0.0


def get(url: str) -> str:
    global _last
    for attempt in range(5):
        wait = DELAY - (time.monotonic() - _last)
        if wait > 0:
            time.sleep(wait)
        _last = time.monotonic()
        try:
            r = _session.get(url, timeout=40)
            if r.status_code == 200:
                r.encoding = "utf-8"
                return r.text
            err = f"HTTP {r.status_code}"
        except requests.RequestException as e:
            err = str(e)
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"{url}: {err}")


def settings() -> dict:
    with open(db.ROOT / "config" / "settings.toml", "rb") as f:
        return tomllib.load(f)


def parse_date(s: str) -> date:
    d, m, y = s.replace("\xa0", " ").replace("&nbsp;", " ").split()
    return date(2000 + int(y), MONTHS[m], int(d))


def parse_size(s: str) -> int | None:
    m = re.match(r"([\d.]+)\s*(\w+)", s.replace("&nbsp;", " ").replace("\xa0", " "))
    if not m or m.group(2) not in SIZE_UNITS:
        return None
    return int(float(m.group(1)) * SIZE_UNITS[m.group(2)])


def parse_listing(page: str) -> list[dict]:
    rows = []
    for row in ROW_RE.findall(page):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        link = re.search(r'<a href="/torrent/(\d+)/[^"]*">([^<]+)</a>', row)
        if not link or len(cells) < 4:
            raise ValueError(f"unexpected listing row: {row[:300]}")
        btih = re.search(r"btih:([0-9a-fA-F]{40})", row)
        seeds = re.search(r'alt="S" />(?:&nbsp;|\s)*(\d+)', row)
        leech = re.search(r'<span class="red">(?:&nbsp;|\s)*(\d+)', row)
        rows.append({
            "source_id": link.group(1),
            "name": html.unescape(link.group(2)).strip(),
            "added_at": parse_date(cells[0]).isoformat(),
            "btih": btih.group(1).lower() if btih else None,
            "size_bytes": parse_size(cells[-2]),
            "seeders": int(seeds.group(1)) if seeds else None,
            "leechers": int(leech.group(1)) if leech else None,
        })
    return rows


def title_years(name: str) -> tuple[int, int] | None:
    """Year or year range from the release name: '(2025)', '(2022-2025)'. Last match wins
    (titles themselves may contain parenthesised numbers)."""
    found = YEAR_RE.findall(name)
    if not found:
        return None
    y1, y2 = found[-1]
    return int(y1), int(y2 or y1)


def in_years(name: str, years: list[int]) -> bool:
    span = title_years(name)
    return bool(span) and span[0] <= max(years) and span[1] >= min(years)


def upsert(conn, rows: list[dict], category: str) -> int:
    new = 0
    excluded = {r[0] for r in conn.execute("SELECT source_id FROM excluded_releases WHERE source='rutor'")}
    with conn:
        for r in rows:
            if r["source_id"] in excluded:
                continue
            cur = conn.execute(
                """UPDATE releases SET name=?, size_bytes=?, seeders=?, leechers=?, btih=coalesce(?, btih),
                       last_seen_at=datetime('now')
                   WHERE source='rutor' AND source_id=?""",
                (r["name"], r["size_bytes"], r["seeders"], r["leechers"], r["btih"], r["source_id"]))
            if cur.rowcount == 0:
                conn.execute(
                    """INSERT INTO releases (source, source_id, url, btih, name, category, added_at,
                                             size_bytes, seeders, leechers)
                       VALUES ('rutor', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (r["source_id"], f"{BASE}/torrent/{r['source_id']}", r["btih"], r["name"], category,
                     r["added_at"], r["size_bytes"], r["seeders"], r["leechers"]))
                new += 1
    return new


def walk_category(conn, cat: int, category: str, years: list[int], cutoff: date = CUTOFF,
                  stop_at_known: int = 0) -> dict:
    """Walk browse pages newest -> oldest until rows are older than cutoff.
    stop_at_known > 0: incremental mode, stop after that many consecutive pages with no new release."""
    stats = {"pages": 0, "rows": 0, "kept": 0, "new": 0}
    page, quiet = 0, 0
    while True:
        rows = parse_listing(get(f"{BASE}/browse/{page}/{cat}/0/0"))
        stats["pages"] += 1
        if not rows:
            break
        stats["rows"] += len(rows)
        kept = [r for r in rows if in_years(r["name"], years)]
        new = upsert(conn, kept, category)
        stats["kept"] += len(kept)
        stats["new"] += new
        quiet = quiet + 1 if new == 0 else 0
        oldest = min(date.fromisoformat(r["added_at"]) for r in rows)
        print(f"  cat {cat} page {page}: {len(kept)}/{len(rows)} kept, {new} new, oldest {oldest}",
              file=sys.stderr, flush=True)
        if oldest < cutoff or (stop_at_known and quiet >= stop_at_known):
            break
        page += 1
    return stats


SEARCH_CAP = 2000
# Terms used to split a capped query into "q term" + "q -term" (method 3 = logical expression,
# where "-word" is NOT). Each split is an exact partition, so the leaves cover the whole query.
SPLIT_TERMS = ["2160p", "1080p", "720p", "BDRip", "WEB", "DLRip", "HDRip", "BDRemux", "AVC",
               "HEVC", "D", "P", "L", "от", "Sub", "TVShows", "MegaPeer", "HDR10", "1", "2"]


def search_all(cat: int, query: str) -> tuple[int, list[dict]]:
    """All pages of one search; returns (reported total, rows). Totals at SEARCH_CAP are capped."""
    out, page = [], 0
    while True:
        text = get(f"{BASE}/search/{page}/{cat}/300/0/{quote(query)}")
        m = re.search(r"Результатов поиска (\d+)", text)
        total = int(m.group(1)) if m else 0
        rows = parse_listing(text)
        out += rows
        if not rows or len(out) >= total:
            return total, out
        page += 1


def search_partition(cat: int, query: str, terms: list[str] = SPLIT_TERMS) -> dict[str, dict]:
    """Every hit of `query`, splitting recursively while a query is capped."""
    total, rows = search_all(cat, query)
    if total < SEARCH_CAP:
        print(f"  search cat {cat} [{query}]: {total}", file=sys.stderr, flush=True)
        return {r["source_id"]: r for r in rows}
    used = set(query.split())
    rest = [t for t in terms if t not in used and f"-{t}" not in used]
    if not rest:
        raise RuntimeError(f"cannot split capped query [{query}] in cat {cat}: no terms left")
    term = rest[0]
    hits = search_partition(cat, f"{query} {term}", terms)
    hits.update(search_partition(cat, f"{query} -{term}", terms))
    return hits


def search_sweep(conn, cat: int, category: str, years: list[int]) -> dict:
    """Exhaustive year searches. rutor's browse listing hides some releases that search finds,
    so this is a required pass, not only a check. Year ranges like (2022-2025) always contain
    their end year, so searching each year up to max+1 covers every overlapping range."""
    known = {r[0] for r in conn.execute("SELECT source_id FROM releases WHERE source='rutor'")}
    report = {}
    for year in range(min(years), max(years) + 2):
        hits = search_partition(cat, str(year))
        in_range = [r for r in hits.values() if in_years(r["name"], years)]
        missing = [r for r in in_range if r["source_id"] not in known]
        upsert(conn, missing, category)
        known.update(r["source_id"] for r in missing)
        report[year] = {"hits": len(hits), "in_range": len(in_range), "not_in_walk": len(missing),
                        "not_in_walk_examples": [(r["source_id"], r["added_at"], r["name"]) for r in missing[:10]]}
        print(f"  sweep cat {cat} {year}: {report[year]}", file=sys.stderr, flush=True)
    return report


def fetch_pages(conn, limit: int | None = None, shard: tuple[int, int] = (0, 1)) -> int:
    """Download release pages not yet in release_raw (zlib-compressed full HTML).
    shard=(i, n): this worker takes releases with id % n == i, so n workers can run side by side."""
    todo = conn.execute(
        """SELECT r.id, r.url FROM releases r LEFT JOIN release_raw w ON w.release_id = r.id
           WHERE r.source='rutor' AND w.release_id IS NULL AND r.page_skip = 0 AND r.id % ? = ?
           ORDER BY r.added_at DESC""", (shard[1], shard[0])).fetchall()
    if limit:
        todo = todo[:limit]
    done = 0
    for i, (rid, url) in enumerate(todo, 1):
        try:
            body = get(url)
        except RuntimeError as e:
            print(f"  FAILED {url}: {e}", file=sys.stderr, flush=True)
            continue
        if 'id="details"' not in body:
            print(f"  NO DETAILS {url}", file=sys.stderr, flush=True)
            continue
        with conn:
            conn.execute("INSERT OR REPLACE INTO release_raw (release_id, body) VALUES (?, ?)",
                         (rid, zlib.compress(body.encode("utf-8"), 9)))
        done += 1
        if i % 100 == 0:
            print(f"  pages {i}/{len(todo)}", file=sys.stderr, flush=True)
    return done


def raw_page(conn, release_id: int) -> str | None:
    row = conn.execute("SELECT body FROM release_raw WHERE release_id=?", (release_id,)).fetchone()
    return zlib.decompress(row[0]).decode("utf-8") if row else None


def h264_equiv(codec: str | None, bit_depth: int | None, height: int | None, kbps: int | None,
               factors: dict) -> int | None:
    """kbps × codec efficiency factor (config [codec_efficiency], hd/uhd by frame height)."""
    if not codec or not kbps:
        return None
    key = {("AVC", 10): "AVC10", ("HEVC", 8): "HEVC8"}.get((codec, bit_depth), codec)
    f = factors.get(key)
    if not f:
        return None
    return round(kbps * f["uhd" if (height or 0) >= 1440 else "hd"])


def parse_pages(conn, reparse: bool = False, delete_dv5: bool = True, ids: list[int] | None = None) -> dict:
    """Run films.parse.rutor over cached pages and fill release columns. DV-profile-5-only releases are
    remembered in excluded_releases (owner: never store them) and removed — or, with delete_dv5=False, kept with
    their parsed columns (rank.compute skips every release listed in excluded_releases). ids: only these releases."""
    import json
    from ..parse import rutor as parser

    factors = settings()["codec_efficiency"]
    version = str(Path(parser.__file__).stat().st_mtime_ns)
    rows = conn.execute(
        f"""SELECT r.id, r.source_id, r.name FROM releases r JOIN release_raw w ON w.release_id = r.id
            WHERE r.source='rutor' {'' if reparse else 'AND r.parsed IS NULL'}""").fetchall()
    if ids is not None:
        want = set(ids)
        rows = [r for r in rows if r[0] in want]
    stats = {"parsed": 0, "dv5_excluded": 0, "failed": 0}
    for i, (rid, sid, name) in enumerate(rows, 1):
        try:
            d = parser.parse_release_page(raw_page(conn, rid))
        except Exception as e:  # noqa: BLE001 — record and continue; reported in stats
            stats["failed"] += 1
            print(f"  PARSE FAILED {sid}: {e!r}", file=sys.stderr, flush=True)
            continue
        with conn:
            if d.get("dv5_only"):
                conn.execute("INSERT OR IGNORE INTO excluded_releases (source, source_id, reason, name) "
                             "VALUES ('rutor', ?, 'dv5_only', ?)", (sid, name))
                stats["dv5_excluded"] += 1
            if d.get("dv5_only") and delete_dv5:
                conn.execute("DELETE FROM release_raw WHERE release_id=?", (rid,))
                conn.execute("DELETE FROM release_titles WHERE release_id=?", (rid,))
                conn.execute("DELETE FROM releases WHERE id=?", (rid,))
                continue
            np = d.get("name_parsed") or {}
            js = lambda v: json.dumps(v, ensure_ascii=False) if v is not None else None  # noqa: E731
            conn.execute(
                """UPDATE releases SET season=?, episodes=?, resolution=?, source_type=?, source_detail=?,
                       video_codec=?, video_bit_depth=?, video_kbps=?, h264_equiv_kbps=?, hdr=?, dv_profile=?,
                       dv_profile_full=?, dv_fallback=?, dv_static_md=?, is_imax_cut=?, edition=?, is_upscale=?,
                       is_ai_upscale=?, audio=?, best_voice=?, forced_subs=?, parsed=?, parsed_version=?,
                       details_parsed_at=datetime('now')
                   WHERE id=?""",
                (d.get("season"), d.get("episodes"), d.get("resolution"), d.get("source_type"),
                 np.get("source_raw"), d.get("video_codec"), d.get("video_bit_depth"), d.get("video_kbps"),
                 h264_equiv(d.get("video_codec"), d.get("video_bit_depth"),
                            d.get("video_height") or d.get("resolution"), d.get("video_kbps"), factors),
                 js(d.get("hdr")), d.get("dv_profile"), d.get("dv_profile_full"), d.get("dv_fallback"),
                 d.get("dv_static_md"), d.get("is_imax_cut"), js(d.get("edition")), d.get("is_upscale"),
                 1 if d.get("is_upscale") else None,  # trackers only ever label fan upscales
                 js(d.get("audio")), d.get("best_voice"), js(d.get("forced_subs")),
                 js(d), version, rid))
            stats["parsed"] += 1
        if i % 1000 == 0:
            print(f"  parsed {i}/{len(rows)}", file=sys.stderr, flush=True)
    return stats


def dv_flags(conn) -> dict:
    """Re-check the Dolby Vision of every cached rutor page without a full re-parse (R31 / R8):
    releases.dv_static_md = 1 (DV rebuilt from HDR10: generated RPU / static metadata), 2 (converted from HDR10+),
    0 (DV, no sign of a rebuild); DV-profile-5-only releases (no HDR10 / HLG layer) go to excluded_releases —
    the row stays, rank.compute skips it."""
    from ..parse import rutor as parser
    rows = conn.execute(
        """SELECT r.id, r.source_id, r.name, r.dv_static_md FROM releases r JOIN release_raw w ON w.release_id = r.id
           WHERE r.source = 'rutor' AND (r.hdr LIKE '%"DV"%' OR r.dv_profile IS NOT NULL)""").fetchall()
    st = {"dv_pages": len(rows), "static_1": 0, "hdr10plus_2": 0, "changed": 0, "dv5_only": 0, "evidence": []}
    for rid, sid, name, cur in rows:
        d = parser.parse_release_page(raw_page(conn, rid))
        if "DV" not in d["hdr"]:
            continue
        flag = d.get("dv_static_md") or 0
        st["static_1"] += flag == 1
        st["hdr10plus_2"] += flag == 2
        if flag:
            st["evidence"].append({"release_id": rid, "flag": flag, "evidence": d.get("dv_rebuild_evidence"), "name": name})
        with conn:
            if cur != flag:
                conn.execute("UPDATE releases SET dv_static_md = ? WHERE id = ?", (flag, rid))
                st["changed"] += 1
            if d.get("dv5_only"):
                st["dv5_only"] += 1
                conn.execute("INSERT OR IGNORE INTO excluded_releases (source, source_id, reason, name) "
                             "VALUES ('rutor', ?, 'dv5_only', ?)", (sid, name))
    return st


def collect(conn, incremental: bool = False) -> dict:
    cfg = settings()
    years = db.years()
    result = {}
    for cat, category in cfg["rutor"]["categories"].items():
        cat = int(cat)
        print(f"walk cat {cat} ({category})", file=sys.stderr, flush=True)
        walk = walk_category(conn, cat, category, years, stop_at_known=3 if incremental else 0)
        # releases bumped to the top (re-dated on update) while we were walking deeper
        rewalk = walk_category(conn, cat, category, years, stop_at_known=2)
        result[category] = {"walk": walk, "rewalk": rewalk, "sweep": search_sweep(conn, cat, category, years)}
    return result


if __name__ == "__main__":
    import json
    conn = db.connect()
    db.migrate(conn)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "collect"
    if cmd == "collect":
        print(json.dumps(collect(conn, incremental="--incremental" in sys.argv), ensure_ascii=False, indent=1))
    elif cmd == "pages":
        print(fetch_pages(conn, int(sys.argv[2]) if len(sys.argv) > 2 else None))
    elif cmd == "dv-flags":
        print(json.dumps(dv_flags(conn), ensure_ascii=False, indent=1))
