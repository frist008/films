"""Ratings onto titles: IMDb (datasets) and Rotten Tomatoes (both scores; per season for series).

IMDb: movie rating = the title's rating; a season gets the series rating plus the mean of its episodes'
ratings (title.episode + title.ratings datasets). RT: films.sources.rt.find with titles, year, people and
the series' start year; only clear matches are written, ambiguous ones go to match_issues.
"""
import csv
import gzip
import json
import sys

from . import db
from .sources import imdb, rt

IMDB_CACHE = db.ROOT / "data" / "cache" / "imdb"


def _tsv(name: str):
    with gzip.open(IMDB_CACHE / name, "rt", encoding="utf-8", newline="") as f:
        reader = csv.reader(f, delimiter="\t", quoting=csv.QUOTE_NONE)
        next(reader)
        yield from reader


def imdb_ratings(conn) -> dict:
    with conn:
        conn.execute("""UPDATE titles SET imdb_rating = (SELECT rating FROM imdb_ratings r WHERE r.tconst = titles.imdb_id),
                                          imdb_votes  = (SELECT votes  FROM imdb_ratings r WHERE r.tconst = titles.imdb_id),
                                          ratings_checked_at = datetime('now')
                        WHERE season_number IS NULL AND imdb_id IS NOT NULL""")
        conn.execute("""UPDATE titles SET imdb_rating = (SELECT rating FROM imdb_ratings r WHERE r.tconst = titles.series_imdb_id),
                                          imdb_votes  = (SELECT votes  FROM imdb_ratings r WHERE r.tconst = titles.series_imdb_id),
                                          ratings_checked_at = datetime('now')
                        WHERE season_number IS NOT NULL AND series_imdb_id IS NOT NULL""")
    # titles of any year (watched / bought / franchise films, older releases): since R5 the loader also loads every
    # tconst linked in titles, so imdb_ratings normally has them; this pass over the cached ratings file stays as a
    # safety net for titles linked after the last `imdb load` (same file → same numbers as the loader would give)
    need = {r[0]: r[1] for r in conn.execute(
        "SELECT CASE WHEN season_number IS NULL THEN imdb_id ELSE series_imdb_id END, season_number IS NULL FROM titles "
        "WHERE imdb_rating IS NULL AND (CASE WHEN season_number IS NULL THEN imdb_id ELSE series_imdb_id END) IS NOT NULL")}
    found = {}
    for tconst, rating, votes in _tsv("title.ratings.tsv.gz"):
        if tconst in need:
            found[tconst] = (float(rating), int(votes))
    with conn:
        for tconst, (rating, votes) in found.items():
            conn.execute("UPDATE titles SET imdb_rating = ?, imdb_votes = ?, ratings_checked_at = datetime('now') "
                         "WHERE imdb_rating IS NULL AND ((season_number IS NULL AND imdb_id = ?) OR "
                         "(season_number IS NOT NULL AND series_imdb_id = ?))", (rating, votes, tconst, tconst))
    # per-season mean of episode ratings
    wanted = {(r[0], r[1]) for r in conn.execute(
        "SELECT series_imdb_id, season_number FROM titles WHERE season_number IS NOT NULL AND series_imdb_id IS NOT NULL")}
    parents = {p for p, _ in wanted}
    ep_season: dict[str, tuple[str, int]] = {}
    for tconst, parent, season, _ep in _tsv("title.episode.tsv.gz"):
        if parent in parents and season != "\\N" and (parent, int(season)) in wanted:
            ep_season[tconst] = (parent, int(season))
    acc: dict[tuple[str, int], list] = {}
    for tconst, rating, votes in _tsv("title.ratings.tsv.gz"):
        key = ep_season.get(tconst)
        if key:
            acc.setdefault(key, []).append((float(rating), int(votes)))
    with conn:
        for (parent, season), vals in acc.items():
            conn.execute("UPDATE titles SET imdb_season_rating = ?, imdb_season_votes = ? "
                         "WHERE series_imdb_id = ? AND season_number = ?",
                         (round(sum(r for r, _ in vals) / len(vals), 2), sum(v for _, v in vals), parent, season))
    return {"movies_rated": conn.execute("SELECT count(*) FROM titles WHERE season_number IS NULL AND imdb_rating IS NOT NULL").fetchone()[0],
            "seasons_series_rated": conn.execute("SELECT count(*) FROM titles WHERE season_number IS NOT NULL AND imdb_rating IS NOT NULL").fetchone()[0],
            "seasons_episode_mean": len(acc), "episodes_used": sum(len(v) for v in acc.values())}


def season_means(pairs: set[tuple[str, int]]) -> dict[tuple[str, int], tuple[float, int]]:
    """(series tconst, season) → (mean episode rating, total votes) from the datasets — the same as titles.imdb_season_rating,
    for seasons that have no title row yet (the «что видел» survey: owner 2026-10-27 «У каждого сезона своя» оценка)."""
    parents = {p for p, _ in pairs}
    ep_season: dict[str, tuple[str, int]] = {}
    for tconst, parent, season, _ep in _tsv("title.episode.tsv.gz"):
        if parent in parents and season != "\\N" and (parent, int(season)) in pairs:
            ep_season[tconst] = (parent, int(season))
    acc: dict[tuple[str, int], list] = {}
    for tconst, rating, votes in _tsv("title.ratings.tsv.gz"):
        key = ep_season.get(tconst)
        if key:
            acc.setdefault(key, []).append((float(rating), int(votes)))
    return {k: (round(sum(r for r, _ in v) / len(v), 1), sum(n for _, n in v)) for k, v in acc.items()}


def _people(conn, title_id: int) -> list[str]:
    rows = conn.execute("""SELECT p.name FROM credits c JOIN people p ON p.id = c.person_id WHERE c.title_id = ?
                           ORDER BY CASE c.role WHEN 'director' THEN 0 WHEN 'actor' THEN 1 ELSE 2 END, c.ord LIMIT 5""",
                        (title_id,)).fetchall()
    return [r[0] for r in rows]


def _store_page(conn, page: dict, url: str, kind: str, season: int | None, parent: str | None, title_id: int):
    conn.execute(
        """INSERT OR REPLACE INTO rt_pages (url, kind, parent_url, season, title, year, critics, critics_reviews,
               critics_liked, critics_not_liked, critics_certified, critics_top, audience, audience_type,
               audience_ratings, audience_banded, audience_liked, audience_not_liked, audience_all, audience_verified,
               poster_url, ems_id, rt_id, info, fetched_at, title_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), ?)""",
        (url, kind, parent, season, page.get("title"), page.get("year"), page.get("critics"), page.get("critics_reviews"),
         page.get("critics_liked"), page.get("critics_not_liked"), page.get("critics_certified"), page.get("critics_top"),
         page.get("audience"), page.get("audience_type"), page.get("audience_ratings"),
         json.dumps(page.get("audience_banded"), ensure_ascii=False) if page.get("audience_banded") is not None else None,
         page.get("audience_liked"), page.get("audience_not_liked"),
         json.dumps(page.get("audience_all"), ensure_ascii=False, default=str) if isinstance(page.get("audience_all"), (dict, list)) else page.get("audience_all"),
         json.dumps(page.get("audience_verified"), ensure_ascii=False, default=str) if isinstance(page.get("audience_verified"), (dict, list)) else page.get("audience_verified"),
         page.get("poster_url"), page.get("ems_id"), page.get("rt_id"),
         json.dumps(page.get("info"), ensure_ascii=False, default=str) if page.get("info") else None, title_id))


def _rt_ambiguous(conn, tid: int, candidates: list[dict], fetch=None, fetch_season=None) -> bool:
    """An ambiguous RT search: first the triage rule (match_triage.rt_decide — year + director, duplicate stub pages,
    the only page listing the season); decided → the page is written, True. Otherwise one 'rt_ambiguous' issue per title,
    ever: no new row when one exists for that title, open or closed (triage closed 434 of them as rt_decided /
    rt_undecided and every run used to recreate them, 2026-10-25). Returns False (the caller marks the title checked)."""
    from . import match_triage
    fetch = fetch or (lambda u: rt.fetch(u))
    fetch_season = fetch_season or (lambda u, s: rt.fetch(u, season_pages=[s]))
    t = dict(conn.execute("SELECT * FROM titles WHERE id = ?", (tid,)).fetchone())
    cands = [{k: c.get(k) for k in ("url", "title", "year", "score")} for c in candidates]
    page, why = match_triage.rt_decide(conn, t, cands, fetch)
    if page:
        with conn:
            match_triage._write_rt(conn, t, page["url"], page, fetch_season)
        return True
    with conn:
        known = conn.execute("SELECT 1 FROM match_issues WHERE problem = 'rt_ambiguous' "
                             "AND json_extract(candidates, '$.title_id') = ? LIMIT 1", (tid,)).fetchone()
        if not known:
            conn.execute("INSERT INTO match_issues (problem, candidates) VALUES ('rt_ambiguous', ?)",
                         (json.dumps({"title_id": tid, "candidates": cands[:5], "triage": why}, ensure_ascii=False),))
    return False


RECHECK_SQL = """SELECT id, kind, season_number, series_imdb_id, title_original, title_latin, title_ru, title_uk, year
    FROM titles t WHERE coalesce(title_original, title_latin, title_ru, title_uk) IS NOT NULL
      AND coalesce(tracker_only, 0) = 0
      AND (rt_checked_at IS NULL OR rt_checked_at < datetime('now', '-20 hours'))   -- not again within one run
      AND ((release_date BETWEEN date('now', ?) AND date('now'))                    -- released in the last N days
           OR (rt_critics IS NULL AND rt_audience IS NULL                           -- or never had an RT score …
               AND EXISTS (SELECT 1 FROM release_titles r WHERE r.title_id = t.id)  -- … and has a release (shown) …
               AND (rt_checked_at IS NULL OR rt_checked_at < datetime('now', '-7 days'))))  -- … weekly (≈3 000 titles,
    ORDER BY year DESC"""                                                            # ~5 s each: not every run)


def rt_ratings(conn, limit: int | None = None, recheck_days: float = 30, never_checked: bool = False,
               stop_on_block: bool = False, recent_days: int | None = None) -> dict:
    """never_checked: every title with rt_checked_at NULL, TMDB-checked or not (R5).
    recent_days: update-db step 5 — titles released in the last N days and titles with releases but no RT score yet
    (RECHECK_SQL), whenever they were last checked (except within the last 20 h: the same run's step 4).
    stop_on_block: the first 403/429 stops the run (stats['blocked'] = the error), nothing is retried around it."""
    if recent_days:
        rows = conn.execute(RECHECK_SQL, (f"-{int(recent_days)} days",)).fetchall()
    elif never_checked:
        rows = conn.execute(
            """SELECT id, kind, season_number, series_imdb_id, title_original, title_latin, title_ru, title_uk, year
               FROM titles WHERE rt_checked_at IS NULL
                 AND coalesce(title_original, title_latin, title_ru, title_uk) IS NOT NULL
                 AND coalesce(tracker_only, 0) = 0   -- no data but the tracker's: a name-only RT hit would be a guess
               ORDER BY year DESC""").fetchall()
    else:
        rows = conn.execute(
            """SELECT id, kind, season_number, series_imdb_id, title_original, title_latin, title_ru, title_uk, year
               FROM titles WHERE tmdb_checked_at IS NOT NULL
                 AND (rt_checked_at IS NULL OR rt_checked_at < datetime('now', ?))
               ORDER BY year DESC""", (f"-{recheck_days} days",)).fetchall()
    rt.STOP_ON_BLOCK = stop_on_block
    if limit:
        rows = rows[:limit]
    stats = {"titles": len(rows), "matched": 0, "with_scores": 0, "ambiguous": 0, "not_found": 0, "errors": 0}
    for i, (tid, kind, season, series_imdb, t_orig, t_lat, t_ru, t_uk, year) in enumerate(rows, 1):
        # progress before the title, not after it: `continue` below skipped the line at i % 100 == 0, and the watchdog
        # saw «НЕТ ДВИЖЕНИЯ» on a working run (2026-10-27)
        if i > 1 and (i - 1) % 100 == 0:
            print(f"  rt {i - 1}/{len(rows)} {stats}", file=sys.stderr, flush=True)
        try:
            series_start = None
            if kind == "series" and series_imdb:
                b = conn.execute("SELECT start_year FROM imdb_basics WHERE tconst = ?", (series_imdb,)).fetchone()
                series_start = b[0] if b else None
            res = rt.find([t_orig, t_lat, t_ru, t_uk], year, kind, people=_people(conn, tid), series_start=series_start)
            if res["status"] == "ambiguous" and _rt_ambiguous(conn, tid, res["candidates"]):
                stats["matched"] += 1
                stats["ambiguous_decided"] = stats.get("ambiguous_decided", 0) + 1
                continue
            if res["status"] != "matched":
                with conn:
                    conn.execute("UPDATE titles SET rt_checked_at = datetime('now') WHERE id = ?", (tid,))
                stats[res["status"]] += 1
                continue
            url = res["match"]["url"]
            page = rt.fetch(url, season_pages=[season] if kind == "series" and season else False)
            if page is None:
                stats["not_found"] += 1
                with conn:
                    conn.execute("UPDATE titles SET rt_checked_at = datetime('now') WHERE id = ?", (tid,))
                continue
            src, src_url, parent = page, url, None
            if kind == "series" and season:
                s = next((s for s in page.get("seasons") or [] if s.get("season") == season), None)
                src = s or {}
                src_url = (s or {}).get("url")
                parent = url
            with conn:
                _store_page(conn, page, url, page.get("kind"), None, None, tid if not season else None)
                if src_url and src is not page:
                    _store_page(conn, src, src_url, "season", season, parent, tid)
                conn.execute(
                    """UPDATE titles SET rt_url = ?, rt_critics = ?, rt_audience = ?, rt_critics_reviews = ?,
                           rt_audience_ratings = ?, rt_audience_type = ?, rt_checked_at = datetime('now') WHERE id = ?""",
                    (src_url, src.get("critics"), src.get("audience"), src.get("critics_reviews"),
                     src.get("audience_ratings"), page.get("audience_type"), tid))
            stats["matched"] += 1
            if src.get("critics") is not None or src.get("audience") is not None:
                stats["with_scores"] += 1
        except rt.RTBlocked as e:
            stats["blocked"] = str(e)
            stats["stopped_at"] = i
            print(f"  RT BLOCKED at {i}/{len(rows)} (title {tid}): {e} -- stopping", file=sys.stderr, flush=True)
            break
        except Exception as e:  # noqa: BLE001 — one bad page must not stop a 4000-title run; counted and logged
            stats["errors"] += 1
            print(f"  RT ERROR title {tid}: {e!r}", file=sys.stderr, flush=True)
    return stats
