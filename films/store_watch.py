"""Watched films → stores (R94, the owner 2026-10-24): price / availability on YouTube movies and the Google TV watchlist.

Browser side (Chrome, owner's account): the YouTube tab searches every watched-but-not-owned film (movie filter, hl en/ru/uk),
opens the candidates' watch pages (languages, «Это платное видео» / OK = owned) and get_offers (prices) — read only;
the google.com tab adds the films to the Google TV watchlist through films/browser/gtv_watchlist_guard.js.
Both export compact JSON which is ingested here:

  python -X utf8 -m films.store_watch youtube <file.json>   # [{id, infos:[{vid, st, lang, offers, t, date, dur}]}]
  python -X utf8 -m films.store_watch gtv <file.json>       # [{id, state, action, verified, title, sub}]

Version choice for buying / the wishlist (CLAUDE.md, 2026-10-23): Russian audio first, then Ukrainian, then any.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone

from . import db
from .sources import stores

LANG = {"русский": "ru", "украинский": "uk", "английский": "en", "немецкий": "de", "французский": "fr", "испанский": "es",
        "итальянский": "it", "японский": "ja", "польский": "pl", "португальский": "pt", "китайский": "zh", "корейский": "ko",
        "турецкий": "tr", "чешский": "cs", "венгерский": "hu", "хинди": "hi", "тайский": "th"}


def _langs(s: str) -> list[str]:
    out = []
    for part in re.split(r"[,;]\s*", s or ""):
        p = part.strip().lower()
        if not p:
            continue
        base = p.split(" (")[0]
        out.append(LANG.get(base, base))
    return sorted(set(out))


def _offers(s: str | None) -> list[dict]:
    """'Купить: Высо 676,00 грн.; Стан 400,00 грн. | Взять напрокат: Высо 133,00 грн.  93,00 грн.' -> [{type, q, price}]."""
    out = []
    for sec in (s or "").split(" | "):
        if ": " not in sec:
            continue
        kind, rest = sec.split(": ", 1)
        for o in rest.split("; "):
            q, _, price = o.partition(" ")
            out.append({"type": kind, "q": q, "price": price})
    return out


def _rank(i: dict) -> tuple:
    la = i["audio_langs"]
    return (0 if "ru" in la else 1 if "uk" in la else 2, i["price"] if i["price"] is not None else 1e9)


def ingest_youtube(rows: list[dict], conn=None, year_check: bool = True) -> dict:
    conn = conn or db.connect()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    items, st = [], {"titles": 0, "found": 0, "buyable": 0, "owned": 0, "absent": 0, "ru": 0, "uk": 0, "wrong_year": 0}
    for r in rows:
        st["titles"] += 1
        year = (conn.execute("SELECT year FROM titles WHERE id = ?", (r["id"],)).fetchone() or [None])[0]
        cands = []
        for i in r.get("infos") or []:
            if not i.get("vid") or i.get("err"):
                continue
            # title search alone confuses the parts of a series (Bad Boys → Bad Boys for Life): the release year of the
            # watch page («Дата выхода») must be the film's year ±1
            y = re.search(r"\b(19|20)\d\d\b", i.get("date") or "")
            if year_check and year and y and abs(int(y.group(0)) - year) > 1:
                st["wrong_year"] += 1
                continue
            owned = (i.get("st") or "").startswith("OK") and bool(i.get("got"))   # OK without «Приобретено» = free
            paid = "платное" in (i.get("st") or "")
            free = (i.get("st") or "").startswith("OK") and not i.get("got")   # free with ads
            offers = _offers(i.get("offers"))
            price, cur, disc = stores.best_buy(offers)
            cands.append({"vid": i["vid"], "owned": owned, "free": free, "available": owned or paid or free, "price": price, "currency": cur,
                          "discount_pct": disc, "audio_langs": _langs(i.get("lang")), "offers": offers,
                          "title": i.get("t"), "release": i.get("date"), "duration": i.get("dur"), "acquired": i.get("got")})
        buyable = [c for c in cands if c["available"]]
        pref = min(buyable, key=_rank)["vid"] if buyable else None
        if cands:
            st["found"] += 1
        if any(c["owned"] for c in cands):
            st["owned"] += 1
        if buyable:
            st["buyable"] += 1
            la = min(buyable, key=_rank)["audio_langs"]
            if "ru" in la:
                st["ru"] += 1
            elif "uk" in la:
                st["uk"] += 1
        for c in cands:
            items.append({"store": "youtube", "item_id": c["vid"], "title": c["title"], "title_id": r["id"],
                          "url": f"https://www.youtube.com/watch?v={c['vid']}", "owned": 1 if c["owned"] else 0,
                          "available": 1 if c["available"] else 0, "price": c["price"], "currency": c["currency"],
                          "discount_pct": c["discount_pct"],
                          "raw": {"audio_langs": c["audio_langs"], "offers": c["offers"], "release": c["release"],
                                  "duration": c["duration"], "acquired": c["acquired"] or None,
                                  "preferred": c["vid"] == pref, "free": c["free"] or None, "found_by": f"store_watch {now[:10]} (watched, not owned)"}})
        if not cands:
            st["absent"] += 1
            items.append({"store": "youtube", "item_id": f"none:{r['id']}", "title_id": r["id"], "available": 0, "owned": 0,
                          "raw": {"availability_reason": "не найден в поиске фильмов YouTube (UA) по оригинальному/RU/UK названию — "
                                                         "«не найдено» ≠ «не продаётся»", "found_by": f"store_watch {now[:10]}"}})
    st["upsert"] = stores.upsert(items, conn)
    _link(conn, items)
    return st


def _link(conn, items: list[dict]):
    """stores.upsert does not write title_id, and `films link stores` recomputes it on every run from raw.imdb_ids (else a
    TMDB name search) — so the match made here (title + year/runtime) is pinned as raw.imdb_ids (store-sync skill, step 6).
    Overrides a previous link: 2026-10-23 bound the bought Inception to the Georgian «Начало» (2020) by its RU title."""
    with conn:
        for it in items:
            imdb = (conn.execute("SELECT imdb_id FROM titles WHERE id = ?", (it["title_id"],)).fetchone() or [None])[0]
            row = conn.execute("SELECT raw FROM store_items WHERE store = ? AND item_id = ?", (it["store"], str(it["item_id"]))).fetchone()
            raw = json.loads(row[0] or "{}") if row else {}
            if imdb:
                raw["imdb_ids"] = [imdb]
                raw["imdb_source"] = "store_watch 2026-10-24 (title + year/runtime checked)"
            conn.execute("UPDATE store_items SET title_id = ?, raw = ? WHERE store = ? AND item_id = ?",
                         (it["title_id"], json.dumps(raw, ensure_ascii=False, sort_keys=True), it["store"], str(it["item_id"])))


def ingest_gtv(rows: list[dict], conn=None) -> dict:
    conn = conn or db.connect()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    items, st = [], {"added": 0, "already": 0, "unverified": 0, "nopanel": 0, "other": 0}
    for r in rows:
        if r.get("action") == "added" and r.get("verified"):
            st["added"] += 1
            how = f"added by gtv_watchlist_guard {now[:10]}"
        elif r.get("state") == "in":
            st["already"] += 1
            how = "already in the Google TV watchlist (button state)"
        else:
            st["unverified" if r.get("action") == "added" else "nopanel" if r.get("state") == "nopanel" else "other"] += 1
            continue
        items.append({"store": "google_tv", "item_id": f"title:{r['id']}", "title": r.get("title"), "title_id": r["id"],
                      "in_wishlist": 1, "raw": {"how": how, "card_subtitle": r.get("sub"), "query": r.get("q")}})
    st["upsert"] = stores.upsert(items, conn)
    _link(conn, items)
    return st


def _norm(s: str | None) -> str:
    s = (s or "").lower().replace("ё", "е")
    s = re.sub(r"^007:\s*", "", s)
    return re.sub(r"\s+", " ", re.sub(r"[«»\"'’`:;,.!?()\[\]\-–—·½⅓]", " ", s)).strip()


def ingest_lines(path: str, conn=None, todo: str | None = None) -> dict:
    """Compact export of the YouTube tab (the javascript_tool output is cut at ~1000 chars):
    `title_id|vid,S,langs,buy,buy_old,rent,yt_year,acquired,e|y,minutes;…` — S: O playable (owned when acquired=1,
    else free), P paid, N unavailable; langs ru+uk+en(+x other); e = title equals one of the film's names, y = only the
    year matched. Matching rule (2026-10-24, after Bad Boys → Bad Boys for Life, Saw II → Saw, The Grudge 2004 →
    2019, Naked Gun 1988 → 2025): exact title + YouTube year ±1, or exact title + runtime ±1 min when YouTube shows a
    LATER year (re-release: Inception «2020») and the DB has no same-named film of that year (a remake);
    year-only match + runtime ±5 min."""
    conn = conn or db.connect()
    rows = []
    for line in open(path, encoding="utf-8").read().splitlines():
        if "|" not in line:
            continue
        tid, rest = line.split("|", 1)
        tid = int(tid)
        t = conn.execute("SELECT year, runtime_min, title_original, title_ru, title_uk FROM titles WHERE id = ?", (tid,)).fetchone()
        names = {_norm(n) for n in t[2:] if n}
        infos = []
        for c in rest.split(";"):
            vid, s, langs, buy, buy_old, rent, yy, got, how, mins = c.split(",")
            yy, mins = int(yy or 0), int(mins or 0)
            dy = abs(yy - t[0]) if yy and t[0] else None
            dd = abs(mins - t[1]) if mins and t[1] else None
            if how == "e":
                ok = (dy is not None and dy <= 1) or (dd is not None and dd <= 1 and yy > (t[0] or 0))
                if ok and dy and dy > 1:
                    namesake = conn.execute("SELECT id, title_original, title_ru, title_uk, runtime_min FROM titles WHERE "
                                            "kind = 'movie' AND id != ? AND year BETWEEN ? AND ?", (tid, yy - 1, yy + 1)).fetchall()
                    # a same-named film of the YouTube year that also fits the runtime is that film (Naked Gun 2025);
                    # one that does not fit (the Georgian «Начало» 2020, 125 min, vs Inception 148) is no rival
                    if any(names & {_norm(n) for n in r[1:4] if n} and (not r[4] or not mins or abs(r[4] - mins) <= 3)
                           for r in namesake):
                        ok = False
            else:
                ok = dy == 0 and (dd is None or dd <= 5)
            if not ok:
                continue
            la = {"ru": "русский", "uk": "украинский", "en": "английский", "x": "другие"}
            offers = ([{"type": "Купить", "q": "", "price": f"{buy_old},00 грн. {buy},00 грн." if buy_old else f"{buy},00 грн."}] if buy else []) + \
                     ([{"type": "Взять напрокат", "q": "", "price": f"{rent},00 грн."}] if rent else [])
            infos.append({"vid": vid, "st": "OK:" if s == "O" else "UNPLAYABLE:Это платное видео" if s == "P" else "UNPLAYABLE:",
                          "lang": ", ".join(la[x] for x in langs.split("+") if x in la), "date": str(yy or ""),
                          "got": "yes" if got == "1" else "", "dur": f"{mins // 60}:{mins % 60:02d}:00" if mins else "",
                          "offers": " | ".join(f"{o['type']}: {o['q']} {o['price']}" for o in offers)})
        rows.append({"id": tid, "infos": infos})
    if todo:   # films searched but with no candidate line → «not found in the store» rows
        have = {r["id"] for r in rows}
        rows += [{"id": o["title_id"], "infos": []} for o in json.load(open(todo, encoding="utf-8")) if o["title_id"] not in have]
    return ingest_youtube(rows, conn, year_check=False)


# ------------------------------------------------------------------------------------- «Купить» (owner, 2026-10-24)
QUALITY_ORDER = ("uhd", "hd", "sd")   # the best available quality decides the price; rent and worse qualities do not matter


def ingest_buy(path: str, conn=None) -> dict:
    """Buy prices per quality from the YouTube tab, one video per line: `vid ru+en uhd:480:600 hd:377 sd:230`
    (price[:old price]; `=`/`;` were cut by the browser tool's output filter as query strings)."""
    conn = conn or db.connect()
    st = {"videos": 0, "with_price": 0}
    with conn:
        for line in open(path, encoding="utf-8").read().splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            vid, langs, qs = parts[0], parts[1].strip("-"), parts[2:]
            buy = {}
            for part in qs:
                k, *v = part.split(":")
                p = [float(x) for x in v]
                buy[k] = {"price": p[0], **({"old_price": p[1]} if len(p) > 1 and p[1] > p[0] else {})}
            best = next((q for q in QUALITY_ORDER if q in buy), None)
            row = conn.execute("SELECT raw FROM store_items WHERE store = 'youtube' AND item_id = ?", (vid,)).fetchone()
            if not row:
                continue
            raw = json.loads(row[0] or "{}")
            raw.update({"buy_by_quality": buy, "best_quality": best, "checked_buy": datetime.now(timezone.utc).strftime("%Y-%m-%d")})
            if langs:
                raw["audio_langs"] = sorted(set(langs.split("+")))
            price = buy[best]["price"] if best else None
            old = buy[best].get("old_price") if best else None
            conn.execute("UPDATE store_items SET raw = ?, price = ?, discount_pct = ? WHERE store = 'youtube' AND item_id = ?",
                         (json.dumps(raw, ensure_ascii=False, sort_keys=True), price,
                          round((old - price) * 100 / old) if old else None, vid))
            st["videos"] += 1
            st["with_price"] += price is not None
    return st


if __name__ == "__main__":
    if sys.argv[1] == "lines":
        out = ingest_lines(sys.argv[2], todo=sys.argv[3] if len(sys.argv) > 3 else None)
    elif sys.argv[1] == "buy":
        out = ingest_buy(sys.argv[2])
    else:
        data = json.load(open(sys.argv[2], encoding="utf-8"))
        out = ingest_youtube(data) if sys.argv[1] == "youtube" else ingest_gtv(data)
    print(json.dumps(out, ensure_ascii=False))
