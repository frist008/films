"""Completeness audit (PLAN, stage 4): for the titles the sheet can show (those with a release), how many lack each field.

Run: python -X utf8 -m films.audit  → prints the table and writes data/cache/audit.json
"""
from __future__ import annotations

import json
from datetime import datetime

from . import db

FIELDS = {
    "IMDb id": "imdb_id IS NULL AND series_imdb_id IS NULL",
    "рейтинг IMDb": "coalesce(imdb_season_rating, imdb_rating) IS NULL",
    "RT проверен": "rt_checked_at IS NULL",
    "RT критики": "rt_critics IS NULL",
    "RT зрители": "rt_audience IS NULL",
    "TMDB": "tmdb_checked_at IS NULL",
    "название RU": "title_ru IS NULL",
    "название UK": "title_uk IS NULL",
    "дата выхода": "release_date IS NULL",
    "страны": "countries IS NULL OR countries IN ('', '[]')",
    "жанры": "genres IS NULL OR genres IN ('', '[]')",
    "длительность": "runtime_min IS NULL AND kind = 'movie'",
    "студии": "studios IS NULL OR studios IN ('', '[]')",
    "режиссёр": "NOT EXISTS (SELECT 1 FROM credits c WHERE c.title_id = t.id AND c.role = 'director') AND kind = 'movie'",
    "актёры": "NOT EXISTS (SELECT 1 FROM credits c WHERE c.title_id = t.id AND c.role = 'actor')",
    "трейлер (любой)": "trailer_ru_url IS NULL AND trailer_alt_url IS NULL",
    "русский трейлер": "trailer_ru_url IS NULL",
    "постер": "poster_url IS NULL",
    "бюджет (фильмы)": "budget_usd IS NULL AND kind = 'movie'",
    "IMAX проверен": "imax_source IS NULL AND kind = 'movie'",
}


def run(conn) -> dict:
    base = "FROM titles t WHERE EXISTS (SELECT 1 FROM release_titles rt WHERE rt.title_id = t.id)"
    total = conn.execute(f"SELECT count(*) {base}").fetchone()[0]
    out = {"generated_at": datetime.now().isoformat(timespec="seconds"), "titles_with_releases": total, "missing": {}}
    for name, cond in FIELDS.items():
        try:
            n = conn.execute(f"SELECT count(*) {base} AND ({cond})").fetchone()[0]
        except Exception as e:  # noqa: BLE001 — a renamed column must not hide the rest of the report
            out["missing"][name] = f"error: {e}"
            continue
        out["missing"][name] = {"n": n, "pct": round(100 * n / total, 1) if total else 0}
    (db.ROOT / "data" / "cache" / "audit.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


if __name__ == "__main__":
    r = run(db.connect())
    print(f"тайтлов с раздачами: {r['titles_with_releases']}")
    for k, v in r["missing"].items():
        print(f"  нет «{k}»: {v['n']} ({v['pct']}%)" if isinstance(v, dict) else f"  {k}: {v}")
