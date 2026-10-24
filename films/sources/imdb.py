"""IMDb official datasets (https://datasets.imdbws.com/) -> imdb_basics / imdb_ratings / imdb_akas.

Usage:
    python -X utf8 -m films.sources.imdb load [--no-download] [--force-download]
    python -X utf8 -m films.sources.imdb lookup "Title" [year] [--kind movie|series]

What is loaded (see docs/ratings.md for the reasoning):
  * title types: movie, tvMovie, video, tvSpecial, tvSeries, tvMiniSeries
    (not tvEpisode / short / tvShort / tvPilot / videoGame / musicVideo / podcast*).
    `video` is kept: direct-to-video features (many animated films, some genre films) are typed so,
    and they do appear on the trackers.  isAdult = 1 titles are skipped (counted in the report).
  * years: startYear 2022..2027, OR endYear >= 2024, OR (series only) the series has at least one
    episode with startYear >= 2024 according to title.episode -- this catches long-running shows
    whose endYear is empty (still running), e.g. a show started in 2019 with a 2025 season.
  * ratings: every loaded tconst that has a rating.
  * akas: every row (all regions / languages) of every loaded tconst.

Refresh = replace: rows are upserted in ~50k-row transactions (tables stay readable for concurrent
readers during the load) and rows not present in the current dataset are removed at the end.

Files are cached in data/cache/imdb/ and re-downloaded only when the server's ETag / size changed.
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import pickle
import re
import sys
import time
import unicodedata
from pathlib import Path

import requests

from films import db

BASE_URL = "https://datasets.imdbws.com/"
CACHE_DIR = db.ROOT / "data" / "cache" / "imdb"
FILES = ["title.basics.tsv.gz", "title.episode.tsv.gz", "title.ratings.tsv.gz", "title.akas.tsv.gz"]

MOVIE_TYPES = {"movie", "tvMovie", "video", "tvSpecial"}
SERIES_TYPES = {"tvSeries", "tvMiniSeries"}
KEEP_TYPES = MOVIE_TYPES | SERIES_TYPES
YEAR_FROM = 2022
YEAR_TO = __import__("datetime").date.today().year + 2   # open-ended: the DB is kept for good (owner, 2026-10-23)
END_YEAR_FROM = 2024
EPISODE_YEAR_FROM = 2024
CHUNK = 50_000

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) films-db/0.1 (personal, non-commercial)"


# ----------------------------------------------------------------------------------------------
# download
# ----------------------------------------------------------------------------------------------

def _meta_path(name: str) -> Path:
    return CACHE_DIR / (name + ".meta.json")


def download(name: str, force: bool = False, log=print) -> Path:
    """Download one dataset file into the cache unless the cached copy is current (ETag + size)."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    url = BASE_URL + name
    dest = CACHE_DIR / name
    head = requests.head(url, headers={"User-Agent": UA}, timeout=60)
    head.raise_for_status()
    remote = {"etag": head.headers.get("ETag"), "size": int(head.headers.get("Content-Length", -1)),
              "last_modified": head.headers.get("Last-Modified")}
    if not force and dest.exists() and _meta_path(name).exists():
        local = json.loads(_meta_path(name).read_text(encoding="utf-8"))
        if local.get("etag") == remote["etag"] and dest.stat().st_size == remote["size"]:
            log(f"  {name}: cached ({remote['size'] / 1e6:.0f} MB, {remote['last_modified']})")
            return dest
    log(f"  {name}: downloading {remote['size'] / 1e6:.0f} MB ...")
    t0 = time.time()
    tmp = dest.with_suffix(dest.suffix + ".part")
    for attempt in range(1, 4):
        try:
            with requests.get(url, headers={"User-Agent": UA}, stream=True, timeout=120) as r:
                r.raise_for_status()
                with open(tmp, "wb") as f:
                    for block in r.iter_content(1 << 20):
                        f.write(block)
            break
        except requests.RequestException as e:
            log(f"    attempt {attempt} failed: {e}")
            if attempt == 3:
                raise
            time.sleep(5 * attempt)
    got = tmp.stat().st_size
    if remote["size"] >= 0 and got != remote["size"]:
        raise RuntimeError(f"{name}: size mismatch {got} != {remote['size']}")
    os.replace(tmp, dest)
    remote["downloaded_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _meta_path(name).write_text(json.dumps(remote, indent=1), encoding="utf-8")
    log(f"    done in {time.time() - t0:.0f} s")
    return dest


def _rows(path: Path):
    """Stream a gzipped IMDb TSV (no quoting; '\\N' = NULL). Yields lists of str|None, header skipped."""
    with gzip.open(path, "rt", encoding="utf-8", newline="\n") as f:
        header = f.readline().rstrip("\n").split("\t")
        n = len(header)
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) != n:  # never observed; counted by caller via a sentinel
                yield None
                continue
            yield [None if p == "\\N" else p for p in parts]


def _int(v):
    try:
        return int(v) if v is not None else None
    except ValueError:
        return None


# ----------------------------------------------------------------------------------------------
# load
# ----------------------------------------------------------------------------------------------

def load(conn, download_files: bool = True, force_download: bool = False, log=print) -> dict:
    t_all = time.time()
    stats: dict = {}
    db.migrate(conn)
    if download_files:
        log("download:")
        for name in FILES:
            download(name, force=force_download, log=log)
    for name in FILES:
        if not (CACHE_DIR / name).exists():
            raise FileNotFoundError(f"{CACHE_DIR / name} missing; run without --no-download")

    # ---- pass 1: basics (selection + recent episodes)
    t0 = time.time()
    keep: dict[str, tuple] = {}         # tconst -> row for imdb_basics
    old_series: dict[str, tuple] = {}   # series not selected by year (decided after title.episode)
    recent_episodes: set[str] = set()
    c = {"rows": 0, "bad_lines": 0, "adult_skipped": 0, "no_start_year_skipped": 0}
    for row in _rows(CACHE_DIR / "title.basics.tsv.gz"):
        c["rows"] += 1
        if row is None:
            c["bad_lines"] += 1
            continue
        tconst, ttype, primary, original, is_adult, sy, ey, runtime, genres = row
        if ttype == "tvEpisode":
            y = _int(sy)
            if y is not None and y >= EPISODE_YEAR_FROM:
                recent_episodes.add(tconst)
            continue
        if ttype not in KEEP_TYPES:
            continue
        sy, ey = _int(sy), _int(ey)
        rec = (tconst, ttype, primary, original, sy, ey, _int(runtime), genres)
        in_years = (sy is not None and YEAR_FROM <= sy <= YEAR_TO) or (ey is not None and ey >= END_YEAR_FROM)
        if is_adult == "1":
            if in_years:
                c["adult_skipped"] += 1
            continue
        if in_years:
            keep[tconst] = rec
        elif ttype in SERIES_TYPES and (sy is None or sy < YEAR_FROM) and ey is None:
            old_series[tconst] = rec
        elif sy is None and ey is None:
            c["no_start_year_skipped"] += 1
    stats["basics_scan"] = dict(c, recent_episodes=len(recent_episodes), seconds=round(time.time() - t0))
    log(f"basics scanned: {c['rows']:,} rows, selected by year {len(keep):,}, "
        f"open-ended older series {len(old_series):,}, recent episodes {len(recent_episodes):,} "
        f"({time.time() - t0:.0f} s)")

    # ---- pass 2: title.episode -> series with episodes in >= 2024
    t0 = time.time()
    by_episodes = 0
    for row in _rows(CACHE_DIR / "title.episode.tsv.gz"):
        if row is None:
            continue
        if row[0] in recent_episodes:
            parent = row[1]
            rec = old_series.pop(parent, None)
            if rec is not None:
                keep[parent] = rec
                by_episodes += 1
    stats["series_added_by_recent_episodes"] = by_episodes
    log(f"title.episode: +{by_episodes:,} older open-ended series with episodes in {EPISODE_YEAR_FROM}+ "
        f"({time.time() - t0:.0f} s)")
    del recent_episodes, old_series

    seen = set(keep)
    conn.execute("DROP TABLE IF EXISTS temp.imdb_seen")
    conn.execute("CREATE TEMP TABLE imdb_seen (tconst TEXT PRIMARY KEY)")
    conn.executemany("INSERT INTO temp.imdb_seen VALUES (?)", ((t,) for t in seen))
    conn.commit()

    # ---- write basics
    t0 = time.time()
    rows = list(keep.values())
    del keep
    for i in range(0, len(rows), CHUNK):
        with conn:
            conn.executemany(
                "INSERT INTO imdb_basics (tconst, title_type, primary_title, original_title, start_year,"
                " end_year, runtime_min, genres) VALUES (?,?,?,?,?,?,?,?)"
                " ON CONFLICT(tconst) DO UPDATE SET title_type=excluded.title_type,"
                " primary_title=excluded.primary_title, original_title=excluded.original_title,"
                " start_year=excluded.start_year, end_year=excluded.end_year,"
                " runtime_min=excluded.runtime_min, genres=excluded.genres", rows[i:i + CHUNK])
    with conn:
        stale = conn.execute("DELETE FROM imdb_basics WHERE tconst NOT IN (SELECT tconst FROM temp.imdb_seen)").rowcount
    by_type: dict[str, int] = {}
    for r in rows:
        by_type[r[1]] = by_type.get(r[1], 0) + 1
    stats["basics"] = {"rows": len(rows), "by_type": by_type, "stale_removed": stale,
                       "seconds": round(time.time() - t0)}
    log(f"imdb_basics: {len(rows):,} rows {by_type}; stale removed {stale} ({time.time() - t0:.0f} s)")
    del rows

    # ---- ratings
    t0 = time.time()
    batch, n, total = [], 0, 0
    now = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    for row in _rows(CACHE_DIR / "title.ratings.tsv.gz"):
        if row is None:
            continue
        total += 1
        if row[0] in seen:
            batch.append((row[0], float(row[1]), int(row[2]), now))
            if len(batch) >= CHUNK:
                n += _write_ratings(conn, batch)
                batch = []
    n += _write_ratings(conn, batch)
    with conn:
        stale = conn.execute("DELETE FROM imdb_ratings WHERE tconst NOT IN (SELECT tconst FROM temp.imdb_seen)"
                             " OR loaded_at < ?", (now,)).rowcount
    stats["ratings"] = {"rows": n, "file_rows": total, "stale_removed": stale, "seconds": round(time.time() - t0)}
    log(f"imdb_ratings: {n:,} of {total:,} file rows; stale removed {stale} ({time.time() - t0:.0f} s)")

    # ---- akas (file is grouped by titleId; per-chunk delete+insert of whole tconst groups)
    t0 = time.time()
    batch, n, total, cur = [], 0, 0, None
    flushed: set[str] = set()
    group_tconsts: list[str] = []
    bad = 0
    for row in _rows(CACHE_DIR / "title.akas.tsv.gz"):
        if row is None:
            bad += 1
            continue
        total += 1
        tid = row[0]
        if tid not in seen:
            continue
        if tid != cur:
            if len(batch) >= CHUNK:
                n += _write_akas(conn, batch, group_tconsts, flushed)
                batch, group_tconsts = [], []
            cur = tid
            group_tconsts.append(tid)
        batch.append((tid, int(row[1]), row[2], row[3], row[4]))
    n += _write_akas(conn, batch, group_tconsts, flushed)
    with conn:
        stale = conn.execute("DELETE FROM imdb_akas WHERE tconst NOT IN (SELECT tconst FROM temp.imdb_seen)").rowcount
        no_akas = conn.execute("SELECT count(*) FROM temp.imdb_seen s WHERE NOT EXISTS"
                               " (SELECT 1 FROM imdb_akas a WHERE a.tconst = s.tconst)").fetchone()[0]
    # a loaded tconst that had akas before but has none in this file would keep its old rows
    orphan = 0
    missing = [(t,) for t in seen - flushed]
    for i in range(0, len(missing), CHUNK):
        with conn:
            orphan += conn.executemany("DELETE FROM imdb_akas WHERE tconst = ?", missing[i:i + CHUNK]).rowcount
    stats["akas"] = {"rows": n, "file_rows": total, "bad_lines": bad, "stale_removed": stale + orphan,
                     "tconsts_without_akas": no_akas, "seconds": round(time.time() - t0)}
    log(f"imdb_akas: {n:,} of {total:,} file rows; tconsts without akas {no_akas}; "
        f"stale removed {stale + orphan} ({time.time() - t0:.0f} s)")
    conn.execute("DROP TABLE IF EXISTS temp.imdb_seen")
    conn.commit()

    _invalidate_index()
    stats["seconds_total"] = round(time.time() - t_all)
    (CACHE_DIR / "last_load.json").write_text(json.dumps(stats, indent=1, ensure_ascii=False), encoding="utf-8")
    log(f"total {stats['seconds_total']} s")
    return stats


def _write_ratings(conn, batch) -> int:
    if not batch:
        return 0
    with conn:
        conn.executemany("INSERT INTO imdb_ratings (tconst, rating, votes, loaded_at) VALUES (?,?,?,?)"
                         " ON CONFLICT(tconst) DO UPDATE SET rating=excluded.rating, votes=excluded.votes,"
                         " loaded_at=excluded.loaded_at", batch)
    return len(batch)


def _write_akas(conn, batch, tconsts, flushed: set) -> int:
    if not batch:
        return 0
    fresh = [t for t in tconsts if t not in flushed]
    with conn:
        conn.executemany("DELETE FROM imdb_akas WHERE tconst = ?", ((t,) for t in fresh))
        conn.executemany("INSERT OR REPLACE INTO imdb_akas (tconst, ordering, title, region, language)"
                         " VALUES (?,?,?,?,?)", batch)
    flushed.update(tconsts)
    return len(batch)


# ----------------------------------------------------------------------------------------------
# lookup
# ----------------------------------------------------------------------------------------------

_PUNCT = re.compile(r"[\W_]+", re.UNICODE)


def norm(s: str | None) -> str:
    """Case/diacritics/punctuation-insensitive key; ё->е (й->и, ї->і as a side effect of stripping marks)."""
    if not s:
        return ""
    s = s.replace("&", " and ")
    s = unicodedata.normalize("NFKD", s.casefold())
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return _PUNCT.sub(" ", s).strip()


_ARTICLES = ("the ", "a ", "an ", "le ", "la ", "les ", "l ", "der ", "die ", "das ", "el ", "los ", "las ", "il ")


def _variants(title: str) -> list[tuple[str, str]]:
    """(normalized key, how) — exact first, then looser variants."""
    k = norm(title)
    out = [(k, "exact")]
    for a in _ARTICLES:
        if k.startswith(a):
            out.append((k[len(a):], "no_article"))
            break
    for sep in (":", " - ", " – ", " — "):
        if sep in title:
            head = norm(title.split(sep)[0])
            if head and head != k:
                out.append((head, "before_subtitle"))
            break
    return out


_INDEX = None
_INDEX_FILE = CACHE_DIR / "title_index.pickle"


def _invalidate_index():
    global _INDEX
    _INDEX = None
    try:
        _INDEX_FILE.unlink()
    except FileNotFoundError:
        pass


def _signature(conn) -> list:
    return [conn.execute("SELECT count(*), max(rowid) FROM imdb_basics").fetchone()[:],
            conn.execute("SELECT count(*), max(rowid) FROM imdb_akas").fetchone()[:],
            conn.execute("SELECT max(loaded_at) FROM imdb_ratings").fetchone()[0]]


def _index(conn) -> dict:
    """norm(title) -> {tconst: set(sources)}; built from the DB, cached in memory and on disk."""
    global _INDEX
    sig = _signature(conn)
    if _INDEX is not None and _INDEX[0] == sig:
        return _INDEX[1]
    if _INDEX_FILE.exists():
        try:
            with open(_INDEX_FILE, "rb") as f:
                cached = pickle.load(f)
            if cached[0] == sig:
                _INDEX = cached
                return cached[1]
        except Exception:
            pass
    idx: dict[str, dict[str, set]] = {}

    def add(key, tconst, src):
        if key:
            idx.setdefault(key, {}).setdefault(tconst, set()).add(src)

    for t, p, o in conn.execute("SELECT tconst, primary_title, original_title FROM imdb_basics"):
        add(norm(p), t, "primary")
        add(norm(o), t, "original")
    for t, title, region, lang in conn.execute("SELECT tconst, title, region, language FROM imdb_akas"):
        add(norm(title), t, "aka:" + (region or lang or "-"))
    _INDEX = (sig, idx)
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with open(_INDEX_FILE, "wb") as f:
            pickle.dump(_INDEX, f, protocol=pickle.HIGHEST_PROTOCOL)
    except OSError:
        pass
    return idx


_TYPE_FIT = {
    "movie": {"movie": 1.0, "tvMovie": 0.9, "video": 0.85, "tvSpecial": 0.6, "tvMiniSeries": 0.2, "tvSeries": 0.1},
    "series": {"tvSeries": 1.0, "tvMiniSeries": 1.0, "tvSpecial": 0.3, "tvMovie": 0.2, "movie": 0.1, "video": 0.1},
}
_HOW_FIT = {"exact": 1.0, "no_article": 0.9, "before_subtitle": 0.75, "tokens": 0.6}


def _year_fit(year, ttype, sy, ey):
    if year is None:
        return 1.0, None
    if sy is None:
        return 0.3, None
    if ttype in SERIES_TYPES:
        hi = ey if ey is not None else 9999
        diff = 0 if sy <= year <= hi else (sy - year if year < sy else year - hi)
    else:
        diff = abs(sy - year)
    return {0: 1.0, 1: 0.75, 2: 0.35}.get(diff, 0.0), diff


def lookup(conn, title: str, year: int | None = None, kind: str | None = None, limit: int = 10) -> list[dict]:
    """Candidate tconsts for a title (any of primary/original/aka), best first.

    kind: 'movie' / 'series' (titles.kind) or a raw IMDb titleType.  Score in 0..1 =
    name fit × year fit × type fit, plus a tiny votes tiebreak.  Candidates with year diff > 2 are
    dropped when a year is given.  The caller decides what is confident enough (e.g. score >= 0.7
    and a clear gap to #2); nothing is chosen here.
    """
    idx = _index(conn)
    if kind in SERIES_TYPES:
        kind = "series"
    elif kind in MOVIE_TYPES:
        kind = "movie"
    found: dict[str, tuple[float, str, set]] = {}
    for key, how in _variants(title):
        for tconst, srcs in idx.get(key, {}).items():
            hf = _HOW_FIT[how]
            if tconst not in found or found[tconst][0] < hf:
                found[tconst] = (hf, how, srcs)
    if not found:
        # fallback: every word of the query is a word of the title (extra words allowed), capped
        qt = norm(title).split()
        if qt:
            qs = set(qt)
            for key, tmap in idx.items():
                if all(t in key for t in qt) and qs <= set(key.split()):
                    for tconst, srcs in tmap.items():
                        if tconst not in found:
                            found[tconst] = (_HOW_FIT["tokens"], "tokens", srcs)
                    if len(found) > 300:
                        break
    if not found:
        return []
    q = ",".join("?" * len(found))
    rows = {r["tconst"]: r for r in conn.execute(
        f"SELECT b.*, r.rating, r.votes FROM imdb_basics b LEFT JOIN imdb_ratings r USING (tconst)"
        f" WHERE b.tconst IN ({q})", list(found))}
    out = []
    for tconst, (hf, how, srcs) in found.items():
        r = rows.get(tconst)
        if r is None:
            continue
        yf, diff = _year_fit(year, r["title_type"], r["start_year"], r["end_year"])
        if year is not None and yf == 0.0:
            continue
        tf = _TYPE_FIT.get(kind, {}).get(r["title_type"], 1.0 if kind is None else 0.1)
        votes = r["votes"] or 0
        score = hf * yf * tf + min(math.log10(votes + 1) / 1000, 0.007)
        out.append({"tconst": tconst, "score": round(score, 4), "title_type": r["title_type"],
                    "primary_title": r["primary_title"], "original_title": r["original_title"],
                    "start_year": r["start_year"], "end_year": r["end_year"], "year_diff": diff,
                    "rating": r["rating"], "votes": r["votes"], "matched": how,
                    "matched_on": sorted(srcs)})
    out.sort(key=lambda d: (-d["score"], -(d["votes"] or 0)))
    return out[:limit]


def resolve(conn, titles: list[str], year: int | None = None, kind: str | None = None,
            accept: float = 0.8, margin: float = 0.15) -> dict:
    """Try every given title; return {status: matched|ambiguous|not_found, match, candidates}.

    'matched' when the best score >= accept and the runner-up is >= margin lower, or when both are exact
    name matches but only the best one matches on its primary/original title (the runner-up only via an
    aka, e.g. a working title) and it has >= 20x the votes.  Anything else -> candidates for review.
    """
    merged: dict[str, dict] = {}
    for t in dict.fromkeys(x for x in titles if x):
        for c in lookup(conn, t, year, kind, limit=20):
            if c["tconst"] not in merged or merged[c["tconst"]]["score"] < c["score"]:
                merged[c["tconst"]] = dict(c, query=t)
    ranked = sorted(merged.values(), key=lambda d: (-d["score"], -(d["votes"] or 0)))
    if not ranked:
        return {"status": "not_found", "match": None, "candidates": []}
    best = ranked[0]
    if best["score"] < accept:
        return {"status": "ambiguous", "match": None, "candidates": ranked[:8], "reason": "low score"}
    if len(ranked) == 1 or best["score"] - ranked[1]["score"] >= margin:
        return {"status": "matched", "match": best, "candidates": ranked[:8], "reason": "clear"}
    second = ranked[1]
    own = lambda c: any(s in ("primary", "original") for s in c["matched_on"])
    if own(best) and not own(second) and (best["votes"] or 0) >= 20 * max(second["votes"] or 0, 1):
        return {"status": "matched", "match": best, "candidates": ranked[:8],
                "reason": "runner-up matched only via aka and has far fewer votes"}
    close = [c for c in ranked[1:] if best["score"] - c["score"] < margin]
    if (best["votes"] or 0) >= 1000 and all(not c["votes"] for c in close):
        return {"status": "matched", "match": best, "candidates": ranked[:8],
                "reason": "close runner-ups have no IMDb votes at all"}
    return {"status": "ambiguous", "match": None, "candidates": ranked[:8], "reason": "close runner-up"}


def rating(conn, tconst: str):
    r = conn.execute("SELECT rating, votes FROM imdb_ratings WHERE tconst = ?", (tconst,)).fetchone()
    return (r["rating"], r["votes"]) if r else (None, None)


SUGGEST_URL = "https://v3.sg.media-imdb.com/suggestion/x/{q}.json"
_last_suggest = 0.0


def suggest(tconst: str, refresh: bool = False, max_age_days: float = 30) -> dict | None:
    """IMDb's public search-suggestion JSON for a tconst (plain HTTP, no WAF, no browser).

    Returns {"tconst", "title", "year", "type", "poster_url", "poster_w", "poster_h", "stars"} or None.
    Cached in data/cache/imdb/suggest/.  >= 1 s between network calls.
    """
    global _last_suggest
    d = CACHE_DIR / "suggest"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{tconst}.json"
    if not refresh and p.exists() and time.time() - p.stat().st_mtime < max_age_days * 86400:
        data = json.loads(p.read_text(encoding="utf-8"))
    else:
        data = None
        for attempt in range(4):
            wait = _last_suggest + 1.0 - time.time()
            if wait > 0:
                time.sleep(wait)
            _last_suggest = time.time()
            try:
                r = requests.get(SUGGEST_URL.format(q=tconst), headers={"User-Agent": UA}, timeout=30)
                if r.status_code == 200:
                    data = r.json()
                    break
                if r.status_code == 404:
                    data = {"d": []}
                    break
            except (requests.RequestException, ValueError):
                pass
            time.sleep(2 ** (attempt + 1))
        if data is None:
            raise RuntimeError(f"IMDb suggestion failed for {tconst}")
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    for it in data.get("d", []):
        if it.get("id") == tconst:
            img = it.get("i") or {}
            return {"tconst": tconst, "title": it.get("l"), "year": it.get("y"), "type": it.get("q"),
                    "poster_url": img.get("imageUrl"), "poster_w": img.get("width"),
                    "poster_h": img.get("height"), "stars": it.get("s")}
    return None


def poster_url(url: str, height: int | None = None) -> str:
    """Normalise an m.media-amazon.com poster URL; optionally request a given height (keeps aspect)."""
    m = re.match(r"(https://m\.media-amazon\.com/images/M/[^.]+)\.", url)
    if not m:
        return url
    return m.group(1) + (f"._V1_UY{height}_.jpg" if height else "._V1_.jpg")


# ----------------------------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(prog="films.sources.imdb")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("load", help="download datasets and (re)load imdb_* tables")
    p.add_argument("--no-download", action="store_true", help="use cached files only")
    p.add_argument("--force-download", action="store_true")
    p = sub.add_parser("lookup", help="find candidate tconsts by title")
    p.add_argument("title")
    p.add_argument("year", nargs="?", type=int)
    p.add_argument("--kind", choices=["movie", "series"])
    p.add_argument("-n", type=int, default=10)
    a = ap.parse_args(argv)
    conn = db.connect()
    if a.cmd == "load":
        load(conn, download_files=not a.no_download, force_download=a.force_download)
    else:
        t0 = time.time()
        res = lookup(conn, a.title, a.year, a.kind, a.n)
        for r in res:
            yrs = f"{r['start_year']}" + (f"-{r['end_year'] or ''}" if r["title_type"] in SERIES_TYPES else "")
            print(f"{r['score']:.3f}  {r['tconst']:11} {r['title_type']:12} {yrs:10} "
                  f"{r['rating'] or '-':>4} {r['votes'] or 0:>8}  {r['primary_title']}"
                  f"{' / ' + r['original_title'] if r['original_title'] != r['primary_title'] else ''}"
                  f"  [{r['matched']}: {', '.join(r['matched_on'][:6])}{'…' if len(r['matched_on']) > 6 else ''}]")
        if not res:
            print("no candidates")
        print(f"({time.time() - t0:.2f} s)", file=sys.stderr)


if __name__ == "__main__":
    main()
