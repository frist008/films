"""Triage of the open match_issues queue (plan audit 2026-10-25): decide what the data settles, close what is obsolete
or out of scope with the reason as a note, keep open only what needs the owner.

Problem types and who decides:
  * release level (no_imdb_*, tmdb_no_title, season_year_mismatch, collection_pack) — the matcher (films/match.py), the
    tracker-only titles (films/tracker_only.py, R175 «добавлять в таблицу просто без данных») and the out-of-scope rules
    (tracker_only.close_out_of_scope: anime outside the owner's anime franchises, before 2024 and not encountered, КПК,
    old packs of no franchise);
  * tmdb_year_mismatch — match.recheck_year_mismatch (same name = the tracker's premiere year; otherwise re-resolved);
  * tmdb_no_movie_for_imdb / tmdb_no_tv_for_imdb / tmdb_no_season / tmdb_same_as_title — enrich (films/enrich.py) with
    its TMDB name search; here: obsolete ones (the title became a duplicate or was enriched since) are closed, the rest
    are TMDB data gaps (TMDB lacks the id / the season yet): closed with the note, the title keeps its IMDb + tracker
    data and enrich looks again on every run — nothing for the owner;
  * rt_ambiguous — rt_triage below: duplicate RT stub pages of one work, a single candidate confirmed by year and
    director; undecided → closed «без рейтинга RT» (the owner cannot pick an RT page better than the data).
Nothing is deleted: a closed issue keeps its candidates, the note goes into the JSON ('resolved_rule', 'resolved_note').

Run: python -X utf8 -m films.match_triage summary
     python -X utf8 -m films.match_triage titles [--dry]
     python -X utf8 -m films.match_triage rt [--dry] [--limit N]
"""
from __future__ import annotations

import argparse
import json
import re
import sys

from . import db
from .sources.imdb import norm
from .tracker_only import with_note

TITLE_PROBLEMS = ("tmdb_no_movie_for_imdb", "tmdb_no_tv_for_imdb", "tmdb_no_season", "tmdb_same_as_title",
                  "tmdb_movie_404")
GAP_NOTE = {
    "tmdb_no_movie_for_imdb": "TMDB не знает этот IMDb id и поиск по всем названиям не дал однозначного фильма — тайтл "
                              "остаётся с данными IMDb и трекера; enrich проверяет снова при каждом прогоне. Вопрос владельцу не нужен.",
    "tmdb_no_tv_for_imdb": "TMDB не знает этот IMDb id сериала и поиск по названиям не дал однозначного — сезон остаётся "
                           "с данными IMDb и трекера; enrich проверяет снова при каждом прогоне. Вопрос владельцу не нужен.",
    "tmdb_no_season": "в TMDB этого сезона ещё нет или сезоны пронумерованы иначе (аниме, ремейки, новый сезон) — "
                      "сезон остаётся с данными трекера и IMDb; enrich проверяет снова при каждом прогоне. Вопрос владельцу не нужен.",
    "tmdb_same_as_title": "тот же тайтл под другим IMDb id (IMDb объединил записи) — дубль помечен duplicate_of, раздачи перенесены.",
    "tmdb_movie_404": "фильм удалён из TMDB — тайтл остаётся с данными IMDb и трекера.",
}


def _cands(text):
    try:
        return json.loads(text) if text else None
    except ValueError:
        return None


def _close(conn, iid: int, cands: str | None, rule: str, note: str, **extra):
    conn.execute("UPDATE match_issues SET resolved_at = datetime('now'), candidates = ? WHERE id = ?",
                 (with_note(cands, resolved_rule=rule, resolved_note=note, **extra), iid))


# ------------------------------------------------------------------------------------------------ title level

def titles(conn, dry: bool = False, log=print) -> dict:
    """Title-level enrich issues: duplicates of one title and problem (the newest kept), obsolete ones (the title is a
    duplicate now, or TMDB data arrived since), the rest closed as TMDB data gaps with the note."""
    st = {"superseded": 0, "obsolete": 0, "data_gap": {}, "kept": 0}
    rows = conn.execute(f"SELECT id, problem, candidates FROM match_issues WHERE resolved_at IS NULL AND release_id IS NULL "
                        f"AND problem IN ({','.join('?' * len(TITLE_PROBLEMS))}) ORDER BY id DESC", TITLE_PROBLEMS).fetchall()
    seen = set()
    with conn:
        for iid, problem, cands in rows:
            d = _cands(cands) or {}
            tid = d.get("title_id") if isinstance(d, dict) else None
            if (problem, tid) in seen:
                st["superseded"] += 1
                if not dry:
                    _close(conn, iid, cands, "superseded", "повтор: та же проблема того же тайтла открыта более новой записью")
                continue
            seen.add((problem, tid))
            t = conn.execute("SELECT duplicate_of, tmdb_checked_at, tracker_only FROM titles WHERE id = ?", (tid,)).fetchone() if tid else None
            if t is None or t[0] or t[2] or (t[1] and problem != "tmdb_same_as_title"):
                st["obsolete"] += 1
                why = ("тайтла нет" if t is None else f"тайтл — дубль {t[0]}, enrich его не обрабатывает" if t[0] else
                       "тайтл только с трекера" if t[2] else "данные TMDB с тех пор получены")
                if not dry:
                    _close(conn, iid, cands, "obsolete", f"устарело: {why}")
                continue
            st["data_gap"][problem] = st["data_gap"].get(problem, 0) + 1
            if not dry:
                _close(conn, iid, cands, "tmdb_data_gap", GAP_NOTE.get(problem, "нет данных TMDB"))
    log(f"  titles: {st}")
    return st


# ------------------------------------------------------------------------------------------------ Rotten Tomatoes

def _loose(s: str | None) -> str:
    return re.sub(r"[\W_]+", "", norm(s or ""))


def _directors(conn, tid: int) -> set[str]:
    return {norm(r[0]) for r in conn.execute(
        "SELECT p.name FROM credits c JOIN people p ON p.id = c.person_id WHERE c.title_id = ? AND c.role = 'director'", (tid,))}


def _page_directors(page: dict) -> set[str]:
    info = page.get("info") or {}
    raw = info.get("Director") or info.get("Directors") or ""
    return {norm(x) for x in re.split(r",|\band\b", raw) if x.strip()}


def _has_scores(page: dict) -> bool:
    return any(page.get(k) is not None for k in ("critics", "audience")) or bool(page.get("critics_reviews"))


def _weight(page: dict) -> int:
    return (page.get("critics_reviews") or 0) * 10 + (page.get("audience_ratings") or 0)


def rt_decide(conn, t: dict, cands: list[dict], fetch) -> tuple[dict | None, str]:
    """→ (the RT page of the title, why) or (None, why). Candidates come from rt.find (url, title, year, score).
    Movies: the candidates close to the best (score within 0.15, ≥ 0.6) are fetched; a page is the title's when its year
    is within one of the title's and its director is the title's (TMDB credits); without a director on either side, the
    candidates that are one work's duplicate RT pages (same name, same year — old stubs «…_2009_2») decide by the only
    page with scores (or one with ten times the reviews of the others).
    Series seasons: among the close candidates' pages, the only one listing that season (or the only one with scores
    for it among duplicate pages of one show)."""
    if not cands:
        return None, "нет кандидатов"
    cands = sorted(cands, key=lambda c: -(c.get("score") or 0))
    top = cands[0].get("score") or 0
    pool = [c for c in cands if (c.get("score") or 0) >= max(0.6, top - 0.15)][:6] or cands[:1]
    pages = []
    for c in pool:
        pg = fetch(c["url"])
        if pg:
            pages.append(pg)
    if not pages:
        return None, "страницы RT не открылись"
    if t["kind"] == "series":
        s = t.get("season_number") or 1
        start = None
        if t.get("series_imdb_id"):
            r = conn.execute("SELECT start_year FROM imdb_basics WHERE tconst = ?", (t["series_imdb_id"],)).fetchone()
            start = r[0] if r else None
        if start is None and s == 1:
            start = t.get("year")
        # the show must have begun when the series did (± 1): a remake / namesake lists a season of that number too
        pages = [p for p in pages if start and p.get("year") and abs(p["year"] - start) <= 1]
        with_s = [p for p in pages if any(x.get("season") == s for x in p.get("seasons") or [])]
        if len(with_s) == 1:
            return with_s[0], f"единственная страница с сезоном {s}"
        if with_s and len({_loose(p.get("title")) for p in with_s}) == 1:
            scored = [p for p in with_s if any(x.get("season") == s and x.get("critics") is not None for x in p.get("seasons") or [])]
            if len(scored) == 1:
                return scored[0], f"дубли страниц одного сериала — только у одной есть оценки сезона {s}"
            best = max(with_s, key=lambda p: len(p.get("seasons") or []))
            if sum(1 for p in with_s if len(p.get("seasons") or []) == len(best.get("seasons") or [])) == 1 and \
                    len(best.get("seasons") or []) >= 2:
                return best, f"дубли страниц одного сериала — выбрана полная (сезонов {len(best['seasons'])})"
        return None, f"страниц с сезоном {s}: {len(with_s)} — не решено"
    year = t.get("year")
    ok_year = [p for p in pages if not year or not p.get("year") or abs(p["year"] - year) <= 1]
    dirs = _directors(conn, t["id"])
    by_dir = [p for p in ok_year if dirs and (_page_directors(p) & dirs)]
    if len(by_dir) == 1:
        return by_dir[0], "год и режиссёр совпадают"
    if len(by_dir) > 1:
        ok_year = by_dir
    elif dirs and ok_year and all(_page_directors(p) for p in ok_year):
        return None, "режиссёр ни на одной странице не совпал"
    if not ok_year:
        return None, "год ни на одной странице не совпал"
    if len(ok_year) == 1:
        p = ok_year[0]
        if len(pool) == 1 and (pool[0].get("score") or 0) >= 0.9 and _has_scores(p):
            return p, "единственный кандидат: точное название и год"
        return None, "единственный кандидат не подтверждён (ни режиссёра, ни точного названия с оценками)"
    if len({(_loose(p.get("title")), p.get("year")) for p in ok_year}) == 1:
        scored = [p for p in ok_year if _has_scores(p)]
        if len(scored) == 1:
            return scored[0], "дубли страниц одного фильма — оценки только на одной"
        if len(scored) > 1:
            scored.sort(key=lambda p: -_weight(p))
            if _weight(scored[0]) >= 10 * max(_weight(scored[1]), 1):
                return scored[0], "дубли страниц одного фильма — на одной в 10+ раз больше отзывов"
        return None, "дубли страниц одного фильма — оценки на нескольких, не решено"
    return None, f"кандидатов {len(ok_year)} подходят по году — не решено"


def _write_rt(conn, t: dict, url: str, page: dict, fetch_season):
    from .ratings import _store_page
    season = t.get("season_number") if t["kind"] == "series" else None
    src, src_url, parent = page, url, None
    if season:
        full = fetch_season(url, season) or page
        s = next((x for x in full.get("seasons") or [] if x.get("season") == season), None)
        src, src_url, parent = (s or {}), (s or {}).get("url"), url
        page = full
    _store_page(conn, page, url, page.get("kind"), None, None, t["id"] if not season else None)
    if src_url and src is not page:
        _store_page(conn, src, src_url, "season", season, parent, t["id"])
    conn.execute("""UPDATE titles SET rt_url = ?, rt_critics = ?, rt_audience = ?, rt_critics_reviews = ?,
                        rt_audience_ratings = ?, rt_audience_type = ?, rt_checked_at = datetime('now') WHERE id = ?""",
                 (src_url, src.get("critics"), src.get("audience"), src.get("critics_reviews"),
                  src.get("audience_ratings"), page.get("audience_type"), t["id"]))


def rt(conn, dry: bool = False, limit: int | None = None, log=print) -> dict:
    from .sources import rt as rtsrc
    st = {"issues": 0, "superseded": 0, "obsolete": 0, "matched": 0, "undecided": 0, "errors": 0, "why": {}}
    rows = conn.execute("SELECT id, candidates FROM match_issues WHERE resolved_at IS NULL AND problem = 'rt_ambiguous' "
                        "ORDER BY id DESC").fetchall()
    seen = set()
    done = 0
    for iid, cands in rows:
        d = _cands(cands) or {}
        tid = d.get("title_id")
        st["issues"] += 1
        if tid in seen:
            st["superseded"] += 1
            if not dry:
                with conn:
                    _close(conn, iid, cands, "superseded", "повтор: тот же тайтл открыт более новой записью")
            continue
        seen.add(tid)
        t = conn.execute("SELECT * FROM titles WHERE id = ?", (tid,)).fetchone()
        if t is None or t["duplicate_of"] or t["rt_url"]:
            st["obsolete"] += 1
            if not dry:
                with conn:
                    _close(conn, iid, cands, "obsolete", "устарело: " + ("тайтла нет" if t is None else
                           "тайтл — дубль" if t["duplicate_of"] else "страница RT уже найдена"))
            continue
        if limit and done >= limit:
            continue
        done += 1
        t = dict(t)
        try:
            page, why = rt_decide(conn, t, d.get("candidates") or [], lambda u: rtsrc.fetch(u))
        except rtsrc.RTError as e:
            st["errors"] += 1
            log(f"  rt {tid}: {e}")
            continue
        key = re.sub(r"\d+", "N", why)
        st["why"][key] = st["why"].get(key, 0) + 1
        if page:
            st["matched"] += 1
            if not dry:
                with conn:
                    _write_rt(conn, t, page["url"], page, lambda u, s: rtsrc.fetch(u, season_pages=[s]))
                    _close(conn, iid, cands, "rt_decided", f"страница RT выбрана по данным: {why}", rt_url=page["url"])
            log(f"  rt {tid} → {page['url']} ({why})")
        else:
            st["undecided"] += 1
            if not dry:
                with conn:
                    _close(conn, iid, cands, "rt_undecided",
                           f"RT: однозначной страницы нет ({why}) — тайтл без рейтинга RT; страницу RT владелец по данным "
                           f"не выберет лучше, вопрос не нужен; ratings перепроверит при следующем прогоне")
    log(f"  rt: {st}")
    return st


# ------------------------------------------------------------------------------------------------ summary

def summary(conn) -> dict:
    open_ = dict(conn.execute("SELECT problem, count(*) FROM match_issues WHERE resolved_at IS NULL GROUP BY 1").fetchall())
    rules = {}
    for (c,) in conn.execute("SELECT candidates FROM match_issues WHERE resolved_at > datetime('now', '-1 day') "
                             "AND json_valid(candidates)"):
        r = (_cands(c) or {}).get("resolved_rule") if isinstance(_cands(c), dict) else None
        if r:
            rules[r] = rules.get(r, 0) + 1
    return {"open": open_, "open_total": sum(open_.values()), "closed_last_day_by_rule": rules}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="films.match_triage")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("summary")
    p = sub.add_parser("titles")
    p.add_argument("--dry", action="store_true")
    p = sub.add_parser("rt")
    p.add_argument("--dry", action="store_true")
    p.add_argument("--limit", type=int)
    p = sub.add_parser("all", help="update-db step: year-mismatch and page-id rechecks, then titles and RT triage")
    p.add_argument("--dry", action="store_true")
    a = ap.parse_args(argv)
    conn = db.connect()
    log = lambda *x: print(*x, file=sys.stderr)
    if a.cmd == "summary":
        res = summary(conn)
    elif a.cmd == "titles":
        res = titles(conn, a.dry, log)
    elif a.cmd == "all":
        from . import match
        res = {"year_recheck": match.recheck_year_mismatch(conn, a.dry, log),
               "page_links": match.recheck_page_links(conn, None, a.dry, log),
               "titles": titles(conn, a.dry, log), "rt": rt(conn, a.dry, None, log), "open": summary(conn)}
    else:
        res = rt(conn, a.dry, a.limit, log)
    print(json.dumps(res, ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    main()
