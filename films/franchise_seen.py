"""What the owner has / has not seen in his franchises (R45) — the input of the «что из франшиз я видел» survey.

Run: python -X utf8 -m films.franchise_seen [report]

For every franchise with scope "all" in config/franchises.toml: its works (franchise_members, not 'review'), released
(year ≤ this year), each marked
  seen     — a title with that IMDb id (or that series) is watched: plex / jellyfin / manual (not *_partial),
             or the Plex history (plex_history.guids) has the IMDb id;
  partial  — a series with some seasons watched;
  unseen   — the rest → survey questions (category, year, Russian title, short description at ask time).
Output: data/cache/franchise_seen.json.
"""
from __future__ import annotations

import json
import re
import sys
import tomllib
from collections import defaultdict
from datetime import date

from . import db

OUT = db.ROOT / "data" / "cache" / "franchise_seen.json"
CATEGORY = {"film": "фильм", "animated film": "мультфильм", "series": "сериал", "animated series": "мультсериал",
            "short": "короткометражка", "special": "спецвыпуск", "documentary": "документальный"}


def owner_series_ids(conn) -> set[str]:
    """Series the owner sent himself (the «Хочу» list) — always kept, whatever series_only says."""
    try:
        from .watchlist import title_ids
        ids = title_ids(conn)
    except Exception:
        return set()
    return {r[0] for r in conn.execute(f"SELECT DISTINCT series_imdb_id FROM titles WHERE id IN ({','.join('?' * len(ids))})", list(ids))
            if r[0]} if ids else set()


_KIDS: set[str] = set()


def countries(conn) -> dict[str, list]:
    from .sources import tmdb
    with open(db.ROOT / "config" / "franchises.toml", "rb") as f:
        keys = [fr["key"] for fr in tomllib.load(f)["franchise"] if fr.get("hide_country")]
    out = {}
    for (imdb,) in conn.execute(f"SELECT DISTINCT imdb_id FROM franchise_members WHERE imdb_id IS NOT NULL AND franchise_key IN "
                                f"({','.join('?' * len(keys))})", keys).fetchall() if keys else []:
        f = tmdb.find_by_imdb(conn, imdb)
        raw = (tmdb.tv_raw(conn, f["tv"]) if f.get("tv") else tmdb.movie_raw(conn, f["movie"]) if f.get("movie") else None) or {}
        out[imdb] = [c["iso_3166_1"] for c in raw.get("production_countries") or []] or raw.get("origin_country") or []
    return out


def kids_ids(conn) -> set[str]:
    """Kids' cartoons among franchise works: animated + TMDB genre «Детский» / «семейный» (10762 / 10751). The owner
    (2026-10-23): kids' animated spin-offs of any franchise go to Яна, he is not asked. Anime franchises excluded."""
    from .sources import tmdb
    out = set()
    for imdb, kind in conn.execute("""SELECT DISTINCT imdb_id, kind FROM franchise_members
                                      WHERE kind LIKE '%animated%' AND imdb_id IS NOT NULL AND confidence != 'review'"""):
        f = tmdb.find_by_imdb(conn, imdb)
        raw = (tmdb.tv_raw(conn, f["tv"]) if f.get("tv") else tmdb.movie_raw(conn, f["movie"]) if f.get("movie") else None) or {}
        if {g.get("id") for g in raw.get("genres") or []} & {10762, 10751}:
            out.add(imdb)
    return out


_COUNTRY: dict[str, list] = {}   # imdb → production / origin countries, for franchises with hide_country
_ANIMATED_FR: set[str] = set()   # franchises that are cartoons themselves (Shrek, Toy Story…) — the owner's own
_ANIM_TITLES: set[str] = set()   # members whose title is a cartoon by its genres (a member kind «short» / «special» does
                                 # not say it: «Бегущий по лезвию: Блэкаут 2022», review 2026-10-28)


def animation_decided(fr: dict) -> bool:
    """Are this franchise's cartoons settled? The owner (2026-10-23): «лучше бы ты спрашивал по франшизам, интересны
    ли мне мультики, перед тем как их предлагать» → a franchise with cartoons gets one gate question first
    (`seen_survey gates`); its answer is the `animation` flag: "ask" / "films_only" / "hide". Cartoon franchises
    themselves, anime and DC / Marvel / Star Wars / Spider-Verse (own rules) need no gate."""
    return bool(fr.get("animation") or fr.get("anime") or fr["key"] in _ANIMATED_FR
                or fr["key"] in ("dc", "marvel", "star_wars", "spider_verse")
                or fr.get("hide_animation") or fr.get("animation_only") is not None)


def _too_old(fr: dict, m: dict) -> bool:
    """hide_films_before / hide_animated_films_before of a franchise (DC: films < 1990, animated films < 2009)."""
    if m.get("imdb_id") in (fr.get("hide") or []):   # hidden one by one by the owner
        return True
    animated = "animated" in (m.get("kind") or "") or m.get("imdb_id") in _ANIM_TITLES
    if animated and (fr.get("animation") == "hide" or (fr.get("animation") == "films_only" and "series" in m.get("kind", ""))):
        return True
    if fr.get("hide_all_series") and "series" in (m.get("kind") or ""):
        return True
    if fr.get("hide_country"):   # e.g. Звонок: all Japanese works; Проклятие: Japanese before 2020
        for code, before in fr["hide_country"].items():
            if code in _COUNTRY.get(m.get("imdb_id"), ()) and (m.get("year") or 0) < before:
                return True
    if fr.get("keep_only") is not None and m.get("imdb_id") not in fr["keep_only"]:   # only these works matter
        return True
    if fr.get("hide_animation") and animated:
        return True
    if animated and fr.get("animation_only") is not None and m["imdb_id"] not in fr["animation_only"]:
        return True
    # shorts / specials / documentaries follow the films' cut-off, cartoon series the series' one (Captain America 1944
    # serial slipped through as a «short» past Marvel's «до 1990 — скрыть», 2026-10-24)
    films_lim = fr.get("hide_films_before")
    lim = {"film": films_lim, "short": films_lim, "special": films_lim, "documentary": films_lim,
           "animated film": fr.get("hide_animated_films_before"), "series": fr.get("hide_series_before"),
           "animated series": fr.get("hide_series_before")}.get(m.get("kind"))
    return bool(lim and m.get("year") and m["year"] < lim)


def kept_elsewhere(fr: dict, imdb: str, followed: dict, in_frs: dict) -> bool:
    """Another followed franchise (scope all) has this work and does not hide it — `fr`'s hide rules do not apply
    (review 2026-10-28: Marvel's «animation only MCU» hid «Паук внутри» of the owner's own Spider-Verse franchise)."""
    return any(o["franchise_key"] != fr["key"] and o["franchise_key"] in followed
               and not _too_old(followed[o["franchise_key"]], o) for o in in_frs.get(imdb, []))


def _give_yana(conn, items: list) -> int:
    """Kids' animated spin-offs of franchises with animation_to_yana → Яна's list (config/yana.toml), except what the
    owner has seen or has downloaded / queued (the owner, 2026-10-23)."""
    from . import link
    from .seen_survey import have
    yana = db.ROOT / "config" / "yana.toml"
    text = yana.read_text(encoding="utf-8")
    skip = have(conn) | {r[0] for r in conn.execute(
        """SELECT coalesce(t.imdb_id, t.series_imdb_id) FROM watched w JOIN titles t ON t.id = w.title_id
           WHERE substr(w.source, -8) != '_partial'""")}
    add = []
    for fr, m in items:
        if m["imdb_id"] in skip or m["imdb_id"] in text:
            continue
        name = (m.get("title_ru") or m.get("title") or "").replace('"', "")
        add.append("\n".join(["[[title]]", f'name = "{name}"', f'imdb_id = "{m["imdb_id"]}"', f'year = {m.get("year") or 0}',
                              f'note = "детское мульт-ответвление «{fr["name"]}» — владелец 2026-10-23: «отдать Яне»"', ""]))
    if add:
        yana.write_text(text.rstrip() + "\n" + "".join(add), encoding="utf-8")
    return len(add)


def hide_excluded_series(conn) -> int:
    """series_only franchises: the other series' titles (all seasons) → user_marks not_interested (hidden, kept in DB);
    the same for films / animated films older than hide_*_before."""
    with open(db.ROOT / "config" / "franchises.toml", "rb") as f:
        franchises = tomllib.load(f)["franchise"]
    global _KIDS, _ANIMATED_FR
    _KIDS = kids_ids(conn)
    _COUNTRY.update(countries(conn))
    _ANIM_TITLES.update(r[0] for r in conn.execute(
        """SELECT imdb_id FROM titles WHERE imdb_id IS NOT NULL AND season_number IS NULL
           AND (genres LIKE '%мультфильм%' OR genres LIKE '%Animation%')"""))
    _ANIMATED_FR = {k for k, a, t in conn.execute(
        """SELECT franchise_key, sum(kind LIKE '%animated%'), count(*) FROM franchise_members
           WHERE confidence != 'review' GROUP BY 1""") if t and a / t >= 0.5}
    keep, n = owner_series_ids(conn), 0
    to_yana = []
    followed = {f["key"]: f for f in franchises if f.get("scope") == "all"}
    in_frs: dict[str, list] = {}
    for m in conn.execute("SELECT * FROM franchise_members WHERE imdb_id IS NOT NULL AND confidence != 'review'"):
        in_frs.setdefault(m["imdb_id"], []).append(dict(m))
    for fr in franchises:
        if not (fr.get("hide_films_before") or fr.get("hide_animated_films_before") or fr.get("hide_series_before") or fr.get("hide") or fr.get("keep_only") is not None or fr.get("animation_only") is not None or True):
            continue
        for m in conn.execute("SELECT * FROM franchise_members WHERE franchise_key = ? AND imdb_id IS NOT NULL", (fr["key"],)).fetchall():
            if not _too_old(fr, dict(m)):
                continue
            # a member of another followed franchise that keeps it is not hidden by this one (review 2026-10-28:
            # «Паук внутри» — Marvel's «animation only MCU» hid a member of the owner's own Spider-Verse franchise)
            if kept_elsewhere(fr, m["imdb_id"], followed, in_frs):
                continue
            for (tid,) in conn.execute("""SELECT id FROM titles WHERE (imdb_id = ? AND season_number IS NULL)
                                           OR series_imdb_id = ?""", (m["imdb_id"], m["imdb_id"])).fetchall():
                n += conn.execute("""INSERT INTO user_marks (title_id, mark, note) VALUES (?, 'not_interested', ?)
                                     ON CONFLICT(title_id) DO UPDATE SET mark = 'not_interested', note = excluded.note
                                     WHERE user_marks.mark IS NULL""", (tid, f"{fr['name']}: hide_*_before / animation_only")).rowcount
    cfg = [fr for fr in franchises if fr.get("series_only") is not None]
    for fr in cfg:
        for (imdb,) in conn.execute("""SELECT imdb_id FROM franchise_members WHERE franchise_key = ? AND kind LIKE '%series%'
                                       AND imdb_id IS NOT NULL""", (fr["key"],)).fetchall():
            if imdb in fr["series_only"] or imdb in keep:
                continue
            # a serial filed as one movie title (Batman and Robin 1949: imdb_id, no season) is the same series
            for (tid,) in conn.execute("""SELECT id FROM titles WHERE series_imdb_id = ?
                                           OR (imdb_id = ? AND season_number IS NULL)""", (imdb, imdb)).fetchall():
                n += conn.execute("""INSERT INTO user_marks (title_id, mark, note) VALUES (?, 'not_interested', ?)
                                     ON CONFLICT(title_id) DO UPDATE SET mark = 'not_interested', note = excluded.note
                                     WHERE user_marks.mark IS NULL""", (tid, f"{fr['name']}: сериалы вне series_only")).rowcount
    conn.commit()
    return n


def compute(conn) -> list[dict]:
    with open(db.ROOT / "config" / "franchises.toml", "rb") as f:
        cfg = {fr["key"]: fr for fr in tomllib.load(f)["franchise"] if fr.get("scope") == "all"}
    watched_imdb = defaultdict(set)   # imdb → {'movie'} or {season numbers}
    for imdb, sim, season, src in conn.execute(
            """SELECT t.imdb_id, t.series_imdb_id, t.season_number, w.source FROM watched w JOIN titles t ON t.id = w.title_id
               WHERE substr(w.source, -8) != '_partial'"""):
        if imdb:
            watched_imdb[imdb].add("movie")
        if sim and season:
            watched_imdb[sim].add(season)
    partial = {r[0] for r in conn.execute(
        """SELECT DISTINCT t.series_imdb_id FROM watched w JOIN titles t ON t.id = w.title_id
           WHERE substr(w.source, -8) = '_partial' AND t.series_imdb_id IS NOT NULL""")}
    for (guids,) in conn.execute("SELECT guids FROM plex_history WHERE guids LIKE '%imdb://%'"):
        for g in json.loads(guids or "[]"):
            if g.startswith("imdb://"):
                watched_imdb[g[7:]].add("movie")
    seasons_total = {r[0]: r[1] for r in conn.execute(
        "SELECT series_imdb_id, max(seasons_total) FROM titles WHERE series_imdb_id IS NOT NULL GROUP BY 1")}
    ru = {}
    for imdb, sim, title_ru in conn.execute("SELECT imdb_id, series_imdb_id, title_ru FROM titles WHERE title_ru IS NOT NULL"):
        ru.setdefault(imdb or sim, title_ru)
    owner_series = owner_series_ids(conn)
    this_year = date.today().year
    out = []
    for key, fr in cfg.items():
        works = []
        for m in conn.execute("""SELECT * FROM franchise_members WHERE franchise_key = ? AND confidence != 'review'
                                 ORDER BY year""", (key,)):
            m = dict(m)
            if not m.get("imdb_id") or (m.get("year") or 0) > this_year:
                continue
            if "series" in (m.get("kind") or "") and fr.get("series_only") is not None                     and m["imdb_id"] not in fr["series_only"] and m["imdb_id"] not in owner_series:
                continue   # the owner does not watch this franchise's other series (DC: only the new DCU)
            if "animated" in (m.get("kind") or "") and fr.get("animation_only") is not None                     and m["imdb_id"] not in fr["animation_only"] and m["imdb_id"] not in owner_series:
                continue   # Marvel: only MCU animation
            if _too_old(fr, m):
                continue   # the owner: old films / animated films of this franchise — not asked, hidden
            if "animated" in (m.get("kind") or "") and not animation_decided(fr):
                continue   # wait for the gate question «интересны ли мультики этой франшизы»
            seen = watched_imdb.get(m["imdb_id"], set())
            series = "series" in (m.get("kind") or "")
            if series:
                tot = seasons_total.get(m["imdb_id"])
                got = {s for s in seen if isinstance(s, int)}
                state = "seen" if (tot and len(got) >= tot) or ("movie" in seen and not tot) else \
                        "partial" if got or m["imdb_id"] in partial else "unseen"
            else:
                state = "seen" if seen else "unseen"
            works.append({"imdb_id": m["imdb_id"], "title": ru.get(m["imdb_id"]) or m.get("title_ru") or m.get("title"),
                          "title_original": m.get("title"), "year": m.get("year"),
                          "category": CATEGORY.get(m.get("kind"), m.get("kind") or "?"), "state": state,
                          "seasons_seen": sorted(s for s in seen if isinstance(s, int)) if series else None})
        if works:
            out.append({"key": key, "name": fr["name"], "works": works,
                        "seen": sum(w["state"] == "seen" for w in works),
                        "unseen": sum(w["state"] != "seen" for w in works)})
    out.sort(key=lambda f: (-f["seen"], f["name"]))
    return out


if __name__ == "__main__":
    c = db.connect()
    print("hidden series titles:", hide_excluded_series(c))   # also fills _KIDS for compute()
    res = compute(c)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(res)} franchises, works seen {sum(f['seen'] for f in res)}, to ask {sum(f['unseen'] for f in res)}")
    for f in res[:40]:
        print(f"  {f['name'][:40]}: видел {f['seen']}, спросить {f['unseen']}")
