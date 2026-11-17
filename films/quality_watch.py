"""Owner's rutor list «Обновить качество» (2026-10-24): films he owns (downloaded / queued) — collect their rutor releases
for any year (the main sweep covers 2024+), so the sheet can show the best copy, «дубляж важнее качества всегда».
List: config/settings.toml [quality_watch] imdb. After collect: films rutor pages → parse → films match.

Each film is searched ONCE (R212 / R322, owner 2026-11-13: «ищем только всё новое … и не трогаем ничего старого на что
материал уже найден … Если даже новое качество для фильма вышло на трекере … это просто новое качество для обновления»;
data/cache/quality_watch_searched.json): only films new on the list. A later better release comes from the rutor category
listing (films.sources.rutor.tracked_index keeps rows of every [quality_watch] film — via films.encountered «lists»).

Run: python -X utf8 -m films.quality_watch [--again]     (--again: every film of the list again — owner's request only)
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date

from . import db, rank
from .sources import rutor

CATS = ((1, "foreign_films"), (7, "animation"))
SEARCHED = db.ROOT / "data" / "cache" / "quality_watch_searched.json"   # imdb → date of its one search (R322)


def _norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w]+", " ", (s or "").lower().replace("ё", "е"))).strip()


def load_searched() -> dict:
    try:
        return json.loads(SEARCHED.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_searched(v: dict) -> None:
    SEARCHED.parent.mkdir(parents=True, exist_ok=True)
    tmp = SEARCHED.with_suffix(".tmp")
    tmp.write_text(json.dumps(v, ensure_ascii=False, indent=0, sort_keys=True), encoding="utf-8")
    tmp.replace(SEARCHED)


def collect(conn, log=print, again: bool = False) -> dict:
    every = rank.settings().get("quality_watch", {}).get("imdb") or []
    searched = load_searched()
    imdb = list(every) if again else [tt for tt in every if tt not in searched]
    st = {"films": len(every), "already_searched": len(every) - len(imdb), "searched": 0, "queries": 0, "new": 0,
          "with_hits": 0, "no_hits": []}
    log(f"quality_watch: {len(every)} films on the list, to search {len(imdb)} (searched before: {st['already_searched']})")
    for tt in imdb:
        t = conn.execute("SELECT id, title_ru, title_original, year FROM titles WHERE imdb_id = ?", (tt,)).fetchone()
        if not t:
            st["no_hits"].append(tt)   # no title yet: not searched, tried again next run
            continue
        names = [n for n in (t["title_ru"], t["title_original"]) if n]
        keys = {_norm(n) for n in names}
        hits = {}
        ok = 0
        for name in names:
            for cat, category in CATS:
                try:
                    # rutor search breaks on «!» / «?» / «:» (missed «Скуби-Ду! И Крипто тоже!»)
                    _, rows = rutor.search_all(cat, f"{re.sub(r'[!?:]+', ' ', name).strip()} {t['year']}")
                except Exception as e:  # noqa: BLE001
                    log(f"  rutor error {name}: {e}")
                    continue
                ok += 1
                st["queries"] += 1
                for r in rows:
                    n = _norm(r["name"])
                    years = {int(y) for y in re.findall(r"\b(?:19|20)\d\d\b", r["name"])}
                    if any(k and k in n for k in keys) and any(abs(y - t["year"]) <= 1 for y in years):
                        hits[r["source_id"]] = (r, category)
        if hits:
            st["with_hits"] += 1
            for category in {c for _, c in hits.values()}:
                st["new"] += rutor.upsert(conn, [r for r, c in hits.values() if c == category], category)
        else:
            st["no_hits"].append(f"{t['title_ru']} ({t['year']})")
        if ok:   # every query failed (network): not searched, the next run tries it again
            st["searched"] += 1
            searched[tt] = date.today().isoformat()
            _save_searched(searched)
        log(f"  {t['title_ru']} ({t['year']}): {len(hits)} раздач")
    return st


if __name__ == "__main__":
    c = db.connect()
    print(json.dumps(collect(c, log=lambda m: print(m, file=sys.stderr, flush=True), again="--again" in sys.argv),
                     ensure_ascii=False))
