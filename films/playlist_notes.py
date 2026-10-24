"""Short original Russian descriptions of the trailer playlists (R68), prepared in advance so the owner gets them
the moment he asks («описание к плейлисту X») right before starting the playlist.

Run: python -X utf8 -m films.playlist_notes [build|show <key>]   (build is the default; --online: fetch missing TMDB)

Playlists (members in playlist order: year ↑, release date ↑, title — as the responsible-playlist skill orders them):
  kostya_want — «Костя Хочет»: title_ids answered "playlist" in data/cache/kostya_want_answers.json (survey still
                running, so the file is read on every build);
  probably    — «Вероятно хочу»: big-list titles with 💛/🟡 (films.kostya_list, marker «maybe»);
  possible    — «Возможно»: big-list titles without a marker;
  dima / yana — «Для Димы» / «Для Яны»: responsibility rows with source manual or auto; Яна also gets the "yana"
                answers of the ❤️ survey and the 🧸 lines of the big list.
Every playlist drops watched titles (any `watched` row except *_partial) and keeps only the first unseen part of a
film series (R66): one member per TMDB collection (`belongs_to_collection`) and one season per series. A kept film
whose collection has an earlier released part not marked watched in the DB gets a «⚠» line (that part is not in the
playlist — the owner may have seen it outside Plex, or the playlist should start with it).

Description = TMDB ru-RU overview, verbatim, cut at a sentence boundary to 1–2 sentences (≤ 300 chars); no Russian
overview → «описания нет» + the first sentence of the original-language overview marked «(англ.)» (or the language).
Never a paraphrase. The DB is opened read-only and TMDB is read from its cache (tmdb_raw) unless --online.

Output: data/cache/playlist_notes/<key>.md and index.json (key → playlist name, count, generated_at).
Rebuild after every playlist change.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from datetime import datetime

from . import db, kostya_list
from .sources import tmdb

OUT = db.ROOT / "data" / "cache" / "playlist_notes"
ANSWERS = db.ROOT / "data" / "cache" / "kostya_want_answers.json"
PLAYLISTS = {"kostya_want": "Костя Хочет", "probably": "Вероятно хочу", "possible": "Возможно",
             "dima": "Для Димы", "yana": "Для Яны"}
MAX_CHARS = 300
_LANG_RU = {"en": "англ.", "fr": "франц.", "de": "нем.", "es": "исп.", "it": "итал.", "ja": "япон.", "ko": "кор.",
            "zh": "кит.", "uk": "укр.", "pl": "польск.", "pt": "порт.", "sv": "швед.", "da": "дат.", "no": "норв.",
            "nl": "нидерл.", "tr": "тур.", "hi": "хинди", "fi": "фин.", "cs": "чеш.", "th": "тайск."}


def connect_ro() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db.DB_PATH.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


# ------------------------------------------------------------------------------------------- members

def _answers(value: str) -> list[int]:
    if not ANSWERS.exists():
        return []
    return [int(k) for k, v in json.loads(ANSWERS.read_text(encoding="utf-8")).items() if v == value]


def _responsible(conn, person: str) -> list[int]:
    return [r[0] for r in conn.execute("SELECT title_id FROM responsibility WHERE person = ? AND source IN ('manual', 'auto')",
                                       (person,))]


def raw_members(conn, key: str) -> list[int]:
    markers = kostya_list.markers_by_title()
    from . import fresh
    auto = fresh.auto_marks(conn)   # R99: new since 2026 with RT ≥ 90 → ❤️ here, 80–89 → 💛 «Вероятно хочу» (owner, 2026-10-24)
    if key == "kostya_want":
        return _answers("playlist") + [t for t, m in auto.items() if m == "want" and t not in markers]
    if key == "probably":
        return [t for t, m in markers.items() if m == "maybe"] + [t for t, m in auto.items() if m == "maybe" and t not in markers]
    if key == "possible":
        return [t for t, m in markers.items() if m == "possible"]
    if key == "dima":
        return _responsible(conn, "Дима")
    if key == "yana":
        return _responsible(conn, "Яна") + _answers("yana") + [t for t, m in markers.items() if m == "yana"]
    raise KeyError(key)


def watched_ids(conn) -> set[int]:
    return {r[0] for r in conn.execute("SELECT title_id FROM watched WHERE source NOT LIKE '%\\_partial' ESCAPE '\\'")}


def title_rows(conn, ids) -> list[dict]:
    ids = list(dict.fromkeys(ids))
    out = []
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        out += [dict(r) for r in conn.execute(
            f"SELECT id, kind, season_number, series_key, series_tmdb_id, tmdb_id, tmdb_type, title_ru, title_original,"
            f" year, release_date FROM titles WHERE id IN ({','.join('?' * len(chunk))})", chunk)]
    return out


def _order(t: dict) -> tuple:
    return (t["year"] or 9999, t["release_date"] or "9999", t["title_ru"] or t["title_original"] or "",
            t["season_number"] or 0)


def first_parts(conn, rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """R66: of several parts of one series keep the first (rows are already unseen and in order).
    Parts = one TMDB collection (films) or one series (seasons). Returns (kept, dropped)."""
    seen, kept, dropped = {}, [], []
    for t in rows:
        group = None
        if t["kind"] == "series":
            group = f"series:{t['series_key'] or t['series_tmdb_id'] or t['tmdb_id']}"
        elif t["tmdb_id"]:
            coll = (_raw(conn, "movie", t["tmdb_id"]) or {}).get("belongs_to_collection")
            group = f"collection:{coll['id']}" if coll else None
        if group and group in seen:
            dropped.append(dict(t, kept_instead=seen[group]))
            continue
        if group:
            seen[group] = t
        kept.append(t)
    return kept, dropped


def earlier_unseen_part(conn, t: dict, watched_tmdb: set[int]) -> dict | None:
    """R66 check for a kept film: an earlier released part of its TMDB collection that the owner has not watched
    (it would be the right «first part» for the playlist). None when unknown (collection not cached)."""
    if t["kind"] != "movie" or not t["tmdb_id"]:
        return None
    coll = (_raw(conn, "movie", t["tmdb_id"]) or {}).get("belongs_to_collection")
    try:
        parts = (tmdb.collection(conn, coll["id"]) or {}).get("parts") or [] if coll else []
    except tmdb.TmdbError:
        return None
    today = datetime.now().strftime("%Y-%m-%d")
    mine = next((p["release_date"] for p in parts if p["tmdb_id"] == t["tmdb_id"]), None) or t["release_date"]
    return next((p for p in parts if p["release_date"] and mine and p["release_date"] < mine
                 and p["release_date"] <= today and p["tmdb_id"] not in watched_tmdb), None)


def watched_movie_tmdb(conn) -> set[int]:
    return {r[0] for r in conn.execute(
        "SELECT t.tmdb_id FROM titles t JOIN watched w ON w.title_id = t.id WHERE t.kind = 'movie'"
        " AND t.tmdb_id IS NOT NULL AND w.source NOT LIKE '%\\_partial' ESCAPE '\\'")}


def _skip_imdb(conn=None) -> set[str]:
    """[playlists] skip_imdb + every member of [playlists] skip_franchises (sort priority, no trailer)."""
    import tomllib
    with open(db.ROOT / "config" / "settings.toml", "rb") as f:
        pl = tomllib.load(f).get("playlists", {})
    out = set(pl.get("skip_imdb", []))
    if conn is not None and pl.get("skip_franchises"):
        q = ",".join("?" * len(pl["skip_franchises"]))
        out |= {r[0] for r in conn.execute(f"SELECT imdb_id FROM franchise_members WHERE franchise_key IN ({q})", pl["skip_franchises"])}
    return out


_VISIBLE: dict | None = None


def visible_order() -> dict[int, int]:
    """title_id → position in the sheet (rank.compute): only titles the main filter shows (not watched, IMDb ≥ 5 or
    fresh, has a voice-over, not «не интересен», …). Playlists = the same filter and the same order as the sheet
    (owner, 2026-10-24: «сортировка в плейлистах … такая же по приоритету, как и в итоговом документе»)."""
    global _VISIBLE
    if _VISIBLE is None:
        from . import rank
        rows = rank.compute(db.connect())
        _VISIBLE, i = {}, 0
        for r in rows:
            if r["variant_index"] == 0 and not r["hidden_reasons"] and r["title"]["id"] not in _VISIBLE:
                _VISIBLE[r["title"]["id"]] = i
                i += 1
            if r["franchise"] or r.get("top"):
                _FOLLOWED.add(r["title"]["id"])
    return _VISIBLE


_FOLLOWED: set[int] = set()


def followed() -> set[int]:
    """Titles of the franchises the owner follows (franchise points in the sheet) and the «в топ» titles: they rise by
    priority alone, a trailer is not needed (owner, 2026-10-24: «Хищник — часть франшизы, которую мы утвердили … что он
    делает в трейлерах моих папок?»; Крёстный отец, Супер Марио, Хитрый койот, Дьявол носит Prada 2 before)."""
    visible_order()
    return _FOLLOWED


def members(conn, key: str) -> tuple[list[dict], dict]:
    from .seen_survey import have
    # never in a playlist: [playlists] skip_imdb (The Godfather: franchise sort only) and whatever is downloaded (Plex,
    # Jellyfin, disks) or queued in qBittorrent — the owner, 2026-10-24: «не пушка трейлеры на то, что скачано или в ожидании»
    skip = _skip_imdb(conn) | have(conn)
    # never series in a playlist (owner, 2026-10-24: «в плейлистах никогда не должно быть трейлеров сериалов»)
    ids = [t for t in raw_members(conn, key)
           if (r := conn.execute("SELECT coalesce(imdb_id, series_imdb_id), kind FROM titles WHERE id = ?", (t,)).fetchone())
           and r[1] == "movie" and r[0] not in skip]
    rows = title_rows(conn, ids)
    missing = set(ids) - {t["id"] for t in rows}
    w = watched_ids(conn)
    unseen = sorted((t for t in rows if t["id"] not in w), key=_order)
    kept, dropped = first_parts(conn, unseen)
    # R66 (owner, 2026-10-24 — every «предыдущий фильм серии» asked was «не видел»): a sequel gives its place to the
    # first unseen earlier film of its TMDB collection when that film is in the DB; for Дима only if it is a comedy
    # too (comedy as the 1st or 2nd genre), for Яна only if it is a cartoon
    wt, swapped, out = watched_movie_tmdb(conn), [], []
    for t in kept:
        e = earlier_unseen_part(conn, t, wt)
        row = conn.execute("SELECT id FROM titles WHERE tmdb_id = ? AND kind = 'movie'", (e["tmdb_id"],)).fetchone() if e else None
        if row and row[0] not in w and (conn.execute("SELECT imdb_id FROM titles WHERE id = ?", (row[0],)).fetchone()[0]
                                        not in skip):   # the earlier film obeys the same rules (not downloaded / watched / skipped)
            prev = title_rows(conn, [row[0]])[0]
            gr = conn.execute("SELECT genres, is_animation FROM titles WHERE id = ?", (row[0],)).fetchone()
            g = json.loads(gr[0] or "[]")
            # Яна — cartoons only (Babylon 5: The Gathering is live action, its sequel The Road Home is animated)
            if (key != "dima" or "комедия" in g[:2]) and (key != "yana" or gr[1]):
                swapped.append((t["id"], t["title_ru"], prev["title_ru"]))
                t = prev
        if t["id"] not in {x["id"] for x in out}:
            out.append(t)
    # the franchise is already chosen when another film of its TMDB collection is downloaded / queued / watched — no
    # trailer for the other parts, they rise by the franchise points (owner, 2026-10-24: «Я иду искать скачано, зачем нам
    # трейлер второй части, если мы уже утвердили франшизу тем, что скачали первую часть?»)
    have_imdb = have(conn)
    approved = {cid for cid, tid, imdb in conn.execute("SELECT collection_id, id, imdb_id FROM titles WHERE collection_id IS NOT NULL")
                if tid in w or imdb in have_imdb}
    col = {t["id"]: conn.execute("SELECT collection_id FROM titles WHERE id = ?", (t["id"],)).fetchone()[0] for t in out}
    franchise_approved = [(t["id"], t["title_ru"]) for t in out if col[t["id"]] in approved or t["id"] in followed()]
    out = [t for t in out if col[t["id"]] not in approved and t["id"] not in followed()]
    vis = visible_order()
    out = [t for t in out if t["id"] in vis]
    out.sort(key=lambda t: vis[t["id"]])
    return out, {"listed": len(set(ids)), "franchise_approved": franchise_approved, "missing_in_db": sorted(missing), "watched": len(rows) - len(unseen),
                 "later_parts": [(t["id"], t["title_ru"], t["kept_instead"]["title_ru"]) for t in dropped],
                 "swapped_for_earlier": swapped}


# ------------------------------------------------------------------------------------------- descriptions

def _raw(conn, kind: str, tmdb_id, season: int | None = None) -> dict | None:
    try:
        if kind == "season":
            return tmdb.season_raw(conn, int(tmdb_id), season)
        return tmdb.movie_raw(conn, int(tmdb_id)) if kind == "movie" else tmdb.tv_raw(conn, int(tmdb_id))
    except tmdb.TmdbError:
        return None


def _sentences(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text or "").strip()
    return [s for s in re.split(r"(?<=[.!?…])\s+(?=[«\"“(—–-]?[A-ZА-ЯЁІЇЄҐ0-9])", text) if s]


def shorten(text: str, max_sentences: int = 2, limit: int = MAX_CHARS) -> str:
    """The first 1–2 whole sentences within `limit`; a single over-long sentence is cut at a word + «…»."""
    out = ""
    for s in _sentences(text)[:max_sentences]:
        cand = f"{out} {s}".strip()
        if out and len(cand) > limit:
            break
        out = cand
    if len(out) > limit:
        out = out[:limit].rsplit(" ", 1)[0].rstrip(" ,;:—–-") + "…"
    return out


def _original_overview(body: dict) -> tuple[str, str] | None:
    lang = body.get("original_language") or "en"
    tr = (body.get("translations") or {}).get("translations") or []
    for want in (lang, "en"):
        for t in tr:
            ov = ((t.get("data") or {}).get("overview") or "").strip()
            if t.get("iso_639_1") == want and ov:
                return want, ov
    return None


def description(conn, t: dict) -> str:
    tid = t["series_tmdb_id"] or t["tmdb_id"]
    if not tid:
        return "описания нет (нет TMDB id)"
    if t["kind"] == "series":
        series = _raw(conn, "tv", tid)
        season = _raw(conn, "season", tid, t["season_number"]) if t["season_number"] else None
        body = series
        ru = ((season or {}).get("overview") or "").strip() or ((series or {}).get("overview") or "").strip()
    else:
        body = _raw(conn, "movie", tid)
        ru = ((body or {}).get("overview") or "").strip()
    if body is None:
        return "описания нет (TMDB не в кэше)"
    if ru:
        return shorten(ru)
    orig = _original_overview(body)
    if not orig:
        return "описания нет"
    lang, ov = orig
    return f"описания нет; {shorten(ov, 1)} ({_LANG_RU.get(lang, lang)})"


def _name(t: dict) -> str:
    n = t["title_ru"] or t["title_original"] or f"#{t['id']}"
    return f"{n}, сезон {t['season_number']}" if t["kind"] == "series" and t["season_number"] else n


def render(conn, key: str, final: list[dict] | None = None) -> tuple[str, int, dict]:
    """The notes follow the playlist exactly: the final list (all_members — franchise grouping, moves between
    playlists), only titles that have a trailer in it, in the playlist's order."""
    rows, info = members(conn, key)
    if final is not None:
        rows = [t for t in final if any(_vid(u) for u in conn.execute(
            "SELECT trailer_ru_url, trailer_alt_url FROM titles WHERE id = ?", (t["id"],)).fetchone())]
    wt = watched_movie_tmdb(conn)
    lines = [f"# {PLAYLISTS[key]} — {len(rows)}", ""]
    info["earlier_unseen"] = []
    for i, t in enumerate(rows, 1):
        lines.append(f"{i}. **{_name(t)}** ({t['year'] or '?'}) — {description(conn, t)}")
        p = earlier_unseen_part(conn, t, wt)
        if p:
            info["earlier_unseen"].append((t["id"], t["title_ru"], p["title_ru"], p["year"]))
            lines.append(f"   ⚠ раньше вышла часть «{p['title_ru'] or p['title_original']}» ({p['year']}): в базе не отмечена "
                         f"просмотренной и в плейлисте её нет")
    return "\n".join(lines) + "\n", len(rows), info


def build(conn) -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    index = {}
    final = all_members(conn)
    # the notes describe exactly the checked playlist (playlists_desired.json: trailer 1:30–5:00, playable in UA)
    checked = json.loads(DESIRED.read_text(encoding="utf-8")).get("titles", {}) if DESIRED.exists() else {}
    for key, name in PLAYLISTS.items():
        rows = final[key]
        if key in checked:
            order = {tid: i for i, tid in enumerate(checked[key])}
            rows = sorted((t for t in rows if t["id"] in order), key=lambda t: order[t["id"]])
        text, n, info = render(conn, key, rows)
        (OUT / f"{key}.md").write_text(text, encoding="utf-8")
        index[key] = {"name": name, "count": n, "generated_at": now}
        print(f"{key}: {n} (в списке {info['listed']}, просмотрено {info['watched']}, "
              f"следующих частей убрано {len(info['later_parts'])}, нет в БД {len(info['missing_in_db'])}, "
              f"есть более ранняя не отмеченная просмотренной часть {len(info['earlier_unseen'])})", file=sys.stderr)
        for e in info["earlier_unseen"]:
            print(f"   ⚠ {e[1]} (#{e[0]}): раньше «{e[2]}» ({e[3]})", file=sys.stderr)
    (OUT / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    return index


# ------------------------------------------------------------------------------------------- franchise grouping

def _group_key(conn, t: dict) -> str | None:
    """Franchise of a playlist title: a followed franchise (franchise_members) or else its TMDB collection."""
    imdb = (conn.execute("SELECT imdb_id FROM titles WHERE id = ?", (t["id"],)).fetchone() or [None])[0]
    if imdb:
        r = conn.execute("SELECT franchise_key FROM franchise_members WHERE imdb_id = ? AND confidence != 'review' "
                         "ORDER BY franchise_key LIMIT 1", (imdb,)).fetchone()
        if r:
            return "fr:" + r[0]
    if t.get("tmdb_id"):
        coll = (_raw(conn, "movie", t["tmdb_id"]) or {}).get("belongs_to_collection")
        if coll:
            return f"coll:{coll['id']}"
    return None


def _fits(conn, key: str, tid: int) -> bool:
    """Can a moved title live in playlist `key`? Дима — comedy as 1st/2nd genre, Яна — a cartoon; the owner's lists — any."""
    g, anim = conn.execute("SELECT genres, is_animation FROM titles WHERE id = ?", (tid,)).fetchone()
    if key == "dima":
        return "комедия" in json.loads(g or "[]")[:2]
    if key == "yana":
        return bool(anim)
    return True


PRECEDENCE = ("dima", "yana", "kostya_want", "probably", "possible")


def all_members(conn) -> dict[str, list[dict]]:
    """Every playlist, with the owner's franchise rules (2026-10-24):
    1) trailers of one franchise sit next to each other (at the place of its best-ranked title, oldest film first);
    2) a franchise spread over several playlists lives where its first film is — sequels move there (if they fit)."""
    lists = {k: members(conn, k)[0] for k in PLAYLISTS}
    # 0) one title — one playlist (owner, 2026-10-24: «в плейлистах не может быть повторений», «не может быть в Костя
    # хочет фильмов, которые есть у Димки или у Яны»): Дима / Яна first, then Костя Хочет → Вероятно → Возможно
    taken = set()
    for k in PRECEDENCE:
        lists[k] = [t for t in lists[k] if t["id"] not in taken]
        taken |= {t["id"] for t in lists[k]}
    gk = {}
    for k, rows in lists.items():
        for t in rows:
            gk[t["id"]] = _group_key(conn, t)
    # 2) home playlist of each franchise = the one holding its earliest film
    home = {}
    for k, rows in lists.items():
        for t in rows:
            g = gk[t["id"]]
            if g and (g not in home or (t["release_date"] or "9999") < home[g][1]):
                home[g] = (k, t["release_date"] or "9999")
    for k in list(lists):
        keep = []
        for t in lists[k]:
            g = gk[t["id"]]
            h = home.get(g, (k,))[0] if g else k
            if h != k and _fits(conn, h, t["id"]) and all(x["id"] != t["id"] for x in lists[h]):
                lists[h].append(t)          # placed by the regrouping below
            else:
                keep.append(t)
        lists[k] = keep
    # 1) regroup: a franchise block at the position of its first (best-ranked) title, oldest film first inside
    vis = visible_order()
    for k, rows in lists.items():
        rows.sort(key=lambda t: vis.get(t["id"], 10 ** 9))
        out, done = [], set()
        for t in rows:
            g = gk[t["id"]]
            if g is None:
                out.append(t)
            elif g not in done:
                done.add(g)
                out += sorted((x for x in rows if gk[x["id"]] == g), key=lambda x: x["release_date"] or "9999")
        lists[k] = out
    return lists

# ------------------------------------------------------------------------------------------- the YouTube target

DESIRED = db.ROOT / "data" / "cache" / "playlists_desired.json"
TRAILER_MIN, TRAILER_MAX = 90, 300   # seconds: a teaser below, a whole film above (owner, 2026-10-24)
REJECTED = db.ROOT / "data" / "cache" / "trailer_rejected.json"


def _vid(url: str | None) -> str | None:
    m = re.search(r"(?:v=|youtu\.be/|shorts/)([\w-]{11})", url or "")
    return m.group(1) if m else None


def desired(conn) -> dict:
    """The only source of what may be put into the YouTube playlists (checked by .claude/hooks/playlist-guard.sh).
    Every rule of the owner is asserted here, not just applied: a violation stops the build."""
    from .seen_survey import have
    lists = all_members(conn)
    got, w = have(conn), watched_ids(conn)
    rejected = {v["url"].split("v=")[-1] for v in json.loads(REJECTED.read_text(encoding="utf-8")).values()} if REJECTED.exists() else set()
    vis = visible_order()
    skip_imdb = _skip_imdb(conn)
    chosen = {cid for cid, tid, imdb in conn.execute("SELECT collection_id, id, imdb_id FROM titles WHERE collection_id IS NOT NULL")
              if tid in w or imdb in got}
    col_of = lambda tid: conn.execute("SELECT collection_id FROM titles WHERE id = ?", (tid,)).fetchone()[0]
    from .sources.youtube import YouTube
    yt = YouTube(log=lambda m: None)
    out, problems, bad_videos = {}, [], []
    out_titles = {}
    for k, rows in lists.items():
        vids, tids = [], []
        for t in rows:
            r = conn.execute("SELECT kind, coalesce(imdb_id, series_imdb_id), trailer_ru_url, trailer_alt_url, trailer_ru_check "
                             "FROM titles WHERE id = ?", (t["id"],)).fetchone()
            teaser_ok = (r[4] or "") == "teaser_only"   # a longer trailer was searched for and not found
            if r[0] != "movie": problems.append((k, t["id"], "series"))
            if r[1] in got: problems.append((k, t["id"], "downloaded/queued"))
            if t["id"] in w: problems.append((k, t["id"], "watched"))
            if t["id"] not in vis: problems.append((k, t["id"], "hidden by the sheet filter"))
            if t["id"] in followed(): problems.append((k, t["id"], "followed franchise / top — no trailer"))
            if r[1] in skip_imdb: problems.append((k, t["id"], "skip_imdb"))
            if col_of(t["id"]) in chosen: problems.append((k, t["id"], "collection chosen by a downloaded/watched part"))
            # the first of RU / fallback trailer that YouTube plays in UA and that is a trailer by length (1:30–5:00:
            # shorter is a teaser, longer is a whole film — owner, 2026-10-24); a title without one waits for a new search
            v = None
            for cand in (_vid(r[2]), _vid(r[3])):
                if not cand or cand in rejected:
                    continue
                info = yt.watch(cand)
                lo = 30 if teaser_ok else TRAILER_MIN
                ok = info.get("status") == "OK" and info.get("length") and lo <= info["length"] <= TRAILER_MAX
                if ok:
                    v = cand
                    break
                bad_videos.append({"title_id": t["id"], "video": cand, "status": info.get("status"), "length": info.get("length")})
            if v and v not in vids:
                vids.append(v)
                tids.append(t["id"])
        out[k] = vids
        out_titles[k] = tids
    seen_t, seen_v = {}, {}
    for k in out:   # no title and no video in two playlists
        for tid, v in zip(out_titles[k], out[k]):
            if tid in seen_t: problems.append((k, tid, f"also in {seen_t[tid]}"))
            if v in seen_v: problems.append((k, tid, f"video {v} also in {seen_v[v]}"))
            seen_t.setdefault(tid, k)
            seen_v.setdefault(v, k)
    if problems:
        raise SystemExit(f"playlist rules violated: {problems[:10]}")
    doc = {"generated_at": datetime.now().isoformat(timespec="seconds"), "playlists": out, "titles": out_titles,
           "bad_videos": bad_videos}
    DESIRED.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    # the exact text to load into the YouTube tab (playlist-guard.sh accepts only this text, while it is fresh)
    DESIRED.with_suffix(".js").write_text("window.__want = " + json.dumps(out) + ";", encoding="utf-8")
    return doc


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--online"]
    tmdb.OFFLINE = "--online" not in sys.argv
    c = db.connect() if "--online" in sys.argv else connect_ro()
    cmd = (args or ["build"])[0]
    if cmd == "build":
        print(json.dumps(build(c), ensure_ascii=False, indent=1))
    elif cmd == "desired":
        d = desired(db.connect())
        print(DESIRED.with_suffix(".js").read_text(encoding="utf-8") if "--js" in args else
              json.dumps({**{k: len(v) for k, v in d["playlists"].items()}, "bad_videos": len(d["bad_videos"])}))
    elif cmd == "show" and len(args) > 1 and args[1] in PLAYLISTS:
        print((OUT / f"{args[1]}.md").read_text(encoding="utf-8"))
    else:
        sys.exit(f"usage: python -X utf8 -m films.playlist_notes [build|desired [--js]|show {'|'.join(PLAYLISTS)}] [--online]")

