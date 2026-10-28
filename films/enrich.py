"""Fill titles from TMDB (stage 4): names RU/UK, countries, genres, runtime, studios, budget/gross,
seasons, Russian trailer, credits, collections.

Tracker data is never overwritten with an empty TMDB value. Disagreements that point at a wrong IMDb id
(year off by more than one, season missing on TMDB, IMDb id unknown to TMDB) go to match_issues.
"""
import json
import re
import sys

from . import db
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
    # closed by films.match_triage as a TMDB data gap (nothing to ask the owner): enrich keeps re-checking the title every
    # run, but the same gap is not reopened as a new issue (triage 2026-10-25: 113 issues came back after one enrich run)
    if conn.execute("SELECT 1 FROM match_issues WHERE problem = ? AND resolved_at IS NOT NULL AND json_valid(candidates) "
                    "AND json_extract(candidates, '$.title_id') = ? "
                    "AND json_extract(candidates, '$.resolved_rule') = 'tmdb_data_gap'", (problem, title_id)).fetchone():
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
           WHERE rt.title_id = ? AND json_extract(r.parsed, '$.year') IS NOT NULL
             AND coalesce(rt.how, '') NOT LIKE 'wrong_link%'""", (title_id,))]


def season_year(conn, title_id: int) -> int | None:
    """The year of a title from its own releases (review 2026-10-28: a season pack «[S01-03] (2018-2025)» gave season 3 the
    year 2018): a film / a one-season release — its first year; the last season of a pack — the pack's last year, the
    first — its first year; a middle season of a pack says nothing (None when nothing else does)."""
    from .match import _seasons, _years
    from .tracker_only import name_seasons
    row = conn.execute("SELECT kind, season_number FROM titles WHERE id = ?", (title_id,)).fetchone()
    if not row:
        return None
    if row[0] != "series":
        ry = _release_years(conn, title_id)
        return min(ry) if ry else None
    s, ys = row[1], []
    for name, parsed in conn.execute(
            """SELECT r.name, r.parsed FROM release_titles rt JOIN releases r ON r.id = rt.release_id
               WHERE rt.title_id = ? AND r.parsed IS NOT NULL AND coalesce(rt.how, '') NOT LIKE 'wrong_link%'""", (title_id,)):
        try:
            p = json.loads(parsed)
        except ValueError:
            continue
        y1, y2 = _years(p)
        seasons = _seasons(p.get("name_parsed") or {}) or name_seasons(name)
        if not y1:
            continue
        if len(seasons) <= 1:
            ys.append(y1)
        elif s == max(seasons):
            ys.append(y2 or y1)
        elif s == min(seasons):
            ys.append(y1)
    return min(ys) if ys else None


def _fill_unknown_season(conn, title_id: int, series: dict | None) -> None:
    """A season TMDB does not have yet («Люпен» S4, «Клиника» S10 — review 2026-10-28: 54 seasons hidden «год None»):
    the year from its releases, the series-level columns from TMDB — only where the row has nothing (never overwritten)."""
    vals = {}
    sy = season_year(conn, title_id)
    if sy:
        vals["year"] = sy
    if series:
        vals.update({"series_tmdb_id": series.get("tmdb_id"), "is_animation": int(bool(series.get("is_animation"))),
                     "title_original": series.get("title_original"), "title_latin": series.get("title_latin"),
                     "title_ru": series.get("title_ru"), "title_uk": series.get("title_uk"),
                     "countries": json.dumps(series["countries"], ensure_ascii=False) if series.get("countries") else None,
                     "genres": json.dumps(series["genres"], ensure_ascii=False) if series.get("genres") else None,
                     "main_genre": series.get("main_genre"), "seasons_total": series.get("number_of_seasons"),
                     "series_status": series.get("series_status"),
                     "studios": json.dumps(series["studios"], ensure_ascii=False) if series.get("studios") else None})
    for k, v in vals.items():
        if v is not None:
            conn.execute(f"UPDATE titles SET {k} = ? WHERE id = ? AND {k} IS NULL", (v, title_id))


def fill_years_from_releases(conn) -> int:
    """Any title still without a year (a TMDB season with no air date — «Гранчестер» S11; a season TMDB / IMDb lack) takes
    `season_year`; a season TMDB has not got at all (an issue «tmdb_no_season» / «tmdb_no_tv_for_imdb») is corrected to it
    too — its year came only from the releases, by the earlier «first year of any release» rule (review 2026-10-28:
    «Террор» S3 → 2018, «Доктор Кто» S14 → 2005); so is a season TMDB has without an air date (review 2026-10-28:
    «Александра Эль» S7 of the pack «1-7 сезоны … 2018-2025» kept 2018)."""
    n = 0
    no_tmdb = {r[0] for r in conn.execute(
        """SELECT DISTINCT json_extract(candidates, '$.title_id') FROM match_issues
           WHERE problem IN ('tmdb_no_season', 'tmdb_no_tv_for_imdb') AND json_valid(candidates)""") if r[0]}
    with conn:
        for tid, year, tmdb_id, rdate in conn.execute(
                """SELECT t.id, t.year, t.tmdb_id, t.release_date FROM titles t WHERE t.duplicate_of IS NULL AND EXISTS (
                     SELECT 1 FROM release_titles x WHERE x.title_id = t.id AND coalesce(x.how, '') NOT LIKE 'wrong_link%')
                   AND (t.year IS NULL OR (t.kind = 'series' AND (t.tmdb_id IS NULL OR t.release_date IS NULL)))""").fetchall():
            if year is not None and tid not in no_tmdb and not (tmdb_id and not rdate):
                continue
            sy = season_year(conn, tid)
            if sy and sy != year:
                n += conn.execute("UPDATE titles SET year = ? WHERE id = ?", (sy, tid)).rowcount
    return n


def fill_ru_names_from_releases(conn) -> int:
    """A title TMDB gave no Russian name («双世异闻录», «偷生») takes the Russian name its releases write most often
    (rutor first) — only an empty title_ru (review 2026-10-28: 49 visible rows showed hieroglyphs only). Not from a pack
    of several films (review round 9: «Next Avengers», «Inhumans» got «Люди Икс» from «X-Men Complete Collection»)."""
    n = 0
    for (tid,) in conn.execute(
            """SELECT t.id FROM titles t WHERE t.title_ru IS NULL AND t.duplicate_of IS NULL AND EXISTS (
                 SELECT 1 FROM release_titles x WHERE x.title_id = t.id AND coalesce(x.how, '') NOT LIKE 'wrong_link%')""").fetchall():
        name = ru_name_from_releases(conn, tid)
        if name:
            n += conn.execute("UPDATE titles SET title_ru = ? WHERE id = ? AND title_ru IS NULL", (name, tid)).rowcount
    conn.commit()
    return n


def ru_name_from_releases(conn, tid: int) -> str | None:
    """The Russian name the title's own releases write most often (rutor first); packs of several films and Ukrainian
    trackers left out."""
    from collections import Counter
    from .tracker_only import clean_name
    rank = {"rutor": 0, "rutracker": 1, "nnmclub": 2, "kinozal": 3, "rustorka": 4}
    votes: Counter = Counter()
    for src, parsed in conn.execute(
            """SELECT r.source, r.parsed FROM release_titles x JOIN releases r ON r.id = x.release_id WHERE x.title_id = ?
                 AND coalesce(x.how, '') NOT LIKE 'wrong_link%' AND coalesce(x.how, '') NOT LIKE 'collection_pack%'
                 AND r.source NOT IN ('toloka', 'mazepa')
                 AND (SELECT count(*) FROM release_titles y JOIN titles o ON o.id = y.title_id WHERE y.release_id = r.id
                      AND o.kind = 'movie' AND coalesce(y.how, '') NOT LIKE 'wrong_link%') < 2""", (tid,)):
        try:
            p = json.loads(parsed) if parsed else {}
        except ValueError:
            continue
        ru = clean_name(p.get("title_ru") or (p.get("name_parsed") or {}).get("title_ru"))
        if ru and re.search(r"[а-яё]", ru, re.I):
            votes[ru.strip()] += 10 - rank.get(src, 5)
    return votes.most_common(1)[0][0] if votes else None


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


def title_fix() -> dict:
    """[title_fix] "<imdb id>" = {ru = "…", uk = "…"} — names TMDB gets wrong, applied on every enrich."""
    import tomllib
    with open(db.ROOT / "config" / "settings.toml", "rb") as f:
        return tomllib.load(f).get("title_fix", {})


def _apply(conn, title_id: int, row: dict, extra: dict):
    cur = dict(conn.execute("SELECT * FROM titles WHERE id = ?", (title_id,)).fetchone())
    sets = {k: row[k] for k in UPDATABLE if k in row and row[k] not in (None, "", "[]")}
    if cur.get("title_ru") and not row.get("title_ru"):
        sets.pop("title_ru", None)  # keep the tracker's Russian title when TMDB has none
    # TMDB's own mistakes in a localized name (config/settings.toml [title_fix]; «Братство: Последняя фантазия XV» has
    # Kingsglaive's Russian name on TMDB, review 2026-10-28)
    fix = title_fix().get(cur.get("imdb_id") or cur.get("series_imdb_id") or "", {})
    sets.update({f"title_{k}": v for k, v in fix.items() if k in ("ru", "uk")})
    if "title_uk" in sets and "title_uk_source" in cur:
        # TMDB's Ukrainian title wins over the IMDb-aka fallback (owner 2026-10-25); without one the fallback stays
        sets["title_uk_source"] = "tmdb" + (f":{row['title_uk_source']}" if row.get("title_uk_source") else "")
    sets.update({k: v for k, v in extra.items() if v is not None})
    # a TMDB film length under 10 min against IMDb's feature length is TMDB's gap (review round 13: «Крепкий Харт 2» 1 min,
    # IMDb 92) → IMDb's
    if cur.get("kind") == "movie" and (sets.get("runtime_min") or 99) < 10 and cur.get("imdb_id"):
        b = conn.execute("SELECT runtime_min FROM imdb_basics WHERE tconst = ?", (cur["imdb_id"],)).fetchone()
        if b and b[0] and b[0] >= 40:
            sets["runtime_min"] = b[0]
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
                    _fill_unknown_season(conn, tid, None)   # the year from its releases at least
                    continue
                if sid not in series_cache:
                    series_cache[sid] = tmdb.tv(conn, sid, max_age_days)
                series = series_cache[sid]
                s = tmdb.season(conn, sid, season_no, max_age_days) if series else None
                if not s:
                    issue(tid, "tmdb_no_season", {"series_imdb_id": key, "tmdb_tv": sid, "season": season_no,
                                                  "tmdb_seasons": series.get("number_of_seasons") if series else None})
                    _fill_unknown_season(conn, tid, series)
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
    stats["years_from_releases"] = fill_years_from_releases(conn)   # a season TMDB has with no air date: «год None»
    stats["ru_names_from_releases"] = fill_ru_names_from_releases(conn)   # TMDB gave no Russian name
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
