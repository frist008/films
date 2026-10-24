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
    for f in json.loads(SEEN.read_text(encoding="utf-8")):
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
        return f"{title} — сезон {w['season']}", " · ".join(bits)
    f = tmdb.find_by_imdb(conn, w["imdb_id"])
    body = tmdb.movie(conn, f["movie"]) if f.get("movie") else tmdb.tv(conn, f["tv"]) if f.get("tv") else None
    raw = (tmdb.movie_raw(conn, f["movie"]) if f.get("movie") else tmdb.tv_raw(conn, f["tv"]) if f.get("tv") else None) or {}
    ov = (raw.get("overview") or "").strip()
    ov = re.split(r"(?<=[.!?])\s", ov)[0][:110] if ov else ""
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
    title = (body or {}).get("title_ru") or w["title"]
    return title, " · ".join(bits) + (f". {ov}" if ov else "")


def next_batch(conn, n: int = 16, films_only: bool = False) -> list[dict]:
    q = queue(conn)
    if films_only:   # the owner (2026-10-23): series are asked after the full disk inventory (R85)
        q = [(f, w) for f, w in q if w["category"] in ("фильм", "мультфильм")]
    # the owner: never mix series and films in one question → the batch is grouped by category (4 per question)
    head = q[:n * 3]
    groups: dict[str, list] = {}
    for f, w in head:
        groups.setdefault(w["category"], []).append((f, w))
    picked = []
    for cat in sorted(groups, key=lambda c: head.index(groups[c][0])):
        items = groups[cat]
        picked += items[:len(items) // 4 * 4] or items
        if len(picked) >= n:
            break
    batch = []
    for f, w in picked[:n]:
        title, desc = describe(conn, w)
        batch.append({"imdb_id": w["imdb_id"], "franchise": f["name"], "title": title, "year": w["year"],
                      "desc": desc[:240]})
    CUR.write_text(json.dumps({"batch": batch, "left": len(q)}, ensure_ascii=False), encoding="utf-8")
    return batch


def questions(batch: list[dict], left: int) -> list[dict]:
    """Ready AskUserQuestion questions: 4 × 4, «N/M ·», franchise + categories in the text, Russian."""
    done = len(json.loads(ANS.read_text(encoding="utf-8"))) if ANS.exists() else 0
    first, total = done // 4 + 1, (done + left + 3) // 4
    out = []
    for i in range(0, len(batch), 4):
        part = batch[i:i + 4]
        cats = sorted({b["desc"].split(" · ")[0] for b in part})
        kinds = " и ".join({"фильм": "фильмы", "мультфильм": "мультфильмы", "сериал": "сериалы",
                            "мультсериал": "мультсериалы"}.get(c, c) for c in cats)
        fr = " / ".join(dict.fromkeys(b["franchise"] for b in part))
        out.append({"question": f"{first + i // 4}/{total} · {fr} — {kinds}. Что из этого ты видел?",
                    "header": fr[:12], "multiSelect": True,
                    "options": [{"label": f"{b['title']} ({b['year']})", "description": b["desc"][:200]} for b in part]})
    return out


def answer(ticks: dict) -> int:
    ans = json.loads(ANS.read_text(encoding="utf-8")) if ANS.exists() else {}
    ans.update(ticks)
    ANS.write_text(json.dumps(ans, ensure_ascii=False, indent=1), encoding="utf-8")
    return len(ans)


def apply(conn, seen_labels: list[str], unseen_labels: list[str]) -> list[str]:
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
    from .survey_gates import yana_sync
    out.append(yana_sync(conn, [pick(x) for x in unseen_labels], [pick(x) for x in seen_labels]))
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
    elif sys.argv[1] == "gates":
        print(json.dumps(gates(c), ensure_ascii=False, indent=1))
    elif sys.argv[1] == "gate":   # gate KEY ask|films_only|hide
        set_gate(sys.argv[2], sys.argv[3])
    elif sys.argv[1] == "apply":   # apply '<json {"seen": [labels], "unseen": [labels]}>'
        a = json.loads(sys.argv[2])
        for line in apply(c, a.get("seen", []), a.get("unseen", [])):
            print(line)
    else:
        print(answer(json.loads(sys.argv[2])))
