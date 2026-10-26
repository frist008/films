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

PACK_RE = re.compile(r"Коллекция|Антология|Дилогия|Трилогия|Тетралогия|Квадрилогия|Квадрология|Пенталогия|Гексалогия|"
                     r"Collection|Anthology|Antology|Dilogy|Duology|Trilogy|Trilogie|Tetralogy|Quadrilogy|Pentalogy|Hexalogy",
                     re.I)
# how many films a pack word promises («Голодные игры: Квадрология» = 4); «Коллекция» / «Антология» say nothing
PACK_COUNT = {"дилогия": 2, "dilogy": 2, "duology": 2, "трилогия": 3, "trilogy": 3, "trilogie": 3, "тетралогия": 4,
              "квадрилогия": 4, "квадрология": 4, "tetralogy": 4, "quadrilogy": 4, "пенталогия": 5, "pentalogy": 5,
              "гексалогия": 6, "hexalogy": 6}
# «Сага» is a pack only with a year span («Сумерки. Сага (2008-2012)»): «Horizon: An American Saga - Chapter 1 (2024)»,
# «Корни: Сага о вампирах / Feed (2026)» are single films; so is «Вселенная» («Вселенная «Заклятия» / The Conjuring
# Universe (2013-2025)» holds the whole universe; its page linked only «Проклятие монахини»)
SAGA_RE = re.compile(r"Сага\b|Saga\b|Вселенная\b|Universe\b", re.I)
# «Человек-паук: Новый день 3D / Spider-Man: Brand New Day 3D (2026)» is the ordinary film
SUFFIX_3D_RE = re.compile(r"\s+3D$", re.I)
SERIES_TYPES = {"tvSeries", "tvMiniSeries"}
MOVIE_TYPES = {"movie", "tvMovie", "video", "tvSpecial", "short"}


def _basics(conn, tconst: str):
    return conn.execute("SELECT * FROM imdb_basics WHERE tconst = ?", (tconst,)).fetchone()


def _keep_episode_id(np: dict) -> bool:
    """A release of one special episode (a Christmas special, a pilot film) may rightly link an IMDb episode id."""
    return bool(re.search(r"(?i)спецвыпуск|special|рождеств|christmas|пилот|pilot|эпизод|episode", np.get("name") or ""))


def _seasons(np: dict) -> list[int]:
    """Seasons of the release name; a block the name parser left unread is read here: «S01-03 из 03», «S01, 03 из 03»,
    «S01-06, 10», «S14x01-10 из 18» («Аватар: Легенда об Аанге. Трилогия [S01-03 из 03]» was taken for a film pack)."""
    if np.get("seasons"):
        return np["seasons"]
    t = (np.get("season_raw") or "").replace("х", "x").replace("Х", "x")
    if not re.match(r"(?i)\s*s\d", t):
        return []
    out = []
    for piece in t.split(","):
        m = re.match(r"(?i)\s*s?(\d{1,3})(?:\s*-\s*s?(\d{1,3})(?![\dx]))?", piece)
        if m:
            a, z = int(m.group(1)), int(m.group(2) or m.group(1))
            out += range(a, z + 1) if z >= a else [a]
    return list(dict.fromkeys(out))


def _page_record(conn, tconst: str):
    """The IMDb record of a page id: the datasets hold only 2022+ titles (and linked ones), so an older id is typed from
    TMDB /find (cached): movie / tvSeries / tvEpisode / tvSeason with its year; 'unknown' when TMDB has nothing either
    (a podcast episode: «Я краснею» → tt18097030)."""
    b = _basics(conn, tconst)
    if b is not None:
        return b
    f = tmdb.find_by_imdb(conn, tconst)
    if f.get("movie"):
        ttype, year, name = "movie", f.get("movie_year"), f.get("movie_title")
    elif f.get("tv"):
        ttype, year, name = "tvSeries", f.get("tv_year"), f.get("tv_title")
    elif f.get("tv_episode"):
        ttype, year, name = "tvEpisode", None, (f["tv_episode"] or {}).get("name")
    elif f.get("tv_season"):
        ttype, year, name = "tvSeason", None, None
    else:
        ttype, year, name = "unknown", None, None
    return {"tconst": tconst, "title_type": ttype, "primary_title": name, "original_title": None, "start_year": year,
            "end_year": None, "runtime_min": None, "genres": "", "from_tmdb": True}


def _page_id_distrust(b, np: dict, seasons: list[int], y1: int | None, y2: int | None) -> str | None:
    """Why a page IMDb id must not be trusted (None = trust it): its IMDb type contradicts the release —
    an episode (a typo of the film's id: «Пассажиры» 2016 → tt13556444; a special released as an episode keeps it),
    a TV season, a film / short / video for a release of numbered seasons («Приключения Джимми Нейтрона [S01-03]» → the
    2001 film), or an id neither the datasets nor TMDB know for a 2022+ release (the datasets hold every 2022+ film and
    series, so it is a podcast / episode: «Я краснею» (2022) → tt18097030)."""
    if b is None:
        return None
    t = b["title_type"]
    if t == "tvEpisode" and not _keep_episode_id(np):
        return "episode id"
    if t == "tvSeason":
        return "season id"
    if seasons and t in MOVIE_TYPES - {"tvSpecial"}:
        return f"{t} id for a season release"
    if t == "unknown" and (y2 or y1 or 0) >= imdb.YEAR_FROM:
        return "id unknown to the IMDb datasets and TMDB"
    return None


def _page_names_disjoint(conn, tconst: str, b, p: dict) -> bool:
    """The page id names another work: the release has an original (Latin) name, the IMDb datasets know the id's names
    (primary / original / aka) and not one release name is among them («Дьявол может плакать / Devil May Cry [S02]
    (2026)» whose page linked «Dandadan» tt30217403). Only a hint — the caller re-resolves by name and keeps the page
    id unless another title is found."""
    if b is None or b["title_type"] == "unknown" or not _original_names(p):
        return False
    known = set() if isinstance(b, dict) and b.get("from_tmdb") else _imdb_names(conn, tconst, b)
    loose = lambda s: re.sub(r"[\W_]+", "", s or "")
    known = {loose(k) for k in known} - {""}
    if not known:
        return False
    mine = {loose(imdb.norm(t)) for t in _title_names(p)} - {""}
    # punctuation never decides («R.J. Decker» / «RJ Decker», «Ghouls Rule!» / «Ghoul's Rule!»), nor an edition or a
    # franchise prefix around the same name («Blade Runner (Final Cut)», «James Bond: The Spy Who Loved Me»)
    return not any(a == k or (min(len(a), len(k)) >= 5 and (a in k or k in a)) for a in mine for k in known)


def _page_alternative(conn, tconst: str, b, p: dict, category: str, y1: int | None, y2: int | None,
                      seasons: list[int]) -> bool:
    """The page id is another work's (_page_names_disjoint) and the names find a different one: an IMDb title (the page
    id excluded), or a TMDB entry carrying another IMDb id. A TMDB entry without an IMDb id, or with the page's own, is
    no evidence (the same work under another spelling) — the page id stays."""
    if not _page_names_disjoint(conn, tconst, b, p):
        return False
    kind = _kind(p, category, None)   # what the release itself is («Lift (2024)» whose page linked «What If...?»)
    src, key, _ = _resolve(conn, p, category, kind, y1, y2, seasons, frozenset({tconst}))
    if src == "imdb":
        return key != tconst
    if src == "tmdb":
        raw = (tmdb.movie_raw(conn, key) if kind == "movie" else tmdb.tv_raw(conn, key)) or {}
        other = raw.get("imdb_id") or (raw.get("external_ids") or {}).get("imdb_id")
        return bool(other) and canonical_imdb(other) != tconst
    return False


def _film_not_series(conn, p: dict, category: str, b, seasons: list[int], y1: int | None, tconst: str | None = None) -> bool:
    """A film-category release without season / episode marks whose page id is a series, while TMDB has a film of that
    exact name and year: the release is the film («Лига справедливости: Боги и монстры (2015)» → the «Chronicles» series,
    «Константин: Город демонов (2018) BDRemux» → the series of shorts the feature film was cut from). A TMDB film carrying
    the page's own id is the same work typed differently («Зверский коп / Bêtes de Flic (2026)») — trusted."""
    np = p.get("name_parsed") or {}
    if (b is None or b["title_type"] not in SERIES_TYPES or category not in ("foreign_films", "animation") or seasons
            or np.get("episodes") or np.get("episode_from") is not None or not y1):
        return False
    tr = tmdb.search(conn, _title_names(p), y1, "movie")
    best = tr.get("best") if tr["status"] == "matched" else None
    return bool(best and (best.get("name_fit") or 0) >= 0.9 and best.get("year_diff") == 0
                and (not tconst or best.get("imdb_id") != tconst))


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
    if (category in ("foreign_series", "russian_series") or _seasons(np) or np.get("episodes")
            or re.search(r"(?i)сери[ия]", np.get("season_raw") or "")):   # «Рэй / Ray The Animation [1-13 серии из 13]»
        return "series"
    return "movie"


def _issue(conn, release_id: int, problem: str, candidates=None):
    conn.execute(
        "INSERT INTO match_issues (release_id, problem, candidates) VALUES (?, ?, ?)",
        (release_id, problem, json.dumps(candidates, ensure_ascii=False, default=str) if candidates else None))


def _pack_head(name: str) -> str:
    """The names part of a release name — before the year: «Старикам тут не место / No Country for Old Men (2007) BDRemux
    | Criterion Collection» is one film, the edition tag after the year says nothing about a pack."""
    return re.split(r"\(\s*\d{4}|\[", name or "", maxsplit=1)[0]


def _is_pack(name: str, y1: int | None, y2: int | None, seasons: list[int] | None = None) -> bool:
    """A multi-film pack: a pack word in the names part («Омен: Коллекция», «Блэйд: Трилогия»), «Сага» only with a year
    span. A season pack is a series release, not a film pack («Аватар: Легенда об Аанге. Трилогия [S01-03]»)."""
    if seasons:
        return False
    head = _pack_head(name)
    return bool(PACK_RE.search(head) or (SAGA_RE.search(head) and y1 and y2 and y2 > y1))


def _pack_expected(name: str) -> int | None:
    """Number of films the pack name promises: «Дилогия» 2 … «Гексалогия» 6, «The Addams Family 2 Movie Collection» 2."""
    head = _pack_head(name)
    m = re.search(r"\b(\d{1,2})[\s-]*(?:Movies?|Films?)\s+Collection", head, re.I)
    if m:
        return int(m.group(1))
    words = {PACK_COUNT[w.casefold()] for w in PACK_RE.findall(head) if w.casefold() in PACK_COUNT}
    return words.pop() if len(words) == 1 else None


_PACK_TAIL_RE = re.compile(r"[\s:.,\-–—]*(?:(?:the\s+)?complete\s+|полная\s+)?(?:\d{1,2}[\s-]*(?:movies?|films?)\s+)?"
                           r"(?:" + PACK_RE.pattern + r")\b.*$", re.I)


def _pack_base_names(p: dict) -> list[str]:
    """The franchise name of a pack: the release names without the pack word («The Omen: Collection» → «The Omen»,
    «Монстры на каникулах: Квадрология» → «Монстры на каникулах») — the queries for a TMDB collection search."""
    out = [_PACK_TAIL_RE.sub("", t).strip(" :.-–—") for t in _title_names(p)]
    return [t for t in dict.fromkeys(out) if len(t) >= 2]


def _fit_parts(parts: list[dict], y1: int | None, y2: int | None, n: int | None, slack: int = 0,
               tol: int = 1) -> list[dict] | None:
    """The films of a pack among candidate parts ({'year': …}): those dated within the release's year span (± slack); the
    span's ends must be there (a film of the first year and one of the last, ± tol — «Чужой: Коллекция (1979-2024)» holds
    «Чужой» and «Ромул»), the count must be the promised one (n) and at least two. Anything else — None, never guessed."""
    if not y1:
        return None
    y2 = y2 or y1
    got = [x for x in parts if x.get("year") and y1 - slack <= x["year"] <= y2 + slack]
    if len(got) < 2 or (n and len(got) != n):
        return None
    years = [x["year"] for x in got]
    if abs(min(years) - y1) > tol or abs(max(years) - y2) > tol:
        return None
    return got


def page_imdb_ids(conn, release_id: int) -> list[str]:
    """Every IMDb id of a release page's description (the parser keeps only the first): a pack page lists each film."""
    row = conn.execute("SELECT body FROM release_raw WHERE release_id = ?", (release_id,)).fetchone()
    if not row:
        return []
    import zlib
    from .parse.rutor import _split_details
    desc = _split_details(zlib.decompress(row[0]).decode("utf-8", "replace"))["desc_html"]
    ids = re.findall(r"imdb\.com/(?:[a-z]{2}/)?title/(tt\d{7,9})", desc, re.I)
    ids += re.findall(r"(?<![\w/])(tt\d{7,9})(?![\w])", desc)
    return list(dict.fromkeys(canonical_imdb(t) for t in ids))


def _search_collections(conn, names: list[str]) -> list[int]:
    """TMDB collection ids found by the pack's franchise names (ru-RU and en-US queries, cached like tmdb.search)."""
    out = []
    for q in names:
        for lang in ("ru-RU", "en-US"):
            st, body = tmdb.get(conn, "search", f"collection||{q}", lang, "/search/collection",
                                {"query": q, "include_adult": "false"})
            for r in (body or {}).get("results") or []:
                nm = {imdb.norm(r.get("name")), imdb.norm(r.get("original_name"))}
                if any(imdb.norm(q) and imdb.norm(q) in n for n in nm if n):
                    out.append(r["id"])
    return list(dict.fromkeys(out))


def _collection_parts(conn, cid: int) -> list[dict]:
    col = tmdb.collection(conn, cid) or {}
    return [dict(x, collection_id=cid) for x in col.get("parts") or [] if x.get("media_type") in (None, "movie")]


def _franchise_films(conn, cid: int) -> list[dict]:
    """Films (not series / shorts / documentaries, not 'review') of the franchises in config/franchises.toml built on
    TMDB collection cid ({'tmdb_id', 'imdb_id', 'year', 'title', 'collection_id'})."""
    import tomllib
    from . import db
    try:
        with open(db.ROOT / "config" / "franchises.toml", "rb") as f:
            keys = [fr["key"] for fr in tomllib.load(f).get("franchise", []) if cid in (fr.get("tmdb_collections") or [])]
        rows = conn.execute(
            f"""SELECT imdb_id, tmdb_id, year, title FROM franchise_members WHERE franchise_key IN
                ({','.join('?' * len(keys))}) AND confidence != 'review' AND tmdb_type = 'movie'
                AND kind IN ('film', 'animated film') AND year IS NOT NULL AND tmdb_id IS NOT NULL""", keys).fetchall() if keys else []
    except Exception:  # noqa: BLE001 — no config / table in a bare test DB
        return []
    out = {}
    for imdb_id, tmdb_id, year, title in rows:
        out.setdefault(tmdb_id, {"tmdb_id": tmdb_id, "imdb_id": imdb_id, "year": year, "title_original": title,
                                 "collection_id": cid, "via_franchise": True})
    return list(out.values())


def _pack_parts(conn, rid: int, p: dict, y1: int | None, y2: int | None) -> tuple[list[dict] | None, dict]:
    """The films of a pack release → ([{'tmdb_id', 'imdb_id', 'year', 'title'}], info) or (None, info with the reason).

    1. The page's own list: every IMDb id of the description that TMDB knows as a film of the year span («Омен: Коллекция
       (1976-2024)» lists all six); series ids are skipped («Police Squad!» on «Голый пистолет: Антология»).
    2. The TMDB collection of the page's film (or of the found films), parts within the year span.
    3. A TMDB collection found by the franchise name — only when exactly one collection fits the span and the count.
    The span's ends and the promised count («Трилогия» = 3) must agree (_fit_parts); otherwise left for review."""
    name = (p.get("name_parsed") or {}).get("name") or p.get("name") or ""
    n = _pack_expected(name)
    info: dict = {"page_imdb": canonical_imdb(p.get("imdb_id")), "years": [y1, y2], "expected": n}
    page = []
    for tt in page_imdb_ids(conn, rid) or ([info["page_imdb"]] if info["page_imdb"] else []):
        f = tmdb.find_by_imdb(conn, tt)
        if f.get("movie"):
            page.append({"tmdb_id": f["movie"], "imdb_id": tt, "year": f.get("movie_year"), "title": f.get("movie_title")})
        else:
            info.setdefault("page_skipped", []).append({"imdb_id": tt, "tv": f.get("tv"), "tv_title": f.get("tv_title")})
    info["page_films"] = [(x["imdb_id"], x["year"]) for x in page]
    got = _fit_parts(page, y1, y2, n, slack=1)
    if got:
        return got, dict(info, via="page_imdb_ids")
    # 2. the collection of a film the page names
    cids = []
    for x in page:
        m = tmdb.movie(conn, x["tmdb_id"]) or {}
        if m.get("collection"):
            cids.append(m["collection"]["id"])
    cids = list(dict.fromkeys(cids))
    via = "tmdb_collection"
    if not cids:
        # 3. a collection found by the franchise name
        cids, via = _search_collections(conn, _pack_base_names(p)), "tmdb_collection_search"
    parts = {cid: _collection_parts(conn, cid) for cid in cids}
    for cid in cids:
        # the collection lacks a film of the span («Джон Уик: Коллекция (2014-2025)»: TMDB's John Wick collection ends
        # in 2023, «Балерина» 2025 is in the franchise only) — the films of the owner's franchise built on it are added
        known = {x.get("tmdb_id") for x in parts[cid]}
        parts[cid] += [x for x in _franchise_films(conn, cid) if x["tmdb_id"] not in known]
    fits = []
    for tol, slack in ((0, 0), (1, 0), (1, 1)):
        # the exact span first: «РЭД: Дилогия (2010-2013)» is RED + RED 2, not another «Red» collection of 2010 and 2012
        fits = [(cid, got) for cid in cids if (got := _fit_parts(parts[cid], y1, y2, n, slack, tol))]
        if fits:
            break
    info["collections"] = cids
    if len(fits) == 1:
        cid, got = fits[0]
        return [dict(x, title=x.get("title_original") or x.get("title_ru")) for x in got], dict(info, via=via, collection=cid)
    # the page's own list lacking a film of the span's end, nothing promised by count: the films it names are linked (each
    # of them is in the pack), marked partial («Джон Уик: Антология (2014-2025)» names four films and the series)
    near = [x for x in page if x.get("year") and y1 and y1 - 1 <= x["year"] <= (y2 or y1) + 1]
    if not fits and not n and len(near) >= 2:
        return near, dict(info, via="page_imdb_ids", partial=True)
    info["reason"] = ("several TMDB collections fit" if fits else
                      "no TMDB collection fits the year span / count" if cids else "no TMDB collection found")
    if fits:
        info["fitting"] = {cid: [(x["tmdb_id"], x["year"]) for x in got] for cid, got in fits}
    return None, info


def _link_pack(conn, rid: int, p: dict, category: str, y1: int | None, y2: int | None) -> tuple[list[int] | None, dict]:
    """Link a pack release to every film it holds (release_titles.how = 'collection_pack') → (title ids, info)."""
    parts, info = _pack_parts(conn, rid, p, y1, y2)
    if not parts:
        return None, info
    tids = []
    with conn:
        for x in parts:
            tid = ensure_movie(conn, x["imdb_id"], "match_pack") if x.get("imdb_id") else _tmdb_movie_title(conn, x["tmdb_id"])
            if tid is None:
                tid = _tmdb_movie_title(conn, x["tmdb_id"])
            if tid is None:
                raise LookupError(f"TMDB has no movie {x}")
            tids.append(tid)
            x["title_id"] = tid
        for tid in dict.fromkeys(tids):
            conn.execute("INSERT OR IGNORE INTO release_titles (release_id, title_id, how) VALUES (?, ?, 'collection_pack')",
                         (rid, tid))
            conn.execute("UPDATE titles SET tracker_category = ? WHERE id = ? AND tracker_category IS NULL", (category, tid))
    info["parts"] = [{k: x.get(k) for k in ("title_id", "imdb_id", "tmdb_id", "year", "title")} for x in parts]
    return list(dict.fromkeys(tids)), info


def _resolve_pack_issue(conn, rid: int, info: dict):
    """A linked pack is recorded in match_issues as resolved (problem 'collection_pack'), the open issue closed."""
    js = json.dumps(info, ensure_ascii=False, default=str)
    with conn:
        n = conn.execute("UPDATE match_issues SET resolved_at = datetime('now'), candidates = ? WHERE release_id = ? "
                         "AND problem = 'collection_pack' AND resolved_at IS NULL", (js, rid)).rowcount
        if not n:
            conn.execute("INSERT INTO match_issues (release_id, problem, candidates, resolved_at) "
                         "VALUES (?, 'collection_pack', ?, datetime('now'))", (rid, js))


def link_packs(conn, log=lambda *_: None) -> dict:
    """Retry the open 'collection_pack' issues (a pack left earlier: TMDB knew less, or the release is no pack under the
    current rule — then the issue is closed and the release goes through the normal matching)."""
    st = {"packs": 0, "linked": 0, "parts": 0, "not_pack": 0, "left": 0}
    rows = conn.execute("""SELECT r.id, r.category, r.parsed FROM releases r JOIN match_issues m ON m.release_id = r.id
                           WHERE m.problem = 'collection_pack' AND m.resolved_at IS NULL AND r.parsed IS NOT NULL""").fetchall()
    for rid, category, parsed in rows:
        st["packs"] += 1
        p = json.loads(parsed)
        np = p.get("name_parsed") or {}
        y1, y2 = _years(p)
        if not _is_pack(np.get("name") or "", y1, y2, _seasons(np)):
            with conn:
                conn.execute("UPDATE match_issues SET resolved_at = datetime('now') WHERE release_id = ? "
                             "AND problem = 'collection_pack' AND resolved_at IS NULL", (rid,))
            st["not_pack"] += 1
            continue
        try:
            tids, info = _link_pack(conn, rid, p, category, y1, y2)
        except LookupError as e:
            tids, info = None, {"error": str(e)}
        if tids:
            _resolve_pack_issue(conn, rid, info)
            st["linked"] += 1
            st["parts"] += len(tids)
            log(f"  pack {rid}: {len(tids)} films via {info.get('via')}")
        else:
            with conn:
                conn.execute("UPDATE match_issues SET candidates = ? WHERE release_id = ? AND problem = 'collection_pack' "
                             "AND resolved_at IS NULL", (json.dumps(info, ensure_ascii=False, default=str), rid))
            st["left"] += 1
            log(f"  pack {rid}: left — {info.get('reason') or info.get('error')}")
    return st


def _title_names(p: dict) -> list[str]:
    """Original first, then the other latin / native names, then Russian / Ukrainian (the search order)."""
    np = p.get("name_parsed") or {}
    out = [p.get("title_original"), np.get("title_original"), *(np.get("titles_other") or []),
           p.get("title_ru"), np.get("title_ru"), np.get("title_uk"), *(np.get("titles_ru_alt") or [])]
    out = [SUFFIX_3D_RE.sub("", t).strip() for t in out if t]
    # «Shrek 4-D : Shrek. The Ghost of Lord Farquaad» — a spaced colon joins two names of one work (the IMDb title is
    # the second part, the first is its aka): both parts are searched too, after the full names
    parts = [s.strip() for t in out if " : " in t for s in t.split(" : ")]
    out += [s for s in parts if len(s) >= 3]
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


def _agree_pick(res: dict, tr: dict, kind: str, y1: int | None, y2: int | None, seasons: list[int],
                exclude: frozenset = frozenset()) -> str | None:
    """IMDb and TMDB, each alone undecided, point at the same title: the only IMDb candidate with a fitting name a year off
    at most (score >= 0.7) and the only TMDB candidate with an exact name (>= 0.9) and the year within 1, the TMDB record
    carrying that IMDb id — two sources matched by different names («Время сиять / Waindeueop: deo mubi (2025)»: IMDb
    «Wind Up: The Movie» by the romanized aka, TMDB «Время сиять» by the Russian name — both tt43672139, 2026)."""
    def fits(start):
        # a later season under the show's own start year («Бэтмен / The Batman [S05] (2004)») — only when both agree
        return (_season_fit(kind, seasons, y1, y2, start)
                or (kind == "series" and min(seasons or [1]) >= 2 and y1 and start == y1 and (y2 or y1) == y1))
    ic = [c for c in res.get("candidates") or [] if c["tconst"] not in exclude and c["score"] >= 0.7 and fits(c["start_year"])]
    tc = [c for c in tr.get("candidates") or [] if (c.get("name_fit") or 0) >= 0.9 and (c.get("score") or 0) >= 0.7
          and (kind == "series" or not y1 or c.get("year_diff") is None or c["year_diff"] <= 1) and fits(c.get("year"))]
    if len(ic) == 1 and len(tc) == 1 and tc[0].get("imdb_id") == ic[0]["tconst"]:
        return ic[0]["tconst"]
    return None


def _dub_year_pick(tr: dict, names: list[str], kind: str, y1: int | None, seasons: list[int]) -> int | None:
    """A season-1 release whose tracker year is the dub's, years after the show began (the case _season_fit leaves for
    review), accepted only when nothing else is possible: the only TMDB series with a fitting name at all carries EVERY
    name of the release (the Russian dub title too: «Руфусы. Хранители добрых дел / The Roofus [S01] (2025)» = TMDB
    «Руфусы. Хранители добрых дел» 2023), began 1–12 years before, and the tracker's country (or director) agrees.
    «Команда малюток. Мир фантастических насекомых / Mooboo Cops [S01] (2024)» names only the base show — left."""
    if kind != "series" or not y1 or not seasons or min(seasons) != 1:
        return None
    named = [c for c in tr.get("candidates") or [] if (c.get("name_fit") or 0) >= 0.9]
    if len(named) != 1:
        return None
    c = named[0]
    notes = c.get("hint_notes") or []
    if not (c.get("verified") and c.get("year") and 1 <= y1 - c["year"] <= 12
            and set(names) <= set(c.get("titles_matched") or [])
            and ("country matches" in notes or "director matches" in notes)):
        return None
    return c["tmdb_id"]


def _director_pick(tr: dict, kind: str, y1: int | None) -> int | None:
    """Namesake films of the same year, decided by the tracker's director: of the TMDB films with an exact name and the
    exact year (every one checked in full), only one has him («Робин Гуд / Robin Hood (1991)», John Irvin — not the
    1991 namesake nor «Принц воров»)."""
    if kind != "movie" or not y1:
        return None
    exact = [c for c in tr.get("candidates") or [] if (c.get("name_fit") or 0) >= 0.9 and c.get("year_diff") == 0]
    if not exact or not all(c.get("verified") for c in exact):
        return None
    hit = [c for c in exact if "director matches" in (c.get("hint_notes") or [])]
    return hit[0]["tmdb_id"] if len(hit) == 1 else None


def _other_kind_pick(conn, names: list[str], hints: dict, category: str, kind: str, y1: int | None, y2: int | None,
                     seasons: list[int]) -> tuple[int, str] | None:
    """Animation / anime mix films and series and the release name may mislead the kind: nothing found as the guessed
    kind, no season written → the other kind, accepted only when TMDB matches it outright with an exact name and year:
    a film with the tracker's director («Психопаспорт 3: Первый инспектор [01-03 из 03] (2020)» — a film in three parts),
    a series with a single season and a single year («Последняя фантазия: Легенда кристаллов [OVA] (1994)»).
    → (tmdb_id, 'tmdb_as_movie' | 'tmdb_as_series')."""
    if category not in ("animation", "anime") or seasons or not y1:
        return None
    other = "tv" if kind == "movie" else "movie"
    tr = tmdb.search(conn, names, y1, other, hints=hints)
    b = tr.get("best") if tr["status"] == "matched" else None
    if not b or (b.get("name_fit") or 0) < 0.9 or b.get("year") != y1:
        return None
    if other == "movie":
        return (b["tmdb_id"], "tmdb_as_movie") if "director matches" in (b.get("hint_notes") or []) else None
    t = tmdb.tv_raw(conn, b["tmdb_id"]) or {}
    regular = [s for s in t.get("seasons") or [] if s.get("season_number")]
    return (b["tmdb_id"], "tmdb_as_series") if len(regular) == 1 and (y2 or y1) == y1 else None


_EPISODE_RANGE_RE = re.compile(r"^\s*\d{1,3}\s*-\s*\d{1,3}\b")
_SERIES_WORD_RE = re.compile(r"(?i)animated series|animation series|the series\b|мультсериал|анимационн\w* сери")


def _imdb_series_pick(conn, names: list[str], p: dict, category: str, kind: str, y1: int | None,
                      seasons: list[int]) -> str | None:
    """An animation / anime release guessed a film (no season written) that is a series by its own name: an episode
    range «[01-22 (22)]» or the words «Animated Series» («Звездный путь: Анимационные серии / Star Trek: The Animated
    Series [01-22 (22)] (1973)», «Человек-Паук / Spider-Man: The Animated Series (1994)»). Nothing was found as a film,
    so the IMDb datasets are asked for a series: accepted only when they match outright (one exact-name candidate) and
    the series began in the release's year. → tconst."""
    if category not in ("animation", "anime") or kind != "movie" or seasons or not y1:
        return None
    np = p.get("name_parsed") or {}
    if not (np.get("episodes") or _EPISODE_RANGE_RE.match(np.get("season_raw") or "")
            or any(_SERIES_WORD_RE.search(t) for t in names)):
        return None
    res = imdb.resolve(conn, names, y1, "series")
    m = res.get("match") if res.get("status") == "matched" else None
    if not m or m.get("start_year") != y1 or (m.get("score") or 0) < 0.9:
        return None
    b = _basics(conn, m["tconst"])
    return m["tconst"] if b is not None and b["title_type"] in SERIES_TYPES else None


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
    """Names -> ('imdb', tconst, how) | ('tmdb', tmdb_id, how) | (None, problem, candidates): IMDb datasets first, TMDB
    second; how is None for the ordinary picks, 'imdb_agree' / 'tmdb_dub_year' / 'tmdb_director' / 'tmdb_as_movie' / 'tmdb_as_series' for
    the last-resort rules."""
    names = _title_names(p)
    res = imdb.resolve(conn, names, y1, "series" if kind == "series" else "movie")
    cands = [c for c in res["candidates"] if c["tconst"] not in exclude]
    if (res["status"] == "matched" and res["match"]["tconst"] not in exclude
            # a series must fit the season too («Семь смертных грехов. Спешл [S01] (2016)» is not season 1 of the 2014 show)
            and _season_fit(kind, seasons, y1, y2, res["match"].get("start_year"))):
        return "imdb", res["match"]["tconst"], None
    pick = _imdb_pick(conn, res, p, category, kind, y1, y2, seasons, exclude) if cands else None
    if pick:
        return "imdb", pick["tconst"], None
    hints = {"directors": p.get("directors") or [], "countries": p.get("countries") or []}
    tr = tmdb.search(conn, names, y1, "tv" if kind == "series" else "movie", hints=hints)
    tpick = _tmdb_pick(conn, tr, p, category, kind, y1, y2, seasons) if tr["candidates"] else None
    if tpick:
        return "tmdb", tpick["tmdb_id"], None
    agreed = _agree_pick(res, tr, kind, y1, y2, seasons, exclude)
    if agreed:
        return "imdb", agreed, "imdb_agree"
    dub = _dub_year_pick(tr, names, kind, y1, seasons)
    if dub:
        return "tmdb", dub, "tmdb_dub_year"
    by_dir = _director_pick(tr, kind, y1)
    if by_dir:
        return "tmdb", by_dir, "tmdb_director"
    flip = _other_kind_pick(conn, names, hints, category, kind, y1, y2, seasons)
    if flip:
        return "tmdb", flip[0], flip[1]
    as_series = _imdb_series_pick(conn, names, p, category, kind, y1, seasons)
    if as_series and as_series not in exclude:
        return "imdb", as_series, "imdb_as_series"
    status ="ambiguous" if cands or any((c.get("name_fit") or 0) >= 0.9 for c in tr["candidates"]) else "not_found"
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


class SeasonYearMismatch(Exception):
    pass


def _check_season_years(conn, tids: list[int], y1: int | None, y2: int | None, tconst: str | None, tmdb_id: int | None, b):
    """A season found by name must have aired around the release's years: «Sword Art Online: Alicization - War of
    Underworld [S02] (2020)» counts the sub-series' own seasons — SAO season 2 aired in 2014. Allowed when the tracker
    wrote the show's own start year («Бэтмен / The Batman [S05] (2004)»). A season without a known year passes."""
    if not y1:
        return
    start = (tmdb._year((tmdb.tv_raw(conn, tmdb_id) or {}).get("first_air_date")) if tmdb_id
             else (b["start_year"] if b is not None else None))
    if start == y1 and (y2 or y1) == y1:
        return
    years = {tid: conn.execute("SELECT year FROM titles WHERE id = ?", (tid,)).fetchone()[0] for tid in tids}
    bad = {tid: y for tid, y in years.items() if y and not (y1 - 1 <= y <= (y2 or y1) + 1)}
    if bad:
        raise SeasonYearMismatch(bad)


_REDIRECTS: dict[str, str] | None = None


def imdb_redirects() -> dict[str, str]:
    """IMDb ids merged into another one ({old: canonical}), as the IMDb GraphQL cache recorded them (imdb_specs: status
    «redirect»). An old id on a tracker page made a duplicate title («Стук в хижине»: tt15679400 → tt8272064, 2026-10-25)."""
    global _REDIRECTS
    if _REDIRECTS is None:
        import glob
        import gzip
        from .sources import imdb_specs
        _REDIRECTS = {}
        for path in glob.glob(str(imdb_specs.CACHE_DIR / "tt*.json.gz")):
            try:
                r = json.loads(gzip.open(path).read())
            except (OSError, ValueError):
                continue
            if r.get("status") == "redirect" and r.get("canonical"):
                _REDIRECTS[r["tconst"]] = r["canonical"]
    return _REDIRECTS


def canonical_imdb(tconst: str | None) -> str | None:
    """A page IMDb id normalized (an extra leading zero dropped: tt03748172 → tt3748172) and followed through IMDb's
    redirects."""
    from .parse.rutor import norm_imdb_id
    tconst = norm_imdb_id(tconst)
    return imdb_redirects().get(tconst, tconst) if tconst else tconst


def merge_redirected_titles(conn, log=print) -> dict:
    """Titles whose IMDb id IMDb has merged into another (films and the seasons of a series): no canonical title yet →
    the id is replaced; a canonical title exists → the duplicate's release links move to it and the duplicate keeps its
    row with duplicate_of = the canonical title (not shown, not enriched; nothing is deleted from the data)."""
    out = {"renamed": 0, "merged": 0, "marked_duplicate": 0}

    def merge(dup: int, main: int, what: str):
        if conn.execute("SELECT 1 FROM release_titles WHERE title_id = ?", (dup,)).fetchone():
            conn.execute("INSERT OR IGNORE INTO release_titles (release_id, title_id, how) SELECT release_id, ?, "
                         "coalesce(how, '') || ' imdb_redirect' FROM release_titles WHERE title_id = ?", (main, dup))
            conn.execute("DELETE FROM release_titles WHERE title_id = ?", (dup,))
            conn.execute("UPDATE releases SET title_id = ? WHERE title_id = ?", (main, dup))
            out["merged"] += 1
            log(f"  title {dup} ({what}) merged into {main}")
        # the row stays, marked as the duplicate (enrich / posters skip it; the owner's marks copied over when the
        # real title has none) — triage 2026-10-25: merged rows kept raising «TMDB doesn't know this IMDb id»
        if conn.execute("UPDATE titles SET duplicate_of = ?, updated_at = datetime('now') WHERE id = ? AND duplicate_of IS NULL",
                        (main, dup)).rowcount:
            conn.execute("INSERT OR IGNORE INTO user_marks (title_id, mark, ordered_by, note, updated_at) "
                         "SELECT ?, mark, ordered_by, note, updated_at FROM user_marks WHERE title_id = ?", (main, dup))
            out["marked_duplicate"] += 1

    with conn:
        for old, new in imdb_redirects().items():
            dup = conn.execute("SELECT id FROM titles WHERE imdb_id = ? AND season_number IS NULL", (old,)).fetchone()
            if dup:
                main = conn.execute("SELECT id FROM titles WHERE imdb_id = ? AND season_number IS NULL", (new,)).fetchone()
                if not main:
                    conn.execute("UPDATE titles SET imdb_id = ? WHERE id = ?", (new, dup[0]))
                    out["renamed"] += 1
                    log(f"  title {dup[0]}: {old} → {new}")
                elif main[0] != dup[0]:
                    merge(dup[0], main[0], old)
            # seasons of a series made from the merged id («High Potential»: tt26748649 → tt22091076)
            for sid, season in conn.execute("SELECT id, season_number FROM titles WHERE series_imdb_id = ? "
                                            "AND season_number IS NOT NULL", (old,)).fetchall():
                main = conn.execute("SELECT id FROM titles WHERE series_imdb_id = ? AND season_number = ? AND id != ? "
                                    "AND duplicate_of IS NULL ORDER BY id LIMIT 1", (new, season, sid)).fetchone()
                if main:
                    merge(sid, main[0], f"{old} season {season}")
                else:
                    conn.execute("UPDATE titles SET series_imdb_id = ? WHERE id = ?", (new, sid))
                    if not conn.execute("SELECT 1 FROM titles WHERE series_key = ? AND season_number = ?",
                                        (f"imdb:{new}", season)).fetchone():
                        conn.execute("UPDATE titles SET series_key = ? WHERE id = ? AND series_key = ?",
                                     (f"imdb:{new}", sid, f"imdb:{old}"))
                    out["renamed"] += 1
                    log(f"  title {sid}: series {old} → {new}")
    return out


def match_releases(conn, retry_issues: bool = False) -> dict:
    """Link every parsed, not-yet-linked release to its title(s). A critical section: one at a time across processes
    (semaphore `match`, films/locks.py — owner, 2026-10-26: «понять критические области и не пытаться с ними работать
    параллельно»; sources and agents call it from their own runs)."""
    from . import locks
    with locks.lock("match", timeout=6 * 3600):
        return _match_releases(conn, retry_issues)


def _match_releases(conn, retry_issues: bool = False) -> dict:
    merged = merge_redirected_titles(conn, log=lambda *_: None)
    if retry_issues:
        # superseded, not deleted (nothing is deleted from the DB): the retry writes a fresh issue if it is still unmatched
        with conn:
            conn.execute("UPDATE match_issues SET resolved_at = datetime('now') WHERE resolved_at IS NULL AND release_id IS NOT NULL")
    packs = link_packs(conn)   # packs left earlier: retried (cached TMDB), no-longer-packs back to the normal matching
    from .tracker_only import reopen_in_scope
    reopened = reopen_in_scope(conn)   # out-of-scope closures whose rule no longer holds come back (an open issue)
    pending = conn.execute(
        """SELECT r.id, r.category, r.parsed FROM releases r
           WHERE r.parsed IS NOT NULL
             AND NOT EXISTS (SELECT 1 FROM release_titles t WHERE t.release_id = r.id)
             AND NOT EXISTS (SELECT 1 FROM match_issues m WHERE m.release_id = r.id AND m.resolved_at IS NULL)
             AND NOT EXISTS (SELECT 1 FROM match_issues m WHERE m.release_id = r.id AND m.resolved_at IS NOT NULL
                             AND json_valid(m.candidates) AND json_extract(m.candidates, '$.out_of_scope') IS NOT NULL
                             AND json_extract(m.candidates, '$.reopened_at') IS NULL)""").fetchall()
    stats = {"releases": len(pending), "linked": 0, "via_page_imdb": 0, "via_resolve": 0, "via_tmdb": 0,
             "via_agree": 0, "via_dub_year": 0, "via_other_kind": 0, "via_director": 0,
             "issues": {}, "season_assumed_1": 0, "imdb_redirects": merged, "packs": 0, "pack_parts": 0,
             "packs_retried": packs, "out_of_scope_reopened": reopened}
    for i, (rid, category, parsed) in enumerate(pending, 1):
        def issue(problem, candidates, rid=rid):
            with conn:
                _issue(conn, rid, problem, candidates)
            stats["issues"][problem] = stats["issues"].get(problem, 0) + 1

        match_one(conn, rid, category, json.loads(parsed), stats, issue)
        if i % 2000 == 0:
            print(f"  matched {i}/{len(pending)}", file=sys.stderr, flush=True)
    return stats


def match_one(conn, rid: int, category: str, p: dict, stats: dict, issue) -> list[int] | None:
    """Identify one parsed release and link it to its title(s) → the title ids, or None after `issue(problem,
    candidates)` (match_releases writes it to match_issues; the tracker-only re-identification passes a no-op)."""
    np = p.get("name_parsed") or {}
    y1, y2 = _years(p)
    tconst, how, tmdb_id = canonical_imdb(p.get("imdb_id")), "imdb_page", None
    seasons = _seasons(np)
    b = _page_record(conn, tconst) if tconst else None
    exclude, mismatch = frozenset(), None
    why = _page_id_distrust(b, np, seasons, y1, y2) if tconst else None
    if tconst and not why and _film_not_series(conn, p, category, b, seasons, y1, tconst):
        why = "series id for a film release (TMDB has the film)"
    if tconst and not why and _page_alternative(conn, tconst, b, p, category, y1, y2, seasons):
        why = "page id names another work: no release name among its IMDb names, another title found by name"
    if why:
        # the page links something else than the release (an episode, a podcast, a film for a season pack …) —
        # resolved by name instead, that id excluded (34 titles were made from such ids, fixed 2026-10-25)
        exclude, mismatch = frozenset({tconst}), {"page_imdb": tconst, "imdb_type": b["title_type"], "why": why}
        tconst, b = None, None
    elif b is not None and b["title_type"] == "unknown":
        b = None   # an older id TMDB does not know either: the page link is the only evidence, as before
    kind = _kind(p, category, b)

    if kind == "movie" and _is_pack(np.get("name") or "", y1, y2, seasons):
        # multi-film pack: the page links only the first film — linked to every film it holds (the page's list of
        # IMDb ids, else the TMDB collection); recorded as a resolved 'collection_pack' issue, an open one if not
        try:
            tids, info = _link_pack(conn, rid, p, category, y1, y2)
        except LookupError as e:
            tids, info = None, {"page_imdb": tconst, "years": [y1, y2], "error": str(e)}
        if not tids:
            issue("collection_pack", info)
            return None
        _resolve_pack_issue(conn, rid, info)
        stats["linked"] += 1
        stats["packs"] += 1
        stats["pack_parts"] += len(tids)
        return tids
    if tconst and b is not None and not _page_year_ok(conn, tconst, b, kind, p, y1, y2, seasons):
        # the page links another title (often an earlier part: «Коты Эрмитажа 2 (2026)» -> «Коты Эрмитажа» 2023):
        # resolved by name instead, that title excluded
        exclude = frozenset({tconst})
        mismatch = {"page_imdb": tconst, "imdb_year": b["start_year"], "release_years": [y1, y2]}
        tconst = None
    if not tconst:
        src, key, cands = _resolve(conn, p, category, kind, y1, y2, seasons, exclude)
        if src is None and mismatch and (mismatch.get("why") or "").endswith("for a season release"):
            # no series of that name either: the film the page names is the release («Бэтмен и Робин [01x01-15]
            # (1949)» — a serial IMDb and TMDB keep as one film)
            src, key, cands = "imdb", mismatch["page_imdb"], "imdb_page"
            kind = "movie"
        if src is None:
            issue("imdb_year_mismatch", dict(mismatch, candidates=cands)) if mismatch else issue(key, cands)
            return None
        if src == "imdb":
            tconst, how = key, cands or "imdb_resolve"   # on success the third item names a special rule
            b = _page_record(conn, tconst) if how == "imdb_page" else _basics(conn, tconst)
            kind = "movie" if how == "imdb_page" else _kind(p, category, b)
        else:
            tmdb_id, how = key, cands or "tmdb_search"
            kind = {"tmdb_as_movie": "movie", "tmdb_as_series": "series"}.get(how, kind)
    if kind == "series" and not seasons:
        # "[1-8 из 8]" style or a miniseries: no season number written -> season 1, counted — only when the year fits
        # season 1 («Интерны [101-153] (2012)», «Sword Art Online: Alicization - War of Underworld (2019)» are later
        # seasons of shows begun in 2010 / 2012: left for review, never put on season 1)
        start = (tmdb._year((tmdb.tv_raw(conn, tmdb_id) or {}).get("first_air_date")) if tmdb_id
                 else (b["start_year"] if b is not None else None))
        if not _season_fit("series", [1], y1, y2, start) or (y1 and y2 and y2 - y1 >= 2):
            # … nor for a span of three years and more: the whole show («Аватар: Легенда об Аанге (2005-2008)»)
            issue("series_no_season", {"imdb": tconst, "tmdb_id": tmdb_id, "series_start": start,
                                       "release_years": [y1, y2], "how": how})
            return None
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
            if kind == "series" and how not in ("imdb_page", "tmdb_dub_year"):   # the dub rule checked its years
                _check_season_years(conn, tids, y1, y2, tconst, tmdb_id, b)
            for tid in tids:
                conn.execute("INSERT OR IGNORE INTO release_titles (release_id, title_id, how) VALUES (?, ?, ?)",
                             (rid, tid, how))
                # a title made earlier out of tracker scope (watched / bought / TMDB) now has a tracker release
                conn.execute("UPDATE titles SET tracker_category = ? WHERE id = ? AND tracker_category IS NULL",
                             (category, tid))
    except SeasonYearMismatch as e:
        issue("season_year_mismatch", {"imdb": tconst, "tmdb_id": tmdb_id, "how": how, "seasons": seasons,
                                       "release_years": [y1, y2], "season_years": e.args[0]})
        return None
    except (LookupError, sqlite3.IntegrityError) as e:
        issue("tmdb_no_title", {"tmdb_id": tmdb_id, "kind": kind, "seasons": seasons, "error": str(e)})
        return None
    stats["linked"] += 1
    stats[{"imdb_page": "via_page_imdb", "imdb_resolve": "via_resolve", "tmdb_search": "via_tmdb",
           "imdb_agree": "via_agree", "tmdb_dub_year": "via_dub_year", "tmdb_as_movie": "via_other_kind", "tmdb_director": "via_director",
           "tmdb_as_series": "via_other_kind", "imdb_as_series": "via_other_kind"}[how]] += 1
    return tids


def _names_of_title(conn, tid: int) -> set[str]:
    """Normalized names of a title: its own columns and, for an IMDb id, the dataset's primary / original / aka titles."""
    t = conn.execute("SELECT title_original, title_latin, title_ru, title_uk, imdb_id FROM titles WHERE id = ?", (tid,)).fetchone()
    out = {imdb.norm(n) for n in tuple(t)[:4] if n}
    if t[4]:
        out |= _imdb_names(conn, t[4], _basics(conn, t[4]))
    return out - {""}


def recheck_year_mismatch(conn, dry: bool = False, log=print) -> dict:
    """Open enrich issues 'tmdb_year_mismatch' (TMDB's year of a film is > 1 year off its releases') decided by data:
      * the link is right — a release name equals the title's name (own, IMDb primary / original / aka: the tracker
        wrote the Russian premiere or production year: «Вертикаль / Vertical (2024)» of 2021), or the IMDb datasets'
        record of the id agrees with the release's year (TMDB maps the id to a wrong entry: «Over the Garden Wall: 10th
        Anniversary Tribute» 2024, TMDB «2016»);
      * otherwise the release is resolved by its names again with that id excluded (_resolve); a different title found
        → the release is linked to it (how '<rule> year_recheck') and the old link kept as 'wrong_link: year mismatch
        → <ids>' («Кровавая вечеринка 2 (1987)» was on the 1982 film, «Прошлой ночью в Сохо» on another film);
      * a pack of the universe («Вселенная «Заклятия» (2013-2025)») goes to the pack linker.
    The issue is closed with the verdict as the note; undecided ones stay open. Nothing is deleted."""
    from .tracker_only import with_note
    st = {"issues": 0, "link_right": 0, "relinked": 0, "packs": 0, "left": 0, "details": []}
    rows = conn.execute("SELECT id, candidates FROM match_issues WHERE problem = 'tmdb_year_mismatch' AND resolved_at IS NULL "
                        "AND json_valid(candidates)").fetchall()
    for iid, cands in rows:
        d = json.loads(cands)
        tid = d.get("title_id")
        if not tid:
            continue
        st["issues"] += 1
        tnames = _names_of_title(conn, tid)
        t_imdb, t_year = conn.execute("SELECT imdb_id, year FROM titles WHERE id = ?", (tid,)).fetchone()
        b = _basics(conn, t_imdb) if t_imdb else None
        verdicts = []
        for rid, category, parsed, how in conn.execute(
                "SELECT r.id, r.category, r.parsed, x.how FROM release_titles x JOIN releases r ON r.id = x.release_id "
                "WHERE x.title_id = ? AND coalesce(x.how, '') NOT LIKE 'wrong_link%' AND r.parsed IS NOT NULL", (tid,)).fetchall():
            p = json.loads(parsed)
            y1, y2 = _years(p)
            names = {imdb.norm(n) for n in _title_names(p)} - {""}
            same_name = bool(names & tnames)
            if (b is not None and b["start_year"] and y1 and abs(b["start_year"] - y1) <= 1) or (
                    same_name and (not y1 or not t_year or abs(t_year - y1) <= 3)):
                verdicts.append(("right", rid, None))
                continue
            # a bigger gap with the same name may be a remake («Полночное солнце / Midnight Sun (2025)» is the Korean
            # film, not the 2006 Japanese one): resolved by name below; nothing else found → the link stays
            np = p.get("name_parsed") or {}
            if _is_pack(np.get("name") or "", y1, y2, _seasons(np)):
                tids, info = (None, {}) if dry else _link_pack(conn, rid, p, category, y1, y2)
                verdicts.append(("pack", rid, tids))
                if tids and not dry:
                    _resolve_pack_issue(conn, rid, info)
                continue
            src, key, extra = _resolve(conn, p, category, _kind(p, category, None), y1, y2, _seasons(np),
                                       frozenset({t_imdb} if t_imdb else ()))
            new = None
            if src == "imdb":
                bb = _basics(conn, key)
                if bb is not None and bb["title_type"] in MOVIE_TYPES and not dry:
                    with conn:
                        new = _movie_title(conn, key, bb, p, category)
            elif src == "tmdb" and not dry:
                with conn:
                    new = _tmdb_movie_title(conn, key)
            if src and dry:
                new = -1
            if new and new != tid:
                if not dry:
                    with conn:
                        conn.execute("INSERT OR IGNORE INTO release_titles (release_id, title_id, how) VALUES (?, ?, ?)",
                                     (rid, new, f"{extra or ('imdb_resolve' if src == 'imdb' else 'tmdb_search')} year_recheck"))
                        conn.execute("UPDATE release_titles SET how = ? WHERE release_id = ? AND title_id = ?",
                                     (f"wrong_link: year mismatch → {new} (было {how})", rid, tid))
                        conn.execute("UPDATE releases SET title_id = ? WHERE id = ? AND title_id = ?", (new, rid, tid))
                verdicts.append(("relinked", rid, new))
            else:
                verdicts.append(("right" if same_name else "left", rid, None))
        kinds = {v[0] for v in verdicts}
        st["details"].append({"title_id": tid, "verdicts": verdicts})
        if not verdicts or "left" in kinds or ("pack" in kinds and not all(v[2] for v in verdicts if v[0] == "pack")):
            st["left"] += 1
            continue
        for k in ("relinked", "pack"):
            st[k if k != "pack" else "packs"] += sum(1 for v in verdicts if v[0] == k)
        if kinds == {"right"}:
            st["link_right"] += 1
        note = ("год TMDB расходится с раздачей, но связь верна: название раздачи совпадает с названием тайтла / IMDb "
                "(трекер пишет год российской премьеры или производства)" if kinds == {"right"} else
                "раздачи с чужим тайтлом перепривязаны по названию (старая связь сохранена как wrong_link), пачки — к фильмам пачки")
        if not dry:
            with conn:
                conn.execute("UPDATE match_issues SET resolved_at = datetime('now'), candidates = ? WHERE id = ?",
                             (with_note(cands, resolved_rule="year_recheck", resolved_note=note,
                                        verdicts=[list(v) for v in verdicts]), iid))
        log(f"  year mismatch title {tid}: {sorted(kinds)}")
    return st


def recheck_page_links(conn, title_ids: list[int] | None = None, dry: bool = False, log=print) -> dict:
    """Releases linked by their page's IMDb id (how 'imdb_page') whose names the id does not know (_page_names_disjoint)
    and that resolve by name to another title: matched again (match_one now distrusts such a page id) and the old link
    kept as 'wrong_link: page id names another work → <ids>'. title_ids limits the check (None = every page link)."""
    q = ("SELECT x.release_id, x.title_id, r.category, r.parsed, r.name FROM release_titles x JOIN releases r ON r.id = x.release_id "
         "WHERE x.how = 'imdb_page' AND r.parsed IS NOT NULL")
    args: list = []
    if title_ids:
        q += f" AND x.title_id IN ({','.join('?' * len(title_ids))})"
        args = list(title_ids)
    st = {"checked": 0, "disjoint": 0, "relinked": [], "kept": 0}
    by_rel: dict[int, list] = {}
    for rid, tid, category, parsed, name in conn.execute(q, args).fetchall():
        by_rel.setdefault(rid, [category, parsed, name, []])[3].append(tid)
    for rid, (category, parsed, name, tids) in by_rel.items():
        st["checked"] += 1
        p = json.loads(parsed)
        tconst = canonical_imdb(p.get("imdb_id"))
        if not tconst:
            continue
        b = _page_record(conn, tconst)
        y1, y2 = _years(p)
        np = p.get("name_parsed") or {}
        seasons = _seasons(np)
        kind = _kind(p, category, b)
        stale_year = (b is not None and b["title_type"] != "unknown" and kind == "movie"
                      and not _page_year_ok(conn, tconst, b, kind, p, y1, y2, seasons))   # linked by older rules
        if not stale_year and not _page_names_disjoint(conn, tconst, b, p):
            continue
        st["disjoint"] += 1
        if not (_page_alternative(conn, tconst, b, p, category, y1, y2, seasons) or
                (stale_year and _resolve(conn, p, category, kind, y1, y2, seasons, frozenset({tconst}))[0])):
            st["kept"] += 1
            continue
        if dry:
            st["relinked"].append({"release_id": rid, "name": name[:90], "from": tids})
            continue
        mark = f"wrong_link: page id names another work ({tconst})"
        with conn:
            conn.execute(f"UPDATE release_titles SET how = ? WHERE release_id = ? AND how = 'imdb_page'", (mark, rid))
        from .tracker_only import _Counter
        new = match_one(conn, rid, category, p, _Counter(issues={}), lambda *_: None)
        new = [t for t in new or [] if t not in tids]
        with conn:
            if new:
                conn.execute("UPDATE release_titles SET how = ? WHERE release_id = ? AND how = ?",
                             (f"{mark} → {','.join(map(str, new))}", rid, mark))
                conn.execute("UPDATE releases SET title_id = ? WHERE id = ?", (new[0], rid))
                st["relinked"].append({"release_id": rid, "name": name[:90], "from": tids, "to": new})
                log(f"  relinked {rid}: {tids} → {new} — {name[:80]}")
            else:   # nothing better after all: the page link stays
                conn.execute("UPDATE release_titles SET how = 'imdb_page' WHERE release_id = ? AND how = ?", (rid, mark))
                st["kept"] += 1
    return st


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
