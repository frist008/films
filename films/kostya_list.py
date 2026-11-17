"""The owner's own list 2021–2025 (R54): config/kostya_list_raw.txt — «date<TAB>marker+text», one per line.

Run: python -X utf8 -m films.kostya_list [resolve|report]

Markers (owner, 2026-10-23):
  ❤️ / 🔴 / ❤️❤️❤️ / ❤️💛 → «want»     — playlist «Костя Хочет»; a checkbox survey decides which go to the playlist,
                                         the ones NOT ticked go to the very top of the sheet as «срочно скачать»
  💛 / 🟡              → «maybe»    — playlist «Вероятно хочу»
  (none)              → «possible» — playlist «Возможно»; in the sheet a 🔵 blue dot
  🧞‍♀                  → «order»    — someone ordered it; always at the very top
  🧸                  → Yana's cartoons (config/yana.toml)
The sheet shows the same marker next to these titles.

resolve: every line → TMDB (film first, then series; «RU / EN» names split; a year in the text is used; notes like
«комедия», «детектив», «90%», «8/10» are dropped). Ambiguous names are decided by the year in the text or by TMDB votes
(×5 rule); the rest stays ambiguous for the owner. Result: data/cache/kostya_list_map.json (keyed by the raw line;
'manual' entries are kept) and a title for every match (ensure_movie / ensure_season).
"""
from __future__ import annotations

import json
import re
import sys

from . import db
from .sources import tmdb

RAW = db.ROOT / "config" / "kostya_list_raw.txt"
MAP = db.ROOT / "data" / "cache" / "kostya_list_map.json"

MARKERS = [("❤️❤️❤️", "want"), ("❤️💛", "want"), ("❤️", "want"), ("🔴", "want"), ("💛", "maybe"), ("🟡", "maybe"),
           ("🧞‍♀", "order"), ("🧞", "order"), ("🧸", "yana")]
SYMBOL = {"want": "❤️", "maybe": "💛", "possible": "🔵", "order": "🧞‍♀", "yana": "🧸"}
_NOTES = re.compile(r"\b(?:комедия|комедии|детектив|ужас|ужасы|слешер|боевик|триллер|фильм|аниме|трилогия|imax|бафта|"
                    r"золотой глобус|оценка \d+|игра актеров|про баб|об ии сексе|популярный на торренте|советы торрентов|"
                    r"если понравится.*|один из глобуса|пародия начало|стивен кинг|тарантино|кейдж|роб зомби|"
                    r"\d+(?:[.,]\d+)?\s*(?:%|/10)|\d+-\d+%|покорна|попкорна|томатов|ублюдок)\b", re.I)


def entries() -> list[dict]:
    out = []
    for line in RAW.read_text(encoding="utf-8").splitlines():
        if not line.strip() or "\t" not in line:
            continue
        date_, text = line.split("\t", 1)
        marker = "possible"
        t = text.strip()
        for sym, m in MARKERS:
            if t.startswith(sym):
                marker = m
                t = t[len(sym):].strip()
                break
        out.append({"raw": line, "date": date_, "marker": marker, "text": t})
    return out


def _query(text: str) -> tuple[list[str], int | None]:
    t = text
    if t.startswith("http") or t.startswith("[заметка"):
        return [], None
    years = re.findall(r"\b(19[5-9]\d|20[0-3]\d)\b", t)
    year = int(years[0]) if years else None
    t = re.sub(r"\(.*?\)", " ", t)
    t = _NOTES.sub(" ", t)
    t = re.sub(r"\b(19[5-9]\d|20[0-3]\d)\b", " ", t)
    t = re.sub(r"\d+\s*[-–]\s*\d+\s*%|\d+\s*©|[\d.,]+\s*%|%|©|\b\d+[.,]\d+\b|\b\d+/\d+\b", " ", t)
    names = []
    for n in re.split(r"\s/\s|\s\\\s", t):
        n = re.sub(r"\s+", " ", n).strip(" -–—.,")
        names.append(n)
        # trailing «87 90» may be scores — or part of the title («Тюрьма 77», «The 355»): search both
        short = re.sub(r"(?:\s\d{2,3}){1,2}$", "", n).strip()
        if short != n:
            names.append(short)
    return [n for n in dict.fromkeys(names) if len(n) >= 2], year


def resolve(conn) -> dict:
    from .link import _popular_before, ensure_movie, ensure_season
    cur = json.loads(MAP.read_text(encoding="utf-8")) if MAP.exists() else {}
    stats = {"lines": 0, "matched": 0, "ambiguous": 0, "not_found": 0, "skipped": 0}
    for e in entries():
        stats["lines"] += 1
        if cur.get(e["raw"], {}).get("status") == "manual":
            stats["matched"] += 1
            continue
        names, year = _query(e["text"])
        if not names:
            cur[e["raw"]] = {"status": "skipped", "marker": e["marker"]}
            stats["skipped"] += 1
            continue
        m = {"status": "not_found", "marker": e["marker"], "names": names, "year": year}
        for kind in ("movie", "tv"):
            res = tmdb.search(conn, names, year, kind)
            best = res.get("best") if res["status"] == "matched" else None
            if not best and res["status"] == "ambiguous":
                if year:
                    same = [c for c in res.get("candidates") or [] if c.get("year") == year and (c.get("score") or 0) >= 0.75]
                    best = same[0] if len(same) == 1 else None
                if not best:
                    best = _popular_before(conn, res, "2099") if kind == "tv" else None
                    if not best and kind == "movie":
                        raw = [((tmdb.movie_raw(conn, c["tmdb_id"]) or {}).get("vote_count") or 0, c)
                               for c in res.get("candidates") or [] if (c.get("score") or 0) >= 0.9]
                        raw.sort(key=lambda x: -x[0])
                        best = raw[0][1] if raw and raw[0][0] >= 50 and (len(raw) == 1 or raw[0][0] >= 5 * raw[1][0]) else None
            if best:
                m.update(status="matched" if res["status"] == "matched" else "auto", kind=kind, tmdb_id=best["tmdb_id"],
                         title=best.get("title"), year_found=best.get("year"))
                break
            if res["status"] == "ambiguous" and m["status"] == "not_found":
                m.update(status="ambiguous", kind=kind, candidates=[{k: c.get(k) for k in ("tmdb_id", "title", "original_title", "year")}
                                                                   for c in (res.get("candidates") or [])[:5]])
        if m.get("tmdb_id"):
            with conn:
                if m["kind"] == "movie":
                    mv = tmdb.movie(conn, m["tmdb_id"]) or {}
                    tid = ensure_movie(conn, mv["imdb_id"], "kostya_list") if mv.get("imdb_id") else None
                    m["title_ids"] = [tid] if tid else []
                else:
                    tv = tmdb.tv(conn, m["tmdb_id"]) or {}
                    m["title_ids"] = [t for t in (ensure_season(conn, m["tmdb_id"], s["season_number"])
                                                  for s in tv.get("seasons") or [] if s.get("season_number")) if t]
        stats[m["status"] if m["status"] in ("ambiguous", "not_found") else "matched"] += 1
        cur[e["raw"]] = m
        MAP.parent.mkdir(parents=True, exist_ok=True)
        MAP.write_text(json.dumps(cur, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"  {stats['lines']}: {e['text'][:40]} → {m['status']} {m.get('title') or ''}", file=sys.stderr, flush=True)
    db.mark_sync(conn, "kostya_list", None, stats)
    return stats


def markers_by_title() -> dict[int, str]:
    """title_id → marker (the strongest when a title is listed several times)."""
    if not MAP.exists():
        return {}
    rank = {"order": 0, "want": 1, "yana": 2, "maybe": 3, "possible": 4}
    out: dict[int, str] = {}
    for m in json.loads(MAP.read_text(encoding="utf-8")).values():
        for t in m.get("title_ids") or []:
            if t not in out or rank[m["marker"]] < rank[out[t]]:
                out[t] = m["marker"]
    return out


# ------------------------------------------------------------------------------------------- rutor check
# The owner copied the names from rutor release titles (sometimes RT), and the message date is when the release
# was on rutor (a film released ~6–9 months before). So every line is checked against rutor: releases found by
# the name, added within [date − 18 months, date + 1 month]; their «RU / EN (year)» must agree with the match.

def _release_names(name: str) -> tuple[list[str], int | None]:
    head = re.split(r"\s(?:WEB|BDRip|BDRemux|HDRip|DVDRip|UHD|HDTV|TVRip|WEBRip|Blu-?Ray|DLRip)\b", name, 1)[0]
    m = re.search(r"\((\d{4})(?:-\d{4})?\)", head)
    year = int(m.group(1)) if m else None
    head = re.sub(r"\[.*?\]|\(.*?\)", " ", head)
    return [n.strip() for n in head.split(" / ") if n.strip()], year


def verify(conn, only: list[str] | None = None, log=print) -> dict:
    from datetime import date, timedelta
    from .sources import rutor
    cur = json.loads(MAP.read_text(encoding="utf-8"))
    stats = {"checked": 0, "confirmed": 0, "mismatch": 0, "no_rutor": 0, "fixed_unresolved": 0}
    for raw, m in cur.items():
        if only and raw not in only:
            continue
        names, year = _query(raw.split("\t", 1)[1])
        if not names:
            continue
        d0 = date.fromisoformat(raw.split("\t", 1)[0])
        lo, hi = d0 - timedelta(days=548), d0 + timedelta(days=31)
        rows, all_rows = [], []
        for cat in (1, 4, 7, 10):
            try:
                _, found = rutor.search_all(cat, names[0])
            except Exception as e:  # noqa: BLE001
                log(f"  rutor error {names[0]}: {e}")
                continue
            all_rows += found
            rows += [r for r in found if lo <= date.fromisoformat(r["added_at"]) <= hi]
        if not rows and m.get("title_ids"):
            # an older film listed later (Сноуден 2016 in a 2021 message): any date, but the matched year must agree
            ys = {r[0] for r in conn.execute(
                f"SELECT year FROM titles WHERE id IN ({','.join('?' * len(m['title_ids']))})", m["title_ids"])}
            rows = [r for r in all_rows if (_release_names(r["name"])[1] or 0) in ys]
        stats["checked"] += 1
        cands = {}
        for r in rows:
            rn, ry = _release_names(r["name"])
            if ry and (not year or abs(ry - year) <= 1):
                cands.setdefault((tuple(rn), ry), []).append(r["added_at"])
        m["rutor"] = [{"names": list(k[0]), "year": k[1], "added": sorted(v)[:3]} for k, v in cands.items()][:6]
        if not cands:
            m["rutor_check"] = "no_rutor"
            stats["no_rutor"] += 1
            continue
        # does any rutor release name the matched film (same year ±1 and a name TMDB knows for it)?
        ok = False
        for tid in m.get("title_ids") or []:
            t = conn.execute("SELECT title_ru, title_original, title_latin, year FROM titles WHERE id = ?", (tid,)).fetchone()
            if not t:
                continue
            tn = {x.lower() for x in (t["title_ru"], t["title_original"], t["title_latin"]) if x}
            for (rn, ry) in cands:
                if t["year"] and abs(ry - t["year"]) <= 1 and any(n.lower() in tn for n in rn):
                    ok = True
        if ok:
            m["rutor_check"] = "confirmed"
            stats["confirmed"] += 1
            continue
        # the rutor release points elsewhere (or the line was unresolved): resolve by the release's own names
        (rn, ry), _ = max(cands.items(), key=lambda kv: len(kv[1]))
        res = tmdb.search(conn, list(rn), ry, "movie")
        kind = "movie"
        if res["status"] != "matched":
            res, kind = tmdb.search(conn, list(rn), ry, "tv"), "tv"
        m["rutor_check"] = "mismatch" if m.get("title_ids") else "resolved_by_rutor"
        m["rutor_suggest"] = {"names": list(rn), "year": ry, "status": res["status"],
                              "tmdb_id": (res.get("best") or {}).get("tmdb_id"), "kind": kind,
                              "title": (res.get("best") or {}).get("title")}
        stats["mismatch" if m.get("title_ids") else "fixed_unresolved"] += 1
        log(f"  {raw.split(chr(9),1)[1][:40]} → rutor: {' / '.join(rn)} ({ry}) [{m['rutor_check']}]")
        MAP.write_text(json.dumps(cur, ensure_ascii=False, indent=1), encoding="utf-8")
    MAP.write_text(json.dumps(cur, ensure_ascii=False, indent=1), encoding="utf-8")
    db.mark_sync(conn, "kostya_list_rutor_check", None, stats)
    return stats


def releases(conn, log=print) -> dict:
    """rutor search, all years, for every title of the owner's list (and Dima's / Yana's manual lists): original and
    Russian names in foreign films / series / animation (+ anime for anime series); then films rutor pages/parse
    and films match link them."""
    from .sources import rutor
    ids = set(markers_by_title())
    ids |= {r[0] for r in conn.execute("SELECT title_id FROM responsibility WHERE source = 'manual'")}
    have = {r[0] for r in conn.execute("SELECT DISTINCT title_id FROM release_titles")}
    stats = {"titles": 0, "queries": 0, "new": 0}
    done = set()
    for tid in sorted(ids - have):
        t = conn.execute("SELECT * FROM titles WHERE id = ?", (tid,)).fetchone()
        if not t:
            continue
        stats["titles"] += 1
        cats = [(4, "foreign_series"), (7, "animation"), (10, "anime")] if t["kind"] == "series" else                [(1, "foreign_films"), (7, "animation")]
        for q in dict.fromkeys(x for x in (t["title_original"], t["title_ru"]) if x and len(x) >= 3):
            if (q, t["kind"]) in done:
                continue
            done.add((q, t["kind"]))
            for cat, category in cats:
                _, rows = rutor.search_all(cat, q)
                stats["queries"] += 1
                stats["new"] += rutor.upsert(conn, rows, category)
        log(f"  {stats['titles']}: {t['title_ru']} {t['year']} → {stats}")
    db.mark_sync(conn, "kostya_list_releases", None, stats)
    return stats


def mark_relevant(conn, log=print) -> dict:
    """After a broad name search: keep pages only for listing rows that name a wanted title — one of its names
    (ru / original / latin, normalized) equal to one of the release's names, year ±1 (series: any season year).
    The rest get releases.page_skip = 1 (rows are kept, their pages are not downloaded)."""
    def norm(x):
        return re.sub(r"[^0-9a-zа-я]+", " ", (x or "").lower().replace("ё", "е")).strip()
    ids = set(markers_by_title()) | set(watchlist_ids(conn))
    ids |= {r[0] for r in conn.execute("SELECT title_id FROM responsibility WHERE source = 'manual'")}
    want = {}
    for t in conn.execute(f"SELECT * FROM titles WHERE id IN ({','.join('?' * len(ids))})", list(ids)):
        for n in (t["title_ru"], t["title_original"], t["title_latin"]):
            if n:
                want.setdefault(norm(n), set()).add((t["year"], t["kind"]))
    rows = conn.execute("""SELECT r.id, r.name FROM releases r LEFT JOIN release_raw w ON w.release_id = r.id
                           WHERE r.source = 'rutor' AND w.release_id IS NULL AND r.page_skip = 0""").fetchall()
    keep = skip = 0
    with conn:
        for rid, name in rows:
            names, year = _release_names(name)
            ok = any(norm(n) in want and any(k == "series" or not y or not year or abs(y - year) <= 1
                                             for y, k in want[norm(n)]) for n in names)
            if ok:
                keep += 1
            else:
                conn.execute("UPDATE releases SET page_skip = 1 WHERE id = ?", (rid,))
                skip += 1
    log(f"  relevant {keep}, skipped {skip}")
    return {"keep": keep, "skip": skip}


def watchlist_ids(conn) -> set[int]:
    from . import watchlist
    return watchlist.title_ids()


if __name__ == "__main__":
    c = db.connect()
    db.migrate(c)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "resolve"
    lg = lambda x: print(x, file=sys.stderr, flush=True)  # noqa: E731
    out = verify(c, log=lg) if cmd == "verify" else releases(c, log=lg) if cmd == "releases" else         mark_relevant(c, log=lg) if cmd == "relevant" else resolve(c)
    print(json.dumps(out, ensure_ascii=False, indent=1))
