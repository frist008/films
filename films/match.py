"""Group releases into titles (stage 3).

A title is a movie, or ONE season of a series (owner, 2026-10-23). A release is linked through
release_titles to every title it contains (a S01-03 pack -> three season titles).

Identity comes from the IMDb id on the release page; it is accepted only when IMDb's own year/type agree
with the release. Otherwise the release's titles are resolved against the IMDb datasets
(films.sources.imdb.resolve); anything not clearly matched goes to match_issues — never guessed.
"""
import json
import re
import sys

from .sources import imdb

PACK_RE = re.compile(r"Коллекция|Антология|Дилогия|Трилогия|Тетралогия|Квадрилогия|Пенталогия|Гексалогия|"
                     r"Сага\b|Collection|Anthology|Dilogy|Duology|Trilogy|Quadrilogy|Saga\b", re.I)
SERIES_TYPES = {"tvSeries", "tvMiniSeries"}
MOVIE_TYPES = {"movie", "tvMovie", "video", "tvSpecial", "short"}


def _basics(conn, tconst: str):
    return conn.execute("SELECT * FROM imdb_basics WHERE tconst = ?", (tconst,)).fetchone()


def _years(p: dict) -> tuple[int | None, int | None]:
    np = p.get("name_parsed") or {}
    y1 = np.get("year_from") or np.get("year") or p.get("year")
    y2 = np.get("year_to") or y1
    return y1, y2


def _year_ok(b, kind: str, y1: int | None, y2: int | None) -> bool:
    """IMDb record agrees with the release's year (movies ±1; series: ranges overlap ±1)."""
    if not y1 or not b["start_year"]:
        return True  # nothing to compare — accepted, the page link is the only evidence
    if kind == "movie":
        return abs(b["start_year"] - y1) <= 1
    end = b["end_year"] or 9999
    return b["start_year"] - 1 <= y2 and y1 <= end + 1


def _kind(p: dict, category: str, b) -> str:
    if b is not None:
        if b["title_type"] in SERIES_TYPES:
            return "series"
        if b["title_type"] in MOVIE_TYPES:
            return "movie"
    np = p.get("name_parsed") or {}
    if category in ("foreign_series", "russian_series") or np.get("seasons") or np.get("episodes"):
        return "series"
    return "movie"


def _issue(conn, release_id: int, problem: str, candidates=None):
    conn.execute(
        "INSERT INTO match_issues (release_id, problem, candidates) VALUES (?, ?, ?)",
        (release_id, problem, json.dumps(candidates, ensure_ascii=False, default=str) if candidates else None))


def _title_names(p: dict) -> list[str]:
    np = p.get("name_parsed") or {}
    out = [p.get("title_original"), np.get("title_original"), *(np.get("titles_other") or []),
           p.get("title_ru"), np.get("title_ru"), np.get("title_uk"), *(np.get("titles_ru_alt") or [])]
    return [t for t in dict.fromkeys(out) if t]


def _movie_title(conn, tconst: str, b, p: dict, category: str) -> int:
    row = conn.execute("SELECT id FROM titles WHERE imdb_id = ? AND season_number IS NULL", (tconst,)).fetchone()
    if row:
        return row[0]
    np = p.get("name_parsed") or {}
    cur = conn.execute(
        """INSERT INTO titles (kind, is_animation, tracker_category, title_original, title_ru, title_uk, year,
                               imdb_id, runtime_min)
           VALUES ('movie', ?, ?, ?, ?, ?, ?, ?, ?)""",
        (1 if category in ("animation", "anime") or "Animation" in ((b["genres"] if b else "") or "") else 0, category,
         (b["original_title"] if b else None) or p.get("title_original") or np.get("title_original"),
         p.get("title_ru") or np.get("title_ru"), np.get("title_uk"),
         (b["start_year"] if b else None) or p.get("year"), tconst,
         (b["runtime_min"] if b else None)))
    return cur.lastrowid


def _season_titles(conn, tconst: str, b, p: dict, category: str, seasons: list[int]) -> list[int]:
    ids = []
    np = p.get("name_parsed") or {}
    key = f"imdb:{tconst}"
    for s in seasons:
        row = conn.execute("SELECT id FROM titles WHERE series_key = ? AND season_number = ?", (key, s)).fetchone()
        if row:
            ids.append(row[0])
            continue
        cur = conn.execute(
            """INSERT INTO titles (kind, season_number, series_key, series_imdb_id, is_animation, tracker_category,
                                   title_original, title_ru, title_uk)
               VALUES ('series', ?, ?, ?, ?, ?, ?, ?, ?)""",
            (s, key, tconst,
             1 if category in ("animation", "anime") or "Animation" in ((b["genres"] if b else "") or "") else 0, category,
             (b["original_title"] if b else None) or p.get("title_original") or np.get("title_original"),
             p.get("title_ru") or np.get("title_ru"), np.get("title_uk")))
        ids.append(cur.lastrowid)
    return ids


def match_releases(conn, retry_issues: bool = False) -> dict:
    """Link every parsed, not-yet-linked release to its title(s)."""
    if retry_issues:
        with conn:
            conn.execute("DELETE FROM match_issues WHERE resolved_at IS NULL AND release_id IS NOT NULL")
    pending = conn.execute(
        """SELECT r.id, r.category, r.parsed FROM releases r
           WHERE r.parsed IS NOT NULL
             AND NOT EXISTS (SELECT 1 FROM release_titles t WHERE t.release_id = r.id)
             AND NOT EXISTS (SELECT 1 FROM match_issues m WHERE m.release_id = r.id AND m.resolved_at IS NULL)""").fetchall()
    stats = {"releases": len(pending), "linked": 0, "via_page_imdb": 0, "via_resolve": 0,
             "issues": {}, "season_assumed_1": 0}
    for i, (rid, category, parsed) in enumerate(pending, 1):
        p = json.loads(parsed)
        np = p.get("name_parsed") or {}
        y1, y2 = _years(p)
        tconst, how = p.get("imdb_id"), "imdb_page"
        b = _basics(conn, tconst) if tconst else None
        kind = _kind(p, category, b)
        if kind == "movie" and PACK_RE.search(np.get("name") or ""):
            # multi-film pack: the page links only the first film; linked to all parts later (TMDB collection)
            with conn:
                _issue(conn, rid, "collection_pack", {"page_imdb": tconst, "years": [y1, y2]})
            stats["issues"]["collection_pack"] = stats["issues"].get("collection_pack", 0) + 1
            continue
        if tconst and b is not None and not _year_ok(b, kind, y1, y2):
            with conn:
                _issue(conn, rid, "imdb_year_mismatch",
                       {"page_imdb": tconst, "imdb_year": b["start_year"], "release_years": [y1, y2]})
            stats["issues"]["imdb_year_mismatch"] = stats["issues"].get("imdb_year_mismatch", 0) + 1
            continue
        if not tconst:
            res = imdb.resolve(conn, _title_names(p), y1, "series" if kind == "series" else "movie")
            if res["status"] != "matched":
                with conn:
                    _issue(conn, rid, f"no_imdb_{res['status']}",
                           [{k: c.get(k) for k in ("tconst", "title", "year", "type", "score", "votes")}
                            for c in res["candidates"][:5]])
                key = f"no_imdb_{res['status']}"
                stats["issues"][key] = stats["issues"].get(key, 0) + 1
                continue
            tconst, how = res["match"]["tconst"], "imdb_resolve"
            b = _basics(conn, tconst)
            kind = _kind(p, category, b)
        with conn:
            if kind == "movie":
                tids = [_movie_title(conn, tconst, b, p, category)]
            else:
                seasons = np.get("seasons") or []
                if not seasons:
                    # "[1-8 из 8]" style or a miniseries: no season number written -> season 1, counted
                    seasons = [1]
                    stats["season_assumed_1"] += 1
                tids = _season_titles(conn, tconst, b, p, category, seasons)
            for tid in tids:
                conn.execute("INSERT OR IGNORE INTO release_titles (release_id, title_id, how) VALUES (?, ?, ?)",
                             (rid, tid, how))
        stats["linked"] += 1
        stats["via_page_imdb" if how == "imdb_page" else "via_resolve"] += 1
        if i % 2000 == 0:
            print(f"  matched {i}/{len(pending)}", file=sys.stderr, flush=True)
    return stats


def link_exact_names(conn) -> dict:
    """Fallback for releases `match_releases` left unlinked (no IMDb link on the page): «RU / … / Original (year)» equal to
    the RU or original name of exactly one film title, year ±1 (R144, 2026-10-24: «Наследники», «Джиперс Криперс 3» were
    left without their releases). Films only (a series release needs its season); ambiguous names are not touched."""
    names: dict[tuple[str, int], set[int]] = {}
    for tid, ru, orig, y in conn.execute("SELECT id, title_ru, title_original, year FROM titles WHERE kind = 'movie' AND year IS NOT NULL"):
        for n in (ru, orig):
            if n:
                names.setdefault((n.casefold().replace("ё", "е").strip(), y), set()).add(tid)
    rows = conn.execute("""SELECT r.id, r.name FROM releases r LEFT JOIN release_titles x ON x.release_id = r.id
                           WHERE x.release_id IS NULL AND r.category IN ('foreign_films', 'animation')""").fetchall()
    st = {"unlinked": len(rows), "linked": 0, "ambiguous": 0}
    with conn:
        for rid, name in rows:
            m = re.match(r"^(.*?)\((\d{4})\)", name or "")
            if not m:
                continue
            y = int(m.group(2))
            cand = set()
            for part in (p.strip().casefold().replace("ё", "е") for p in m.group(1).split("/") if p.strip()):
                for yy in (y, y - 1, y + 1):
                    cand |= names.get((part, yy), set())
            if len(cand) == 1:
                tid = cand.pop()
                conn.execute("UPDATE releases SET title_id = ? WHERE id = ? AND title_id IS NULL", (tid, rid))
                conn.execute("INSERT OR IGNORE INTO release_titles (release_id, title_id, how) VALUES (?, ?, 'exact_name_year')",
                             (rid, tid))
                st["linked"] += 1
            elif cand:
                st["ambiguous"] += 1
    return st
