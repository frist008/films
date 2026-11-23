"""Batches for the «что из франшиз я видел» survey (R45) in AskUserQuestion: 4 questions × 4 works.

Run: python -X utf8 -m films.seen_survey next        # prints the next batch (JSON) and remembers the cursor
     python -X utf8 -m films.seen_survey answer '<json {imdb_id: true/false}>'   # stores ticks of the last batch

Order: franchises with most seen works first; inside — by year; only films / cartoons / series / cartoon series
(shorts, specials, documentaries later). Each work: Russian title, year, category, a short line «what it is»
(TMDB overview, first sentence) and the top cast — the owner wants to recognise the film. Answers:
data/cache/franchise_seen_answers.json (imdb → seen true/false); seen → mark-watched (owner's words).
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date

from . import db
from .sources import tmdb

SEEN = db.ROOT / "data" / "cache" / "franchise_seen.json"
ANS = db.ROOT / "data" / "cache" / "franchise_seen_answers.json"
CUR = db.ROOT / "data" / "cache" / "franchise_seen_cursor.json"
MAIN = ("фильм", "мультфильм", "сериал", "мультсериал")


def _raw(conn, imdb: str) -> dict:
    f = tmdb.find_by_imdb(conn, imdb)
    return (tmdb.movie_raw(conn, f["movie"]) if f.get("movie") else tmdb.tv_raw(conn, f["tv"]) if f.get("tv") else None) or {}


def _votes(conn, imdb: str) -> int:
    return _raw(conn, imdb).get("vote_count") or 0


def _released(conn, imdb: str) -> bool:
    """Out already (TMDB release / first air date ≤ today); a year alone lets announced 2026 works through
    («Мстители: Доктор Дум», «Гарри Поттер» 2026 were asked)."""
    from datetime import date
    raw = _raw(conn, imdb)
    d = raw.get("release_date") or raw.get("first_air_date")
    if d:
        return d <= date.today().isoformat()
    return imdb in _dataset_ratings()   # no TMDB date: rated on IMDb = released


def have(conn) -> set[str]:
    """IMDb ids that are downloaded (Plex / Jellyfin, present) or queued in qBittorrent: the owner has not seen them
    (he watches and then deletes) — their status is «скачан» / «В очереди», no survey question."""
    from .sources.qbittorrent import queued_titles
    tids = {r[0] for r in conn.execute("SELECT title_id FROM plex_items WHERE present = 1 AND title_id IS NOT NULL")}
    tids |= {r[0] for r in conn.execute("SELECT title_id FROM jellyfin_items WHERE title_id IS NOT NULL")}
    from .disks import linked_title_ids   # every video file on the local drives, known to a media server or not (R85)
    tids |= linked_title_ids(conn)
    tids |= set(queued_titles(conn))
    from .watchlist import title_ids   # the owner's «Хочу» list: he wants to watch them = not seen
    tids |= set(title_ids(conn))
    want = db.ROOT / "data" / "cache" / "kostya_want_answers.json"   # ❤️ survey: playlist / urgent = not seen yet
    if want.exists():
        tids |= {int(k) for k, v in json.loads(want.read_text(encoding="utf-8")).items() if v in ("playlist", "urgent")}
    out = set()
    for tid in tids:
        r = conn.execute("SELECT imdb_id, series_imdb_id FROM titles WHERE id = ?", (tid,)).fetchone()
        if r:
            out |= {x for x in r if x}
    return out


def queue(conn) -> list[tuple[dict, dict]]:
    """Released works only (a year, not in the future), the most popular first inside each franchise (the owner most
    likely saw those) — not the 1940s serials and announced projects first (the first batch did that)."""
    from datetime import date
    ans = json.loads(ANS.read_text(encoding="utf-8")) if ANS.exists() else {}
    seen_imdb = have(conn)   # downloaded / queued = not seen for sure (owner, 2026-10-23) — never asked
    out = []
    import tomllib
    with open(db.ROOT / "config" / "franchises.toml", "rb") as fh:   # «Я так много не готов» (Star Trek, 2026-10-27)
        skip = {fr["key"] for fr in tomllib.load(fh)["franchise"] if fr.get("survey_skip")}
    for f in json.loads(SEEN.read_text(encoding="utf-8")):
        if f.get("key") in skip:
            continue
        works = [w for w in f["works"] if w["state"] != "seen" and w["category"] in MAIN and w.get("year")
                 and w["year"] <= date.today().year and w["imdb_id"] not in ans and w["imdb_id"] not in seen_imdb]
        works = [w for w in works if _released(conn, w["imdb_id"])]
        works.sort(key=lambda w: -_votes(conn, w["imdb_id"]))
        for w in works:
            seen_imdb.add(w["imdb_id"])   # a work in two universes (Marvel + X-Men…) is asked once
            if w["category"] in ("сериал", "мультсериал"):
                out += [(f, s) for s in _seasons(conn, w, ans)]
            else:
                out.append((f, w))
    return out


def _seasons(conn, w: dict, ans: dict) -> list[dict]:
    """A series is asked season by season (the owner, 2026-10-23: «only by seasons — when you ask about a series I
    think you mean season 1»). An old whole-series answer counts for season 1 only. Skips aired-later, watched
    and downloaded seasons."""
    from datetime import date
    from .disks import linked_title_ids
    tv = tmdb.find_by_imdb(conn, w["imdb_id"]).get("tv")
    if not tv:
        return []
    on_disk = linked_title_ids(conn)
    out = []
    for s in (tmdb.tv_raw(conn, tv) or {}).get("seasons", []):
        n, aired = s.get("season_number"), s.get("air_date")
        if not n or not aired or aired > date.today().isoformat():
            continue
        key = f"{w['imdb_id']}#S{n}"
        if key in ans or (n == 1 and w["imdb_id"] in ans):
            continue
        row = conn.execute("SELECT id FROM titles WHERE series_imdb_id = ? AND season_number = ?", (w["imdb_id"], n)).fetchone()
        if row and (conn.execute("""SELECT 1 FROM watched WHERE title_id = ? AND substr(source, -8) != '_partial'
                                    UNION SELECT 1 FROM plex_items WHERE title_id = ? AND present = 1
                                    UNION SELECT 1 FROM jellyfin_items WHERE title_id = ?""",
                                 (row[0], row[0], row[0])).fetchone() or row[0] in on_disk):
            continue   # watched, or downloaded (Plex / Jellyfin / any file on disk — R85): never asked
        out.append({**w, "imdb_id": key, "series_imdb_id": w["imdb_id"], "tv": tv, "season": n,
                    "year": int(aired[:4]), "episodes": s.get("episode_count")})
    return out


_RATINGS: dict | None = None


def _dataset_ratings() -> dict:
    """The whole IMDb title.ratings dataset (imdb_ratings keeps only the 2024+ scope; franchise works are of any year)."""
    global _RATINGS
    if _RATINGS is None:
        import gzip
        _RATINGS = {}
        with gzip.open(db.ROOT / "data" / "cache" / "imdb" / "title.ratings.tsv.gz", "rt", encoding="utf-8") as f:
            next(f)
            for line in f:
                t, r, v = line.split()
                _RATINGS[t] = (float(r), int(v))
    return _RATINGS


def _votes_str(n: int) -> str:
    return f"{n / 1e6:.1f} млн" if n >= 1e6 else f"{n / 1e3:.0f} тыс." if n >= 1e3 else str(n)


def describe(conn, w: dict) -> str:
    if w.get("season"):   # one season of a series
        title, desc = describe(conn, {**w, "imdb_id": w["series_imdb_id"], "season": None})
        bits = desc.split(" · ")
        bits.insert(1, f"сезон {w['season']}" + (f", {w['episodes']} серий" if w.get("episodes") else ""))
        # its own rating, not the series' (owner, 2026-10-27: «И у тебя баг нет оценки», «У каждого сезона своя»)
        sr = season_rating(conn, w["series_imdb_id"], int(w["season"]))
        if sr:   # the series' «IMDb x (y). overview» bit → «IMDb сезона … . overview»
            bits = [re.sub(r"^IMDb (?:—|\d+(?:\.\d+)? \([^)]*\))", sr, b) if b.startswith("IMDb") else b for b in bits]
        return f"{title} — сезон {w['season']}", " · ".join(bits)
    f = tmdb.find_by_imdb(conn, w["imdb_id"])
    body = tmdb.movie(conn, f["movie"]) if f.get("movie") else tmdb.tv(conn, f["tv"]) if f.get("tv") else None
    raw = (tmdb.movie_raw(conn, f["movie"]) if f.get("movie") else tmdb.tv_raw(conn, f["tv"]) if f.get("tv") else None) or {}
    ov = (raw.get("overview") or "").strip()
    if not ov:   # no Russian overview (owner, 2026-10-27: «Хз тут англ названия»): the uk / en one, marked — the question
        tr = {t.get("iso_639_1"): (t.get("data") or {}) for t in (raw.get("translations") or {}).get("translations", [])}
        for lang in ("uk", "en"):   # is written in Russian from it (a retelling of the data, never an invented title)
            if (tr.get(lang) or {}).get("overview"):
                ov = f"[{lang}] " + tr[lang]["overview"].strip()
                break
    ov = re.split(r"(?<=[.!?])\s", ov)[0][:160] if ov else ""
    cast = ", ".join(p["name"] for p in (body or {}).get("cast", [])[:2])
    r = conn.execute("SELECT rating, votes FROM imdb_ratings WHERE tconst = ?", (w["imdb_id"],)).fetchone()         or _dataset_ratings().get(w["imdb_id"])
    bits = [w["category"]]
    # the country makes namesakes clear (Godzilla: американский или японский — the owner, 2026-10-23)
    codes = [c["iso_3166_1"] for c in raw.get("production_countries") or []] or raw.get("origin_country") or []
    if codes:
        bits.append("/".join(tmdb._countries(conn, codes[:2])))
    if cast:
        bits.append(cast)
    # the owner wants IMDb ratings only (2026-10-23) — never TMDB scores in questions
    bits.append(f"IMDb {r[0]:.1f} ({_votes_str(r[1])})" if r else "IMDb —")
    title = (body or {}).get("title_ru") or _release_ru(conn, w["imdb_id"]) or w["title"]
    return title, " · ".join(bits) + (f". {ov}" if ov else "")


SEASON_R = db.ROOT / "data" / "cache" / "survey_season_ratings.json"   # «tt…#S» → [mean, votes] (season-ratings)


def season_rating(conn, series: str, season: int) -> str | None:
    """«IMDb сезона 7.9 (12 тыс.)» (+ RT of the season when known): the season row's imdb_season_rating, else the
    precomputed survey cache (`python -X utf8 -m films.seen_survey season-ratings`)."""
    r = conn.execute("SELECT imdb_season_rating, imdb_season_votes, rt_critics FROM titles WHERE series_imdb_id = ? "
                     "AND season_number = ? AND duplicate_of IS NULL", (series, season)).fetchone()
    mean, votes, rt = (r[0], r[1], r[2]) if r and r[0] else (None, None, r[2] if r else None)
    if mean is None and SEASON_R.exists():
        mean, votes = (json.loads(SEASON_R.read_text(encoding="utf-8")).get(f"{series}#S{season}") or [None, None])
    if mean is None:
        return None
    return f"IMDb сезона {mean:.1f} ({_votes_str(votes or 0)})" + (f", RT {rt}%" if rt is not None else "")


def precompute_season_ratings(conn) -> int:
    from .ratings import season_means
    pairs = {(w["imdb_id"].split("#S")[0], int(w["imdb_id"].split("#S")[1])) for _, w in queue(conn) if "#S" in w["imdb_id"]}
    got = season_means(pairs)
    SEASON_R.write_text(json.dumps({f"{p}#S{s}": v for (p, s), v in got.items()}), encoding="utf-8")
    return len(got)


def _release_ru(conn, imdb: str) -> str | None:
    """No Russian title at TMDB → the Russian part of a tracker release name of that title («RU / EN / год …»; owner,
    2026-10-27: «С чего ты взял что лет русских названий? Вот прямо торрент фулл англ?»)."""
    for (name,) in conn.execute("""SELECT r.name FROM releases r JOIN titles t ON t.id = r.title_id
                                   WHERE t.imdb_id = ? OR t.series_imdb_id = ? ORDER BY r.seeders DESC LIMIT 20""",
                                (imdb, imdb)).fetchall():
        ru = (name or "").split(" / ")[0].strip()
        if re.search(r"[А-Яа-яЁё]", ru):
            return re.sub(r"\s*\((19|20)\d\d\).*$|\s*\[.*$", "", ru)
    return None


def chunks(q: list[tuple[dict, dict]]) -> list[list[tuple[dict, dict]]]:
    """Question-sized pieces of the queue: ONE franchise and one category per question, up to 4 works (owner, 2026-10-27:
    «одна франшиза один вопрос», «В самом вопросе одна франшиза» — the old batches joined «X / Y» in one question; and
    never films with series in one question, 2026-10-23)."""
    # 2026-10-27, later the same day: «давай … по 4 вопроса с 4 вариантами … В вопросе перечислять много нельзя, в ответе по
    # одному. Но в итоге то можно 4 ответа и итого 4 игры, и если ещё и 4 вопроса то сразу 16 медиа покрываем» — so a
    # question = one category, 4 works (each option one work, its franchise named in the option), works of one franchise
    # stay together and in queue order; films and series never share a question
    groups: dict[str, list] = {}
    for f, w in q:
        groups.setdefault(w["category"], []).append((f, w))
    return [items[i:i + 4] for items in groups.values() for i in range(0, len(items), 4)]


def next_batch(conn, n: int = 16, films_only: bool = False) -> list[dict]:
    q = queue(conn)
    if films_only:   # the owner (2026-10-23): series are asked after the full disk inventory (R85)
        q = [(f, w) for f, w in q if w["category"] in ("фильм", "мультфильм")]
    parts = chunks(q)
    batch = []
    for k, part in enumerate(parts[:max(1, n // 4)]):   # 4 questions per AskUserQuestion call
        for f, w in part:
            title, desc = describe(conn, w)
            batch.append({"imdb_id": w["imdb_id"], "franchise": f["name"], "title": title, "year": w["year"],
                          "desc": desc[:240], "q": k})
    CUR.write_text(json.dumps({"batch": batch, "left": len(q), "questions_left": len(parts)}, ensure_ascii=False),
                   encoding="utf-8")
    return batch


QCOUNT = db.ROOT / "data" / "cache" / "franchise_seen_qcount.json"   # questions already answered (for «N/M»)


def questions(batch: list[dict], left: int) -> list[dict]:
    """Ready AskUserQuestion questions: one franchise per question (up to 4 works), «N/M ·», Russian."""
    asked = json.loads(QCOUNT.read_text(encoding="utf-8")).get("asked", 0) if QCOUNT.exists() else 0
    cur = json.loads(CUR.read_text(encoding="utf-8")) if CUR.exists() else {}
    total = asked + (cur.get("questions_left") or (left + 3) // 4)
    out = []
    order = list(dict.fromkeys(b.get("q", i // 4) for i, b in enumerate(batch)))
    for i, k in enumerate(order):
        part = [b for j, b in enumerate(batch) if b.get("q", j // 4) == k]
        cats = sorted({b["desc"].split(" · ")[0] for b in part})
        kinds = " и ".join({"фильм": "фильмы", "мультфильм": "мультфильмы", "сериал": "сериалы",
                            "мультсериал": "мультсериалы"}.get(c, c) for c in cats)
        # each option: one work, its franchise first («X + Y» reads as several franchises — owner, 2026-10-27: «Я думал
        # что поколение v это не часть пацанов» — hence the note)
        def fr_of(b):
            return f"«{b['franchise']}»" + (" (одна франшиза)" if "+" in b["franchise"] else "")
        opts = [{"label": f"{b['title']} ({b['year']})", "description": f"{fr_of(b)} · {b['desc']}"[:200]} for b in part]
        if len(opts) == 1:   # AskUserQuestion needs two options; the owner's «Ничего не видел» (2026-10-24)
            opts.append({"label": "Не видел", "description": "Не отмечать как просмотренное"})
        out.append({"question": f"{asked + i + 1}/{total} · {kinds.capitalize()} франшиз: что из этого ты видел? "
                                f"(не отмечено = не видел; «не интересно» — своим текстом)",
                    "header": kinds[:12], "multiSelect": True, "options": opts})
    return out


def answer(ticks: dict) -> int:
    ans = json.loads(ANS.read_text(encoding="utf-8")) if ANS.exists() else {}
    ans.update(ticks)
    ANS.write_text(json.dumps(ans, ensure_ascii=False, indent=1), encoding="utf-8")
    return len(ans)


def hide_in_config(franchise_name: str, imdb_ids: list[str], words: str) -> str:
    """«Не интересно» in the survey → `hide = [...]` of that franchise in config/franchises.toml (the owner's words in a
    comment). Owner, 2026-10-27: «Если я ранее отвечал что … не интересна, то я её и не видел» — hidden = not seen."""
    p = db.ROOT / "config" / "franchises.toml"
    s = p.read_text(encoding="utf-8")
    m = re.search(r'(?m)^name = "' + re.escape(franchise_name) + r'"\n', s)
    if not m:
        return f"{franchise_name}: франшиза не найдена в config/franchises.toml — скрыть вручную"
    start = s.rfind("[[franchise]]", 0, m.start())
    end = s.find("[[franchise]]", m.end())
    end = len(s) if end < 0 else end
    block = s[start:end]
    h = re.search(r'(?m)^hide = \[([^\]]*)\]', block)
    have = re.findall(r'"(tt\d+)"', h.group(1)) if h else []
    new = [i for i in dict.fromkeys(imdb_ids) if i not in have]
    if not new:
        return f"{franchise_name}: уже скрыто"
    line = "hide = [" + ", ".join(f'"{i}"' for i in have + new) + "]"
    note = f"\n# владелец {date.today()} в опросе «что видел»: «{words}» → скрыто {', '.join(new)}"
    if h:
        block = block[:h.start()] + line + block[h.end():].replace("\n", note + "\n", 1)
    else:
        block = block.rstrip("\n") + note + "\n" + line + "\n\n"
    p.write_text(s[:start] + block + s[end:], encoding="utf-8")
    return f"{franchise_name}: скрыто {', '.join(new)}"


def apply(conn, seen_labels: list[str], unseen_labels: list[str], hidden: dict | None = None) -> list[str]:
    """hidden: {label: the owner's words} — «не интересно»: not seen, hidden in the franchise (never to Яна)."""
    hidden = hidden or {}
    pre = []
    if hidden:
        batch = json.loads(CUR.read_text(encoding="utf-8"))["batch"]
        for lbl, words in hidden.items():
            hits = [b for b in batch if lbl == f"{b['title']} ({b['year']})" or lbl in b["title"]]
            if len(hits) != 1:
                raise SystemExit(f"label not unique / not found: {lbl}: {[h['title'] for h in hits]}")
            pre.append(hide_in_config(hits[0]["franchise"], [hits[0]["imdb_id"].split("#")[0]], words))
    return pre + _apply(conn, seen_labels, [*unseen_labels, *hidden], no_yana=set(hidden))


def _apply(conn, seen_labels: list[str], unseen_labels: list[str], no_yana: set = frozenset()) -> list[str]:
    """Answers of the last batch by option label (title (year), as in `next`): seen → answers + mark-watched in Plex
    (the owner's words), unseen → answers. Labels may be shortened: a unique substring of the batch title is enough."""
    from . import link, watched
    batch = json.loads(CUR.read_text(encoding="utf-8"))["batch"]
    def pick(lbl):
        hits = [b for b in batch if lbl == f"{b['title']} ({b['year']})" or lbl in b["title"]]
        if len(hits) != 1:
            raise SystemExit(f"label not unique / not found: {lbl}: {[h['title'] for h in hits]}")
        return hits[0]
    out = []
    answer({**{pick(x)["imdb_id"]: True for x in seen_labels}, **{pick(x)["imdb_id"]: False for x in unseen_labels}})
    asked = json.loads(QCOUNT.read_text(encoding="utf-8")).get("asked", 0) if QCOUNT.exists() else 0
    # only the questions that got an answer count (a re-asked one, «Хз тут англ названия», is asked again)
    answered = {pick(x).get("q") for x in [*seen_labels, *unseen_labels]}
    QCOUNT.write_text(json.dumps({"asked": asked + len(answered)}), encoding="utf-8")
    from .survey_gates import yana_sync
    out.append(yana_sync(conn, [pick(x) for x in unseen_labels if x not in no_yana], [pick(x) for x in seen_labels]))
    for x in seen_labels:
        w = pick(x)
        if "#S" in w["imdb_id"]:   # one season (the owner answers per season — never mark a whole series from one tick)
            series, sn = w["imdb_id"].split("#S")
            tv = (tmdb.find_by_imdb(conn, series) or {}).get("tv")
            stid = link.ensure_season(conn, tv, int(sn)) if tv else None
            conn.commit()
            if stid:
                r = watched.mark(conn, stid)
                for ep in (tmdb.season_raw(conn, tv, int(sn)) or {}).get("episodes") or []:
                    watched.mark_episode(conn, series, int(sn), ep["episode_number"])
                out.append(f"{w['title']}: отмечен посерийно")
            else:
                out.append(f"{x}: сезон не найден в TMDB — в Plex не отмечено")
            continue
        if w["desc"].startswith(("сериал", "мультсериал")):   # legacy whole-series item: season 1 only (owner, 2026-10-23)
            tv = (tmdb.find_by_imdb(conn, w["imdb_id"]) or {}).get("tv")
            seasons = [1] if tv else []
            for sn in seasons:
                stid = link.ensure_season(conn, tv, sn)
                conn.commit()
                if stid:
                    r = watched.mark(conn, stid)
                    # Discover keeps watched state per episode — the season-level scrobble alone shows nothing
                    n_ep = (tmdb.season_raw(conn, tv, sn) or {}).get("episodes") or []
                    for ep in n_ep:
                        watched.mark_episode(conn, w["imdb_id"], sn, ep["episode_number"])
                    out.append(f"{r['title']} S{sn}: " + ", ".join(f"{p.get('where')} {p.get('viewCount') or p.get('viewState') or p.get('error')}" for p in r["plex"]))
            if not seasons:
                out.append(f"{x}: сезоны не найдены в TMDB — в Plex не отмечено")
            continue
        tid = link.ensure_movie(conn, w["imdb_id"], "survey_seen")
        conn.commit()
        if not tid:   # TMDB has no movie for this IMDb id (TV movie / pilot / fan film) — the answer is stored anyway
            out.append(f"{x}: нет в TMDB как фильм — в Plex не отмечено")
            continue
        r = watched.mark(conn, tid)
        out.append(f"{r['title']}: " + ", ".join(f"{p.get('where')} {p.get('viewCount') or p.get('error')}" for p in r["plex"]))
    return out


if __name__ == "__main__":
    c = db.connect()
    if sys.argv[1] == "next":
        b = next_batch(c, films_only="films" in sys.argv[2:])
        left = json.loads(CUR.read_text(encoding="utf-8"))["left"]
        print(json.dumps({"left": left, "questions": questions(b, left),
                          "ids": {f"{x['title']} ({x['year']})": x["imdb_id"] for x in b}}, ensure_ascii=False, indent=1))
    elif sys.argv[1] == "season-ratings":   # per-season IMDb means for the survey's seasons (datasets, ~1–2 min)
        print(precompute_season_ratings(c))
    elif sys.argv[1] == "gates":
        print(json.dumps(gates(c), ensure_ascii=False, indent=1))
    elif sys.argv[1] == "gate":   # gate KEY ask|films_only|hide
        set_gate(sys.argv[2], sys.argv[3])
    elif sys.argv[1] == "apply":   # apply '<json {"seen": [labels], "unseen": [labels]}>'
        a = json.loads(sys.argv[2])
        for line in apply(c, a.get("seen", []), a.get("unseen", []), a.get("hidden")):   # hidden: {label: words}
            print(line)
    else:
        print(answer(json.loads(sys.argv[2])))
