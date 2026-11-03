"""Google Play Movies / YouTube store data -> SQLite `store_items` (migration 2).

Collection happens in Chrome (account <google.main>); see docs/stores.md for the URLs,
the extraction JS and the pitfalls. This module only stores what was extracted:

    from films.sources import stores
    stores.upsert([{"store": "youtube", "item_id": "Lyr-0MdFibU", "title": "Tomb Raider", ...}])

Item dict keys (all optional except store + item_id):
    store         'google_play' (Play wishlist / Play order history doc ids)
                  'youtube'     (YouTube movie videoIds: library, offers, alternative versions)
    item_id       store's own id (Play doc id like 041577100D0CB2CFMV / r9FqbC-kvqs.P, or YouTube videoId)
    title, year, url
    owned, in_wishlist, available   1 / 0 / None (None = unknown, keeps the stored value)
    price         buy price (cheapest "Купить" offer), float
    currency      e.g. 'UAH'
    discount_pct  discount of that buy offer (old -> new price), int
    raw           dict, merged key-by-key into the stored raw JSON (audio_langs, offers, rent prices, ...)

`python -m films.sources.stores` prints a summary; `--import DIR` loads a saved extraction
(the JSON/TXT files described in docs/stores.md) and upserts it.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from .. import db

COLUMNS = ("title", "year", "url", "owned", "in_wishlist", "available", "price", "currency", "discount_pct")

PRICE_RE = re.compile(r"(\d[\d\s  ]*(?:[.,]\d+)?)\s*(грн|₴|\$|€|USD|UAH)", re.I)
CURRENCY = {"грн": "UAH", "₴": "UAH", "$": "USD", "€": "EUR", "usd": "USD", "uah": "UAH"}


def parse_prices(text: str | None) -> tuple[list[float], str | None]:
    """'133,00 грн.  93,00 грн.' -> ([133.0, 93.0], 'UAH'). Two prices = old, new (sale)."""
    if not text:
        return [], None
    vals, cur = [], None
    for num, c in PRICE_RE.findall(text):
        num = re.sub(r"[\s  ]", "", num).replace(",", ".")
        vals.append(float(num))
        cur = CURRENCY.get(c.lower(), c)
    return vals, cur


def offer_summary(offers: list[dict]) -> dict:
    """Normalise YouTube get_offers entries [{type, q, price}] -> buy/rent prices and discounts."""
    out: dict = {"buy": [], "rent": []}
    for o in offers or []:
        vals, cur = parse_prices(o.get("price"))
        if not vals:
            continue
        kind = "buy" if "Купить" in (o.get("type") or "") else "rent" if "напрокат" in (o.get("type") or "") else "other"
        e = {"quality": o.get("q"), "price": vals[-1], "currency": cur}
        if len(vals) > 1 and vals[0] > vals[-1]:
            e["old_price"] = vals[0]
            e["discount_pct"] = round((vals[0] - vals[-1]) * 100 / vals[0])
        out.setdefault(kind, []).append(e)
    return out


def best_buy(offers: list[dict]) -> tuple[float | None, str | None, int | None]:
    s = offer_summary(offers)
    if not s["buy"]:
        return None, None, None
    b = min(s["buy"], key=lambda e: e["price"])
    return b["price"], b["currency"], b.get("discount_pct")


def _merge_raw(old: str | None, new: dict | None) -> str | None:
    if new is None:
        return old
    base = {}
    if old:
        try:
            base = json.loads(old)
        except ValueError:
            base = {"_unparsed": old}
    base.update(new)
    return json.dumps(base, ensure_ascii=False, sort_keys=True)


def upsert(items: list[dict], conn: sqlite3.Connection | None = None, checked_at: str | None = None,
           batch: int = 200) -> dict:
    """Insert/update store_items rows. None values never overwrite stored values; raw is merged.

    Short transactions (`batch` rows each) because other collectors write to the DB concurrently.
    """
    own = conn is None
    if own:
        conn = db.connect()
        db.migrate(conn)
    checked_at = checked_at or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    stats = {"inserted": 0, "updated": 0}
    for i in range(0, len(items), batch):
        with conn:
            for it in items[i:i + batch]:
                store, item_id = it["store"], str(it["item_id"])
                row = conn.execute("SELECT * FROM store_items WHERE store=? AND item_id=?", (store, item_id)).fetchone()
                vals = {c: it.get(c) for c in COLUMNS}
                if row is None:
                    conn.execute(
                        f"INSERT INTO store_items (store, item_id, {', '.join(COLUMNS)}, raw, checked_at) "
                        f"VALUES (?, ?, {', '.join('?' * len(COLUMNS))}, ?, ?)",
                        (store, item_id, *vals.values(), _merge_raw(None, it.get("raw")), checked_at))
                    stats["inserted"] += 1
                else:
                    merged = {c: (vals[c] if vals[c] is not None else row[c]) for c in COLUMNS}
                    conn.execute(
                        f"UPDATE store_items SET {', '.join(f'{c}=?' for c in COLUMNS)}, raw=?, checked_at=? "
                        "WHERE store=? AND item_id=?",
                        (*merged.values(), _merge_raw(row["raw"], it.get("raw")), checked_at, store, item_id))
                    stats["updated"] += 1
    if own:
        conn.close()
    return stats


def summary(conn: sqlite3.Connection | None = None) -> str:
    own = conn is None
    if own:
        conn = db.connect()
        db.migrate(conn)
    q = lambda sql: conn.execute(sql).fetchall()  # noqa: E731
    lines = []
    for store, n, owned, wl, avail, unavail, disc in q(
            "SELECT store, count(*), sum(owned=1), sum(in_wishlist=1), sum(available=1), sum(available=0), "
            "sum(coalesce(discount_pct,0)>0) FROM store_items GROUP BY store ORDER BY store"):
        lines.append(f"{store:12} rows={n} owned={owned or 0} wishlist={wl or 0} available={avail or 0} "
                     f"unavailable={unavail or 0} discounted={disc or 0}")
    rows = q("SELECT store, item_id, title, price, currency, discount_pct, raw FROM store_items "
             "WHERE coalesce(discount_pct,0)>0 OR raw LIKE '%discount_pct%' ORDER BY store, title")
    disc = []
    for r in rows:
        raw = json.loads(r["raw"] or "{}")
        rent_disc = [o for o in (raw.get("offer_summary") or {}).get("rent", []) if o.get("discount_pct")]
        if r["discount_pct"] or rent_disc:
            d = f"buy -{r['discount_pct']}% {r['price']} {r['currency']}" if r["discount_pct"] else f"buy {r['price']} {r['currency']}"
            for o in rent_disc:
                d += f"; rent -{o['discount_pct']}% {o['old_price']}->{o['price']} {o['currency']}"
            disc.append(f"  {r['store']}:{r['item_id']} {r['title']}: {d}")
    if disc:
        lines.append("discounts:")
        lines.extend(disc)
    if own:
        conn.close()
    return "\n".join(lines)


# --------------------------------------------------------------------------------------------
# Import of a saved Chrome extraction (format documented in docs/stores.md, section "Файлы выгрузки")
# --------------------------------------------------------------------------------------------

def _read_rows(path: Path) -> list[list[str]]:
    return [l.split("|") for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def build_items(d: Path) -> list[dict]:
    items: dict[tuple[str, str], dict] = {}

    def put(it: dict):
        key = (it["store"], it["item_id"])
        if key in items:
            old = items[key]
            for k, v in it.items():
                if k == "raw":
                    old.setdefault("raw", {}).update(v)
                elif v is not None:
                    old[k] = v
        else:
            items[key] = it

    # 1. YouTube library (Фильмы → Покупки → «Показать все»): owned
    lib_titles = {r[0]: r for r in _read_rows(d / "yt_library_rows.txt")} if (d / "yt_library_rows.txt").exists() else {}
    if (d / "yt_library_audio.txt").exists():
        for vid, year, acquired, owner, langs in _read_rows(d / "yt_library_audio.txt"):
            t = lib_titles.get(vid)
            put({"store": "youtube", "item_id": vid, "title": t[1] if t else None,
                 "year": int(year) if year.isdigit() else None,
                 "url": f"https://www.youtube.com/watch?v={vid}", "owned": 1,
                 "raw": {"audio_langs": [x for x in langs.split(",") if x], "acquired": acquired,
                         "content_owner": owner, "genre_year": t[2] if t else None, "duration": t[3] if t else None,
                         "source": "youtube_library"}})

    # 2. Play order history (movie rows); ids are Play doc ids, not always YouTube videoIds
    if (d / "gp_orderhistory.json").exists():
        oh = json.loads((d / "gp_orderhistory.json").read_text(encoding="utf-8"))
        per: dict[str, dict] = {}
        for r in oh["rows"]:
            if r["type"] != "1" or not r.get("href"):
                continue
            m = re.search(r"[?&]id=([^&]+)", r["href"])
            if not m:
                continue
            pid = m.group(1)
            lines = [x for x in r["lines"] if x != "Сообщить о проблеме"]
            e = per.setdefault(pid, {"title": lines[0], "orders": []})
            e["orders"].append({"date": lines[1] if len(lines) > 1 else None,
                                "price": lines[2] if len(lines) > 2 else None, "extra": lines[3:]})
        for pid, e in per.items():
            put({"store": "google_play", "item_id": pid, "title": e["title"],
                 "url": f"https://play.google.com/store/movies/details?id={pid}", "owned": 1,
                 "raw": {"play_orders": e["orders"], "source_owned": "play_order_history"}})

    # 3. Play wishlist (union over regions) + YouTube matches/offers
    if (d / "wishlist_final.json").exists():
        for it in json.loads((d / "wishlist_final.json").read_text(encoding="utf-8")):
            put(it)
    # 4. Google TV watchlist (google.com/interests/saved) + YouTube listings found for it
    for name in ("youtube_offers.json", "gtv_final.json", "youtube_offers_gtv.json"):
        if (d / name).exists():
            for it in json.loads((d / name).read_text(encoding="utf-8")):
                put(it)
    return list(items.values())


# Play order-history docs with Ukrainian titles (UA-dub listings) -> YouTube library videoId.
# Matched manually 2026-10-23 by identical purchase date ("Приобретено" on the watch page) + title translation.
MANUAL_ORDER_LINKS = {
    "0qah7_vjvIY.P": "N3gPoIaX0oE",  # Вартові галактики 3 / Guardians of the Galaxy Vol. 3
    "3qKOBXu755k.P": "JRtT4id-GG0",  # Русалонька / The Little Mermaid
    "7y7IqR94CaQ.P": "tNQ1mzbbgcc",  # Аватар: Шлях води / Avatar: The Way of Water
    "8QlSCTiMZSw.P": "HFLPXwp_phc",  # Людина-павук: Повернення додому / Spider-Man: Homecoming
    "Ay5FdN8qJnE.P": "oJhiqTMiGsc",  # Аватар / Avatar
    "DWoa7MX6VHo.P": "eNWXz0DUvUE",  # Стихії / Elemental
    "KfE9peWcN8M.P": "nSIIe3stS8k",  # Тор: любов і грім / Thor: Love and Thunder
    "LnDijOPMbFs.P": "zSsH0H8_Cic",  # Історія іграшок / Toy Story
    "OSaijOdSmC4.P": "H0Rz1r7rG5Y",  # Чорна пантера: Ваканда назавжди
    "SgLUeeRBBwA.P": "jk5f5nuIjaw",  # Оселя зла. Острів загибелі / Resident Evil: Death Island
    "WiHGuvmR4Qk.P": "8BX3mmkR9tM",  # Титанік / Titanic
    "Xuh-VVc_Jy4.P": "c3nFC9RiJpU",  # Марвели / The Marvels
    "dTjqNve5Xfo.P": "GxhR0hSX8og",  # Доктор Стрендж у Мультивсесвіті божевілля
    "gLvXDaZKxZk.P": "UZcymsQPF2U",  # Кінгс Мен / The King's Man
    "hCSMugTj4g4.P": "s_izATup57k",  # Крижане серце / Frozen
    "l9MU6kmMMiU.P": "La72fjkqkq0",  # Їжак Сонік 2 / Sonic The Hedgehog 2 (two orders the same day)
    "maC7XUx7C6s.P": "DTfVMm9n7nk",  # Тор 2: Царство темряви / Thor: The Dark World
    "qaWMjJWfYb8.P": "iEC_0IAF4ZA",  # Вартові Галактики / Guardians of the Galaxy
    "5d_LtZX6fm8.P": "iQLFcjBS1_4",  # Крик (2022), bought 18.01.2023 — the RU-audio-only listing
    "HbDe-TmwLv8.P": "J4gtn_-AmXg",  # Крик (2022), bought 15.08.2023 — the multi-audio listing
}


def _norm_title(s: str | None) -> str:
    s = re.sub(r"\(.*?\)", " ", (s or "").lower().replace("ё", "е"))
    return re.sub(r"\s+", " ", re.sub(r"[«»\"'’:;.,!?\-–—&]", " ", s)).strip()


def link_play_orders(conn: sqlite3.Connection | None = None) -> dict:
    """Play order-history rows -> YouTube library item (raw.youtube_library_vid).

    The order history does not say purchase vs rental; an order whose film is not in the YouTube
    library is most likely an expired rental -> owned=NULL, raw.not_in_library=true.
    """
    own = conn is None
    if own:
        conn = db.connect()
        db.migrate(conn)
    lib = {r["item_id"]: r for r in conn.execute("SELECT item_id, title, raw FROM store_items WHERE store='youtube' AND owned=1")}
    by_title: dict[str, list[str]] = {}
    for vid, r in lib.items():
        by_title.setdefault(_norm_title(r["title"]), []).append(vid)
    stats = {"by_id": 0, "by_title": 0, "manual": 0, "not_in_library": 0}
    with conn:
        for r in conn.execute("SELECT item_id, title, raw FROM store_items WHERE store='google_play' "
                              "AND raw LIKE '%play_order_history%'").fetchall():
            pid = r["item_id"]
            vid, how = None, None
            if pid in lib:
                vid, how = pid, "by_id"
            elif len(by_title.get(_norm_title(r["title"]), [])) == 1:
                vid, how = by_title[_norm_title(r["title"])][0], "by_title"
            elif pid in MANUAL_ORDER_LINKS:
                vid, how = MANUAL_ORDER_LINKS[pid], "manual"
            raw = json.loads(r["raw"] or "{}")
            if vid:
                raw.update({"youtube_library_vid": vid, "youtube_link_method": how,
                            "audio_langs": json.loads(lib[vid]["raw"] or "{}").get("audio_langs")})
                raw.pop("not_in_library", None)
                conn.execute("UPDATE store_items SET owned=1, raw=? WHERE store='google_play' AND item_id=?",
                             (json.dumps(raw, ensure_ascii=False, sort_keys=True), pid))
                stats[how] += 1
            else:
                raw.update({"not_in_library": True,
                            "note": "order found in Play order history but film not in YouTube library: probably a rental"})
                conn.execute("UPDATE store_items SET owned=NULL, raw=? WHERE store='google_play' AND item_id=?",
                             (json.dumps(raw, ensure_ascii=False, sort_keys=True), pid))
                stats["not_in_library"] += 1
    if own:
        conn.close()
    return stats


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m films.sources.stores")
    ap.add_argument("--import", dest="import_dir", help="directory with a saved Chrome extraction")
    args = ap.parse_args(argv)
    if args.import_dir:
        items = build_items(Path(args.import_dir))
        print(f"items built: {len(items)}", upsert(items))
        print("play orders -> youtube library:", link_play_orders())
    print(summary())


if __name__ == "__main__":
    sys.exit(main())
