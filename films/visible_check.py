"""Invariants of the rows the sheet is about to show (review 2026-10-28: 149 «не интересен» titles, 27 DV5-only releases and
series with Яна as «ответственный» were in the rebuilt sheet). sheet._rebuild refuses to write when any is broken.

Run:  python -X utf8 -m films.visible_check   (computes the rows the way the sheet does and prints the problems)
"""
from __future__ import annotations

import re
import sys

from . import db


def check(conn, rows: list[dict], top_imdb: list[str] | None = None) -> list[str]:
    """`rows` — rank.compute in its order; `top_imdb` — `[top] imdb` of settings.toml (None: read it)."""
    if top_imdb is None:
        from .rank import settings
        top_imdb = list(settings().get("top", {}).get("imdb") or [])
    shown = [r for r in rows if not r.get("hidden_reasons")]
    ids = {r["title"]["id"] for r in shown}
    problems = lost_links(conn)
    # one release, one row of its title (review round 10: «Шпион» Open Matte = «Оригинал без апскейла», «Ранго» ×2)
    pairs: dict[tuple, int] = {}
    for r in shown:
        if (r.get("release") or {}).get("id"):
            k = (r["title"]["id"], r["release"]["id"])
            pairs[k] = pairs.get(k, 0) + 1
    problems += [f"одна раздача — две строки тайтла: {tid} ← {rid}" for (tid, rid), n in pairs.items() if n > 1]
    problems += film_mismatch_problems(conn, shown)
    # a row with releases hidden only for an unknown year (review 2026-10-28: 54 seasons TMDB has not got yet, «год None»)
    from .enrich import season_year
    seen = set()
    for r in rows:
        t = r["title"]
        if r.get("variant_index") or t["id"] in seen:
            continue
        seen.add(t["id"])
        if r.get("hidden_reasons") == ["год None"] and r.get("release"):
            problems.append(f"скрыт только «год None» при раздачах: {t['id']} {t.get('title_ru') or t.get('title_original')}")
        elif len(r.get("hidden_reasons") or []) == 1 and r["hidden_reasons"][0].startswith("год ") \
                and t.get("kind") == "series" and not (t.get("tmdb_id") and t.get("release_date")):
            # a season TMDB lacks (or has with no air date), its year from the releases: the year of its own releases
            # (review 2026-10-28: a pack's first year — «Террор» S3, «Александра Эль» S7 as 2018)
            sy = season_year(conn, t["id"])
            if sy and sy != t.get("year"):
                problems.append(f"год сезона не по его раздачам ({t.get('year')} ≠ {sy}): {t['id']} "
                                f"{t.get('title_ru') or t.get('title_original')}")
    if not ids:
        return problems
    q = ",".join("?" * len(ids))
    for tid, name in conn.execute(f"""SELECT u.title_id, coalesce(t.title_ru, t.title_original) FROM user_marks u
                                      JOIN titles t ON t.id = u.title_id
                                      WHERE u.mark = 'not_interested' AND u.title_id IN ({q})""", list(ids)):
        problems.append(f"«не интересен» в таблице: {tid} {name}")
    for tid, name in conn.execute(f"""SELECT w.title_id, coalesce(t.title_ru, t.title_original) FROM watched w
                                      JOIN titles t ON t.id = w.title_id
                                      WHERE substr(w.source, -8) != '_partial' AND w.title_id IN ({q})""", list(ids)):
        problems.append(f"просмотренное в таблице: {tid} {name}")
    n = conn.execute("""SELECT count(*) FROM user_marks WHERE mark IS NULL AND ordered_by IS NULL AND note IS NULL""").fetchone()[0]
    if n:
        problems.append(f"пустых пометок user_marks (затёртые?): {n}")
    excluded = {(s, i) for s, i in conn.execute(
        "SELECT source, source_id FROM excluded_releases WHERE reason NOT LIKE 'lifted:%'")}
    for r in shown:
        rel = r.get("release") or {}
        if (rel.get("source"), rel.get("source_id")) in excluded:
            problems.append(f"исключённая раздача в таблице: {rel.get('source')} {rel.get('source_id')} "
                            f"({r['title']['id']})")
    for tid, person in conn.execute(f"""SELECT r.title_id, r.person FROM responsibility r JOIN titles t ON t.id = r.title_id
                                        WHERE t.kind = 'series' AND r.person NOT IN ('—', '') AND r.title_id IN ({q})""",
                                    list(ids)):
        problems.append(f"ответственный у сериала (только фильмы): {tid} {person}")
    # «только полнометражное (≥ 60 мин)»: an automatic one where the runtime is short or unknown (review 2026-10-28: anime TV
    # seasons made as tracker-only «films» got Яна)
    for tid, person in conn.execute(f"""SELECT r.title_id, r.person FROM responsibility r JOIN titles t ON t.id = r.title_id
                                        WHERE r.source != 'manual' AND r.person NOT IN ('—', '') AND r.title_id IN ({q})
                                          AND (t.runtime_min < 60 OR t.runtime_min IS NULL)""",
                                    list(ids)):
        problems.append(f"автоматический ответственный у короткого / без длительности: {tid} {person}")
    # R5: every shown identified title went through TMDB (review 2026-10-28: 8 «Убийства в...» rows of 09:48 with no data)
    # (a title TMDB has nothing for gets a match issue «tmdb_…» from enrich — that is a tried one, not a missed one)
    for tid, name in conn.execute(f"""SELECT t.id, coalesce(t.title_ru, t.title_original) FROM titles t WHERE t.id IN ({q})
                                      AND coalesce(t.tracker_only, 0) = 0 AND t.tmdb_checked_at IS NULL
                                      AND NOT EXISTS (SELECT 1 FROM match_issues m WHERE m.problem LIKE 'tmdb%'
                                          AND (m.candidates LIKE '%"' || coalesce(t.imdb_id, t.series_imdb_id, '-') || '"%'
                                               OR m.release_id IN (SELECT release_id FROM release_titles x
                                                                   WHERE x.title_id = t.id)))""", list(ids)):
        problems.append(f"не дополнен из TMDB (films enrich): {tid} {name}")
    # R5 / R12: the IMDb rating and the RT check too (review 2026-10-28, round 12: titles made by the link rechecks had
    # no rating, so «IMDb < 5» could not hide them)
    for tid, name in conn.execute(f"""SELECT t.id, coalesce(t.title_ru, t.title_original) FROM titles t WHERE t.id IN ({q})
                                      AND t.imdb_rating IS NULL AND t.imdb_id IS NOT NULL AND t.season_number IS NULL
                                      AND EXISTS (SELECT 1 FROM imdb_ratings r WHERE r.tconst = t.imdb_id)""", list(ids)):
        problems.append(f"рейтинг IMDb не перенесён (films ratings imdb): {tid} {name}")
    for tid, name in conn.execute(f"""SELECT t.id, coalesce(t.title_ru, t.title_original) FROM titles t WHERE t.id IN ({q})
                                      AND coalesce(t.tracker_only, 0) = 0 AND t.rt_checked_at IS NULL
                                      AND t.tmdb_checked_at IS NOT NULL""", list(ids)):
        problems.append(f"RT не проверялся (films ratings rt): {tid} {name}")
    problems += order_problems(shown, top_imdb or [])
    # «один сезон — одна строка»: a tracker-only twin of a season shown already (review 2026-10-28: «Фоллаут» S2 twice)
    from .tracker_only import norm
    seasons: dict[tuple, list] = {}
    for r in shown:
        t = r["title"]
        if t.get("kind") == "series":
            for nm in {norm(t.get("title_original")), norm(t.get("title_ru"))} - {""}:
                seasons.setdefault((nm, t.get("season_number")), []).append(t)
    for (nm, s), ts in seasons.items():
        if len({t["id"] for t in ts}) > 1 and any(t.get("tracker_only") for t in ts):
            problems.append(f"сезон дважды (строка только с трекера): {nm} S{s} — {sorted({t['id'] for t in ts})}")
    # one work, two rows of different series (review 2026-10-28: a Chinese drama's TMDB row «双世异闻录 / Princess Across
    # Time» and its tracker-only twin «Shuang Shi Yi Wen Lu»; the «Монстр» anthology season and the part's own TMDB
    # series): by every name — the latin one and the best release's — and the year; one of the two lacks TMDB data
    problems += twin_problems(conn, shown)
    problems = list(dict.fromkeys(problems))
    anime = {tid for tid, in conn.execute(f"""SELECT id FROM titles WHERE tracker_category = 'anime' AND id IN ({q})""",
                                          list(ids))}
    for r in shown:
        t = r["title"]
        if t["id"] in anime and not r.get("anime_ok"):
            problems.append(f"аниме не из франшиз владельца в таблице: {t['id']} {t.get('title_ru') or t.get('title_original')}")
        # «только полнометражные» мультфильмы, shorts only from his franchises / lists (review 2026-10-28)
        if t.get("kind") == "movie" and (t.get("is_animation") or t.get("tracker_category") == "animation") \
                and (t.get("runtime_min") or 60) < 60 and not r.get("short_ok"):
            problems.append(f"короткометражный мультфильм не из франшиз в таблице: {t['id']} {t.get('title_ru') or t.get('title_original')}")
        # owner, 2026-10-28: «Скрыть все» русскоязычные, «кроме выбранных русских франшиз» (and his lists)
        if not r.get("ru_ok") and _russian(t):
            problems.append(f"русскоязычный не из франшиз и списков в таблице: {t['id']} {t.get('title_ru') or t.get('title_original')}")
        # review 2026-10-28: «Больница Питт» S1 showed «(Сезон: 2 / Серии: 1-15 из 15)» — the season in words was not read;
        # a link the season recheck made on purpose (a sub-series / revival numbering) is not a mismatch
        # every variant row too, and the specials: «Атака титанов» OVA (season 0) had a season-1 release as «AI-апскейл»
        rel = r.get("release") or {}
        if t.get("kind") == "series" and t.get("season_number") is not None and rel.get("id"):
            named, name = _release_seasons(conn, rel["id"], t["id"])
            if t["season_number"] and named and t["season_number"] not in named:
                problems.append(f"лучшая раздача — другого сезона (S{t['season_number']} ← {named}): {t['id']} "
                                f"{t.get('title_ru') or t.get('title_original')}")
            elif t["season_number"] == 0 and name is not None and not re.search(_SPECIAL, name):
                problems.append(f"на спецвыпусках (сезон 0) раздача не спецвыпуска ({r.get('variant') or 'Лучшее'}): "
                                f"{t['id']} {t.get('title_ru') or t.get('title_original')}")
    return problems


def film_mismatch_problems(conn, shown: list[dict]) -> list[str]:
    """Review round 11: a shown film on a series' IMDb id with no TMDB film («Наруто + … [01-135, 01-479]»), and a shown
    film row whose release says another film — MediaInfo 20+ min off, none of the title's countries, the link by name
    («Корни (2024)» Kazakhstan 138 min on the Irish «Frewaka»)."""
    import json
    out = []
    for r in shown:
        t, rel = r["title"], r.get("release") or {}
        if t.get("kind") != "movie":
            continue
        if t.get("imdb_id") and not t.get("tmdb_id"):
            row = conn.execute("SELECT title_type FROM imdb_basics WHERE tconst = ?", (t["imdb_id"],)).fetchone()
            if row and row[0] in ("tvSeries", "tvMiniSeries"):
                out.append(f"фильм на IMDb id сериала: {t['id']} {t.get('title_ru') or t.get('title_original')}")
        if not rel.get("id") or not t.get("runtime_min"):
            continue
        row = conn.execute("SELECT r.parsed, r.source, x.how FROM releases r JOIN release_titles x ON x.release_id = r.id "
                           "AND x.title_id = ? WHERE r.id = ?", (t["id"], rel["id"])).fetchone()
        if not row or not row[0] or row[1] in ("toloka", "mazepa") or (row[2] or "").startswith(
                ("imdb_page", "manual", "collection_pack", "page_recheck")):
            continue
        p = json.loads(row[0])
        country = lambda c: re.split(r"\s*[-–,/(]", c or "")[0].strip(" .").casefold()   # noqa: E731
        mine = {country(c) for c in p.get("countries") or []} - {""}
        theirs = {country(c) for c in json.loads(t.get("countries") or "[]")} - {""} \
            if isinstance(t.get("countries"), str) else set()
        if p.get("runtime_min") and mine and abs(p["runtime_min"] - t["runtime_min"]) > 20 and not mine & theirs:
            out.append(f"раздача другого фильма (MediaInfo {p['runtime_min']} мин, {', '.join(sorted(mine))}): {t['id']} "
                       f"{t.get('title_ru') or t.get('title_original')} ← {rel['id']}")
    return out


def lost_links(conn) -> list[str]:
    """A release whose every link is 'wrong_link: … → <id>' — moved to a title, the new link never made (review round 9:
    INSERT OR IGNORE over a wrong_link row of the same pair; such a release is never shown)."""
    rows = conn.execute(
        """SELECT r.id, r.name FROM releases r WHERE EXISTS (SELECT 1 FROM release_titles x WHERE x.release_id = r.id
             AND x.how LIKE 'wrong_link:%→%') AND NOT EXISTS (SELECT 1 FROM release_titles x WHERE x.release_id = r.id
             AND coalesce(x.how, '') NOT LIKE 'wrong_link%')""").fetchall()
    return [f"раздача без действующей привязки (только wrong_link): {rid} {name}" for rid, name in rows]


_SPECIAL = r"(?i)\b(?:OVA|OAD|ONA)\b|special|спецвыпуск|спешл|спэшл|рождеств|christmas|\[0?0x|\bS00"


def twin_problems(conn, shown: list[dict]) -> list[str]:
    import json
    from .match import _title_names
    from .tracker_only import norm
    by: dict[tuple, dict[int, dict]] = {}
    seen, special = set(), {}
    for r in shown:
        t = r["title"]
        if t["id"] in seen or (t.get("kind") == "series" and not t.get("season_number")):
            continue   # a special (season 0) is not a twin of the season
        seen.add(t["id"])
        names = {t.get("title_original"), t.get("title_ru"), t.get("title_latin")}
        rel = r.get("release") or {}
        special[t["id"]] = False
        if rel.get("id"):
            row = conn.execute("SELECT parsed, name FROM releases WHERE id = ?", (rel["id"],)).fetchone()
            try:
                names |= set(_title_names(json.loads(row[0]) if row and row[0] else {}))
            except ValueError:
                pass
            special[t["id"]] = bool(row and re.search(_SPECIAL, row[1] or ""))
        # by a non-Russian name only: «Убийства в...» names many French TV films (Vosges ≠ Corrèze); nor an anthology's
        # shared «Meurtres à...»
        for nm in {norm(n) for n in names if n and not re.search(r"[а-яёіїєґ]", n, re.I)
                   and not n.rstrip().endswith(("...", "…"))} - {""}:
            if len(nm) >= 4:
                by.setdefault((nm, t.get("year")), {})[t["id"]] = t
                if t.get("tracker_only") and t.get("year"):
                    # a tracker writes the year off by one (rustorka «In the Grey … [2025]», the film 2026)
                    for y in (t["year"] - 1, t["year"] + 1):
                        by.setdefault((nm, y), {})[t["id"]] = t
    out = []
    for (nm, y), ts in by.items():
        if len(ts) < 2:
            continue
        keys = {t.get("imdb_id") or t.get("series_imdb_id") or t.get("series_tmdb_id") or t.get("tmdb_id") or ("only", t["id"])
                for t in ts.values()}
        series = [t for t in ts.values() if t.get("kind") == "series"]
        # seasons: the same number, or a one-season series (an anthology part); an ONA of a long show is not its season
        same = len(series) < 2 or len({t.get("season_number") for t in series}) == 1 \
            or any(t.get("seasons_total") == 1 for t in series)
        # two IMDb records are two works («Shifting Gears»: the 2024 film and the sitcom); an OVA / ONA is not the season
        # of its year («Атака титанов» OVA 2017 and S2)
        # — two records known to TMDB too; one found by name on a stray IMDb id is no proof (review round 10: «Смена
        # передач» 3593 on tt18071570, a 2022 series, and 9229 — Tim Allen's sitcom twice)
        imdbs = [t.get("imdb_id") or t.get("series_imdb_id") for t in ts.values()]
        if all(imdbs) and len(set(imdbs)) == len(imdbs) and all(t.get("tmdb_id") for t in ts.values()):
            continue
        if any(special[i] for i in ts) and any(not special[i] and t.get("season_number") for i, t in ts.items()):
            continue
        if (len(keys) > 1 or len({t.get("kind") for t in ts.values()}) > 1) and same \
                and any(t.get("tracker_only") or not t.get("tmdb_id") for t in ts.values()):
            out.append(f"одна работа — две строки: {nm} ({y}) — {sorted(ts)}")
    return list(dict.fromkeys(out))


def _release_seasons(conn, release_id: int, title_id: int) -> tuple[list[int], str | None]:
    """(seasons the release names, its name); ([], None) for a link made on purpose by the season recheck / twin merge
    or by hand («manual: rutracker «ТВ-N» — сквозной номер сезона франшизы», Баки — round 14)."""
    import json
    from .match import _seasons
    from .tracker_only import name_seasons
    row = conn.execute("SELECT r.name, r.parsed, x.how FROM releases r LEFT JOIN release_titles x ON x.release_id = r.id "
                       "AND x.title_id = ? WHERE r.id = ?", (title_id, release_id)).fetchone()
    if not row or (row[2] or "").startswith(("season_recheck", "part_twin", "manual")):
        return [], None
    try:
        np = (json.loads(row[1]) if row[1] else {}).get("name_parsed") or {}
    except ValueError:
        np = {}
    return _seasons(np) or name_seasons(row[0]), row[0] or ""


def _russian(t: dict) -> bool:
    # the language part needs the releases: rank.compute decides it and sets `ru_ok` (True when not Russian); here only
    # TMDB's language is re-checked (review 2026-10-28: the two rules had drifted)
    from .rank import is_cartoon
    return t.get("original_language") == "ru" and not t.get("is_animation") and not is_cartoon(t)


def order_problems(shown: list[dict], top_imdb: list[str]) -> list[str]:
    """[top] rows first, in the owner's order — a season by its series id (review 2026-10-28: «Братство: Последняя фантазия
    XV» named in [top] was not on top)."""
    if not top_imdb:
        return []
    ids = [(r["title"].get("imdb_id") or r["title"].get("series_imdb_id") or "") for r in shown]
    pos = [top_imdb.index(i) if i in top_imdb else None for i in ids]
    out, seen_other = [], False
    for p, r in zip(pos, shown):
        if p is None:
            seen_other = True
        elif seen_other:
            out.append(f"[top] не на верху таблицы: {r['title']['id']} {r['title'].get('title_ru')}")
    tops = [p for p in pos if p is not None]
    if tops != sorted(tops):
        out.append("[top] не в порядке, названном владельцем")
    return out


def main() -> int:
    from . import rank
    conn = db.connect()
    problems = check(conn, rank.compute(conn))
    for p in problems:
        print(p)
    print("ok" if not problems else f"проблем: {len(problems)}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
