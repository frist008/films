"""Group releases into titles (stage 3).

A title is a movie, or ONE season of a series (owner, 2026-10-23). A release is linked through
release_titles to every title it contains (a S01-03 pack -> three season titles).

Identity comes from the IMDb id on the release page; it is accepted only when IMDb's own year/type agree
with the release. Otherwise the release's titles are resolved against the IMDb datasets
(films.sources.imdb.resolve), then — namesakes or nothing found — against TMDB search (original / latin / Russian
names as separate queries); anything not clearly matched goes to match_issues — never guessed.
"""
import json
import re
import sqlite3
import sys

from .enrich import _apply, _collection, _credits
from .link import ensure_movie, ensure_season
from .sources import imdb, tmdb

PACK_RE = re.compile(r"Коллекция|Антология|Дилогия|Трилогия|Тетралогия|Квадрилогия|Пенталогия|Гексалогия|"
                     r"Collection|Anthology|Dilogy|Duology|Trilogy|Quadrilogy", re.I)
# «Сага» is a pack only with a year span («Сумерки. Сага (2008-2012)»): «Horizon: An American Saga - Chapter 1 (2024)»,
# «Корни: Сага о вампирах / Feed (2026)» are single films
SAGA_RE = re.compile(r"Сага\b|Saga\b", re.I)
# «Человек-паук: Новый день 3D / Spider-Man: Brand New Day 3D (2026)» is the ordinary film
SUFFIX_3D_RE = re.compile(r"\s+3D$", re.I)
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


def _is_pack(name: str, y1: int | None, y2: int | None) -> bool:
    return bool(PACK_RE.search(name) or (SAGA_RE.search(name) and y1 and y2 and y2 > y1))


def _title_names(p: dict) -> list[str]:
    """Original first, then the other latin / native names, then Russian / Ukrainian (the search order)."""
    np = p.get("name_parsed") or {}
    out = [p.get("title_original"), np.get("title_original"), *(np.get("titles_other") or []),
           p.get("title_ru"), np.get("title_ru"), np.get("title_uk"), *(np.get("titles_ru_alt") or [])]
    out = [SUFFIX_3D_RE.sub("", t).strip() for t in out if t]
    return [t for t in dict.fromkeys(out) if t]


def _original_names(p: dict) -> list[str]:
    """The non-Cyrillic names of the release (original / latin); empty when it has only Russian ones."""
    return [t for t in _title_names(p) if not re.search(r"[а-яёіїєґ]", t, re.I)]


def _imdb_names(conn, tconst: str, b) -> set[str]:
    out = {imdb.norm(b["primary_title"]), imdb.norm(b["original_title"])} if b is not None else set()
    out |= {imdb.norm(r[0]) for r in conn.execute("SELECT title FROM imdb_akas WHERE tconst = ?", (tconst,))}
    return out - {""}


def _page_year_ok(conn, tconst: str, b, kind: str, p: dict, y1: int | None, y2: int | None, seasons: list[int]) -> bool:
    """_year_ok, widened when a release name equals the IMDb record's name (primary / original / aka): a movie ±2 (the
    tracker writes the production year: «Ложный след / Misdirection (2024)», IMDb and TMDB 2026), a later season of a
    series begun before the release (IMDb's end year is stale: «Word of Honor [S02] (2025)», IMDb 2022–2022)."""
    if _year_ok(b, kind, y1, y2):
        return True
    if not any(imdb.norm(t) in _imdb_names(conn, tconst, b) for t in _title_names(p)):
        return False
    if kind == "movie":
        return abs(b["start_year"] - y1) <= 2
    return max(seasons or [1]) >= 2 and b["start_year"] < y1


def _season_fit(kind: str, seasons: list[int], y1: int | None, y2: int | None, start: int | None) -> bool:
    """A season-1 release is the series' start year ±1; a later season comes after the start year («Pulse [S01] (2025)»
    is not the 2014 «Syke» known as «Pulse»; «Platonic [S02] (2025)» is not the one-season 2025 namesake; «[S03]
    (2024-2026)» of a show begun in 2024 fits). A requirement: a series not fitting is another show (or the tracker's
    year is the dub's, «Kongsuni and Friends [S01-02] (2025)» of 2014) — left for review, never guessed."""
    if kind != "series" or not y1 or not start:
        return True
    if min(seasons or [1]) == 1:
        return abs(start - y1) <= 1
    return start < (y2 or y1)


def _narrow(pool: list[dict], *preds) -> list[dict]:
    """Apply each filter only when it leaves something (a preference, not a requirement)."""
    for pred in preds:
        kept = [c for c in pool if pred(c)]
        if kept:
            pool = kept
    return pool


def _imdb_pick(conn, res: dict, p: dict, category: str, kind: str, y1: int | None, y2: int | None,
               seasons: list[int], exclude: set) -> dict | None:
    """An ambiguous imdb.resolve decided among exact-name candidates (score >= 0.9: name, year and type fit): season fit
    (_season_fit), then animation vs live action by the tracker category («One Piece [S02]» in «Зарубежные сериалы» is
    the 2023 live-action show, not the 1999 anime), then one far more voted (>= 1000 votes and 20x every other: «The
    Gentlemen» 2024 series 211 914 vs «Gentlemen» 2023 42). With no exact-name candidate: the only one a year off with an
    exact name, nothing else close, carrying the release's original name when it has one («Рэкетир. Новые времена
    (2024)» = «Racketeer 3» 2025; «Исчезнувший / My Son» is not «Kill Me Now» found via the Russian name only)."""
    cands = [c for c in res.get("candidates") or [] if c["tconst"] not in exclude]
    fit = [c for c in cands if _season_fit(kind, seasons, y1, y2, c["start_year"])]
    exact = [c for c in fit if c["score"] >= 0.9]
    if exact:
        genres = {c["tconst"]: ((_basics(conn, c["tconst"]) or {"genres": ""})["genres"] or "") for c in exact}
        anim = category in ("animation", "anime")
        pool = _narrow(exact, lambda c: ("Animation" in genres[c["tconst"]]) == anim)
        if len(pool) == 1:
            return pool[0]
        pool.sort(key=lambda c: -(c["votes"] or 0))
        if (pool[0]["votes"] or 0) >= 1000 and pool[0]["votes"] >= 20 * max(pool[1]["votes"] or 0, 1):
            return pool[0]
        return None
    if any(c["score"] >= 0.9 for c in cands):
        return None  # exact-name candidates exist, none fits the season
    near = [c for c in fit if c["score"] >= 0.7 and c["year_diff"] == 1 and c["matched"] == "exact"]
    if len(near) != 1 or any(c is not near[0] and c["score"] >= 0.5 for c in cands):
        return None
    orig = _original_names(p)
    names = _imdb_names(conn, near[0]["tconst"], _basics(conn, near[0]["tconst"]))
    if orig and not any(imdb.norm(t) in names for t in orig):
        return None
    return near[0]


def _tmdb_pick(conn, res: dict, p: dict, category: str, kind: str, y1: int | None, y2: int | None,
               seasons: list[int]) -> dict | None:
    """A tmdb.search result accepted for a release: 'matched' with an exact name (>= 0.9; a loose fit is another film)
    and, for a movie, the year within 1; 'ambiguous' decided like _imdb_pick — season fit, animation by category, far
    more voted (>= 50 votes and 5x the next: the TMDB convention of disks / link), or the only exact-name candidate, a
    year off, that carries the release's original name."""
    cands = [c for c in res.get("candidates") or [] if (c.get("name_fit") or 0) >= 0.9]
    fits = lambda c: _season_fit(kind, seasons, y1, y2, c.get("year"))
    orig = {imdb.norm(t) for t in _original_names(p)}
    covers = lambda c: not orig or bool(orig & {imdb.norm(q) for q in c.get("titles_matched") or []})
    year_ok = lambda c: kind == "series" or not y1 or c.get("year_diff") is None or c["year_diff"] <= 1
    if res["status"] == "matched":
        # season fit too: «Mooboo Cops [S01] (2024)» is a new show, not season 1 of the 2019 namesake TMDB knows
        b = res["best"]
        return b if ((b.get("name_fit") or 0) >= 0.9 and year_ok(b)
                     and fits(b)) else None
    exact = [c for c in cands if c["score"] >= 0.75 and (kind == "series" or c.get("year_diff") == 0) and fits(c)]
    if exact:
        raw = tmdb.movie_raw if kind == "movie" else tmdb.tv_raw
        info = {c["tmdb_id"]: raw(conn, c["tmdb_id"]) or {} for c in exact}
        anim = category in ("animation", "anime")
        pool = _narrow(exact, lambda c: any(g.get("id") == 16 for g in info[c["tmdb_id"]].get("genres") or []) == anim, covers)
        if len(pool) == 1:
            return pool[0]
        votes = sorted(((info[c["tmdb_id"]].get("vote_count") or 0, i, c) for i, c in enumerate(pool)),
                       key=lambda x: (-x[0], x[1]))
        return votes[0][2] if votes[0][0] >= 50 and votes[0][0] >= 5 * votes[1][0] else None
    near = [c for c in cands if year_ok(c) and covers(c) and fits(c)]
    return near[0] if len(near) == 1 and len(cands) == 1 else None


def _tmdb_movie_title(conn, tmdb_id: int) -> int | None:
    """Title for a TMDB movie: by its IMDb id (link.ensure_movie), else the title already holding the TMDB id, else a new
    TMDB-only title filled the way ensure_movie fills one (Central Asian / Russian films often have no IMDb page)."""
    m = tmdb.movie(conn, tmdb_id)
    if not m:
        return None
    if m.get("imdb_id"):
        return ensure_movie(conn, m["imdb_id"], "match_tmdb")
    row = conn.execute("SELECT id FROM titles WHERE tmdb_id = ? AND season_number IS NULL", (tmdb_id,)).fetchone()
    if row:
        return row[0]
    tid = conn.execute("INSERT INTO titles (kind, tmdb_id, tmdb_type) VALUES ('movie', ?, 'movie')", (tmdb_id,)).lastrowid
    _apply(conn, tid, tmdb.movie_row(m), {"collection_id": (m.get("collection") or {}).get("id"),
                                          "country_codes": json.dumps(m.get("country_codes"), ensure_ascii=False)})
    _credits(conn, tid, m)
    if m.get("collection"):
        _collection(conn, m["collection"]["id"])
    return tid


def _resolve(conn, p: dict, category: str, kind: str, y1: int | None, y2: int | None, seasons: list[int],
             exclude: frozenset = frozenset()):
    """Names -> ('imdb', tconst) | ('tmdb', tmdb_id) | (None, problem, candidates): IMDb datasets first, TMDB second."""
    names = _title_names(p)
    res = imdb.resolve(conn, names, y1, "series" if kind == "series" else "movie")
    cands = [c for c in res["candidates"] if c["tconst"] not in exclude]
    if res["status"] == "matched" and res["match"]["tconst"] not in exclude:
        return "imdb", res["match"]["tconst"], None
    pick = _imdb_pick(conn, res, p, category, kind, y1, y2, seasons, exclude) if cands else None
    if pick:
        return "imdb", pick["tconst"], None
    hints = {"directors": p.get("directors") or [], "countries": p.get("countries") or []}
    tr = tmdb.search(conn, names, y1, "tv" if kind == "series" else "movie", hints=hints)
    tpick = _tmdb_pick(conn, tr, p, category, kind, y1, y2, seasons) if tr["candidates"] else None
    if tpick:
        return "tmdb", tpick["tmdb_id"], None
    status = "ambiguous" if cands or any((c.get("name_fit") or 0) >= 0.9 for c in tr["candidates"]) else "not_found"
    return None, f"no_imdb_{status}", (
        [{k: c.get(k) for k in ("tconst", "primary_title", "start_year", "title_type", "score", "votes")} for c in cands[:5]]
        + [{k: c.get(k) for k in ("tmdb_id", "title", "original_title", "year", "score", "vote_count")}
           for c in tr["candidates"][:5]])


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
        # superseded, not deleted (nothing is deleted from the DB): the retry writes a fresh issue if it is still unmatched
        with conn:
            conn.execute("UPDATE match_issues SET resolved_at = datetime('now') WHERE resolved_at IS NULL AND release_id IS NOT NULL")
    pending = conn.execute(
        """SELECT r.id, r.category, r.parsed FROM releases r
           WHERE r.parsed IS NOT NULL
             AND NOT EXISTS (SELECT 1 FROM release_titles t WHERE t.release_id = r.id)
             AND NOT EXISTS (SELECT 1 FROM match_issues m WHERE m.release_id = r.id AND m.resolved_at IS NULL)""").fetchall()
    stats = {"releases": len(pending), "linked": 0, "via_page_imdb": 0, "via_resolve": 0, "via_tmdb": 0,
             "issues": {}, "season_assumed_1": 0}
    for i, (rid, category, parsed) in enumerate(pending, 1):
        p = json.loads(parsed)
        np = p.get("name_parsed") or {}
        y1, y2 = _years(p)
        tconst, how, tmdb_id = p.get("imdb_id"), "imdb_page", None
        b = _basics(conn, tconst) if tconst else None
        kind = _kind(p, category, b)
        seasons = np.get("seasons") or []

        def issue(problem, candidates):
            with conn:
                _issue(conn, rid, problem, candidates)
            stats["issues"][problem] = stats["issues"].get(problem, 0) + 1

        if kind == "movie" and _is_pack(np.get("name") or "", y1, y2):
            # multi-film pack: the page links only the first film; linked to all parts later (TMDB collection)
            issue("collection_pack", {"page_imdb": tconst, "years": [y1, y2]})
            continue
        exclude, mismatch = frozenset(), None
        if tconst and b is not None and not _page_year_ok(conn, tconst, b, kind, p, y1, y2, seasons):
            # the page links another title (often an earlier part: «Коты Эрмитажа 2 (2026)» -> «Коты Эрмитажа» 2023):
            # resolved by name instead, that title excluded
            exclude = frozenset({tconst})
            mismatch = {"page_imdb": tconst, "imdb_year": b["start_year"], "release_years": [y1, y2]}
            tconst = None
        if not tconst:
            src, key, cands = _resolve(conn, p, category, kind, y1, y2, seasons, exclude)
            if src is None:
                issue("imdb_year_mismatch", dict(mismatch, candidates=cands)) if mismatch else issue(key, cands)
                continue
            if src == "imdb":
                tconst, how = key, "imdb_resolve"
                b = _basics(conn, tconst)
                kind = _kind(p, category, b)
            else:
                tmdb_id, how = key, "tmdb_search"
        if kind == "series" and not seasons:
            # "[1-8 из 8]" style or a miniseries: no season number written -> season 1, counted
            seasons = [1]
            stats["season_assumed_1"] += 1
        try:
            with conn:
                if tmdb_id and kind == "movie":
                    tids = [_tmdb_movie_title(conn, tmdb_id)]
                elif tmdb_id:
                    tids = [ensure_season(conn, tmdb_id, s) for s in seasons]
                elif kind == "movie":
                    tids = [_movie_title(conn, tconst, b, p, category)]
                else:
                    tids = _season_titles(conn, tconst, b, p, category, seasons)
                if None in tids:
                    raise LookupError(f"TMDB has no {'movie' if kind == 'movie' else 'season ' + str(seasons)}")
                for tid in tids:
                    conn.execute("INSERT OR IGNORE INTO release_titles (release_id, title_id, how) VALUES (?, ?, ?)",
                                 (rid, tid, how))
                    # a title made earlier out of tracker scope (watched / bought / TMDB) now has a tracker release
                    conn.execute("UPDATE titles SET tracker_category = ? WHERE id = ? AND tracker_category IS NULL",
                                 (category, tid))
        except (LookupError, sqlite3.IntegrityError) as e:
            issue("tmdb_no_title", {"tmdb_id": tmdb_id, "kind": kind, "seasons": seasons, "error": str(e)})
            continue
        stats["linked"] += 1
        stats[{"imdb_page": "via_page_imdb", "imdb_resolve": "via_resolve", "tmdb_search": "via_tmdb"}[how]] += 1
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
            parts = (SUFFIX_3D_RE.sub("", p.strip()).casefold().replace("ё", "е") for p in m.group(1).split("/"))
            for part in (p for p in parts if p):
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
