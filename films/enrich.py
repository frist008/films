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
             "series_tmdb_id", "series_imdb_id", "series_key", "episode_dates"]


def _issue(conn, title_id: int, problem: str, details: dict):
    data = json.dumps(dict(details, title_id=title_id), ensure_ascii=False)
    if conn.execute("SELECT 1 FROM match_issues WHERE problem = ? AND resolved_at IS NULL AND candidates = ?",
                    (problem, data)).fetchone():
        return
    # one open issue per title and problem: newer details (e.g. why the TMDB name search did not decide) replace older
    old = conn.execute("SELECT id FROM match_issues WHERE problem = ? AND resolved_at IS NULL "
                       "AND json_valid(candidates) AND json_extract(candidates, '$.title_id') = ? ORDER BY id LIMIT 1",
                       (problem, title_id)).fetchone()
    if old:
        conn.execute("UPDATE match_issues SET candidates = ? WHERE id = ?", (data, old[0]))
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
    # metadata survey (owner, 2026-10-25): creator / composer / editor stored, DOP / producers shown
    # (films.title_meta backfills)
    for key, role in (("creators", "creator"), ("dops", "dop"), ("producers", "producer"), ("composers", "composer"),
                      ("editors", "editor")):
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
    if "title_uk" in sets and "title_uk_source" in cur:
        # TMDB's Ukrainian title wins over the IMDb-aka fallback (owner 2026-10-25); without one the fallback stays
        sets["title_uk_source"] = "tmdb" + (f":{row['title_uk_source']}" if row.get("title_uk_source") else "")
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


def _search_names(conn, title_id: int) -> tuple[list[str], int | None]:
    """Names to search TMDB by (original, Latin, Russian, Ukrainian — the project's search order) and the year
    (titles.year, else the earliest year of the title's releases)."""
    t = conn.execute("SELECT title_original, title_latin, title_ru, title_uk, year FROM titles WHERE id = ?",
                     (title_id,)).fetchone()
    names = [n for n in (t["title_original"], t["title_latin"], t["title_ru"], t["title_uk"]) if n]
    for r in conn.execute("""SELECT DISTINCT json_extract(r.parsed, '$.title_original'), json_extract(r.parsed, '$.title_ru')
                             FROM release_titles rt JOIN releases r ON r.id = rt.release_id WHERE rt.title_id = ?""",
                          (title_id,)):
        names += [n for n in r if n and isinstance(n, str)]
    ry = _release_years(conn, title_id)
    return list(dict.fromkeys(n.strip() for n in names if n.strip())), t["year"] or (min(ry) if ry else None)


def _imdb_canonical(imdb_id: str) -> str | None:
    """The id IMDb redirects a merged duplicate to (imdb_specs cache record 'canonical'), or None."""
    from .sources import imdb_specs
    return (imdb_specs.load_cached(imdb_id) or {}).get("canonical")


def search_fallback(conn, title_id: int, kind: str, imdb_id: str | None,
                    max_age_days: float = tmdb.DEFAULT_MAX_AGE_DAYS) -> tuple[int | None, str, dict]:
    """TMDB doesn't know the title's IMDb id (new IMDb entry, TMDB entry without an IMDb id, wrong tracker link):
    search TMDB by every name + year + kind and accept only an unambiguous match -> (tmdb_id | None, reason, details).

    Accepted: tmdb.search status 'matched' (one clear candidate — see its docstring), full details checked,
    a film's year within ±1 (a series may have started earlier than the season), and the candidate's own IMDb id
    either absent or equal to ours (another IMDb id = another work or our link is wrong -> not accepted)."""
    names, year = _search_names(conn, title_id)
    if not names:
        return None, "no names to search by", {}
    res = tmdb.search(conn, names, year, "movie" if kind == "movie" else "tv", max_age_days=max_age_days)
    best = res.get("best")
    det = {"names": names, "year": year, "status": res.get("status"), "reason": res.get("reason"),
           "candidates": [{k: c.get(k) for k in ("tmdb_id", "title", "original_title", "year", "score", "imdb_id")}
                          for c in (res.get("candidates") or [])[:4]]}
    if res.get("status") != "matched" or not best:
        return None, f"search {res.get('status')}: {res.get('reason')}", det
    if not best.get("verified"):
        return None, "best candidate's details not checked", det
    if kind == "movie" and year and (best.get("year_diff") is None or best["year_diff"] > 1):
        return None, f"year differs ({best.get('year')} vs {year})", det
    if best.get("imdb_id") and imdb_id and best["imdb_id"] != imdb_id:
        # IMDb merged our id into TMDB's (meta.canonicalId, kept by imdb_specs): the same work -> confirmed
        if _imdb_canonical(imdb_id) == best["imdb_id"]:
            return best["tmdb_id"], f"IMDb redirects {imdb_id} to {best['imdb_id']} (TMDB's id)", det
        return None, f"TMDB candidate has another IMDb id {best['imdb_id']}", det
    return best["tmdb_id"], ("IMDb id confirmed" if best.get("imdb_id") == imdb_id and imdb_id
                             else f"search: {res.get('reason')}"), det


def _resolve_issues(conn, title_id: int, problems: tuple, how: str):
    for row in conn.execute("SELECT id, candidates FROM match_issues WHERE resolved_at IS NULL AND problem IN (%s)"
                            % ",".join("?" * len(problems)), problems).fetchall():
        try:
            if json.loads(row[1] or "{}").get("title_id") != title_id:
                continue
        except ValueError:
            continue
        conn.execute("UPDATE match_issues SET resolved_at = datetime('now'), "
                     "candidates = json_set(candidates, '$.resolved_how', ?) WHERE id = ?", (how, row[0]))


def enrich(conn, only_missing: bool = True, max_age_days: float = tmdb.DEFAULT_MAX_AGE_DAYS,
           ids: list[int] | None = None, search: bool = True) -> dict:
    # a duplicate row (made from a wrong IMDb id, its links moved to the right title: titles.duplicate_of) is not enriched
    # a tracker-only row (films/tracker_only.py) has no IMDb id to look up: the match run re-identifies it instead
    where = ("WHERE duplicate_of IS NULL AND coalesce(tracker_only, 0) = 0"
             + (" AND tmdb_checked_at IS NULL" if only_missing else ""))
    params: list = []
    if ids:
        where += f" AND id IN ({','.join('?' * len(ids))})"
        params = list(ids)
    titles = conn.execute(f"SELECT id, kind, imdb_id, series_imdb_id, season_number, year FROM titles {where}",
                          params).fetchall()
    stats = {"titles": len(titles), "movies": 0, "seasons": 0, "issues": {}, "found_by_search": [],
             "search_rejected": []}

    def issue(tid, problem, details):
        _issue(conn, tid, problem, details)
        stats["issues"][problem] = stats["issues"].get(problem, 0) + 1

    searched: dict[tuple, tuple] = {}

    def fallback(tid, kind, key):
        """2026-10-25 (audit): TMDB search by name when TMDB doesn't know the IMDb id; seasons of one series share it."""
        if not search:
            return None, "search off", {}
        skey = ("tv", key) if kind != "movie" and key else (kind, tid)
        if skey not in searched:
            searched[skey] = search_fallback(conn, tid, kind, key, max_age_days)
        found_id, why, det = searched[skey]
        (stats["found_by_search"] if found_id else stats["search_rejected"]).append(
            {"title_id": tid, "kind": kind, "imdb_id": key, "tmdb_id": found_id, "why": why,
             "names": det.get("names", [])[:3], "year": det.get("year"), "candidates": det.get("candidates", [])[:2]})
        return found_id, why, det

    series_cache: dict[int, dict] = {}
    for i, (tid, kind, imdb_id, series_imdb, season_no, year) in enumerate(titles, 1):
        key = imdb_id if kind == "movie" else series_imdb
        found = tmdb.find_by_imdb(conn, key) if key else {}
        by_search = None
        if kind == "movie" and not found.get("movie") or kind != "movie" and not found.get("tv"):
            fid, why, det = fallback(tid, kind, key)
            if fid:
                found = dict(found, **{"movie" if kind == "movie" else "tv": fid})
                by_search = why
            else:
                found = dict(found, search=why)
        with conn:
            if by_search:
                _resolve_issues(conn, tid, ("tmdb_no_movie_for_imdb", "tmdb_no_tv_for_imdb"),
                                f"tmdb search ({by_search})")
            if kind == "movie":
                mid = found.get("movie")
                if not mid:
                    issue(tid, "tmdb_no_movie_for_imdb", {"imdb_id": key, "tmdb_find": found})
                    continue
                if by_search:
                    other = conn.execute("SELECT id FROM titles WHERE kind = 'movie' AND tmdb_id = ? AND id != ? "
                                         "AND duplicate_of IS NULL", (mid, tid)).fetchone()
                    if other:   # the name search led to a film another row already is: a duplicate row, not merged here
                        issue(tid, "tmdb_same_as_title", {"imdb_id": key, "tmdb_id": mid, "other_title_id": other[0],
                                                          "how": by_search})
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
                srow = tmdb.season_row(series, s)
                if by_search and not series.get("imdb_id"):
                    srow.pop("series_key", None)   # found by name, TMDB has no IMDb id: keep the row's IMDb-based key
                if by_search:
                    other = conn.execute(
                        "SELECT id FROM titles WHERE kind = 'series' AND season_number = ? AND id != ? "
                        "AND duplicate_of IS NULL AND (series_tmdb_id = ? OR series_key = ?)",
                        (season_no, tid, sid, srow.get("series_key") or "")).fetchone()
                    if other:   # this season already has its own row (e.g. under IMDb's canonical id): a duplicate row
                        issue(tid, "tmdb_same_as_title", {"series_imdb_id": key, "tmdb_tv": sid, "season": season_no,
                                                          "other_title_id": other[0], "how": by_search})
                        continue
                _apply(conn, tid, srow, {
                    "country_codes": json.dumps(series.get("country_codes"), ensure_ascii=False),
                    "trailer_ru_source": s.get("trailer_ru_source") if s.get("trailer_ru_url") else None,
                    "trailer_ru_subtitled": int(bool(trailer.get("subtitled"))) if trailer else None})
                _credits(conn, tid, s)
                stats["seasons"] += 1
        if i % 500 == 0:
            print(f"  enriched {i}/{len(titles)}", file=sys.stderr, flush=True)
    return stats


def fill_episode_dates(conn, max_age_days: float = 7) -> dict:
    """Episode air dates of every season row (titles.episode_dates; owner 2026-10-25: «у идущего сезона „вышло N из M“
    — в таблицу»). Running seasons are re-read when the TMDB cache is older than a week, finished ones only once."""
    rows = conn.execute("SELECT id, series_tmdb_id, season_number, series_status, episode_dates FROM titles "
                        "WHERE kind = 'series' AND series_tmdb_id IS NOT NULL AND season_number IS NOT NULL "
                        "AND duplicate_of IS NULL").fetchall()
    stats = {"seasons": len(rows), "filled": 0, "no_tmdb": 0}
    for tid, tv, sn, status, have in rows:
        running = status in ("returning", "in_production")
        if have and not running:
            continue
        s = tmdb.season(conn, tv, sn, max_age_days=max_age_days if running else tmdb.DEFAULT_MAX_AGE_DAYS)
        if not s:
            stats["no_tmdb"] += 1
            continue
        conn.execute("UPDATE titles SET episode_dates = ? WHERE id = ?", (json.dumps(s.get("episode_dates") or []), tid))
        stats["filled"] += 1
    conn.commit()
    return stats


if __name__ == "__main__":
    from . import db
    print(json.dumps(fill_episode_dates(db.connect()), ensure_ascii=False))
