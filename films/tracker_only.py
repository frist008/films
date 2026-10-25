"""Titles built from the tracker data alone — releases neither IMDb nor TMDB identifies.

Owner, 2026-10-25: «Добавлять в таблицу просто без данных, раз не удаётся найти». A release left unlinked by the
matcher (films/match.py: open match_issues `no_imdb_not_found`, `no_imdb_ambiguous`, `tmdb_no_title`,
`season_year_mismatch`, after `link_exact_names`) gets a title of its own:
  * kind: film, or one row per season of the name («[S01-02]» → seasons 1 and 2; no season written → 1);
  * names from the release name (Russian, original / Latin, Ukrainian), year from the name (a season pack: the first
    season the first year, the last season the last year), countries / genres / studios / runtime / premiere date /
    episode count when the page states them; no IMDb / TMDB ids, no ratings;
  * titles.tracker_only = 1, tracker_meta = JSON (release ids, names, the matcher's problem and candidates, franchise);
    release_titles.how = 'tracker_only'; the open match issue is closed (its problem / candidates kept in tracker_meta).
Not made for: film packs (collection_pack), releases in excluded_releases (DV5-only, 3D, TS/TC/CAM, КПК), anime outside
the owner's anime franchises (`anime = true` in config/franchises.toml, matched by name), releases ending before
year_from unless «encountered» (R162: the torrent is in qBittorrent) or of an owner's anime franchise (collected for
all years, decision 2026-10-23). Several releases of one work share the row (same Russian / original name; films year
±1; seasons by series_key 'tracker:<name>').

Out of scope (close_out_of_scope, every match run after create): the open issues of releases the base does not collect
at all — anime outside the owner's anime franchises, before year_from and not encountered, excluded releases, film packs
before year_from of no franchise — are closed with the rule as the note (candidates 'out_of_scope' / 'resolved_note');
match.match_releases no longer re-matches them, reopen_in_scope gives one a new open issue when the rule stops holding.

In rank / sheet they are ordinary titles: no rating → not hidden by the IMDb < 5 rule; the voice filter, the year rule
and «мультсериал» apply as to any title; the sheet leaves the missing columns blank and says «нет в TMDB/IMDb — данные
только с трекера». Poster — films/posters.py (the page's own image when nothing else has one).

Re-identification (every match run, `run()` from `films match` and films.encountered): each tracker-only release goes
through the matcher again (match.match_one — IMDb datasets refreshed by update-db, TMDB searches cached ≤ 30 days), a
film also through the exact name + year rule against the identified films. Found → the release is linked to the real
title(s) (how '<rule> from-tracker_only-<id>'), the old link becomes how 'wrong_link: tracker_only → <ids>' (kept, never
used), and a row with no tracker-only link left gets duplicate_of = the real title (the owner's mark / note and a manual
«ответственный» are copied there when it has none). Nothing is deleted.

Run: python -X utf8 -m films.tracker_only run [--dry]      — re-identify, then create
     python -X utf8 -m films.tracker_only list             — tracker-only rows and what they hold
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import tomllib

from . import db

PROBLEMS = ("no_imdb_not_found", "no_imdb_ambiguous", "tmdb_no_title", "season_year_mismatch")
HOW = "tracker_only"
NOTE = "нет в TMDB/IMDb — данные только с трекера"
_MONTHS = {"января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6, "июля": 7, "августа": 8,
           "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12}
_ANIM_GENRES = {"мультфильм", "мультсериал", "аниме", "анимация", "мультипликация"}


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


def norm(s: str | None) -> str:
    s = (s or "").casefold().replace("ё", "е")
    return " ".join(re.sub(r"[^\w]+", " ", s).split())


def _np(p: dict) -> dict:
    np = p.get("name_parsed") or {}
    if isinstance(np, str):   # very old parses kept a repr; nothing reliable in it
        return {}
    return np


def _cyr(s: str | None) -> bool:
    return bool(re.search(r"[а-яёіїєґ]", s or "", re.I))


def ru_date(s: str | None) -> str | None:
    """«19 августа 2026 года» → 2026-08-19 (anything else → None)."""
    m = re.search(r"(\d{1,2})\s+([а-я]+)\s+(\d{4})", (s or "").lower())
    if not m or m.group(2) not in _MONTHS:
        return None
    return f"{int(m.group(3)):04d}-{_MONTHS[m.group(2)]:02d}-{int(m.group(1)):02d}"


# ------------------------------------------------------------------------------------------------ scope

def anime_franchises(conn) -> dict[str, list[str]]:
    """{franchise key: normalized name keys} of the owner's anime franchises: the configured name split into its parts
    («Мастера меча онлайн (Sword Art Online)» → both) plus the member titles known for it (franchise_members)."""
    with open(db.ROOT / "config" / "franchises.toml", "rb") as f:
        frs = [x for x in tomllib.load(f).get("franchise", []) if x.get("anime") and x.get("scope") != "none"]
    out: dict[str, list[str]] = {}
    for fr in frs:
        keys = []
        for part in re.split(r"[()/+,]|\bвкл\.?", fr.get("name") or ""):
            k = norm(part)
            if len(k) >= 4 and not k.startswith("вкл"):
                keys.append(k)
        try:
            for t, tr in conn.execute("SELECT title, title_ru FROM franchise_members WHERE franchise_key = ? "
                                      "AND confidence != 'review'", (fr["key"],)):
                keys += [k for k in (norm(t), norm(tr)) if len(k) >= 6]
        except Exception:  # noqa: BLE001 — table absent in a bare test DB
            pass
        out[fr["key"]] = list(dict.fromkeys(keys))
    return out


def franchise_of(names: list[str], frs: dict[str, list[str]]) -> str | None:
    """The anime franchise whose name a release name starts with («Мастера меча онлайн: Алисизация …» → sword_art_online).
    A one-word key must be the whole name or be followed by punctuation («Блич: …»), never by another word — «Монстр за
    соседней партой» is not Urasawa's «Монстр»."""
    for raw in names:
        n = norm(raw)
        low = (raw or "").casefold().replace("ё", "е").strip()
        for key, keys in frs.items():
            for k in keys:
                if n == k:
                    return key
                if " " in k:
                    if n.startswith(k + " "):
                        return key
                elif low.startswith(k) and re.match(r"\s*[:.,!\-–—(]", low[len(k):]):
                    return key
    return None


def encountered(conn, rid: int, btih: str | None) -> bool:
    """R162: the owner met this torrent — it is (or was) in his qBittorrent client."""
    try:
        return bool(conn.execute("SELECT 1 FROM qbt_torrents WHERE release_id = ? OR (? IS NOT NULL AND infohash = lower(?))",
                                 (rid, btih, btih)).fetchone())
    except Exception:  # noqa: BLE001
        return False


def eligible(conn, rel: dict, p: dict, problem: str, frs: dict, year_from: int) -> tuple[bool, str, str | None]:
    """(make a tracker-only title?, reason, anime franchise key)."""
    from .match import _is_pack, _seasons, _title_names, _years
    if problem not in PROBLEMS:
        return False, f"problem {problem}", None
    np = _np(p)
    y1, y2 = _years(p)
    if _is_pack(np.get("name") or rel.get("name") or "", y1, y2, _seasons(np)):
        return False, "film pack", None
    if p.get("dv5_only"):
        return False, "dv5_only", None
    ex = conn.execute("SELECT reason FROM excluded_releases WHERE source = ? AND source_id = ? AND reason NOT LIKE 'lifted:%'",
                      (rel["source"], rel["source_id"])).fetchone()
    if ex:
        return False, f"excluded {ex[0]}", None
    names = _title_names(p) or [rel.get("name") or ""]
    fr = franchise_of(names, frs) if rel.get("category") == "anime" else None
    if rel.get("category") == "anime" and not fr:
        return False, "anime outside the owner's anime franchises", None
    if (y2 or y1 or 0) < year_from and not fr and not encountered(conn, rel["id"], rel.get("btih")):
        return False, f"before {year_from}, not encountered", None
    if not (np.get("title_ru") or p.get("title_ru") or np.get("title_original") or p.get("title_original")):
        return False, "no name", None
    return True, "ok", fr


# ------------------------------------------------------------------------------------------------ the title row

def fields(rel: dict, p: dict) -> dict:
    """Title columns from the tracker: {kind, seasons: [(season, year)], title_*, countries, genres, …}."""
    from .match import _kind, _seasons, _years
    np = _np(p)
    category = rel.get("category") or ""
    y1, y2 = _years(p)
    kind = _kind(p, category, None)
    ru = np.get("title_ru") or p.get("title_ru")
    orig = np.get("title_original") or p.get("title_original")
    other = [t for t in np.get("titles_other") or [] if t and t != orig]
    latin = next((t for t in [orig, *other] if t and not _cyr(t)), None)
    if not ru and orig and _cyr(orig):
        ru = orig
    genres = [g for g in p.get("genres") or [] if g]
    out = {
        "kind": kind,
        "is_animation": int(category in ("animation", "anime") or bool(set(g.casefold() for g in genres) & _ANIM_GENRES)),
        "tracker_category": category,
        "title_ru": ru, "title_original": orig or latin or ru,
        "title_latin": latin if latin and latin != (orig or latin) else None,
        "title_uk": np.get("title_uk"),
        "countries": json.dumps(p.get("countries"), ensure_ascii=False) if p.get("countries") else None,
        "genres": json.dumps(genres, ensure_ascii=False) if genres else None,
        "main_genre": genres[0] if genres else None,
        "studios": json.dumps(p.get("production_studios"), ensure_ascii=False) if p.get("production_studios") else None,
        "runtime_min": (p.get("runtime_episode_min") or p.get("runtime_min")) if kind == "series" else p.get("runtime_min"),
        "release_date": ru_date(p.get("premiere_world")) if kind == "movie" else None,
    }
    if kind == "movie":
        out["year"] = y1
        out["seasons"] = []
    else:
        seasons = _seasons(np) or [1]
        lo, hi = min(seasons), max(seasons)
        out["seasons"] = [(s, y1 if s == lo else (y2 or y1) if s == hi else None) for s in seasons]
        out["episodes_total"] = p.get("episodes_count") if len(seasons) == 1 else None
    return out


def series_key(f: dict) -> str:
    return "tracker:" + norm(f["title_ru"] or f["title_original"])


COLS = ("is_animation", "tracker_category", "title_ru", "title_original", "title_latin", "title_uk", "countries", "genres",
        "main_genre", "studios", "runtime_min", "release_date")


def _find_movie(conn, f: dict) -> int | None:
    keys = {k for k in (norm(f["title_ru"]), norm(f["title_original"])) if k}
    for tid, ru, orig, y in conn.execute("SELECT id, title_ru, title_original, year FROM titles WHERE tracker_only = 1 "
                                         "AND duplicate_of IS NULL AND kind = 'movie'"):
        if ({norm(ru), norm(orig)} - {""}) & keys and (not y or not f.get("year") or abs(y - f["year"]) <= 1):
            return tid
    return None


def _meta_add(conn, tid: int, rid: int, problem: str, cands, fr: str | None, names: list[str]):
    cur = json.loads(conn.execute("SELECT tracker_meta FROM titles WHERE id = ?", (tid,)).fetchone()[0] or "{}")
    cur.setdefault("release_ids", [])
    if rid not in cur["release_ids"]:
        cur["release_ids"].append(rid)
    cur.setdefault("problems", {})[str(rid)] = problem
    if cands:
        cur.setdefault("candidates", {})[str(rid)] = cands[:5] if isinstance(cands, list) else cands
    if fr:
        cur["franchise"] = fr
    cur["names"] = list(dict.fromkeys((cur.get("names") or []) + names))
    cur.setdefault("created", time.strftime("%Y-%m-%d"))
    conn.execute("UPDATE titles SET tracker_meta = ? WHERE id = ?", (json.dumps(cur, ensure_ascii=False, default=str), tid))


def _insert(conn, f: dict, extra: dict) -> int:
    vals = {k: f.get(k) for k in COLS}
    vals.update(extra)
    vals["tracker_only"] = 1
    cols = list(vals)
    return conn.execute(f"INSERT INTO titles ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                        [vals[c] for c in cols]).lastrowid


def _fill_missing(conn, tid: int, f: dict, extra: dict):
    """A row made earlier from another release of the work: blanks filled from this one, nothing overwritten."""
    vals = {k: f.get(k) for k in COLS if f.get(k) is not None}
    vals.update({k: v for k, v in extra.items() if v is not None})
    for k, v in vals.items():
        conn.execute(f"UPDATE titles SET {k} = ? WHERE id = ? AND {k} IS NULL", (v, tid))


def make_titles(conn, rel: dict, p: dict, skip_seasons: set | frozenset = frozenset()) -> list[int]:
    """The tracker-only title rows of a release (found or created) — inside the caller's transaction. skip_seasons: seasons
    linked to an identified title instead (known_seasons)."""
    f = fields(rel, p)
    f["seasons"] = [(s, y) for s, y in f["seasons"] if s not in skip_seasons]
    if f["kind"] == "movie":
        tid = _find_movie(conn, f)
        extra = {"kind": "movie", "year": f["year"]}
        if tid:
            _fill_missing(conn, tid, f, {"year": f["year"]})
            return [tid]
        return [_insert(conn, f, extra)]
    key, tids = series_key(f), []
    for s, y in f["seasons"]:
        row = conn.execute("SELECT id FROM titles WHERE series_key = ? AND season_number = ?", (key, s)).fetchone()
        extra = {"year": y, "episodes_total": f.get("episodes_total")}
        if row:
            _fill_missing(conn, row[0], f, extra)
            tids.append(row[0])
        else:
            tids.append(_insert(conn, f, dict(extra, kind="series", season_number=s, series_key=key)))
    return tids


def known_seasons(conn, problem: str, cands) -> dict[int, int]:
    """{season: title id} — `tmdb_no_title`: the series IS on TMDB, only some season of the pack is not; seasons already
    in the DB as identified titles of that series get the link (how 'tracker_only_known_season') instead of a tracker-only
    row («Доктор Кто [S01-14] (2005-2024)»: series 8 is title 8360). Not for season_year_mismatch: there the numbering
    itself is wrong («SAO: Alicization - War of Underworld [S02]» is not SAO season 2)."""
    if problem != "tmdb_no_title" or not isinstance(cands, dict) or not cands.get("tmdb_id") or cands.get("kind") != "series":
        return {}
    out = {}
    for s in cands.get("seasons") or []:
        r = conn.execute("SELECT id FROM titles WHERE series_tmdb_id = ? AND season_number = ? AND coalesce(tracker_only, 0) = 0 "
                         "AND duplicate_of IS NULL", (cands["tmdb_id"], s)).fetchone()
        if r:
            out[s] = r[0]
    return out


# ------------------------------------------------------------------------------------------------ create

def pending(conn) -> list[dict]:
    """Unlinked parsed releases with an open match issue (the matcher's last word on them)."""
    rows = conn.execute(
        """SELECT r.id, r.source, r.source_id, r.name, r.category, r.btih, r.parsed, m.id AS issue_id, m.problem,
                  m.candidates
           FROM releases r JOIN match_issues m ON m.release_id = r.id AND m.resolved_at IS NULL
           WHERE r.parsed IS NOT NULL
             AND NOT EXISTS (SELECT 1 FROM release_titles x WHERE x.release_id = r.id
                             AND coalesce(x.how, '') NOT LIKE 'wrong_link%')
           ORDER BY r.id, m.id DESC""").fetchall()
    seen, out = set(), []
    for r in rows:
        if r["id"] in seen:
            continue   # the newest open issue of a release decides
        seen.add(r["id"])
        out.append(dict(r))
    return out


def create(conn, dry: bool = False, log=print) -> dict:
    from .match import _title_names
    year_from = min(db.years())
    frs = anime_franchises(conn)
    st = {"candidates": 0, "linked_releases": 0, "titles_created": 0, "titles_reused": 0, "skipped": {}, "made": []}
    for rel in pending(conn):
        st["candidates"] += 1
        p = json.loads(rel["parsed"])
        ok, why, fr = eligible(conn, rel, p, rel["problem"], frs, year_from)
        if not ok:
            key = why.split(",")[0] if why.startswith("before") else why
            st["skipped"][key] = st["skipped"].get(key, 0) + 1
            continue
        before = conn.execute("SELECT coalesce(max(id), 0) FROM titles").fetchone()[0]
        cands = json.loads(rel["candidates"]) if rel["candidates"] else None
        if dry:
            f = fields(rel, p)
            st["made"].append({"release_id": rel["id"], "name": rel["name"], "kind": f["kind"],
                               "seasons": f["seasons"], "year": f.get("year"), "franchise": fr})
            st["linked_releases"] += 1
            continue
        known = known_seasons(conn, rel["problem"], cands)
        with conn:
            for tid in dict.fromkeys(known.values()):
                conn.execute("INSERT OR IGNORE INTO release_titles (release_id, title_id, how) VALUES (?, ?, ?)",
                             (rel["id"], tid, "tracker_only_known_season"))
            tids = make_titles(conn, rel, p, set(known))
            for tid in tids:
                conn.execute("INSERT OR IGNORE INTO release_titles (release_id, title_id, how) VALUES (?, ?, ?)",
                             (rel["id"], tid, HOW))
                _meta_add(conn, tid, rel["id"], rel["problem"], cands, fr, _title_names(p))
            conn.execute("UPDATE match_issues SET resolved_at = datetime('now') WHERE release_id = ? AND resolved_at IS NULL",
                         (rel["id"],))
        new = [t for t in tids if t > before]
        st["titles_created"] += len(new)
        st["titles_reused"] += len(tids) - len(new)
        st["linked_releases"] += 1
        st["made"].append({"release_id": rel["id"], "name": rel["name"], "title_ids": tids, "franchise": fr,
                           "known_seasons": known or None})
        log(f"  tracker-only {tids}: {rel['name'][:90]}")
    return st


# ------------------------------------------------------------------------------------------------ out of scope

# eligible() reasons that mean «not collected at all» (rules of the base, not a matching failure)
_SCOPE_RU = (
    ("anime outside", "аниме вне аниме-франшиз владельца — по правилу не собирается (CLAUDE.md, решение 2026-10-23, R44)"),
    ("before ", "вышло до {year} и не встречалось у владельца (нет в qBittorrent) — вне диапазона базы (CLAUDE.md, R162)"),
    ("excluded kpk", "раздача исключена правилом: для КПК / телефона (опрос 2026-10-25)"),
    ("excluded is_3d", "раздача исключена правилом: 3D (опрос 2026-10-25)"),
    ("excluded cam", "раздача исключена правилом: звук или видео TS / TC / CAM (опрос 2026-10-25)"),
    ("excluded dv5", "раздача только с Dolby Vision Profile 5 — не храним (CLAUDE.md, правило 2)"),
    ("dv5_only", "раздача только с Dolby Vision Profile 5 — не храним (CLAUDE.md, правило 2)"),
    ("pack before", "пачка фильмов до {year}, не встречалась у владельца и не из его франшиз — вне диапазона базы"),
)


def _pack_scope(conn, rel: dict, p: dict, year_from: int) -> str | None:
    """A film pack (open 'collection_pack') entirely before year_from, not in qBittorrent and not of an owner's franchise
    (its name is no franchise member's title) → 'pack before …'; None = keep it for the pack matcher."""
    from .match import _pack_base_names, _years
    y1, y2 = _years(p)
    if not (y2 or y1) or (y2 or y1) >= year_from or encountered(conn, rel["id"], rel.get("btih")):
        return None
    names = {norm(t) for t in _pack_base_names(p)} - {""}
    try:
        members = {norm(t) for row in conn.execute("SELECT title, title_ru FROM franchise_members WHERE confidence != 'review'")
                   for t in row if t}
    except Exception:  # noqa: BLE001 — bare test DB
        members = set()
    return None if names & members else f"pack before {year_from}, not encountered, no franchise"


def scope_reason(conn, rel: dict, p: dict, problem: str, frs: dict, year_from: int) -> str | None:
    """Why a release with an open match issue is not collected at all (anime outside the owner's franchises, before
    year_from and not encountered, an excluded release, a pre-year_from pack of no franchise) → the reason; None when
    it is in scope (made a tracker-only title, or left for the pack matcher / the owner)."""
    if problem == "collection_pack":
        return _pack_scope(conn, rel, p, year_from)
    ok, why, _ = eligible(conn, rel, p, problem, frs, year_from)
    if ok or not any(why.startswith(k) for k, _ in _SCOPE_RU if not k.startswith("pack")):
        return None
    return why


def scope_note(why: str, year_from: int) -> str:
    for k, ru in _SCOPE_RU:
        if why.startswith(k):
            return ru.format(year=year_from)
    return why


def with_note(candidates: str | None, **extra) -> str:
    """An issue's candidates JSON with the closing note added (a list is kept under 'candidates')."""
    try:
        c = json.loads(candidates) if candidates else None
    except ValueError:
        c = candidates
    if not isinstance(c, dict):
        c = {"candidates": c} if c is not None else {}
    c.update(extra)
    return json.dumps(c, ensure_ascii=False, default=str)


def close_out_of_scope(conn, dry: bool = False, log=print) -> dict:
    """Open match issues of releases the base does not collect are closed with the rule as the note (candidates
    'out_of_scope', 'resolved_note'); match.match_releases skips such releases from then on, reopen_in_scope brings one
    back when the rule changes (a new anime franchise of the owner, the torrent appears in qBittorrent). Nothing is
    deleted."""
    year_from = min(db.years())
    frs = anime_franchises(conn)
    st = {"closed": 0, "by_reason": {}}
    for rel in pending(conn):
        p = json.loads(rel["parsed"])
        why = scope_reason(conn, rel, p, rel["problem"], frs, year_from)
        if not why:
            continue
        key = why.split(",")[0]
        st["by_reason"][key] = st["by_reason"].get(key, 0) + 1
        st["closed"] += 1
        if dry:
            continue
        with conn:
            for iid, cands in conn.execute("SELECT id, candidates FROM match_issues WHERE release_id = ? AND resolved_at IS NULL",
                                           (rel["id"],)).fetchall():
                conn.execute("UPDATE match_issues SET resolved_at = datetime('now'), candidates = ? WHERE id = ?",
                             (with_note(cands, out_of_scope=why, resolved_rule="out_of_scope",
                                        resolved_note=scope_note(why, year_from)), iid))
        log(f"  out of scope ({key}): {rel['name'][:90]}")
    return st


def reopen_in_scope(conn) -> int:
    """Out-of-scope closures (close_out_of_scope) whose release is still unlinked but no longer out of scope get a new
    open issue (the original problem and candidates); the closed row is marked 'reopened_at'."""
    try:
        rows = conn.execute(
            """SELECT m.id, m.problem, m.candidates, r.id AS rid, r.source, r.source_id, r.name, r.category, r.btih, r.parsed
               FROM match_issues m JOIN releases r ON r.id = m.release_id
               WHERE m.resolved_at IS NOT NULL AND json_valid(m.candidates)
                 AND json_extract(m.candidates, '$.out_of_scope') IS NOT NULL
                 AND json_extract(m.candidates, '$.reopened_at') IS NULL AND r.parsed IS NOT NULL
                 AND NOT EXISTS (SELECT 1 FROM release_titles x WHERE x.release_id = r.id)
                 AND NOT EXISTS (SELECT 1 FROM match_issues o WHERE o.release_id = r.id AND o.resolved_at IS NULL)""").fetchall()
    except Exception:  # noqa: BLE001 — no JSON1 / bare test DB
        return 0
    if not rows:
        return 0
    year_from = min(db.years())
    frs = anime_franchises(conn)
    n = 0
    for r in rows:
        rel = {"id": r[3], "source": r[4], "source_id": r[5], "name": r[6], "category": r[7], "btih": r[8]}
        if scope_reason(conn, rel, json.loads(r[9]), r[1], frs, year_from):
            continue
        c = json.loads(r[2])
        orig = c.get("candidates") if set(c) - {"candidates", "out_of_scope", "resolved_rule", "resolved_note"} == set() \
            else {k: v for k, v in c.items() if k not in ("out_of_scope", "resolved_rule", "resolved_note")}
        with conn:
            conn.execute("UPDATE match_issues SET candidates = ? WHERE id = ?",
                         (json.dumps(dict(c, reopened_at=_now()), ensure_ascii=False, default=str), r[0]))
            conn.execute("INSERT INTO match_issues (release_id, problem, candidates) VALUES (?, ?, ?)",
                         (r[3], r[1], json.dumps(orig, ensure_ascii=False, default=str) if orig else None))
        n += 1
    return n


# ------------------------------------------------------------------------------------------------ re-identify

def _exact_film(conn, p: dict, own: int) -> list[int] | None:
    """A tracker-only film whose Russian or original name and year (±1) equal exactly one identified film (link_exact_names
    for rows made before that film was in the DB)."""
    from .match import _title_names, _years
    y1, _ = _years(p)
    if not y1:
        return None
    keys = {norm(t) for t in _title_names(p)} - {""}
    hit = {tid for tid, ru, orig in conn.execute(
        "SELECT id, title_ru, title_original FROM titles WHERE kind = 'movie' AND coalesce(tracker_only, 0) = 0 "
        "AND duplicate_of IS NULL AND year BETWEEN ? AND ?", (y1 - 1, y1 + 1)) if ({norm(ru), norm(orig)} - {""}) & keys}
    hit.discard(own)
    return [hit.pop()] if len(hit) == 1 else None


def _target_for(conn, old: int, new_tids: list[int]) -> int | None:
    """The real title a tracker-only row became: its season among the new titles, else the only / first one."""
    s = conn.execute("SELECT season_number FROM titles WHERE id = ?", (old,)).fetchone()[0]
    if s is not None:
        for t in new_tids:
            if conn.execute("SELECT season_number FROM titles WHERE id = ?", (t,)).fetchone()[0] == s:
                return t
    return new_tids[0] if new_tids else None


def reidentify(conn, dry: bool = False, log=print, min_age_hours: float = 20) -> dict:
    """min_age_hours: a row tried this recently is skipped (match runs come several times an update — films.encountered
    calls it too; one pass over ~85 releases takes about a minute); 0 = try everything now."""
    from . import match
    st = {"releases": 0, "identified": 0, "via": {}, "rows_merged": 0, "merged": [], "skipped_recent": 0}
    cutoff = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - min_age_hours * 3600))
    rels = conn.execute(
        """SELECT DISTINCT r.id, r.category, r.parsed FROM release_titles x JOIN releases r ON r.id = x.release_id
           JOIN titles t ON t.id = x.title_id
           WHERE x.how = ? AND t.tracker_only = 1 AND t.duplicate_of IS NULL AND r.parsed IS NOT NULL
           ORDER BY r.id""", (HOW,)).fetchall()
    now = _now()
    for rid, category, parsed in rels:
        st["releases"] += 1
        p = json.loads(parsed)
        olds = [r[0] for r in conn.execute(
            "SELECT title_id FROM release_titles WHERE release_id = ? AND how = ?", (rid, HOW))]
        checked = [conn.execute("SELECT identify_checked_at FROM titles WHERE id = ?", (o,)).fetchone()[0] for o in olds]
        if min_age_hours and checked and all(c and c > cutoff for c in checked):
            st["skipped_recent"] += 1
            continue
        problems: list = []
        if dry:
            continue   # the matcher writes its links itself: a dry run does not call it
        try:
            tids = match.match_one(conn, rid, category, p, _Counter(issues={}), lambda pr, c: problems.append(pr))
        except Exception as e:  # noqa: BLE001 — a network / data error on one release must not stop the match run
            log(f"  re-identify {rid}: {type(e).__name__}: {e}")
            st.setdefault("errors", []).append({"release_id": rid, "error": f"{type(e).__name__}: {e}"})
            continue
        via = None
        if tids:
            via = conn.execute("SELECT how FROM release_titles WHERE release_id = ? AND title_id = ?",
                               (rid, tids[0])).fetchone()[0]
        elif all(conn.execute("SELECT kind FROM titles WHERE id = ?", (o,)).fetchone()[0] == "movie" for o in olds):
            tids = _exact_film(conn, p, olds[0] if olds else -1)
            if tids:
                via = "exact_name_year"
                with conn:
                    conn.execute("INSERT OR IGNORE INTO release_titles (release_id, title_id, how) VALUES (?, ?, ?)",
                                 (rid, tids[0], via))
        with conn:
            for o in olds:
                conn.execute("UPDATE titles SET identify_checked_at = ? WHERE id = ?", (now, o))
                if problems:
                    meta = json.loads(conn.execute("SELECT tracker_meta FROM titles WHERE id = ?", (o,)).fetchone()[0] or "{}")
                    meta.setdefault("last_problem", {})[str(rid)] = problems[-1]
                    conn.execute("UPDATE titles SET tracker_meta = ? WHERE id = ?", (json.dumps(meta, ensure_ascii=False), o))
        tids = [t for t in tids or [] if t not in olds]
        if not tids:
            continue
        st["identified"] += 1
        st["via"][via] = st["via"].get(via, 0) + 1
        with conn:
            for o in olds:
                conn.execute("UPDATE release_titles SET how = ? WHERE release_id = ? AND title_id = ? AND how = ?",
                             (f"wrong_link: tracker_only → {','.join(map(str, tids))} ({now[:10]})", rid, o, HOW))
            for t in tids:
                conn.execute("UPDATE release_titles SET how = coalesce(how, '') || ? WHERE release_id = ? AND title_id = ? "
                             "AND coalesce(how, '') NOT LIKE '%from-tracker_only-%'",
                             (f" from-tracker_only-{','.join(map(str, olds))}", rid, t))
            for o in olds:
                if conn.execute("SELECT 1 FROM release_titles WHERE title_id = ? AND how = ?", (o, HOW)).fetchone():
                    continue   # another release of the row is still unidentified
                tgt = _target_for(conn, o, tids)
                if tgt is None:
                    continue
                conn.execute("UPDATE titles SET duplicate_of = ?, updated_at = datetime('now') WHERE id = ?", (tgt, o))
                conn.execute("INSERT OR IGNORE INTO user_marks (title_id, mark, ordered_by, note, updated_at) "
                             "SELECT ?, mark, ordered_by, note, updated_at FROM user_marks WHERE title_id = ?", (tgt, o))
                conn.execute("INSERT OR IGNORE INTO responsibility (title_id, person, source, updated_at) "
                             "SELECT ?, person, source, updated_at FROM responsibility WHERE title_id = ? AND source = 'manual'",
                             (tgt, o))
                st["rows_merged"] += 1
                st["merged"].append({"tracker_only": o, "into": tgt, "release_id": rid, "via": via})
                log(f"  tracker-only {o} identified → {tgt} ({via})")
    return st


class _Counter(dict):
    """match_one's stats for a re-identification run: any counter starts at 0 (they are not reported)."""
    def __missing__(self, key):
        return 0


# ------------------------------------------------------------------------------------------------ entry points

def run(conn, dry: bool = False, log=print, force: bool = False) -> dict:
    """Part of every match run: identify the tracker-only rows again, then make rows for what is still unlinked."""
    return {"reidentify": reidentify(conn, dry, log, min_age_hours=0 if force else 20), "create": create(conn, dry, log),
            "out_of_scope": close_out_of_scope(conn, dry, log)}


def listing(conn) -> list[dict]:
    out = []
    for r in conn.execute("SELECT id, kind, season_number, title_ru, title_original, year, tracker_category, duplicate_of, "
                          "poster_source, tracker_meta FROM titles WHERE tracker_only = 1 ORDER BY id"):
        m = json.loads(r["tracker_meta"] or "{}")
        out.append({"id": r["id"], "kind": r["kind"], "season": r["season_number"], "title": r["title_ru"] or r["title_original"],
                    "year": r["year"], "category": r["tracker_category"], "duplicate_of": r["duplicate_of"],
                    "poster": r["poster_source"], "releases": m.get("release_ids"), "franchise": m.get("franchise")})
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(prog="films.tracker_only")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run", help="re-identify tracker-only rows, then create rows for unidentified releases")
    p.add_argument("--dry", action="store_true")
    p.add_argument("--force", action="store_true", help="re-identify every row now (default: not tried in the last 20 h)")
    sub.add_parser("list")
    a = ap.parse_args(argv)
    conn = db.connect()
    db.migrate(conn)
    if a.cmd == "list":
        print(json.dumps(listing(conn), ensure_ascii=False, indent=1))
        return
    res = run(conn, a.dry, log=lambda *x: print(*x, file=sys.stderr), force=a.force)
    for part in res.values():
        if len(part.get("made") or []) > 30:
            part["made"] = part["made"][:30] + [f"… {len(part['made']) - 30} more"]
    print(json.dumps(res, ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    main()
