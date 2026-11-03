"""Owner's rutor list «Обновить качество» (2026-10-24): films he owns (downloaded / queued) — collect their rutor releases
for any year (the main sweep covers 2024+), so the sheet can show the best copy, «дубляж важнее качества всегда».
List: config/settings.toml [quality_watch] imdb. After collect: films rutor pages → parse → films match.

Run: python -X utf8 -m films.quality_watch
"""
from __future__ import annotations

import json
import re
import sys

from . import db, rank
from .sources import rutor

CATS = ((1, "foreign_films"), (7, "animation"))


def _norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w]+", " ", (s or "").lower().replace("ё", "е"))).strip()


def collect(conn, log=print) -> dict:
    imdb = rank.settings().get("quality_watch", {}).get("imdb") or []
    st = {"films": len(imdb), "new": 0, "with_hits": 0, "no_hits": []}
    for tt in imdb:
        t = conn.execute("SELECT id, title_ru, title_original, year FROM titles WHERE imdb_id = ?", (tt,)).fetchone()
        if not t:
            st["no_hits"].append(tt)
            continue
        names = [n for n in (t["title_ru"], t["title_original"]) if n]
        keys = {_norm(n) for n in names}
        hits = {}
        for name in names:
            for cat, category in CATS:
                try:
                    # rutor search breaks on «!» / «?» / «:» (missed «Скуби-Ду! И Крипто тоже!»)
                    _, rows = rutor.search_all(cat, f"{re.sub(r'[!?:]+', ' ', name).strip()} {t['year']}")
                except Exception as e:  # noqa: BLE001
                    log(f"  rutor error {name}: {e}")
                    continue
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
        log(f"  {t['title_ru']} ({t['year']}): {len(hits)} раздач")
    return st


if __name__ == "__main__":
    c = db.connect()
    print(json.dumps(collect(c, log=lambda m: print(m, file=sys.stderr, flush=True)), ensure_ascii=False))
