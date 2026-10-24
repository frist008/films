"""Audit of the trailers the sheet and the playlists use (R100, owner 2026-10-24).

The owner found: a whole film as a trailer («Зодиак», 2:42), teasers under 1:30 («Супер 8»), videos YouTube hides in UA,
and a trailer of ANOTHER film («Воскрешение» 2022 got «Матриця: Воскресіння»). Every trailer of a visible title is checked
on its YouTube watch page (films.sources.youtube.YouTube.watch — cached, shared rate `youtube_http`):

  full_movie   longer than 5:00                         → rejected (data/cache/trailer_rejected.json), field cleared
  unavailable  not playable in UA (status ≠ OK)         → rejected
  other_film   the video title contains the full name of another film of the DB that is longer than and contains this
               film's name (Матриця: Воскресіння ⊃ Воскресіння)                                     → rejected
  teaser       shorter than 1:30 (unless trailer_ru_check = teaser_only)   → reported (a longer one is searched)
  name_differs none of the film's names in the video title                → reported only (translations, transliterations)

Run: python -X utf8 -m films.trailer_audit [--apply]
"""
from __future__ import annotations

import json
import re
import sys

from . import db

REPORT = db.ROOT / "data" / "cache" / "trailer_audit.json"
REJECTED = db.ROOT / "data" / "cache" / "trailer_rejected.json"


def _n(s: str | None) -> str:
    s = (s or "").lower().replace("ё", "е").replace("ї", "і")
    return re.sub(r"\s+", " ", re.sub(r"[^\w]+", " ", s)).strip()


def _vid(url: str | None) -> str | None:
    m = re.search(r"(?:v=|youtu\.be/|shorts/)([\w-]{11})", url or "")
    return m.group(1) if m else None


def run(apply: bool = False) -> dict:
    from .playlist_notes import visible_order
    from .sources.youtube import YouTube
    conn = db.connect()
    yt = YouTube(log=lambda m: None)
    vis = visible_order()
    names_all = [(r[0], {_n(x) for x in r[1:] if x and len(_n(x)) >= 3}) for r in conn.execute(
        "SELECT id, title_original, title_ru, title_uk FROM titles WHERE kind = 'movie'")]
    rejected = json.loads(REJECTED.read_text(encoding="utf-8")) if REJECTED.exists() else {}
    out = {"checked": 0, "full_movie": [], "unavailable": [], "other_film": [], "teaser": [], "name_differs": []}
    for tid in vis:
        t = conn.execute("SELECT id, title_original, title_ru, title_uk, trailer_ru_url, trailer_alt_url, trailer_ru_check "
                         "FROM titles WHERE id = ?", (tid,)).fetchone()
        mine = {_n(x) for x in t[1:4] if x and len(_n(x)) >= 3}
        for field, url in (("ru", t[4]), ("alt", t[5])):
            v = _vid(url)
            if not v:
                continue
            w = yt.watch(v)
            out["checked"] += 1
            title, ln, st = _n(w.get("title")), w.get("length"), w.get("status")
            rec = {"title_id": tid, "field": field, "video": v, "video_title": w.get("title"), "length": ln, "status": st}
            verdict = None
            if st != "OK":
                verdict = "unavailable"
            elif ln and ln > 300:
                verdict = "full_movie"
            else:
                others = [o for o, ns in names_all if o != tid and any(n in title and any(m in n and m != n for m in mine) for n in ns)]
                if others:
                    rec["other_title_ids"] = others[:3]
                    verdict = "other_film"
                elif ln and ln < 90 and (t[6] or "") != "teaser_only" and field == "ru":
                    verdict = "teaser"
                elif mine and not any(m in title for m in mine):
                    verdict = "name_differs"
            if verdict:
                out[verdict].append(rec)
                if apply and verdict in ("full_movie", "unavailable", "other_film"):
                    col = "trailer_ru_url" if field == "ru" else "trailer_alt_url"
                    with conn:   # short transactions: other jobs write to the DB at the same time
                        conn.execute(f"UPDATE titles SET {col} = NULL, trailer_ru_checked_at = NULL WHERE id = ?", (tid,))
                    rejected[f"{tid}:{field}:{v}"] = {"url": f"https://www.youtube.com/watch?v={v}", "field": field,
                                                      "why": f"trailer_audit: {verdict}", "video_title": w.get("title")}
                    REJECTED.write_text(json.dumps(rejected, ensure_ascii=False, indent=1), encoding="utf-8")
    if apply:
        conn.commit()
        REJECTED.write_text(json.dumps(rejected, ensure_ascii=False, indent=1), encoding="utf-8")
    REPORT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return {k: (len(v) if isinstance(v, list) else v) for k, v in out.items()}


if __name__ == "__main__":
    print(json.dumps(run(apply="--apply" in sys.argv), ensure_ascii=False))
