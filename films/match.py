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
import unicodedata

from . import db
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


def _page_text(conn, rid: int) -> str:
    """The release's name and raw page as plain normalized text (release_raw, no network)."""
    import zlib
    try:
        row = conn.execute("SELECT r.name, w.body FROM releases r LEFT JOIN release_raw w ON w.release_id = r.id "
                           "WHERE r.id = ?", (rid,)).fetchone()
    except sqlite3.OperationalError:   # a bare test DB without the tables
        return ""
    if not row:
        return ""
    body = zlib.decompress(row[1]).decode("utf-8", "replace") if row[1] else ""
    return f" {imdb.norm(re.sub(r'<[^>]+>', ' ', (row[0] or '') + ' ' + body))} "


def _named_on_page(conn, x: dict, text: str) -> bool:
    """A franchise film is in the pack only when the page names it — its IMDb id or a name of its own (review round 14:
    «Супермен (Антология) 1978-2006» took ~40 DC films of the span, «Люди Икс (Коллекция)» «Веном 3» and the MCU)."""
    if x.get("imdb_id") and x["imdb_id"] in text:
        return True
    names = {x.get("title_original"), x.get("title")}
    try:
        names |= {n for row in conn.execute("SELECT title_ru, title_original, title_latin FROM titles WHERE tmdb_id = ? "
                                            "AND kind = 'movie'", (x.get("tmdb_id"),)) for n in row}
    except sqlite3.OperationalError:   # a bare test DB
        pass
    return any(len(k) >= 3 and f" {k} " in text for k in (imdb.norm(n) for n in names if n))


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
    parts = {cid: list(_collection_parts(conn, cid)) for cid in cids}   # a copy: the extras below never leak into it
    text = None
    for cid in cids:
        # the collection lacks a film of the span («Джон Уик: Коллекция (2014-2025)»: TMDB's John Wick collection ends
        # in 2023, «Балерина» 2025 is in the franchise only) — the films of the owner's franchise built on it are added,
        # each only when the page names it (round 14: a franchise is a whole universe)
        known = {x.get("tmdb_id") for x in parts[cid]}
        extra = [x for x in _franchise_films(conn, cid) if x["tmdb_id"] not in known]
        if extra:
            text = _page_text(conn, rid) if text is None else text
            parts[cid] += [x for x in extra if _named_on_page(conn, x, text)]
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


def recheck_pack_links(conn, dry: bool = False, log=print) -> dict:
    """Packs linked earlier, their parts found again (_pack_parts: franchise films only when the page names them —
    review round 14). A film no longer among the parts → 'wrong_link: pack recheck'; a new part linked. A pack whose parts
    are not found now is left as it is (nothing is unlinked on a doubt)."""
    st = {"packs": 0, "unlinked": 0, "linked": 0, "kept": 0}
    rids = [r[0] for r in conn.execute("SELECT DISTINCT release_id FROM release_titles WHERE how = 'collection_pack'")]
    for rid in rids:
        row = conn.execute("SELECT parsed FROM releases WHERE id = ?", (rid,)).fetchone()
        if not row or not row[0]:
            continue
        st["packs"] += 1
        p = json.loads(row[0])
        y1, y2 = _years(p)
        parts, _ = _pack_parts(conn, rid, p, y1, y2)
        if not parts:
            # no parts found now: left as it is — a page without a film list cannot tell («Люди Икс: The Complete
            # Collection 2000-2024» names no film, its trivia mentions «Человек-паук» and «Халк»; Logan and Deadpool are in)
            st["kept"] += 1
            continue
        want = set()
        for x in parts:
            t = conn.execute("SELECT id FROM titles WHERE kind = 'movie' AND duplicate_of IS NULL AND "
                             "(tmdb_id = ? OR (imdb_id = ? AND imdb_id IS NOT NULL)) ORDER BY tmdb_id IS NULL",
                             (x.get("tmdb_id"), x.get("imdb_id"))).fetchone()
            if t is None:
                want = None   # a part not in the DB yet: the normal match makes it; nothing removed now
                break
            want.add(t[0])
        if want is None:
            st["kept"] += 1
            continue
        have = {r[0] for r in conn.execute("SELECT title_id FROM release_titles WHERE release_id = ? AND how = 'collection_pack'",
                                           (rid,))}
        gone, new = have - want, want - have
        if gone or new:
            log(f"  pack {rid}: −{len(gone)} +{len(new)}")
        if dry:
            st["unlinked"] += len(gone)
            st["linked"] += len(new)
            continue
        with conn:
            for tid in gone:
                conn.execute("UPDATE release_titles SET how = 'wrong_link: pack recheck — not on the page (было collection_pack)' "
                             "WHERE release_id = ? AND title_id = ?", (rid, tid))
            for tid in new:
                _relink(conn, rid, tid, "collection_pack")
            if gone and conn.execute("SELECT title_id FROM releases WHERE id = ?", (rid,)).fetchone()[0] in gone:
                conn.execute("UPDATE releases SET title_id = ? WHERE id = ?", (min(want), rid))
        st["unlinked"] += len(gone)
        st["linked"] += len(new)
    return st


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
    # the rating at once from the loaded dataset: a title made by a recheck after `ratings imdb` had none, and the
    # «IMDb < 5» filter could not hide it (review round 12)
    conn.execute("""UPDATE titles SET imdb_rating = (SELECT rating FROM imdb_ratings r WHERE r.tconst = titles.imdb_id),
                                      imdb_votes = (SELECT votes FROM imdb_ratings r WHERE r.tconst = titles.imdb_id)
                    WHERE id = ?""", (cur.lastrowid,))
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


def _shift_seasons(conn, series_imdb: str | None, seasons: list[int], y1: int | None, y2: int | None,
                   yrs: dict[int, int] | None = None, names: list[str] | None = None, eps: int | None = None,
                   counts: dict[int, int] | None = None, need_eps: bool = False) -> list[int] | None:
    """A sub-series numbering its own seasons on the parent's id: «Мыслить как преступник: Эволюция (2 сезон) (2024)» —
    the page links Criminal Minds, whose season 2 aired in 2006; TMDB counts «Evolution» on as seasons 16+ → season 17
    (2024); an anthology's parts («Монстр: История Эда Гейна [S01] (2025)» → «Монстр» S3). A one-season release only (a
    pack's years are often off: «Звёздные врата: ЗВ-1 (1999-2005) Сезон 1 - 4»); the season named aired 3+ years away from
    it — or 2, when the release names the part by its own name (a dub's later year is common: «Юные Титаны, вперед! (4
    сезон) / 2018», «Этим летом я стала красивой / 2 сезон … [2025]» keep the series' name) —, and exactly one season of the series aired in the
    release's first year: shifted to it (review 2026-10-28). A year apart or two is enough when the episodes say so: the
    release holds all episodes 1–N (`eps`), the season of its year has N and the season named has not (review round 14:
    «Клинок, рассекающий демонов: Тренировка столпов (ТВ-4) [8 из 8] 2024» — TMDB splits ТВ-2 in two, so it is S5 of
    2024 with 8 episodes, not S4 «Деревня кузнецов» of 2023 with 11)."""
    if not (series_imdb and len(seasons) == 1 and y1) or (y2 and y2 != y1):
        return None
    if yrs is None:
        yrs = dict(conn.execute("SELECT season_number, year FROM titles WHERE series_imdb_id = ? AND season_number > 0 "
                                "AND duplicate_of IS NULL AND year IS NOT NULL", (series_imdb,)).fetchall())
    if counts is None:
        counts = dict(conn.execute("SELECT season_number, episodes_total FROM titles WHERE series_imdb_id = ? "
                                   "AND season_number > 0 AND duplicate_of IS NULL AND episodes_total IS NOT NULL",
                                   (series_imdb,)).fetchall())
    s = seasons[0]
    first = [k for k, y in yrs.items() if y == y1]
    if s not in yrs or len(first) != 1 or first[0] <= s:
        return None
    by_eps = bool(eps) and first[0] - s <= 2 and counts.get(first[0]) == eps and counts.get(s) not in (None, eps)
    if by_eps:
        # the season named must have ended before the release's year: «Звёздный путь: Вояджер (5 сезон: 1-26 серии из 26)
        # / 1999» — S5 ran 1998-10 … 1999-05 (TMDB counts 25), S6 of 1999 has 26: not a reason to move (round 14 check)
        row = conn.execute("SELECT episode_dates FROM titles WHERE season_number = ? AND duplicate_of IS NULL AND "
                           "(series_imdb_id = ? OR 'tmdb:' || series_tmdb_id = ?) AND episode_dates IS NOT NULL",
                           (s, series_imdb, series_imdb)).fetchone()
        try:
            last = max(json.loads(row[0])) if row else None
        except (TypeError, ValueError):
            last = None
        by_eps = bool(last) and int(str(last)[:4]) < y1
    if need_eps and not by_eps:
        return None
    if abs(yrs[s] - y1) < 2 and not (abs(yrs[s] - y1) == 1 and by_eps):
        return None
    if abs(yrs[s] - y1) == 2 and not (by_eps or _names_part(conn, series_imdb, names or [])):
        return None
    return [first[0]]


def _all_episodes(np: dict) -> int | None:
    """«1-8» / «01-08» in the parsed name — the release holds episodes 1..N: N; anything else (a part, one episode): None."""
    e = np.get("episodes")
    if isinstance(e, (list, tuple)):   # [1, 8] (nnmclub / kinozal) or «1-8» (rutracker / rutor)
        return int(e[1]) if len(e) >= 2 and str(e[0]).isdigit() and int(e[0]) == 1 and str(e[1]).isdigit() else None
    m = re.fullmatch(r"0*1\s*-\s*0*(\d{1,4})", str(e or "").strip())
    return int(m.group(1)) if m else None


def _names_part(conn, series_imdb: str, names: list[str]) -> bool:
    """The release names a part of the series under its own name, not the series' («Монстры: История братьев Менендес /
    Monsters: The Lyle and Erik Menendez Story» of «Монстр»): no release name equals a name of the series' rows."""
    own = {imdb.norm(n) for row in conn.execute("SELECT title_ru, title_original, title_latin, title_uk FROM titles "
                                                "WHERE series_imdb_id = ?", (series_imdb,)) for n in row if n} - {""}
    rel = {imdb.norm(n) for n in names if n} - {""}
    return bool(own and rel) and not (own & rel)


def recheck_series_links(conn, dry: bool = False, log=print) -> dict:
    """Season links of earlier runs checked again by the release's own data (review 2026-10-28):
      * the page's IMDb id is a trusted series id other than the linked series (kinozal «Док / Doc (2025)» pages link the
        US «Doc» tt26748321, the name had found the Italian «Doc – Nelle tue mani») → that series' seasons;
      * the seasons named aired years away from the release and the series has one season of its year (_shift_seasons);
      * the name writes its season in words the parser left unread («(Сезон: 2 / Серии: …)», «(сезон 2, серии …)» —
        review 2026-10-28: such releases sat on season 1; tracker_only.name_seasons) — a missing season is made from TMDB.
    The old link stays as 'wrong_link: season recheck → <ids>'."""
    from .tracker_only import name_seasons
    by_rel: dict[int, list] = {}
    for rid, tid, how, cat, parsed, simdb, s, name in conn.execute(
            """SELECT x.release_id, x.title_id, x.how, r.category, r.parsed, t.series_imdb_id, t.season_number, r.name
               FROM release_titles x JOIN releases r ON r.id = x.release_id JOIN titles t ON t.id = x.title_id
               WHERE t.kind = 'series' AND t.season_number IS NOT NULL AND r.parsed IS NOT NULL
                 AND coalesce(x.how, '') NOT LIKE 'wrong_link%'""").fetchall():
        by_rel.setdefault(rid, [cat, parsed, name, []])[3].append((tid, how, simdb, s))
    st = {"checked": len(by_rel), "relinked": [], "seasons_made": 0}
    known: dict[str, dict[int, tuple[int, int | None]]] = {}   # series → {season: (title id, year)}, one scan
    tv_of: dict[str, int] = {}
    in_tmdb: dict[str, dict[int, int | None]] = {}             # series → {season TMDB has: year}
    for tid, simdb, s, y, tv, tm in conn.execute(
            "SELECT id, series_imdb_id, season_number, year, series_tmdb_id, tmdb_id FROM titles "
            "WHERE series_imdb_id IS NOT NULL AND season_number IS NOT NULL AND duplicate_of IS NULL "
            "ORDER BY tracker_only, id"):
        known.setdefault(simdb, {}).setdefault(s, (tid, y))
        if tv:
            tv_of.setdefault(simdb, tv)
        if tm and s > 0:
            in_tmdb.setdefault(simdb, {})[s] = y
    # the page-id checks of a link that kept its series (review 2026-10-28: 58 releases re-resolved by name on every match,
    # 44 s under the match lock): remembered in sync_state «season_recheck», asked again only for a new link
    row = conn.execute("SELECT details FROM sync_state WHERE source = 'season_recheck'").fetchone()
    try:
        kept = set(json.loads(row[0]) if row and row[0] else [])
    except ValueError:
        kept = set()
    kept_before = set(kept)
    # kinozal «Клиника (1 (10) сезон: …)» writes both numberings: the series' offset between the trackers' count and TMDB's
    offsets: dict[str, set[int]] = {}
    for cat, parsed, rname, links in by_rel.values():
        m = re.search(r"(?i)(?<![\d.])(\d{1,2})\s*\((\d{1,2})\)\s*сезон", rname or "")
        if m and len({link[2] for link in links}) == 1 and links[0][2]:
            offsets.setdefault(links[0][2], set()).add(int(m.group(2)) - int(m.group(1)))
    for rid, (cat, parsed, rname, links) in by_rel.items():
        series = {link[2] for link in links}
        if len(series) != 1 or None in series:
            continue
        series = series.pop()
        p = json.loads(parsed)
        np = p.get("name_parsed") or {}
        seasons = _seasons(np)
        y1, y2 = _years(p)
        named = not seasons
        if named:
            seasons = [s for s in name_seasons(rname) if 0 < s <= 60]
        # a release on the specials (season 0) that names no special: «Атака титанов / Attack on Titan (2013) WEB-DLRip
        # [AV1/2160p]» came over with a duplicate's links onto the OVA row (review 2026-10-28) → season 1 of its year
        to_s1 = (not seasons and all(link[3] == 0 for link in links)
                 and not re.search(r"(?i)\b(?:OVA|OAD|ONA)\b|special|спецвыпуск|спешл|спэшл|рождеств|christmas", rname or ""))
        if to_s1:
            seasons = [1]
        if not seasons:
            continue
        target, tconst = series, canonical_imdb(p.get("imdb_id"))
        # only a series the DB already has (no TMDB / IMDb lookups for the rest — this runs on every match)
        key = f"{rid}|{tconst}|{series}"
        if tconst and tconst != series and tconst in known and all(link[1] != "imdb_page" for link in links) \
                and key not in kept:
            b = _page_record(conn, tconst)
            if (b is not None and b["title_type"] in ("tvSeries", "tvMiniSeries")
                    and not _page_id_distrust(b, np, seasons, y1, y2)
                    and not _page_alternative(conn, tconst, b, p, cat, y1, y2, seasons)
                    and _page_year_ok(conn, tconst, b, "series", p, y1, y2, seasons)):
                target = tconst
            else:
                kept.add(key)
        have = known.get(target) or {}
        if to_s1 and not (1 in have and y1 and have[1][1] and abs(have[1][1] - y1) <= 1):
            continue
        shifted = _shift_seasons(conn, target, seasons, y1, y2,
                                 {s: y for s, (_, y) in have.items() if s > 0 and y is not None},
                                 _title_names(p) + [np.get("name") or ""], eps=_all_episodes(np))
        tm = in_tmdb.get(target) or {}
        if not shifted and len(seasons) == 1 and tm and seasons[0] > max(tm) and y1:
            # a revival TMDB keeps as its own series, the trackers counting from the show's start (review 2026-10-28:
            # «Клиника / Scrubs [S10] (2026)» = the 2026 «Scrubs» season 1): the one TMDB season of the release's year
            same = [k for k, y in tm.items() if y == y1]
            off = offsets.get(target) or set()
            if len(same) == 1:
                shifted = same
            elif len(off) == 1 and seasons[0] - next(iter(off)) in tm:   # two TMDB seasons that year: the names' offset
                shifted = [seasons[0] - next(iter(off))]
        want = shifted or seasons
        # a link carried over from a duplicate row (tracker_only.move_duplicate_links, «via-duplicate») kept no season
        carried = any("via-duplicate" in (link[1] or "") for link in links)
        # linked to seasons TMDB does not have beyond the ones the release names — an old misreading of its name (review
        # round 10: nnmclub «(1-5 сезоны: 1-76 серии из 76)» read as 76 seasons, X-Men 1992 S6–S76)
        linked = {link[3] for link in links}
        stale = set(want) < linked and not (linked - set(want)) & set(tm)
        if target == series and not shifted and not stale and not ((named or carried) and set(want) != linked):
            continue
        missing = [s for s in want if s not in have]
        if missing and named and target == series and tv_of.get(target) and not dry:
            for s in missing:      # «Верующие» S2: the season row does not exist yet
                tid = ensure_season(conn, tv_of[target], s)
                if tid:
                    have = known.setdefault(target, {})
                    have.setdefault(s, (tid, None))
                    st["seasons_made"] += 1
        new = [have[s][0] for s in want if s in have]
        if len(new) != len(want):
            new = []
        old = [link[0] for link in links]
        if not new or set(new) == set(old):
            continue
        st["relinked"].append({"release_id": rid, "from": old, "to": new})
        log(f"  season recheck {rid}: {old} → {new}")
        if dry:
            continue
        with conn:
            for tid, how, *_ in links:
                if tid not in new:
                    conn.execute("UPDATE release_titles SET how = ? WHERE release_id = ? AND title_id = ?",
                                 (f"wrong_link: season recheck → {','.join(map(str, new))} (было {how})", rid, tid))
            for tid in new:
                _relink(conn, rid, tid, "season_recheck")
            conn.execute("UPDATE releases SET title_id = ? WHERE id = ?", (new[0], rid))
    if kept != kept_before and not dry:
        with conn:
            conn.execute("INSERT OR REPLACE INTO sync_state (source, last_run_at, details) VALUES ('season_recheck', "
                         "datetime('now'), ?)", (json.dumps(sorted(kept)),))
    return st


_TWIN_HOW = ("part_twin", "tracker_twin", "imdb_tmdb_twin", "film_not_series", "ona_twin", "season_recheck", "page_recheck",
             "series_pack_twin", "release_recheck", "season_name_twin")


def recheck_movie_links(conn, dry: bool = False, log=print) -> dict:
    """A film linked by its name (imdb_resolve / exact_name_year / tmdb_search …) whose release page names another IMDb film
    — the page fetched after the first match (review round 10: «Взаперти / Shut In (2022)» on a Russian 2023 film, «Братья /
    Frères» on «Brothers», «Резкий / Reckless» on a 19-vote namesake): the page wins when its record is a film, one of the
    release's non-Russian names is among its IMDb names, its year is the release's ±1 and its length the release's ±15 min
    (MediaInfo). Kept: a series record («Invincible», «Constantine: City of Demons»), another year («Midnight Sun» 2006 for
    2025), a title without an IMDb id unless its own length is 20+ min off the release's («Некрополис»: a 9-min TMDB short,
    the release 127 min), and a release whose Russian name is the linked title's while the page's title in the DB has
    another («Teacher's Pet»: two films, trackers' pages crossed). The old link → 'wrong_link: page id recheck → <id>', the
    new one 'page_recheck'."""
    st = {"checked": 0, "relinked": []}
    rows = conn.execute(
        """SELECT r.id, r.parsed, r.category, x.how, t.id, t.imdb_id, t.title_ru, t.runtime_min FROM release_titles x
             JOIN releases r ON r.id = x.release_id JOIN titles t ON t.id = x.title_id
           WHERE t.kind = 'movie' AND json_extract(r.parsed, '$.imdb_id') IS NOT NULL
             AND coalesce(x.how, '') NOT LIKE 'wrong_link%' AND coalesce(x.how, '') NOT LIKE 'imdb_page%'
             AND coalesce(x.how, '') NOT LIKE 'manual%' AND coalesce(x.how, '') NOT LIKE 'collection_pack%'
             AND (SELECT count(*) FROM release_titles y WHERE y.release_id = r.id
                  AND coalesce(y.how, '') NOT LIKE 'wrong_link%') = 1""").fetchall()
    for rid, parsed, category, how, tid, have, ru, runtime in rows:
        if (how or "").startswith(_TWIN_HOW):
            continue
        p = json.loads(parsed)
        tc = canonical_imdb(p.get("imdb_id"))
        if not tc or tc == have:
            continue
        if not have and not (runtime and p.get("runtime_min") and abs(runtime - p["runtime_min"]) > 20):
            continue
        st["checked"] += 1
        b = _basics(conn, tc)
        y1, _ = _years(p)
        if b is None or b["title_type"] not in ("movie", "video", "tvMovie") or not y1 or not b["start_year"] \
                or abs(b["start_year"] - y1) > 1:
            continue
        row = conn.execute("SELECT id, title_ru, duplicate_of FROM titles WHERE imdb_id = ? AND season_number IS NULL",
                           (tc,)).fetchone()
        mine = imdb.norm(p.get("title_ru") or (p.get("name_parsed") or {}).get("title_ru"))
        latin = [n for n in _title_names(p) if not re.search(r"[а-яёіїєґ]", n, re.I)]
        parts = latin + [s for n in latin for s in re.split(r"[()]", n)]   # «(Pri)sons (Carnage)», «Hu Die Da Shia (Anaconda)»
        if not _imdb_names(conn, tc, b) & ({imdb.norm(n) for n in parts} - {""}):
            # no Latin name in common — still the page's film when the release's Russian name is that film's in the DB
            # and MediaInfo fits it (±5 min) and not the linked one (10+ min off): review round 12, «Женщина в чёрном.
            # Невеста дьявола / Pengantin Iblis» 90 min — the page's «Pengantin Setan» 91, not «Pengantin Iblis» 105
            mi = p.get("runtime_min")
            if not (row and mine and imdb.norm(row[1]) == mine and mi and b["runtime_min"] and runtime
                    and abs(mi - b["runtime_min"]) <= 5 and abs(mi - runtime) >= 10):
                continue
        # MediaInfo's length decides between namesakes («Одиссея» 2026: the release runs 86 min — Marcel Walz's film the
        # page names, not Nolan's 173 min)
        if p.get("runtime_min") and b["runtime_min"] and abs(p["runtime_min"] - b["runtime_min"]) > 15:
            continue
        if mine and mine == imdb.norm(ru) and row and imdb.norm(row[1]) != mine:
            continue   # two films, both in the DB, the release named as the linked one («Teacher's Pet»)
        st["relinked"].append({"release_id": rid, "from": tid, "to": row[2] or row[0] if row else tc})
        log(f"  page recheck {rid}: {tid} → {st['relinked'][-1]['to']}")
        if dry:
            continue
        with conn:
            new = row[2] or row[0] if row else _movie_title(conn, tc, _page_record(conn, tc), p, category)
            st["relinked"][-1]["to"] = new
            _relink(conn, rid, new, "page_recheck")
            conn.execute("UPDATE release_titles SET how = ? WHERE release_id = ? AND title_id = ?",
                         (f"wrong_link: page id recheck → {new} (было {how})", rid, tid))
            conn.execute("UPDATE releases SET title_id = ? WHERE id = ?", (new, rid))
            conn.execute("UPDATE titles SET tracker_category = ? WHERE id = ? AND tracker_category IS NULL", (category, new))
    return st


def recheck_release_data(conn, dry: bool = False, log=print) -> dict:
    """A film linked by its name although the release's own data says another film (review round 11: rutor «Корни (2024)»
    — Kazakhstan, 138 min, on the Irish «Frewaka» 103 min; nnmclub «Шайтан (2024)» — Kazakhstan, 62 min, on the Indian
    «Shaitaan»; rustorka «Лакомый кусок / The Rip (Джо Карнахан) [2025]» on an Italian namesake): MediaInfo's length 20+ min
    off the title's, the release's countries none of the title's (or the title has none) and no director in common. The
    release goes to the title of its names (year ±1) whose director it names, or whose countries and length it shares;
    else to a tracker-only row. The old link → 'wrong_link: release data recheck → <id>'."""
    from .tracker_only import make_titles
    st = {"checked": 0, "relinked": []}
    directors: dict[int, list[dict]] = {}
    for tid, n, r in conn.execute("SELECT cr.title_id, p.name, p.name_ru FROM credits cr JOIN people p "
                                  "ON p.id = cr.person_id WHERE cr.role = 'director'"):
        directors.setdefault(tid, []).append({"name": n, "name_ru": r})
    # cross-script, by surname: the tracker writes «Гленн Джордан», the title's credits have only «Glenn Jordan»
    # (review round 12: «Люси / Lucy (2003)» moved off its own film)
    same_dir = lambda said, t: bool(said and directors.get(t) and _director_same(said, directors[t]) is True)   # noqa: E731
    films = conn.execute("SELECT id, title_ru, title_original, title_latin, year, runtime_min, countries FROM titles "
                         "WHERE kind = 'movie' AND duplicate_of IS NULL").fetchall()
    by_name: dict[str, list] = {}
    for f in films:
        for n in {imdb.norm(x) for x in f[1:4] if x} - {""}:
            by_name.setdefault(n, []).append(f)
    rows = conn.execute(
        """SELECT r.id, r.name, r.parsed, r.category, x.how, t.id, t.runtime_min, t.countries, r.source, t.imdb_id FROM release_titles x
             JOIN releases r ON r.id = x.release_id JOIN titles t ON t.id = x.title_id
           WHERE t.kind = 'movie' AND t.runtime_min IS NOT NULL AND json_extract(r.parsed, '$.runtime_min') IS NOT NULL
             AND json_extract(r.parsed, '$.countries') IS NOT NULL
             AND coalesce(x.how, '') NOT LIKE 'wrong_link%' AND coalesce(x.how, '') NOT LIKE 'imdb_page%'
             AND coalesce(x.how, '') NOT LIKE 'manual%' AND coalesce(x.how, '') NOT LIKE 'collection_pack%'
             AND (SELECT count(*) FROM release_titles y WHERE y.release_id = r.id
                  AND coalesce(y.how, '') NOT LIKE 'wrong_link%') = 1""").fetchall()
    # trackers write «Грег Берланти /Greg Berlanti/», «Теренс Малік (Terrence Malick)»
    country = _country
    cl = lambda s: {country(c) for c in (json.loads(s) if s else [])} - {""}   # noqa: E731
    for rid, rname, parsed, category, how, tid, runtime, countries, source, t_imdb in rows:
        if (how or "").startswith(_TWIN_HOW + ("release_recheck",)):
            continue
        p = json.loads(parsed)
        if t_imdb and canonical_imdb(p.get("imdb_id")) == t_imdb:
            continue   # the page names this very film (review round 12: «Люси / Lucy (2003)» TV film, MediaInfo 94 vs 128)
        mine = {country(c) for c in p["countries"]} - {""}
        if source in ("toloka", "mazepa") or not mine:
            continue   # Ukrainian country names never meet the titles' Russian ones
        if cl(countries) & mine:
            continue
        said = [part.strip() for d in p.get("directors") or [] for part in re.split(r"[/()]", d) if part.strip()]
        if same_dir(said, tid):
            continue
        y1, _ = _years(p)
        names = {imdb.norm(n) for n in _title_names(p)} - {""}
        cands = {f[0]: f for n in names for f in by_name.get(n, []) if f[0] != tid and y1 and f[4] and abs(f[4] - y1) <= 1}
        by_director = [f for f in cands.values() if same_dir(said, f[0])]
        # the length off by 20+ min — or another film of the names whose director the release names («Лакомый кусок / The
        # Rip (Джо Карнахан) [2025]» 115 min on an Italian 97-min «RIP»: 897, Carnahan's)
        if abs(runtime - p["runtime_min"]) <= 20 and len(by_director) != 1:
            continue
        st["checked"] += 1
        fit = by_director or [f for f in cands.values()
                              if cl(f[6]) & mine and f[5] and abs(f[5] - p["runtime_min"]) <= 15]
        new = fit[0][0] if len(fit) == 1 else None
        st["relinked"].append({"release_id": rid, "from": tid, "to": new or "tracker_only"})
        log(f"  release data recheck {rid}: {tid} → {new or 'tracker_only'}")
        if dry:
            continue
        with conn:
            if new is None:
                new = make_titles(conn, {"id": rid, "name": rname, "category": category}, p)[0]
                st["relinked"][-1]["to"] = new
            _relink(conn, rid, new, "release_recheck" if not conn.execute(
                "SELECT tracker_only FROM titles WHERE id = ?", (new,)).fetchone()[0] else "tracker_only release_recheck")
            conn.execute("UPDATE release_titles SET how = ? WHERE release_id = ? AND title_id = ?",
                         (f"wrong_link: release data recheck → {new} (было {how})", rid, tid))
            conn.execute("UPDATE releases SET title_id = ? WHERE id = ?", (new, rid))
    return st


def recheck_episode_films(conn, dry: bool = False, log=print) -> dict:
    """A release of episodes linked by its name to a film (review round 13: rutracker «Чёрный клевер / Black Clover [TV]
    [1-51 из 64]» on a film row, «Клинок, рассекающий демонов: Поезд «Бесконечный» (ТВ-2, часть 1)» on the Mugen Train
    film — the episodes «[TV] [N из M]» were read only after a reparse): identified again as a series (match_one); found
    → the old link 'wrong_link: episodes on a film → <ids>'; not found → the link stays."""
    from collections import defaultdict
    st = {"checked": 0, "relinked": []}
    rows = conn.execute(
        """SELECT r.id, r.category, r.parsed, x.title_id, x.how FROM releases r JOIN release_titles x ON x.release_id = r.id
             JOIN titles t ON t.id = x.title_id
           WHERE t.kind = 'movie' AND json_extract(r.parsed, '$.name_parsed.episodes') IS NOT NULL
             AND coalesce(x.how, '') NOT LIKE 'wrong_link%' AND coalesce(x.how, '') NOT LIKE 'imdb_page%'
             AND coalesce(x.how, '') NOT LIKE 'manual%' AND coalesce(x.how, '') NOT LIKE 'collection_pack%'""").fetchall()
    for rid, category, parsed, tid, how in rows:
        if (how or "").startswith(_TWIN_HOW):
            continue
        st["checked"] += 1
        if dry:
            continue
        got = match_one(conn, rid, category, json.loads(parsed), defaultdict(int), lambda *_: None)
        if got and tid not in got:
            with conn:
                conn.execute("UPDATE release_titles SET how = ? WHERE release_id = ? AND title_id = ?",
                             (f"wrong_link: episodes on a film → {','.join(map(str, got))} (было {how})", rid, tid))
                conn.execute("UPDATE releases SET title_id = ? WHERE id = ?", (got[0], rid))
            st["relinked"].append({"release_id": rid, "from": tid, "to": got})
            log(f"  episodes on a film {rid}: {tid} → {got}")
    return st


def _relink(conn, rid: int, tid: int, how: str) -> None:
    """A link made on purpose (a recheck, a twin merge): a new row, or a 'wrong_link' row of the same pair revived — review
    round 9: INSERT OR IGNORE left 4 releases with wrong links only («Легенда о чёрном дереве» S2, «Contagion of Fear»)."""
    conn.execute(db.RELINK_SQL, (rid, tid, how))


def _merge_into(conn, dup: int, main: int, tag: str) -> None:
    """dup's active links → main (how = tag), dup gets duplicate_of = main; the owner's marks and manual «ответственный»
    copied over. Nothing is deleted."""
    for rid, how in conn.execute("SELECT release_id, how FROM release_titles WHERE title_id = ? "
                                 "AND coalesce(how, '') NOT LIKE 'wrong_link%'", (dup,)).fetchall():
        _relink(conn, rid, main, tag)
        conn.execute("UPDATE release_titles SET how = ? WHERE release_id = ? AND title_id = ?",
                     (f"wrong_link: {tag} → {main} (было {how})", rid, dup))
        conn.execute("UPDATE releases SET title_id = ? WHERE id = ? AND title_id = ?", (main, rid, dup))
    conn.execute("UPDATE titles SET duplicate_of = ?, updated_at = datetime('now') WHERE id = ?", (main, dup))
    conn.execute("INSERT OR IGNORE INTO user_marks (title_id, mark, ordered_by, note, updated_at) "
                 "SELECT ?, mark, ordered_by, note, updated_at FROM user_marks WHERE title_id = ?", (main, dup))
    conn.execute("INSERT OR IGNORE INTO responsibility (title_id, person, source, updated_at) "
                 "SELECT ?, person, source, updated_at FROM responsibility WHERE title_id = ? AND source = 'manual'", (main, dup))


_ONA_RE = re.compile(r"(?i)^(.*?)[\s(]*\b(ONA|OVA|OAD)\b")


def merge_title_twins(conn, dry: bool = False, log=print) -> dict:
    """One work, two rows outside the season rules (review 2026-10-28, round 9):
      * a tracker-only row whose releases write no season or episodes, and an identified title of the same names and
        year (rustorka «Сезон охоты / Hunting Season (Раджа Коллинз) [2025 …]» in a series forum = the film 945);
      * a film known to IMDb only and the same film known to TMDB only («Проект «Суперсила». Армия мутантов» 677 /
        11631): the IMDb id moves to the TMDB row;
      * a «series» row on a film's IMDb id that TMDB has as a film («Зверский коп» tt39307240);
      * two tracker-only rows of one ONA / OVA («Моя геройская академия (ONA-2)» / «… ONA (1-2 серии из 2)», 2022)."""
    from .tracker_only import name_seasons
    st = {"merged": []}
    cols = ("id", "kind", "tracker_only", "imdb_id", "series_imdb_id", "tmdb_id", "year", "title_ru", "title_original",
            "title_latin", "title_uk")
    rows = [dict(zip(cols, r)) for r in conn.execute(
        f"SELECT {', '.join(cols)} FROM titles WHERE duplicate_of IS NULL AND year IS NOT NULL AND EXISTS (SELECT 1 FROM "
        "release_titles x WHERE x.title_id = titles.id AND coalesce(x.how, '') NOT LIKE 'wrong_link%')")]
    rel: dict[int, list] = {}
    for tid, name, parsed in conn.execute(
            "SELECT x.title_id, r.name, r.parsed FROM release_titles x JOIN releases r ON r.id = x.release_id "
            "WHERE coalesce(x.how, '') NOT LIKE 'wrong_link%'"):
        rel.setdefault(tid, []).append((name, parsed))

    def names(t, with_rel=True):
        out = {t.get(k) for k in ("title_ru", "title_original", "title_latin", "title_uk")}
        for name, parsed in rel.get(t["id"], []) if with_rel else []:
            try:
                out |= set(_title_names(json.loads(parsed) if parsed else {}))
            except ValueError:
                pass
        return {imdb.norm(n) for n in out if n} - {""}

    def seasonless(t):
        # no season, episodes, special — nor a pack («Безумный Макс / Mad Max (2015 - 2024) [Дилогия]» is not Fury Road)
        return all(not name_seasons(n) and not re.search(
            r"(?i)сери[ийя]|episod|\[\d+\s*из|\bS\d|спецвыпуск|special|\d{4}\s*[-–]\s*\d{4}|дилоги|трилоги|квадрологи|"
            r"тетралоги|пенталоги|гексалоги|коллекци|collection|антологи", n or "") for n, _ in rel.get(t["id"], []))

    def latin(t):
        return {n for n in names(t) if not re.search(r"[а-яёіїєґ]", n)}

    ident = [t for t in rows if not t["tracker_only"]]
    by_name: dict[tuple, set[int]] = {}
    for t in ident:
        for n in names(t):
            by_name.setdefault((n, t["year"]), set()).add(t["id"])
    row_of = {t["id"]: t for t in rows}
    directors: dict[int, set[str]] = {}
    for tid, *nms in conn.execute("SELECT cr.title_id, p.name, p.name_ru, p.name_uk FROM credits cr JOIN people p "
                                  "ON p.id = cr.person_id WHERE cr.role = 'director'"):
        directors.setdefault(tid, set()).update(imdb.norm(n) for n in nms if n)

    def named_director(t, h):
        # «Грязные деньги / In the Grey (Гай Ричи) [2025 …]» — rustorka writes the director in brackets
        said = {imdb.norm(m) for name, _ in rel.get(t["id"], []) for m in re.findall(r"\(([^()]+)\)\s*\[", name or "")}
        return bool(said & directors.get(h, set()))

    moved_off: dict[int, set[int]] = {}   # tracker-only row → titles its releases were moved off (release / page recheck)
    for rid, old, new in conn.execute(
            """SELECT x.release_id, x.title_id, y.title_id FROM release_titles x JOIN release_titles y
                 ON y.release_id = x.release_id AND y.title_id != x.title_id
               WHERE x.how LIKE 'wrong_link: release data recheck%' OR x.how LIKE 'wrong_link: page id recheck%'"""):
        moved_off.setdefault(new, set()).add(old)
    pairs = []
    for t in rows:
        if t["tracker_only"] and seasonless(t):
            hits = set().union(*[by_name.get((n, t["year"]), set()) for n in names(t)] or [set()])
            hits = {h for h in hits if row_of[h]["kind"] == "movie"}   # a season twin is seasons_by_name's
            if not hits:
                # a year off (review round 10: rustorka «In the Grey (Гай Ричи) [2025]», the film 2026) — the director
                # named in the release decides
                near = set().union(*[by_name.get((n, t["year"] + d), set()) for n in names(t) for d in (-1, 1)] or [set()])
                hits = {h for h in near if row_of[h]["kind"] == "movie" and named_director(t, h)}
            # not the film its releases were taken off by their own data (review round 11: «Корни (2024)» Kazakhstan went
            # back onto the Irish «Frewaka» by name and year)
            hits = {h for h in hits if h not in moved_off.get(t["id"], set())}
            if len(hits) == 1:
                pairs.append((t["id"], hits.pop(), "tracker_twin"))
    films_imdb = {t["imdb_id"]: t for t in ident if t["kind"] == "movie" and t["imdb_id"]}
    for t in ident:
        if t["kind"] == "movie" and t["imdb_id"] and not t["tmdb_id"]:
            # a shared non-Russian name too: «Убийства в...» is a name of many TV films (Vosges ≠ Corrèze)
            hits = {h for n in names(t, False) for h in by_name.get((n, t["year"]), set())
                    if row_of[h]["kind"] == "movie" and row_of[h]["tmdb_id"] and not row_of[h]["imdb_id"]
                    and latin(t) & latin(row_of[h])}
            if len(hits) == 1:
                pairs.append((t["id"], hits.pop(), "imdb_tmdb_twin"))
        if t["kind"] == "series" and not t["tmdb_id"] and t["series_imdb_id"] in films_imdb \
                and films_imdb[t["series_imdb_id"]]["tmdb_id"]:
            pairs.append((t["id"], films_imdb[t["series_imdb_id"]]["id"], "film_not_series"))
    # a «film» row on a series' IMDb id with no TMDB film: a pack of the show's episodes (review round 11: «Наруто + Наруто:
    # Ураганные хроники [01-135, 01-479] (2002)» on tt0409591 — shown although the owner saw «Наруто») → its first season
    for tid, sid in conn.execute(
            """SELECT t.id, (SELECT s.id FROM titles s WHERE s.series_imdb_id = t.imdb_id AND s.season_number > 0
                               AND s.duplicate_of IS NULL ORDER BY s.season_number LIMIT 1)
               FROM titles t JOIN imdb_basics b ON b.tconst = t.imdb_id
               WHERE t.kind = 'movie' AND t.tmdb_id IS NULL AND t.duplicate_of IS NULL
                 AND b.title_type IN ('tvSeries', 'tvMiniSeries')""").fetchall():
        if sid and tid in row_of:
            pairs.append((tid, sid, "series_pack_twin"))
    # a tracker-only «film» «<series> N» of an anime season (review round 12: rutracker «Cyberpunk: Edgerunners 2 [ONA]
    # [10 из 10] [2026]» shown next to the season S2 2026) → that season, when its releases read as episodes
    seasons_of: dict[tuple, set[int]] = {}
    for t in ident:
        if t["kind"] == "series":
            sn = conn.execute("SELECT season_number FROM titles WHERE id = ?", (t["id"],)).fetchone()[0]
            for n in names(t, False):
                seasons_of.setdefault((n, sn), set()).add(t["id"])
    for t in rows:
        if not t["tracker_only"] or t["kind"] != "movie" or not any(
                re.search(r"(?i)\[(?:TV|ONA|WEB)|сери[ийя]|\[\d+\s*из\s*\d+|episod", n or "") for n, _ in rel.get(t["id"], [])):
            continue
        hits = {h for n in names(t) if (m := re.match(r"^(.*\S)\s+(\d{1,2})$", n))
                for h in seasons_of.get((m.group(1), int(m.group(2))), set())
                if row_of[h]["year"] is not None and abs(row_of[h]["year"] - t["year"]) <= 1}
        if len(hits) == 1:
            pairs.append((t["id"], hits.pop(), "season_name_twin"))
    ona: dict[tuple, list[int]] = {}
    for t in rows:
        if t["tracker_only"]:
            for name, _ in rel.get(t["id"], []):
                if m := _ONA_RE.match(re.split(r"\s/\s", name or "")[0]):
                    ona.setdefault((imdb.norm(m.group(1)), m.group(2).upper(), t["year"]), []).append(t["id"])
    for ids in ona.values():
        ids = sorted(set(ids))
        pairs += [(d, ids[0], "ona_twin") for d in ids[1:]]
    done = set()
    for dup, main, tag in pairs:
        if dup in done or main in done or dup == main:
            continue
        done.add(dup)
        st["merged"].append((dup, main, tag))
        log(f"  {tag} {dup} → {main}")
        if dry:
            continue
        with conn:
            if tag == "imdb_tmdb_twin":
                old = row_of[dup]["imdb_id"]
                conn.execute("UPDATE titles SET imdb_id = NULL WHERE id = ?", (dup,))
                conn.execute("UPDATE titles SET imdb_id = ? WHERE id = ? AND imdb_id IS NULL", (old, main))
            _merge_into(conn, dup, main, tag)
    return st


def phantom_seasons(conn) -> set[int]:
    """Season rows made only from the trackers' numbering (review 2026-10-28, round 9: «Семь смертных грехов» S5–S7,
    «Рони, дочь разбойника» S2, «Шоу 90-х» S3): no TMDB data, numbered past the series' last TMDB season, and every
    release of theirs is a pack also on the TMDB seasons. A new season TMDB lacks yet («Террор» S3) has releases of its
    own and stays. Hidden by rank, not deleted."""
    tmdb_max = dict(conn.execute("SELECT series_imdb_id, max(season_number) FROM titles WHERE kind = 'series' "
                                 "AND tmdb_id IS NOT NULL AND series_imdb_id IS NOT NULL AND duplicate_of IS NULL "
                                 "GROUP BY series_imdb_id").fetchall())
    out = set()
    for tid, simdb, s in conn.execute(
            "SELECT id, series_imdb_id, season_number FROM titles WHERE kind = 'series' AND tmdb_id IS NULL "
            "AND duplicate_of IS NULL AND series_imdb_id IS NOT NULL AND season_number > 0").fetchall():
        if simdb not in tmdb_max or s <= tmdb_max[simdb]:
            continue
        rels = [r for r, in conn.execute("SELECT release_id FROM release_titles WHERE title_id = ? "
                                         "AND coalesce(how, '') NOT LIKE 'wrong_link%'", (tid,))]
        if rels and all(conn.execute(
                "SELECT 1 FROM release_titles x JOIN titles t ON t.id = x.title_id WHERE x.release_id = ? AND x.title_id != ? "
                "AND coalesce(x.how, '') NOT LIKE 'wrong_link%' AND t.series_imdb_id = ? AND t.tmdb_id IS NOT NULL",
                (r, tid, simdb)).fetchone() for r in rels):
            out.add(tid)
    return out


def _page_linked(conn, tid: int) -> bool:
    return bool(conn.execute("SELECT 1 FROM release_titles WHERE title_id = ? AND how LIKE 'imdb_page%'", (tid,)).fetchone())


def undo_merge(conn, dup: int, main: int, tag: str, main_how: str) -> int:
    """Take back a wrong _merge_into(dup, main, tag): dup's links back as they were, main's link of each release marked
    `main_how` (a wrong_link note — kept, not deleted), duplicate_of cleared."""
    n = 0
    with conn:
        for rid, how in conn.execute("SELECT release_id, how FROM release_titles WHERE title_id = ? AND how LIKE ?",
                                     (dup, f"wrong_link: {tag} → {main} (было %")).fetchall():
            conn.execute("UPDATE release_titles SET how = ? WHERE release_id = ? AND title_id = ?",
                         (how.split("(было ", 1)[1].rstrip(")"), rid, dup))
            conn.execute("UPDATE release_titles SET how = ? WHERE release_id = ? AND title_id = ? AND how = ?",
                         (main_how, rid, main, tag))
            conn.execute("UPDATE releases SET title_id = ? WHERE id = ? AND title_id = ?", (dup, rid, main))
            n += 1
        conn.execute("UPDATE titles SET duplicate_of = NULL WHERE id = ? AND duplicate_of = ?", (dup, main))
    return n


def undo_part_twin(conn, dup: int, main: int) -> int:
    """Take back a wrong merge_part_twins: the links it moved return to the row, duplicate_of cleared."""
    n = 0
    with conn:
        for rid, how in conn.execute("SELECT release_id, how FROM release_titles WHERE title_id = ? AND how LIKE ?",
                                     (dup, f"wrong_link: part twin → {main} (было %")).fetchall():
            old = how.split("(было ", 1)[1].rstrip(")")
            conn.execute("UPDATE release_titles SET how = ? WHERE release_id = ? AND title_id = ?", (old, rid, dup))
            conn.execute("UPDATE release_titles SET how = ? WHERE release_id = ? AND title_id = ? AND how = 'part_twin'",
                         (f"wrong_link: part twin undone → {dup}", rid, main))   # kept, not deleted
            conn.execute("UPDATE releases SET title_id = ? WHERE id = ? AND title_id = ?", (dup, rid, main))
            n += 1
        conn.execute("UPDATE titles SET duplicate_of = NULL WHERE id = ? AND duplicate_of = ?", (dup, main))
    return n


def merge_part_twins(conn, dry: bool = False, log=print) -> dict:
    """A season row TMDB has no data for (an anthology season on the IMDb id: «Монстр» S2–S4; a second IMDb id; a
    tracker-only row) whose names or releases' names are those of a TMDB season row of the same year and season
    («Monsters: The Lyle and Erik Menendez Story», TMDB 225634) — one work shown twice (review 2026-10-28): its links move
    to the TMDB row, the row gets duplicate_of (the owner's marks and manual «ответственный» copied over)."""
    from .tracker_only import name_seasons

    def rel_names(tid):
        out, seasons = set(), set()
        for name, parsed in conn.execute("SELECT r.name, r.parsed FROM release_titles x JOIN releases r ON r.id = x.release_id "
                                         "WHERE x.title_id = ? AND coalesce(x.how, '') NOT LIKE 'wrong_link%'", (tid,)):
            try:
                p = json.loads(parsed) if parsed else {}
            except ValueError:
                continue
            ns = _title_names(p)
            ns += [x for t in ns for x in re.findall(r"\(([^()]{3,})\)", t)] + [re.sub(r"\s*\([^()]*\)", "", t) for t in ns]
            out |= {imdb.norm(n) for n in ns if n}
            seasons |= set(_seasons(p.get("name_parsed") or {}) or name_seasons(name))
        return out - {""}, seasons

    def own_names(row):
        return {imdb.norm(n) for n in row[3:7] if n} - {""}

    # B: a season row with TMDB data, by its names and its releases' names (another IMDb id of the same show — «Чудачка
    # и прекрасный принц» tt36106889 / tt36295861; TMDB names it otherwise — «Турнир на горе Хуа» for «Легенда о героях:
    # Истинная сутра девяти инь»)
    cols = "id, year, season_number, title_original, title_ru, title_latin, title_uk, series_tmdb_id, series_imdb_id"
    one_season = {tv for tv, n in conn.execute("SELECT series_tmdb_id, max(season_number) FROM titles WHERE kind = 'series' "
                                               "AND series_tmdb_id IS NOT NULL AND tmdb_id IS NOT NULL GROUP BY series_tmdb_id")
                  if n == 1}
    idx: dict[tuple[str, int], set[int]] = {}
    brow: dict[int, tuple] = {}
    for row in conn.execute(f"SELECT {cols} FROM titles WHERE kind = 'series' AND tmdb_id IS NOT NULL AND duplicate_of IS NULL "
                            "AND year IS NOT NULL AND season_number > 0"):
        brow[row[0]] = row
        for n in own_names(row) | rel_names(row[0])[0]:
            idx.setdefault((n, row[1]), set()).add(row[0])
    st = {"merged": []}
    rows = conn.execute(
        f"""SELECT {cols} FROM titles t WHERE t.kind = 'series' AND t.tmdb_id IS NULL AND t.duplicate_of IS NULL
              AND t.year IS NOT NULL AND t.season_number > 0
              AND EXISTS (SELECT 1 FROM release_titles x WHERE x.title_id = t.id AND coalesce(x.how, '') NOT LIKE 'wrong_link%')"""
    ).fetchall()
    for row in rows:
        tid, year, season = row[0], row[1], row[2]
        names, rel_seasons = rel_names(tid)
        hits: set[int] = set()
        for n in names | own_names(row):
            hits |= idx.get((n, year), set())
        hits = {h for h in hits if h != tid and not (row[8] and brow[h][8] == row[8])}   # not a season of its own series
        # the same season — or the part's own one-season TMDB series («Монстр» S2 = «Monsters: … Menendez Story» S1);
        # an ONA / special of a long show («Моя геройская академия ONA» 2022 is not its season 6) stays
        # — the part only when its releases name the part's own original name, not the series' («Путешествие к
        # бессмертию» S5 is not the live-action «凡人修仙传» 2025, «Академия единорогов» S5 not «… Secrets Revealed»)
        part = lambda h: (brow[h][7] in one_season and imdb.norm(brow[h][3] or "") in names
                          and imdb.norm(brow[h][3] or "") not in own_names(row))
        hits = {h for h in hits if brow[h][2] == season and (not rel_seasons or season in rel_seasons) or part(h)}
        if len(hits) != 1:
            continue
        main = hits.pop()
        # the page's IMDb id beats a name (review 2026-10-28: «Шип любви» — rutor / kinozal pages link tt33243173, the TMDB
        # row «与君诀» tt32868957 held only a release found by name): another IMDb id with no page link of its own → not merged
        if row[8] and brow[main][8] and brow[main][8] != row[8] and _page_linked(conn, tid) and not _page_linked(conn, main):
            continue
        links = conn.execute("SELECT x.release_id, x.how FROM release_titles x WHERE x.title_id = ? "
                             "AND coalesce(x.how, '') NOT LIKE 'wrong_link%'", (tid,)).fetchall()
        st["merged"].append((tid, main))
        log(f"  part twin {tid} → {main}")
        if dry:
            continue
        with conn:
            for rid, how in links:
                _relink(conn, rid, main, "part_twin")
                conn.execute("UPDATE release_titles SET how = ? WHERE release_id = ? AND title_id = ?",
                             (f"wrong_link: part twin → {main} (было {how})", rid, tid))
                conn.execute("UPDATE releases SET title_id = ? WHERE id = ? AND title_id = ?", (main, rid, tid))
            conn.execute("UPDATE titles SET duplicate_of = ?, updated_at = datetime('now') WHERE id = ?", (main, tid))
            conn.execute("INSERT OR IGNORE INTO user_marks (title_id, mark, ordered_by, note, updated_at) "
                         "SELECT ?, mark, ordered_by, note, updated_at FROM user_marks WHERE title_id = ?", (main, tid))
            conn.execute("INSERT OR IGNORE INTO responsibility (title_id, person, source, updated_at) "
                         "SELECT ?, person, source, updated_at FROM responsibility WHERE title_id = ? AND source = 'manual'",
                         (main, tid))
    return st


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
            for rid, how in conn.execute("SELECT release_id, how FROM release_titles WHERE title_id = ?", (dup,)).fetchall():
                if (how or "").startswith("wrong_link"):
                    conn.execute("INSERT OR IGNORE INTO release_titles (release_id, title_id, how) VALUES (?, ?, ?)",
                                 (rid, main, f"{how} imdb_redirect"))
                else:
                    _relink(conn, rid, main, f"{how or ''} imdb_redirect")
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
        db.purge_dv5(conn)            # DV5-only releases are not stored, not linked (owner; review 2026-10-28)
        return _match_releases(conn, retry_issues)


def _match_releases(conn, retry_issues: bool = False) -> dict:
    merged = merge_redirected_titles(conn, log=lambda *_: None)
    recheck_series_links(conn, log=lambda *_: None)
    recheck_movie_links(conn, log=lambda *_: None)
    recheck_release_data(conn, log=lambda *_: None)
    recheck_episode_films(conn, log=lambda *_: None)
    merge_part_twins(conn, log=lambda *_: None)
    merge_title_twins(conn, log=lambda *_: None)
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
    if kind == "series" and not seasons:
        # the season written in words the name parser left unread (review 2026-10-28: rustorka / rutracker «(Сезон: 2 /
        # Серии: …)», nnmclub «(сезон 2, серии 1-15 (16-30) из 15)» — season 2 releases sat on season 1)
        from .tracker_only import name_seasons
        seasons = name_seasons(p.get("name") or np.get("name"))

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
    if kind == "series" and tconst and not tmdb_id:
        seasons = _shift_seasons(conn, tconst, seasons, y1, y2, names=_title_names(p) + [np.get("name") or ""],
                                 eps=_all_episodes(np)) or seasons   # «… Эволюция (2 сезон) (2024)» → S17
    elif kind == "series" and tmdb_id and len(seasons) == 1:
        # the same by TMDB's own seasons (round 14: «Деревня кузнецов (ТВ-3) [11 из 11] 2023» → S4 of 2023, 11 episodes)
        raw = tmdb.tv_raw(conn, tmdb_id) or {}
        tv = [x for x in raw.get("seasons") or [] if (x.get("season_number") or 0) > 0 and tmdb._year(x.get("air_date"))]
        seasons = _shift_seasons(conn, (raw.get("external_ids") or {}).get("imdb_id") or f"tmdb:{tmdb_id}", seasons,
                                 y1, y2, {x["season_number"]: tmdb._year(x["air_date"]) for x in tv},
                                 _title_names(p) + [np.get("name") or ""], eps=_all_episodes(np),
                                 counts={x["season_number"]: x.get("episode_count") for x in tv}, need_eps=True) \
            or seasons
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


def _country(c: str | None) -> str:
    # trackers write «США.», «США- -Кіностудія», «Великобритания (UK)»
    return re.split(r"\s*[-–,/(]", c or "")[0].strip(" .").casefold()


_NAME_FOLD = (("shch", "sh"), ("sch", "sh"), ("dzh", "j"), ("jh", "j"), ("zh", "j"), ("kh", "h"), ("wr", "r"), ("gh", ""),
              ("ph", "f"), ("th", "t"), ("ck", "k"), ("tch", "c"), ("ch", "c"), ("ts", "s"), ("tz", "s"), ("x", "ks"),
              ("q", "k"), ("y", "i"), ("z", "s"), ("f", "v"), ("gn", "n"))


def _name_key(word: str) -> str:
    """A rough cross-script key of a name word: «Джонсон» / «Johnson» → jnsn, «Марсель» / «Марсель» / «Marcel» → mrsl.
    F and V are one letter («Стивен» / Stephen), W is a vowel («Уэллс» / Wells, «Боузер» / Bowser — review round 14)."""
    w = unicodedata.normalize("NFKD", word.replace("ł", "l").replace("ø", "o").replace("đ", "d"))
    w = "".join(ch for ch in w if not unicodedata.combining(ch))   # Kośmicki = Kosmicki
    for a, b in _NAME_FOLD:
        w = w.replace(a, b)
    w = re.sub(r"g(?=[ei])", "j", re.sub(r"c(?=[eiy])", "s", w).replace("c", "k"))
    # a leading W is В or У+vowel in Russian («Вальц» / Walz, «Уэллс» / Wells, «Уайт» / White); inside — a vowel
    w = re.sub(r"^u(?=[aeiou])", "v", re.sub(r"^w", "v", w)).replace("w", "u")
    if not w:
        return w
    # Russian «Г» for H («Гавлик» = Havlík); a leading vowel or J / Я / И is one sound («Иган» = Egan, «Яндоурек» = Jandourek)
    first = "g" if w[0] == "h" else "V" if w[0] in "aeiouj" else w[0]
    return re.sub(r"(.)\1+", r"\1", first + re.sub(r"[aeiouh]", "", w[1:]))


def _other_script(name: str) -> bool:
    """A name in neither Latin nor Cyrillic letters («丁亮», «గోపీచంద్ మలినేని»): no key to compare."""
    return any(ch.isalpha() and not ("a" <= ch.lower() <= "z" or "Ѐ" <= ch <= "ӿ" or "À" <= ch <= "ɏ")
               for ch in name)


def _director_same(said: list[str], people: list[dict]) -> bool | None:
    """Does a director the release names (any script) match one of the title's? Both the first and the last word of the
    name, in either order («Ким Чжи-ун» / «Jee-woon Kim»); None when no comparison is sure (a short or hyphenated
    East-Asian name, nothing in Latin / Cyrillic) — then the countries decide (review round 13: «Мэтт Джонсон» ≠ Matt
    Johnson by tmdb._person_match; «Нэйтан Сильвер» ~ Sylvester Stallone)."""
    def words(n):
        return [_name_key(w) for w in tmdb._translit(n).replace("-", " ").split() if re.search(r"[a-z]", w)]
    foreign = any(_other_script(n) for p in people for n in (p.get("name"), p.get("name_ru")) if n) or \
        any(_other_script(n) for n in said)
    have = [words(n) for p in people for n in (p.get("name"), p.get("name_ru")) if n]
    want = [words(n) for n in said]
    have, want = [h for h in have if len(h) >= 2], [w for w in want if len(w) >= 2]
    if not have or not want:
        return None
    for w in want:
        for h in have:
            # middle names, when both have them, agree too («Пол Томас Андерсон» ≠ Paul W. S. Anderson, round 14)
            if {w[0], w[-1]} == {h[0], h[-1]} and len(w[-1]) >= 2 and not (
                    len(w) > 2 and len(h) > 2 and not set(w[1:-1]) & set(h[1:-1])):
                return True
    # «no» only for plainly other people: two-word names, every key of 3+ letters, neither the first nor the last word
    # alike (a «Морган Мэтьюз» / Morgan Matthews spelling gap is undecided, not another director)
    sure = not foreign and all(len(w) == 2 and all(len(x) >= 3 for x in w) and w[0] not in h and w[-1] not in h
                               for w in want for h in have)
    return False if sure else None


def _release_data_agrees(conn, p: dict, tid: int) -> bool:
    """The release's own data (MediaInfo / description) names the title's director — or, where either side has no
    director, shares a country with it (a country alone is weak: «Кровавая вечеринка 2» 1987 and the 1982 film are both
    the USA, their directors differ)."""
    said = [part.strip() for d in p.get("directors") or [] for part in re.split(r"[/()]", d) if part.strip()]
    people = [{"name": n, "name_ru": r} for n, r in conn.execute(
        "SELECT p.name, p.name_ru FROM credits cr JOIN people p ON p.id = cr.person_id "
        "WHERE cr.role = 'director' AND cr.title_id = ?", (tid,))]
    if said and people:
        same = _director_same(said, people)   # cross-script: «Кевин Фэйр» = Kevin Fair
        if same is not None:
            return same
    mine = {_country(c) for c in p.get("countries") or []} - {""}
    row = conn.execute("SELECT countries FROM titles WHERE id = ?", (tid,)).fetchone()
    return bool(row and mine & ({_country(c) for c in json.loads(row[0] or "[]")} - {""}))


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
            if (how or "").startswith("imdb_page") and b is not None and b["runtime_min"] and p.get("runtime_min") \
                    and abs(b["runtime_min"] - p["runtime_min"]) <= 10 and _release_data_agrees(conn, p, tid):
                # the page's own film by its length and its country or director: the tracker's year is the dub's
                # (review round 11: «Кулинарная книга любви / Hint of Love (2024)» — MediaInfo 94 min, Canada, Kevin Fair:
                # the 2020 Canadian TV film the page names); the length alone is no proof (round 12: «Midnight Sun (2025)»
                # 109 min vs the 2006 film's 119)
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
                        conn.execute(db.RELINK_SQL,
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
