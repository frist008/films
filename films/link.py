"""Link Plex (downloaded / watched) and stores (owned / wishlist) to titles.

- Movies with an IMDb guid link directly. Everything the owner watched (movies and seasons) or bought gets a
  title row even out of the tracker scope (from TMDB, tracker_category NULL): owner's decision 2026-10-23 —
  these are a second base of the DB (the "Просмотренные" sheet, franchise tracking). They never reach the
  main sheet by themselves: a row there needs a tracker release.
- Series stored in Plex as "movies" have no ids: the show name is resolved with TMDB search; the
  resulting map is written to data/plex_show_map.json (status per name) so it can be reviewed and fixed.
  A season counts as watched when every episode of it was watched; otherwise it is 'plex_partial'.
- Store items link by IMDb id (Google TV list) or by TMDB search on title + year (YouTube library).
"""
import json
import re
import sys
from collections import defaultdict

from . import db
from .enrich import _apply, _credits, _collection
from .sources import tmdb

SHOW_MAP_PATH = db.ROOT / "data" / "plex_show_map.json"
# the owner's own videos (status 'personal') never go to git (owner, 2026-10-25: «их не должно быть в гите»)
SHOW_MAP_PRIVATE = db.ROOT / "data" / "cache" / "plex_show_map_private.json"


def _imdb_ids(guids_json: str | None) -> list[str]:
    return [g[7:] for g in json.loads(guids_json or "[]") if g.startswith("imdb://")]


def ensure_movie(conn, imdb_id: str, how: str) -> int | None:
    """Title id for a movie IMDb id; created from TMDB when missing (out of tracker scope)."""
    row = conn.execute("SELECT id FROM titles WHERE imdb_id = ? AND season_number IS NULL", (imdb_id,)).fetchone()
    if row:
        return row[0]
    mid = tmdb.find_by_imdb(conn, imdb_id).get("movie")
    m = tmdb.movie(conn, mid) if mid else None
    if not m:
        return None
    tid = conn.execute("INSERT INTO titles (kind, imdb_id) VALUES ('movie', ?)", (imdb_id,)).lastrowid
    _apply(conn, tid, tmdb.movie_row(m), {"collection_id": (m.get("collection") or {}).get("id"),
                                          "country_codes": json.dumps(m.get("country_codes"), ensure_ascii=False)})
    _credits(conn, tid, m)
    if m.get("collection"):
        _collection(conn, m["collection"]["id"])
    return tid


def ensure_season(conn, tv_id: int, season: int) -> int | None:
    """Title id for a TMDB series season; created from TMDB when missing (out of tracker scope)."""
    row = conn.execute("SELECT id FROM titles WHERE series_tmdb_id = ? AND season_number = ?", (tv_id, season)).fetchone()
    if row:
        return row[0]
    series = tmdb.tv(conn, tv_id)
    s = tmdb.season(conn, tv_id, season, series=series) if series else None
    if not s:
        return None
    row = tmdb.season_row(series, s)
    # R82: a season made from a tracker release (match.py) has only series_key = 'imdb:…'; reuse it instead of clashing on
    # UNIQUE(series_key, season_number). A namesake show of another TMDB id holding that key → this one keys by TMDB id.
    same = conn.execute("SELECT id, series_tmdb_id FROM titles WHERE series_key = ? AND season_number = ?",
                        (row["series_key"], season)).fetchone()
    if same and same[1] in (None, tv_id):
        conn.execute("UPDATE titles SET series_tmdb_id = ? WHERE id = ?", (tv_id, same[0]))
        return same[0]
    if same:
        row = {**row, "series_key": f"tmdb:{tv_id}"}
    tid = conn.execute("INSERT INTO titles (kind, season_number, series_tmdb_id) VALUES ('series', ?, ?)",
                       (season, tv_id)).lastrowid
    trailer = s.get("trailer_ru") or {}
    _apply(conn, tid, row, {
        "country_codes": json.dumps(series.get("country_codes"), ensure_ascii=False),
        "trailer_ru_source": s.get("trailer_ru_source") if s.get("trailer_ru_url") else None,
        "trailer_ru_subtitled": int(bool(trailer.get("subtitled"))) if trailer else None})
    _credits(conn, tid, s)
    return tid


# ---------------------------------------------------------------------------------------------- shows

SEASON_RES = [re.compile(p, re.I) for p in (r"\bS(\d{1,2})(?:E\d+)?\b", r"Season\s*(\d{1,2})", r"\bTV-(\d)\b",
                                            r"(\d{1,2})\s*сезон", r"\b(\d{1,2})x\d{2}\b")]


def clean_show(name: str) -> tuple[str, int | None]:
    year = None
    m = re.search(r"\b(19|20)\d{2}\b", name)
    if m:
        year = int(m.group(0))
    n = re.sub(r"\[.*?\]|\(.*?\)", " ", name)
    n = re.sub(r"\b(Season|Сезон)\s*\d+|\bS\d{1,2}\b|\bTV-\d\b|\b(19|20)\d{2}\b|\b\d{3,4}p\b|\bBD\b|\bII\b", " ", n, flags=re.I)
    n = re.sub(r"\bMarvels\b", "Marvel's", n)
    return re.sub(r"\s+", " ", n.replace("_", " ")).strip(" -"), year


def _show_folder(path: str | None) -> str | None:
    """«E:\\Marvels.Jessica.Jones.S03.2019.2160p…\\file.mkv» -> the folder name (the show's name lives there
    when Plex has none); generic folders (a drive root, Films, Сериалы…) give None."""
    parts = (path or "").split("\\")
    if len(parts) < 3:
        return None
    folder = parts[-2]
    if folder.lower() in ("films", "сериалы", "serials", "series", "anime", "аниме", "without", "sasha", "stas",
                          "stas & dima"):
        return None
    folder = re.sub(r"\.(?=\S)", " ", folder)  # dotted release names
    folder = re.sub(r"\b(WEB-?DL|WEBRip|BDRip|HDTV|NF|AMZN|DSNP|HDR\d*|DV|DoVi|x26[45]|H 26[45]|HEVC|AAC|DDP?\d?(?: \d)?|"
                    r"Atmos|\d+bit|MediaClub|EniaHD|RHS|FFdub|HDCLUB|AniLibria|AniDub|Rus|Eng|Jap)\b", " ", folder, flags=re.I)
    return re.sub(r"\s+", " ", folder).strip(" -_") or None


# a show name made from a release / folder name: everything from the first release token on is junk
# («Marvels The Punisher S02 2160p by DVT», «Peacemaker 2022 S02 1080p -RGzsRutracker», «Halo Season 01»)
_RELEASE_CUT = re.compile(r"[\s._(\[-]+(?:S\d{1,2}(?:E\d+)?|Season[\s._]*\d+|(?:19|20)\d{2}|\d{3,4}p|1080|720|2160|"
                          r"WEB-?DL|WEBRip|BDRip|BDRemux|HDTV|AMZN|NF|DSNP|BD)(?=[\s._)\]-]|$).*$", re.I)
_NAME_SEASON = [re.compile(p, re.I) for p in (r"(?<![a-z0-9])S(\d{1,2})(?![0-9E])", r"\bSeason[\s._]*(\d{1,2})\b",
                                               r"\b(\d{1,2})[\s._-]*сезон", r"\bсезон[\s._-]*(\d{1,2})\b")]


def show_query(name: str) -> tuple[str, int | None]:
    """(TMDB query, year hint) for a Plex show / folder name: clean_show after cutting the release tail."""
    _, year = clean_show(name)
    q, _ = clean_show(_RELEASE_CUT.sub("", name) or name)
    return q, year


def name_season(name: str | None, matched_title: str | None = None) -> int | None:
    """The season a show name states: «… S02 …», «Season 01», «2 сезон», a trailing «II» (Toaru Majutsu no Index II,
    Seikon no Qwaser II BD — clean_show drops it from the query) unless the matched TMDB name has it itself."""
    for rx in _NAME_SEASON:
        m = rx.search(name or "")
        if m:
            return int(m.group(1))
    if re.search(r"\sII(?:\s+BD)?\s*$", name or "") and not re.search(r"\bII\b", matched_title or ""):
        return 2
    return None


def season_of(*texts) -> int | None:
    for t in texts:
        for rx in SEASON_RES:
            m = rx.search(t or "")
            if m:
                return int(m.group(1))
    return None


def _popular_before(conn, res: dict, first_seen: str | None) -> dict | None:
    """Plex keeps old shows as bare «Name N» files: no year, no ids. An ambiguous search is decided when,
    among the exact-name candidates that premiered before the owner's first watch, one has at least 50 TMDB
    votes and 5x the votes of every other («What If» -> Marvel 2021, not a 2024 Chinese drama).
    («Latest premiere before the watch» was tried first and picked obscure namesakes.)"""
    if not first_seen:
        return None
    exact = []
    for c in res.get("candidates") or []:
        if (c.get("score") or 0) >= 0.9 and c.get("year") and str(c["year"]) <= first_seen[:4]:
            exact.append(((tmdb.tv_raw(conn, c["tmdb_id"]) or {}).get("vote_count") or 0, c))
    exact.sort(key=lambda x: -x[0])
    if not exact or exact[0][0] < 50 or (len(exact) > 1 and exact[0][0] < 5 * exact[1][0]):
        return None
    return exact[0][1]


def build_show_map(conn, names: set[str], first_seen: dict | None = None) -> dict:
    """status: matched (TMDB search) / manual (fixed by hand in the file) / auto_popular (decided by votes and
    the first watch date — see _popular_before; listed to the owner for review) / ambiguous / not_found."""
    current = json.loads(SHOW_MAP_PATH.read_text(encoding="utf-8")) if SHOW_MAP_PATH.exists() else {}
    if SHOW_MAP_PRIVATE.exists():
        current.update(json.loads(SHOW_MAP_PRIVATE.read_text(encoding="utf-8")))
    first_seen = first_seen or {}
    for name in sorted(n for n in names if n):
        if name in current and current[name].get("status") in ("matched", "manual", "personal"):
            continue
        q, year = show_query(name)
        res = tmdb.search(conn, [q], year, "tv") if q else {"status": "not_found", "candidates": []}
        season = None
        tail = re.fullmatch(r"(.+?)\s+(\d{1,2})", q or "")
        if res["status"] != "matched" and tail:
            # «Love, Death & Robots 3», «Psycho-Pass 3 2020»: the number is the season — accepted only when the name
            # without it matches and the series has that many seasons
            # (searched without the year: a year next to it is that season's — checked against its air date instead)
            r2 = tmdb.search(conn, [tail.group(1)], None, "tv")
            n = int(tail.group(2))
            raw2 = (tmdb.tv_raw(conn, r2["best"]["tmdb_id"]) or {}) if r2["status"] == "matched" else {}
            air = next((x.get("air_date") or "" for x in raw2.get("seasons") or [] if x.get("season_number") == n), None)
            if air is not None and (not year or (air[:4].isdigit() and abs(int(air[:4]) - year) <= 1)):
                res, season = dict(r2, reason=f"trailing number {n} = season" + (f" (aired {air[:4]})" if year else "")), n
        if res["status"] == "ambiguous" and not year:
            pick = _popular_before(conn, res, first_seen.get(name))
            if pick:
                res = dict(res, status="auto_popular", best=pick,
                           reason=f"most-voted exact name premiered before first watch {first_seen[name][:10]}")
        best = res.get("best") or {}
        current[name] = {"query": q, "year_hint": year, "status": res["status"], "reason": res.get("reason"),
                         **({"season": season} if season else {}),
                         "tmdb_tv": best.get("tmdb_id"), "title": best.get("title"),
                         "original_title": best.get("original_title"), "first_year": best.get("year"),
                         "candidates": [{k: c.get(k) for k in ("tmdb_id", "title", "original_title", "year")}
                                        for c in (res.get("candidates") or [])[:4]] if res["status"] != "matched" else []}
    _episode_title_shows(conn, current)
    SHOW_MAP_PATH.write_text(json.dumps({k: v for k, v in current.items() if v.get("status") != "personal"},
                                        ensure_ascii=False, indent=1), encoding="utf-8")
    SHOW_MAP_PRIVATE.write_text(json.dumps({k: v for k, v in current.items() if v.get("status") == "personal"},
                                           ensure_ascii=False, indent=1), encoding="utf-8")
    return current


BARE_EPISODE = re.compile(r"^S(\d{1,2})E(\d{1,3})[ ._-]+(.+?)(?:[ ._-]+\d{3,4}p\b.*|\.mkv|\.mp4|\.avi)?$", re.I)


def _norm_episode(s: str) -> str:
    return re.sub(r"[\W_]+", "", (s or "").lower())


def _episode_title_shows(conn, current: dict) -> None:
    """History names that are a bare file name «S01E03.The Asset.1080p.Encoder-…» (no show anywhere): the show whose
    TMDB episode SxxEyy carries that very English title. Candidates = the owner's shows (this map, Plex / Jellyfin /
    watched titles). Accepted only when one show matches at least 3 such names and 3x more than any other; then every
    name whose episode title equals that show's is linked (status 'episode_names')."""
    bare = {}
    for name, v in current.items():
        m = BARE_EPISODE.match(name)
        if m and v.get("status") in ("not_found", "ambiguous", "episode_names"):
            bare[name] = (int(m.group(1)), int(m.group(2)), _norm_episode(m.group(3)))
    if not bare:
        return
    cands = {v["tmdb_tv"] for v in current.values() if v.get("tmdb_tv") and v.get("status") in LINKED_STATUSES}
    cands |= {r[0] for r in conn.execute(
        """SELECT DISTINCT t.series_tmdb_id FROM titles t WHERE t.series_tmdb_id IS NOT NULL AND (
             t.id IN (SELECT title_id FROM plex_items) OR t.id IN (SELECT title_id FROM jellyfin_items)
             OR t.id IN (SELECT title_id FROM watched))""")}
    need = defaultdict(int)  # season -> highest episode number among the bare names
    for s, e, _ in bare.values():
        need[s] = max(need[s], e)
    hits = defaultdict(set)
    ep_names = defaultdict(dict)
    for tv in sorted(cands):
        seasons = {x["season_number"]: x.get("episode_count") or 0 for x in (tmdb.tv_raw(conn, tv) or {}).get("seasons") or []}
        for s, top in need.items():
            if seasons.get(s, 0) < min(top, 3):
                continue
            st, body = tmdb.get(conn, "season", f"{tv}/{s}", "en-US", f"/tv/{tv}/season/{s}", {})
            names = {(s, ep.get("episode_number")): _norm_episode(ep.get("name")) for ep in (body or {}).get("episodes") or []}
            ep_names[tv].update(names)
            for name, (bs, be, title) in bare.items():
                if bs == s and title and names.get((bs, be)) == title:
                    hits[tv].add(name)
    ranked = sorted(hits.items(), key=lambda x: -len(x[1]))
    if not ranked or len(ranked[0][1]) < 3 or (len(ranked) > 1 and len(ranked[0][1]) < 3 * len(ranked[1][1])):
        return
    tv, names = ranked[0]
    # the same show, the same SxxEyy, a title that differs only by «the» / «part» («Frenemy of My Enemy» = «The Frenemy
    # of My Enemy», «S.O.S. Part.1» = «S.O.S. (1)»)
    loose = lambda t: re.sub(r"the|part", "", t or "")
    for name, (bs, be, title) in bare.items():
        theirs = ep_names[tv].get((bs, be))
        if name not in names and title and theirs and (loose(title) == loose(theirs) or
                                                       (len(title) >= 8 and (title in theirs or theirs in title))):
            names = names | {name}
    raw = tmdb.tv_raw(conn, tv) or {}
    for name in names:
        current[name].update(status="episode_names", tmdb_tv=tv, title=raw.get("name"), original_title=raw.get("original_name"),
                             reason=f"episode title = TMDB {tv} S{bare[name][0]:02d}E{bare[name][1]:02d} "
                                    f"({len(names)} such names agree)", candidates=[])


LINKED_STATUSES = ("matched", "manual", "auto_popular", "episode_names")
# films.disks evidence (disk_files.how): ids next to the file / owner-checked links vs a name search / another server
DISK_STRONG = ("manual", "nfo", "folder_tmdbid")


def disk_links(conn) -> dict:
    """lower(path) -> (title_id, how) from the disk scan (films.disks, table disk_files). title_id is None for the
    files it decided are not a work: the owner's own videos ('personal'), extras / creditless OP-ED ('extra'),
    game and SDK assets ('not_film'), or nothing found ('unmatched')."""
    return {p.lower(): (t, how) for p, t, how in conn.execute(
        "SELECT path, title_id, how FROM disk_files WHERE path IS NOT NULL AND how <> 'season_clash'")}


def history_season(conn, tv_id: int, title: str | None, viewed_at: str | None) -> int | None:
    """The season of a Plex history entry whose names carry only an episode number («One_Piece_[692]», «Миротворец 8»):
    the only season; an episode number beyond season 1 = absolute numbering over the TMDB seasons (films.disks); an
    episode within season 1 = season 1 only when no later season had aired by the watch date (Peacemaker watched in
    2024 → S1; What If…? watched in 2024 → S1 or S2, left undecided). A wrong season would mark it watched."""
    from .disks import season_for
    from .sources.plex import parse_episode
    raw = tmdb.tv_raw(conn, tv_id) or {}
    seasons = sorted((s["season_number"], s.get("episode_count") or 0, s.get("air_date"))
                     for s in raw.get("seasons") or [] if s.get("season_number"))
    if len(seasons) == 1:
        return seasons[0][0]
    ep = (parse_episode(title or "", False) or {}).get("episode")
    if not seasons or not isinstance(ep, int) or ep < 1:
        return None
    if ep > seasons[0][1]:
        s, why = season_for(conn, tv_id, None, ep)
        return s if why == "absolute numbering" else None
    later_aired = [n for n, _, air in seasons[1:] if not air or not viewed_at or air <= viewed_at[:10]]
    return seasons[0][0] if not later_aired else None


def _season_title(conn, tv_id: int, season: int | None) -> int | None:
    if season is None:
        series = tmdb.tv(conn, tv_id)
        if not series or series.get("number_of_seasons") != 1:
            return None
        season = 1
    row = conn.execute("SELECT id FROM titles WHERE series_tmdb_id = ? AND season_number = ?", (tv_id, season)).fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------------------------- plex

def link_plex(conn) -> dict:
    from .sources import plex
    stats = defaultdict(int)
    disk = disk_links(conn)
    # 1. downloaded movies; also the items Plex could not match at all (kind 'unmatched'): only the disk scan knows them
    with conn:
        for rk, kind, guids, path in conn.execute(
                "SELECT rating_key, kind, guids, file_path FROM plex_items WHERE kind IN ('movie', 'unmatched')").fetchall():
            ids = _imdb_ids(guids)
            row = conn.execute("SELECT id FROM titles WHERE imdb_id = ? AND season_number IS NULL",
                               (ids[0],)).fetchone() if ids else None
            tid = row[0] if row else (ensure_movie(conn, ids[0], "plex_downloaded") if ids else None)
            key = "items_movie_linked" if row else "items_movie_created" if tid else "items_movie_no_id"
            if not tid:
                tid, dhow = disk.get((path or "").lower(), (None, None))
                key = f"items_{kind}_linked_disk_{dhow}" if tid else f"items_{kind}_{dhow or 'no_id'}"
            conn.execute("UPDATE plex_items SET title_id = ? WHERE rating_key = ?", (tid, rk))
            stats[key] += 1
    # 2. show map for episode-like items and history
    items = conn.execute("SELECT * FROM plex_items WHERE kind = 'episode-like'").fetchall()
    hist = conn.execute("SELECT * FROM plex_history WHERE kind IN ('episode-like', 'episode', 'show')").fetchall()
    names = set()
    item_ep = {}
    for r in items:
        ep = next((e.get("episode") for e in plex.plex_extras(r, "item") if e.get("episode")), None) or {}
        item_ep[r["rating_key"]] = ep
        names.add(ep.get("show"))
    first_seen = {}
    for h in hist:
        n = h["grandparent_title"] or h["title"]
        names.add(n)
        if h["viewed_at"]:
            first_seen[n] = min(first_seen.get(n, h["viewed_at"]), h["viewed_at"])
    show_map = build_show_map(conn, names, first_seen)
    # 3. downloaded seasons: the same file in Jellyfin (it has ids) > the Plex show name > the show folder name
    jf = {(p or "").lower(): (JELLYFIN_TMDB_FIX.get(int(tv), int(tv)), s) for p, tv, s in conn.execute(
        """SELECT e.path, sr.tmdb_id, e.season FROM jellyfin_items e JOIN jellyfin_items sr ON sr.item_id = e.series_item_id
           WHERE e.kind = 'episode' AND sr.tmdb_id IS NOT NULL""")}
    folders = {r["rating_key"]: _show_folder(r["file_path"]) for r in items}
    from datetime import date
    names_f = {f for f in folders.values() if f}  # files on disk: any premiere up to today can be the one
    folder_map = build_show_map(conn, names_f, {f: date.today().isoformat() for f in names_f})
    # a manual disk link (data/disk_map.json → disk_files.how = 'manual') wins: one Plex show name can cover two
    # shows («One Piece» = the anime and the Netflix live-action files, R89) — a wrong link here would let
    # mark_episode scrobble the wrong files
    manual = {(p or "").lower(): t for p, t in conn.execute(
        "SELECT path, title_id FROM disk_files WHERE how = 'manual' AND title_id IS NOT NULL")}
    with conn:
        for r in items:
            ep = item_ep[r["rating_key"]]
            season = ep.get("season") or season_of(r["file_path"])
            path = (r["file_path"] or "").lower()
            tid, how = None, None
            d_tid, d_how = disk.get(path, (None, None))
            if path in manual:
                tid, how = manual[path], "disk_manual"
            elif d_tid and d_how in DISK_STRONG:  # an .nfo / «[tmdbid-N]» next to the file beats any name search
                tid, how = d_tid, f"disk_{d_how}"
            elif path in jf:
                tv, s = jf[path]
                tid, how = ensure_season(conn, int(tv), s) if s is not None else None, "jellyfin"
            for name, label in ((ep.get("show") or "", "plex_show"), (folders[r["rating_key"]] or "", "folder")):
                if tid:
                    break
                m = (show_map if label == "plex_show" else folder_map).get(name, {})
                if m.get("tmdb_tv") and m.get("status") in LINKED_STATUSES:
                    s_name = season if season is not None else (m.get("season") or name_season(name, m.get("original_title")))
                    tid, how = _season_title(conn, m["tmdb_tv"], s_name), label
                    if not tid:
                        s_no = s_name if s_name is not None else (
                            1 if (tmdb.tv(conn, m["tmdb_tv"]) or {}).get("number_of_seasons") == 1 else None)
                        tid = ensure_season(conn, m["tmdb_tv"], s_no) if s_no is not None else None
            if not tid and d_tid:
                # the disk scan (films.disks): absolute numbering over the TMDB seasons («One Piece [693]» → S17),
                # names parsed like torrent names, the owner's numbering in data/disk_map.json
                tid, how = d_tid, f"disk_{d_how}"
            conn.execute("UPDATE plex_items SET title_id = ?, show_title = ?, season = ?, episode = ? WHERE rating_key = ?",
                         (tid, ep.get("show"), season, ep.get("episode"), r["rating_key"]))
            stats[f"items_episode_linked_{how}" if tid else f"items_episode_{d_how or 'unlinked'}"] += 1
    # 4. watched movies (plex history + library view counts)
    watched_movies: dict[str, str] = {}
    for h in conn.execute("SELECT guids, viewed_at FROM plex_history WHERE kind = 'movie'"):
        for i in _imdb_ids(h["guids"])[:1]:
            watched_movies[i] = max(watched_movies.get(i, ""), h["viewed_at"] or "")
    for r in conn.execute("SELECT guids, last_viewed_at FROM plex_items WHERE kind = 'movie' AND view_count > 0"):
        for i in _imdb_ids(r["guids"])[:1]:
            watched_movies[i] = max(watched_movies.get(i, ""), r["last_viewed_at"] or "")
    for i, (imdb_id, at) in enumerate(sorted(watched_movies.items()), 1):
        with conn:
            tid = ensure_movie(conn, imdb_id, "plex_watched")
            if not tid:
                stats["watched_movie_not_on_tmdb"] += 1
                continue
            conn.execute("INSERT OR REPLACE INTO watched (title_id, source, watched_at) VALUES (?, 'plex', ?)",
                         (tid, at or None))
            stats["watched_movies"] += 1
    # 5. watched seasons: distinct episodes watched vs episodes in the season
    seen = defaultdict(set)
    last = {}
    for h in hist:
        name = h["grandparent_title"] or h["title"]
        m = show_map.get(name, {})
        if not m.get("tmdb_tv"):
            continue
        season = h["season"] or season_of(h["title"], name)
        if h["source"] == "plextv" and h["kind"] == "episode":
            found = tmdb.find_by_imdb(conn, _imdb_ids(h["guids"])[0]).get("tv_episode") if _imdb_ids(h["guids"]) else None
            if found:
                season = found["season"]
                seen[(m["tmdb_tv"] if m["tmdb_tv"] == found["show_id"] else found["show_id"], season)].add(found["episode"])
                last[(found["show_id"], season)] = max(last.get((found["show_id"], season), ""), h["viewed_at"] or "")
                continue
        if season is None:
            season = m.get("season") or name_season(name, m.get("original_title")) or history_season(
                conn, m["tmdb_tv"], h["title"], h["viewed_at"])
        key = (m["tmdb_tv"], season)
        seen[key].add(h["title"])
        last[key] = max(last.get(key, ""), h["viewed_at"] or "")
    with conn:
        for (tv_id, season), eps in seen.items():
            tid = _season_title(conn, tv_id, season)
            if not tid:
                s_no = season if season is not None else (
                    1 if (tmdb.tv(conn, tv_id) or {}).get("number_of_seasons") == 1 else None)
                if s_no is not None:
                    tid = ensure_season(conn, tv_id, s_no)
                    stats["watched_season_created" if tid else "watched_season_not_on_tmdb"] += 1
            if not tid:
                stats["watched_season_not_in_db"] += 1
                continue
            total = conn.execute("SELECT episodes_total FROM titles WHERE id = ?", (tid,)).fetchone()[0]
            src = "plex" if total and len(eps) >= total else "plex_partial"
            conn.execute("INSERT OR REPLACE INTO watched (title_id, source, watched_at) VALUES (?, ?, ?)",
                         (tid, src, last.get((tv_id, season))))
            stats[f"watched_season_{src}"] += 1
    stats["show_names"] = len(show_map)
    stats["show_names_unmatched"] = sum(1 for v in show_map.values() if v.get("status") not in LINKED_STATUSES + ("personal",))
    return dict(stats)


# ---------------------------------------------------------------------------------------------- jellyfin

# Jellyfin's own provider ids that are wrong (checked by hand): Jellyfin TMDB id → the right one
JELLYFIN_TMDB_FIX = {114339: 66053}   # «Berserk TV-2» tagged as a Norwegian show; is Берсерк (2016)


PATH_TMDB = re.compile(r"\[tmdbid-(\d+)\]", re.I)
PATH_EPISODE = re.compile(r"\bS(\d{1,2})E\d{1,3}\b", re.I)


def _from_path(conn, path: str) -> int | None:
    """A file a movie library has no ids for (episodes in a «movies» library, e.g. Яна's folder): the folder carries
    «[tmdbid-N]» and the file name «SxxEyy» → that series season; «[tmdbid-N]» alone → that movie."""
    m = PATH_TMDB.search(path)
    if not m:
        return None
    ep = PATH_EPISODE.search(path.rsplit("\\", 1)[-1])
    if ep:
        return ensure_season(conn, int(m.group(1)), int(ep.group(1)))
    raw = tmdb.movie_raw(conn, int(m.group(1))) or {}
    return ensure_movie(conn, raw["imdb_id"], "path_tmdbid") if raw.get("imdb_id") else None


def link_jellyfin(conn) -> dict:
    """Jellyfin items and the owner's Jellyfin played marks -> titles (created from TMDB when out of scope).
    Watched sources: 'jellyfin' (a movie, or every episode of a season) and 'jellyfin_partial'."""
    stats = defaultdict(int)
    # a file Jellyfin has no ids for (episodes outside any series — «Peacemaker.2022.S02E05…» in a movie folder —, «The
    # Continental 1.mkv», Berserk with the owner's numbering): the same path in Plex (linked there) or the disk scan
    disk = disk_links(conn)
    plex_path = {p.lower(): t for p, t in conn.execute(
        "SELECT file_path, title_id FROM plex_items WHERE title_id IS NOT NULL AND file_path IS NOT NULL")}

    def by_path(path: str | None) -> tuple[int | None, str | None]:
        p = (path or "").lower()
        if not p:
            return None, None
        if p in plex_path:
            return plex_path[p], "plex_path"
        t, how = disk.get(p, (None, None))
        return t, (f"disk_{how}" if how else None)

    with conn:
        for iid, imdb, tmdb_id, path in conn.execute(
                "SELECT item_id, imdb_id, tmdb_id, path FROM jellyfin_items WHERE kind = 'movie'").fetchall():
            tid = ensure_movie(conn, imdb, "jellyfin") if imdb else None
            if not tid and path:
                tid = _from_path(conn, path)
                if tid:
                    stats["linked_by_path"] += 1
            if not tid:
                tid, how = by_path(path)
                if how:
                    stats[f"movies_{'linked_' if tid else ''}{how}"] += 1
            conn.execute("UPDATE jellyfin_items SET title_id = ? WHERE item_id = ?", (tid, iid))
            stats["movies_linked" if tid else "movies_no_id"] += 1
        for iid, kind, tv, season, path in conn.execute(
                """SELECT e.item_id, e.kind, sr.tmdb_id, e.season, e.path FROM jellyfin_items e
                   LEFT JOIN jellyfin_items sr ON sr.item_id = e.series_item_id
                   WHERE e.kind IN ('episode', 'season')""").fetchall():
            tv = JELLYFIN_TMDB_FIX.get(int(tv), int(tv)) if tv else None
            tid = ensure_season(conn, tv, season) if tv and season is not None else None
            if not tid and kind == "episode":
                tid, how = by_path(path)
                if how:
                    stats[f"episodes_{'linked_' if tid else ''}{how}"] += 1
            conn.execute("UPDATE jellyfin_items SET title_id = ? WHERE item_id = ?", (tid, iid))
            stats["episodes_seasons_linked" if tid else "episodes_seasons_unlinked"] += 1
        # series / season containers: the title of their episodes when they all are one season («Arcane.S02…» folder,
        # Berserk TV-2 «Сезон не опознан»); a show folder with several seasons stays without a title
        for sid, tids, n_eps in conn.execute(
                """SELECT series_item_id, group_concat(DISTINCT coalesce(title_id, 0)), count(*) FROM jellyfin_items
                   WHERE kind = 'episode' AND series_item_id IS NOT NULL GROUP BY series_item_id""").fetchall():
            one = tids.split(",") if tids else []
            tid = int(one[0]) if len(one) == 1 and one[0] != "0" else None
            for iid, kind in conn.execute(
                    """SELECT item_id, kind FROM jellyfin_items
                       WHERE item_id = ? AND kind = 'series' OR series_item_id = ? AND kind = 'season' AND title_id IS NULL""",
                    (sid, sid)).fetchall():
                conn.execute("UPDATE jellyfin_items SET title_id = ? WHERE item_id = ?", (tid, iid))
                stats[f"{kind}_containers_{'linked' if tid else 'not_one_season'}"] += 1
    seasons = defaultdict(lambda: {"eps": set(), "at": ""})
    with conn:
        for key, kind, imdb, season, episode, at in conn.execute(
                "SELECT key, kind, imdb_id, season, episode, played_at FROM jellyfin_played").fetchall():
            if kind == "movie":
                tid = ensure_movie(conn, imdb, "jellyfin_played")
                if tid:
                    conn.execute("INSERT OR REPLACE INTO watched (title_id, source, watched_at) VALUES (?, 'jellyfin', ?)",
                                 (tid, at))
                conn.execute("UPDATE jellyfin_played SET title_id = ? WHERE key = ?", (tid, key))
                stats["played_movies" if tid else "played_movie_not_on_tmdb"] += 1
                continue
            tv = tmdb.find_by_imdb(conn, imdb).get("tv")
            tid = ensure_season(conn, tv, season) if tv else None
            conn.execute("UPDATE jellyfin_played SET title_id = ? WHERE key = ?", (tid, key))
            if not tid:
                stats["played_episode_unlinked"] += 1
                continue
            seasons[tid]["eps"].add(episode)
            seasons[tid]["at"] = max(seasons[tid]["at"], at or "")
        for tid, v in seasons.items():
            total = conn.execute("SELECT episodes_total FROM titles WHERE id = ?", (tid,)).fetchone()[0]
            src = "jellyfin" if total and len(v["eps"]) >= total else "jellyfin_partial"
            conn.execute("INSERT OR REPLACE INTO watched (title_id, source, watched_at) VALUES (?, ?, ?)",
                         (tid, src, v["at"] or None))
            stats[f"played_season_{src}"] += 1
    return dict(stats)


# ---------------------------------------------------------------------------------------------- stores

EDITION_RE = re.compile(r"\((?:[^()]*\b(?:version|edition|cut|theatrical|unrated|uncensored|верси[яюи]|"
                        r"издание|без цензуры)\b[^()]*)\)|\b(?:extended|unrated|theatrical)\b", re.I)


LOOKALIKE = dict(zip("ACEHKMOPTXaceopxy", "АСЕНКМОРТХасеорху"))


def store_names(title: str, extra: list) -> list[str]:
    """Search names for a store item: «Дюна. Часть Вторая (Dune: Part Two)» -> both halves; edition tags,
    «(2019)» and the «DCU: » shelf prefix removed."""
    out = []
    for t in [title, *extra]:
        if not t:
            continue
        # Latin look-alikes inside Cyrillic words: «Cудная ночь 2» starts with a Latin C
        t = re.sub(r"[A-Za-z](?=[а-яё])|(?<=[а-яё])[A-Za-z]", lambda m: LOOKALIKE.get(m.group(0), m.group(0)), t)
        t = EDITION_RE.sub(" ", t)
        t = re.sub(r"\(\s*(?:19|20)\d{2}\s*\)", " ", t)
        t = re.sub(r"^\s*DCU:\s*", "", t)
        parts = [t]
        m = re.match(r"^(.*?)\s*\(([^()]+)\)\s*$", t)
        if m:
            parts = [m.group(1), m.group(2)]
        for p in parts:
            p = re.sub(r"\s+", " ", p).strip(" -:")
            if p and not p.isupper():
                out.append(p)
            elif p:
                out.append(p.title())  # «ОСТРОВ ФАНТАЗИЙ» -> «Остров Фантазий»
    return list(dict.fromkeys(out))


def _minutes(duration: str | None) -> int | None:
    """YouTube «2:03:45» -> 124."""
    if not duration or not re.fullmatch(r"\d+(:\d\d){1,2}", duration):
        return None
    parts = [int(x) for x in duration.split(":")]
    h, m, s = ([0] + parts)[-3:]
    return round(h * 60 + m + s / 60)


def _by_runtime(conn, res: dict, minutes: int | None) -> dict | None:
    """An ambiguous store search decided by the item's duration: the only candidate whose name fits and
    whose TMDB runtime is within 4 minutes (stores show the full runtime with credits)."""
    if not minutes:
        return None
    fits = []
    for c in res.get("candidates") or []:
        if (c.get("score") or 0) < 0.75:
            continue
        m = tmdb.movie(conn, c["tmdb_id"])
        if m and m.get("runtime_min") and abs(m["runtime_min"] - minutes) <= 4:
            fits.append(((abs(m["runtime_min"] - minutes), -(c.get("score") or 0)), m))
    fits.sort(key=lambda x: x[0])
    # one fit, or the closest runtime (then the best name) strictly beats the rest:
    # Fight Club 1999 139 min = store 2:19:07, Fight Club 2023 135 min, «Fight Club: Members Only» 140 min
    return fits[0][1] if fits and (len(fits) == 1 or fits[0][0] < fits[1][0]) else None


def link_stores(conn) -> dict:
    """Store items -> titles. Every owned item gets a title (created from TMDB when out of tracker scope).
    Google Play items are the same purchases as the YouTube library items they point to
    (raw.youtube_library_vid) — YouTube is resolved first and lends them its IMDb id."""
    stats = defaultdict(int)
    rows = conn.execute("""SELECT store, item_id, title, year, owned, raw FROM store_items
                           ORDER BY store = 'google_play', store, item_id""").fetchall()
    for store, item_id, title, year, owned, raw in rows:
        d = json.loads(raw or "{}")
        imdb_ids = d.get("imdb_ids") or ([d["imdb_resolved"]] if d.get("imdb_resolved") else [])
        yt = d.get("youtube_library_vid")
        if not imdb_ids and yt:
            row = conn.execute("""SELECT t.imdb_id, json_extract(s.raw, '$.imdb_resolved') FROM store_items s
                                  LEFT JOIN titles t ON t.id = s.title_id WHERE s.store = 'youtube' AND s.item_id = ?""",
                               (yt,)).fetchone()
            if row and (row[0] or row[1]):
                imdb_ids = [row[0] or row[1]]
                stats[f"{store}_via_youtube"] += 1
        if not imdb_ids and title:
            if not year:
                m = re.search(r"\((19\d\d|20\d\d)\)", title)
                year = int(m.group(1)) if m else None
            names = store_names(title, [d.get("title_en"), d.get("title_ru")])
            res = tmdb.search(conn, names, year, "movie")
            minutes = _minutes(d.get("duration"))
            if res["status"] != "matched" and minutes:
                m = _by_runtime(conn, res, minutes)
                if not m and year:  # the store's year can be a re-release year (Fight Club «2011»)
                    m = _by_runtime(conn, tmdb.search(conn, names, None, "movie"), minutes)
                if m:
                    res = {"status": "matched", "best": {"tmdb_id": m["tmdb_id"]}}
                    stats[f"{store}_by_runtime"] += 1
            if res["status"] == "matched":
                m = tmdb.movie(conn, res["best"]["tmdb_id"])
                if m and m.get("imdb_id"):
                    imdb_ids = [m["imdb_id"]]
            if not imdb_ids:
                stats[f"{store}_unresolved_{res['status']}"] += 1
        tid = None
        with conn:
            if imdb_ids:
                row = conn.execute("SELECT id FROM titles WHERE imdb_id = ? AND season_number IS NULL", (imdb_ids[0],)).fetchone()
                if row:
                    tid = row[0]
                elif owned:
                    tid = ensure_movie(conn, imdb_ids[0], "store_owned")
                    stats[f"{store}_owned_created" if tid else f"{store}_owned_not_on_tmdb"] += 1
                if not tid:
                    d["imdb_resolved"] = imdb_ids[0]
            conn.execute("UPDATE store_items SET title_id = ?, raw = ? WHERE store = ? AND item_id = ?",
                         (tid, json.dumps(d, ensure_ascii=False), store, item_id))
        stats[f"{store}_linked" if tid else f"{store}_not_in_titles"] += 1
    return dict(stats)
