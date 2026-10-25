"""Fill titles from TMDB (stage 4): names RU/UK, countries, genres, runtime, studios, budget/gross,
seasons, Russian trailer, credits, collections.

Tracker data is never overwritten with an empty TMDB value. Disagreements that point at a wrong IMDb id
(year off by more than one, season missing on TMDB, IMDb id unknown to TMDB) go to match_issues.
"""
import json
import sys

from .sources import tmdb

UPDATABLE = ["is_animation", "title_original", "title_latin", "title_ru", "title_uk", "year", "release_date",
             "countries", "genres", "main_genre", "runtime_min", "studios", "budget_usd", "box_office_usd",
             "seasons_total", "episodes_total", "series_status", "tmdb_id", "tmdb_type", "trailer_ru_url",
             "series_tmdb_id", "series_imdb_id", "series_key"]


def _issue(conn, title_id: int, problem: str, details: dict):
    if conn.execute("SELECT 1 FROM match_issues WHERE problem = ? AND resolved_at IS NULL AND candidates = ?",
                    (problem, json.dumps(dict(details, title_id=title_id), ensure_ascii=False))).fetchone():
        return
    conn.execute("INSERT INTO match_issues (problem, candidates) VALUES (?, ?)",
                 (problem, json.dumps(dict(details, title_id=title_id), ensure_ascii=False)))


def _release_years(conn, title_id: int) -> list[int]:
    return [r[0] for r in conn.execute(
        """SELECT DISTINCT json_extract(r.parsed, '$.year') FROM release_titles rt JOIN releases r ON r.id = rt.release_id
           WHERE rt.title_id = ? AND json_extract(r.parsed, '$.year') IS NOT NULL""", (title_id,))]


def _person(conn, p: dict) -> int:
    row = conn.execute("SELECT id FROM people WHERE tmdb_id = ?", (p["tmdb_id"],)).fetchone()
    if row:
        return row[0]
    return conn.execute("INSERT INTO people (tmdb_id, name) VALUES (?, ?)", (p["tmdb_id"], p["name"])).lastrowid


def _credits(conn, title_id: int, d: dict):
    conn.execute("DELETE FROM credits WHERE title_id = ?", (title_id,))
    rows = []
    for p in d.get("directors") or []:
        rows.append((p, "director", None, None, ", ".join(p.get("jobs") or []), p.get("episodes")))
    for p in d.get("writers") or []:
        rows.append((p, "writer", None, None, ", ".join(p.get("jobs") or []), p.get("episodes")))
    for p in d.get("cast") or []:
        rows.append((p, "actor", p.get("character"), p.get("order"), None, p.get("episodes")))
    # metadata survey (owner, 2026-10-25): creator / composer stored, DOP / producers shown (films.title_meta backfills)
    for key, role in (("creators", "creator"), ("dops", "dop"), ("producers", "producer"), ("composers", "composer")):
        for i, p in enumerate(d.get(key) or []):
            p = p if "name" in p and "tmdb_id" in p else {"tmdb_id": p["id"], **tmdb._pname(p)}
            rows.append((p, role, None, i, ", ".join(p.get("jobs") or []) or None, p.get("episodes")))
    for p, role, character, order, job, episodes in rows:
        conn.execute("""INSERT OR IGNORE INTO credits (title_id, person_id, role, character, ord, job, episodes)
                        VALUES (?, ?, ?, ?, ?, ?, ?)""",
                     (title_id, _person(conn, p), role, character, order, job, episodes))


def _apply(conn, title_id: int, row: dict, extra: dict):
    cur = dict(conn.execute("SELECT * FROM titles WHERE id = ?", (title_id,)).fetchone())
    sets = {k: row[k] for k in UPDATABLE if k in row and row[k] not in (None, "", "[]")}
    if cur.get("title_ru") and not row.get("title_ru"):
        sets.pop("title_ru", None)  # keep the tracker's Russian title when TMDB has none
    sets.update({k: v for k, v in extra.items() if v is not None})
    sets["tmdb_checked_at"] = None  # placeholder replaced by SQL below
    cols = ", ".join(f"{k} = ?" for k in sets if k != "tmdb_checked_at")
    conn.execute(f"UPDATE titles SET {cols}, tmdb_checked_at = datetime('now'), updated_at = datetime('now') "
                 f"WHERE id = ?", [v for k, v in sets.items() if k != "tmdb_checked_at"] + [title_id])


def _collection(conn, cid: int):
    if conn.execute("SELECT 1 FROM collections WHERE id = ?", (cid,)).fetchone():
        return
    col = tmdb.collection(conn, cid)
    if col:
        conn.execute("INSERT OR REPLACE INTO collections (id, name_ru, name_original, parts) VALUES (?, ?, ?, ?)",
                     (cid, col.get("name"), col.get("name_en"), json.dumps(col.get("parts"), ensure_ascii=False)))


def enrich(conn, only_missing: bool = True, max_age_days: float = tmdb.DEFAULT_MAX_AGE_DAYS) -> dict:
    # a duplicate row (made from a wrong IMDb id, its links moved to the right title: titles.duplicate_of) is not enriched
    # a tracker-only row (films/tracker_only.py) has no IMDb id to look up: the match run re-identifies it instead
    where = ("WHERE duplicate_of IS NULL AND coalesce(tracker_only, 0) = 0"
             + (" AND tmdb_checked_at IS NULL" if only_missing else ""))
    titles = conn.execute(f"SELECT id, kind, imdb_id, series_imdb_id, season_number, year FROM titles {where}").fetchall()
    stats = {"titles": len(titles), "movies": 0, "seasons": 0, "issues": {}}

    def issue(tid, problem, details):
        _issue(conn, tid, problem, details)
        stats["issues"][problem] = stats["issues"].get(problem, 0) + 1

    series_cache: dict[int, dict] = {}
    for i, (tid, kind, imdb_id, series_imdb, season_no, year) in enumerate(titles, 1):
        key = imdb_id if kind == "movie" else series_imdb
        found = tmdb.find_by_imdb(conn, key) if key else {}
        with conn:
            if kind == "movie":
                mid = found.get("movie")
                if not mid:
                    issue(tid, "tmdb_no_movie_for_imdb", {"imdb_id": key, "tmdb_find": found})
                    continue
                m = tmdb.movie(conn, mid, max_age_days)
                if not m:
                    issue(tid, "tmdb_movie_404", {"tmdb_id": mid})
                    continue
                ry = _release_years(conn, tid)
                if m.get("year") and ry and min(abs(m["year"] - y) for y in ry) > 1:
                    issue(tid, "tmdb_year_mismatch", {"imdb_id": key, "tmdb_year": m["year"], "release_years": ry})
                trailer = m.get("trailer_ru") or {}
                _apply(conn, tid, tmdb.movie_row(m), {
                    "country_codes": json.dumps(m.get("country_codes"), ensure_ascii=False),
                    "release_date_kind": m.get("release_date_kind"),
                    "trailer_ru_source": "tmdb_movie" if m.get("trailer_ru_url") else None,
                    "trailer_ru_subtitled": int(bool(trailer.get("subtitled"))) if trailer else None,
                    "collection_id": (m.get("collection") or {}).get("id")})
                _credits(conn, tid, m)
                if m.get("collection"):
                    _collection(conn, m["collection"]["id"])
                stats["movies"] += 1
            else:
                sid = found.get("tv")
                if not sid:
                    issue(tid, "tmdb_no_tv_for_imdb", {"imdb_id": key, "tmdb_find": found})
                    continue
                if sid not in series_cache:
                    series_cache[sid] = tmdb.tv(conn, sid, max_age_days)
                series = series_cache[sid]
                s = tmdb.season(conn, sid, season_no, max_age_days) if series else None
                if not s:
                    issue(tid, "tmdb_no_season", {"series_imdb_id": key, "tmdb_tv": sid, "season": season_no,
                                                  "tmdb_seasons": series.get("number_of_seasons") if series else None})
                    continue
                trailer = s.get("trailer_ru") or {}
                _apply(conn, tid, tmdb.season_row(series, s), {
                    "country_codes": json.dumps(series.get("country_codes"), ensure_ascii=False),
                    "trailer_ru_source": s.get("trailer_ru_source") if s.get("trailer_ru_url") else None,
                    "trailer_ru_subtitled": int(bool(trailer.get("subtitled"))) if trailer else None})
                _credits(conn, tid, s)
                stats["seasons"] += 1
        if i % 500 == 0:
            print(f"  enriched {i}/{len(titles)}", file=sys.stderr, flush=True)
    return stats
