"""Trailers of series seasons ≥ 2 that belong to another season (PLAN V4, owner 2026-10-29: «проверить трейлеры сезонов
сериалов; Проверяй»).

A season's trailer was taken from a search page (its title may be a translation), from the season's own TMDB videos, or
— for trailer_alt — from the series-level TMDB videos, i.e. the same (usually season 1) trailer on every season. The
check reads the video's own title on its watch page (youtube.watch, HTTP, no account):
  - the title names seasons → right only if it names this one;
  - no season number → right if it was published in this season's window (the rule of tmdb._season_trailer:
    after the previous premiere + 30 days, up to this premiere + 60 days / the next premiere − 91 days, at most
    400 days before this premiere), wrong if in another season's window or before this one's; a trailer of the whole
    series (published after all seasons, «32 Seasons», «все сезоны») stays on every season (owner, 2026-10-29);
  - no title, no dates, or published between seasons → unknown (kept, listed).
A wrong trailer moves to the season its title names when that season has none, otherwise it is
cleared (`trailer_ru_check = 'wrong_season'`); the owner's own picks are never touched.

    python -X utf8 -m films.season_trailers check [--apply]
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date, timedelta

from . import db
from .sources import trailer_lang as tl
from .sources import tmdb
from .sources import youtube as yt_mod

REPORT = db.ROOT / "data" / "cache" / "season_trailers.json"
# «32 Seasons», «все сезоны», «complete series» — a promo of the whole series, not of one season
_WHOLE = re.compile(r"\b\d{1,2}\s*(?:seasons|сезон(?:ов|а))\b|\b(?:all seasons|все сезоны|complete series|полный сериал)\b")


def windows(seasons: dict[int, str]) -> dict[int, tuple[date, date, date]]:
    """{season: (lo, hi, air)} from {season: air_date} — the date window of tmdb._season_trailer."""
    s = sorted((n, date.fromisoformat(a[:10])) for n, a in seasons.items() if n and a)
    out = {}
    for i, (n, a) in enumerate(s):
        lo = s[i - 1][1] + timedelta(days=30) if i else date.min
        hi = a + timedelta(days=60)
        if i + 1 < len(s):
            hi = min(hi, s[i + 1][1] - timedelta(days=91))
        out[n] = (lo, hi, a)
    return out


def _fits(p: date, w: tuple[date, date, date]) -> bool:
    lo, hi, a = w
    return lo < p <= hi and (a - p).days <= 400


def verdict(n: int, title: str | None, published: str | None, seasons: dict[int, str]) -> tuple[bool | None, str, list[int]]:
    """(right?, why, seasons it belongs to) for a trailer of season n."""
    if not title:
        return None, "no video title", []
    nums = yt_mod.season_mentions(title)
    if nums:
        if n in nums:
            return True, f"title names season {sorted(nums)}", sorted(nums)
        if seasons and not any(k in seasons for k in nums):
            # «Futurama | Season 14 Trailer | Hulu» on TMDB's season 11: the channel numbers the seasons otherwise
            return None, f"title names season {sorted(nums)} — TMDB numbers the seasons otherwise", []
        return False, f"title names season {sorted(nums)}", sorted(nums)
    if _WHOLE.search(yt_mod.norm(title)):
        return True, "a trailer of the whole series", [n]
    ws = windows(seasons)
    if not published or n not in ws:
        return None, "no season number, no dates", []
    p = date.fromisoformat(published[:10])
    if _fits(p, ws[n]):
        return True, f"published {p} in the window of season {n}", [n]
    other = [k for k, w in ws.items() if _fits(p, w)]
    if other:
        return False, f"no season number, published {p} — in the window of season {other}", other
    if p < ws[n][0]:
        return False, f"no season number, published {p} — before season {n}'s window", []
    if p > max(w[1] for w in ws.values()):
        # a trailer of the whole series put out years later (DVD / Blu-ray / streaming promo) stays on every season
        # (owner, 2026-10-29: «Оставить у всех сезонов»)
        return True, f"published {p}, after all seasons — a trailer of the whole series", [n]
    return None, f"no season number, published {p} — between seasons", []


def _season_dates(conn, stm: int, cache: dict) -> dict[int, str]:
    if stm not in cache:
        body = tmdb.tv(conn, stm) or {}
        cache[stm] = {s["season_number"]: s["air_date"] for s in body.get("seasons") or [] if s.get("air_date")}
    return cache[stm]


def check(conn, apply: bool = False, yt=None, log=print) -> dict:
    yt = yt or yt_mod.YouTube(max_age_days=3650)
    rows = conn.execute("""SELECT id, series_tmdb_id, season_number, trailer_ru_url, trailer_ru_source, trailer_ru_check,
                                  trailer_alt_url, trailer_alt_kind, trailer_alt_source, title_ru, title_original
                           FROM titles WHERE kind = 'series' AND season_number >= 2 AND duplicate_of IS NULL
                             AND series_tmdb_id IS NOT NULL
                             AND (trailer_ru_url IS NOT NULL OR trailer_alt_url IS NOT NULL)""" + yt_mod.NOT_WATCHED).fetchall()
    by_season = {(r[1], r[2]): r[0] for r in conn.execute(
        "SELECT id, series_tmdb_id, season_number FROM titles WHERE kind = 'series' AND duplicate_of IS NULL")}
    have = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT id, trailer_ru_url, trailer_alt_url FROM titles")}
    dates, out = {}, {"checked": 0, "right": 0, "unknown": [], "wrong": []}
    for (tid, stm, n, ru, ru_src, ru_chk, alt, alt_kind, alt_src, name_ru, name_orig) in rows:
        for field, url, owner in (("ru", ru, ru_src == "owner" or (ru_chk or "").startswith("owner")),
                                  ("alt", alt, alt_src == "owner")):
            if not url or owner:
                continue
            out["checked"] += 1
            rec = yt.watch(tl._vid(url)) or {}
            if rec.get("status") not in (None, "OK"):   # removed / private: a dead link on the sheet
                ok, why, belongs = False, f"video unavailable: {rec.get('reason') or rec.get('status')}", []
            else:
                ok, why, belongs = verdict(n, rec.get("title"), rec.get("publish_date"), _season_dates(conn, stm, dates))
            item = {"id": tid, "title": name_ru or name_orig, "season": n, "field": field, "url": url,
                    "video_title": rec.get("title"), "why": why}
            if ok:
                out["right"] += 1
                continue
            if ok is None:
                out["unknown"].append(item)
                continue
            target = by_season.get((stm, belongs[0])) if len(belongs) == 1 else None
            free = target is not None and why.startswith("title names") and not any(have.get(target, (None, None)))
            item["moved_to"] = belongs[0] if free else None
            out["wrong"].append(item)
            if not apply:
                continue
            with conn:
                if field == "ru":
                    conn.execute("""UPDATE titles SET trailer_ru_url = NULL, trailer_ru_check = 'wrong_season',
                                    trailer_ru_checked_at = datetime('now') WHERE id = ?""", (tid,))
                    if free:
                        conn.execute("""UPDATE titles SET trailer_ru_url = ?, trailer_ru_source = ?, trailer_ru_check = ?
                                        WHERE id = ?""", (url, f"moved_from_season:{n}", ru_chk, target))
                else:
                    conn.execute("UPDATE titles SET trailer_alt_url = NULL, trailer_alt_kind = NULL, "
                                 "trailer_alt_source = NULL WHERE id = ?", (tid,))
                    if free:
                        conn.execute("UPDATE titles SET trailer_alt_url = ?, trailer_alt_kind = ?, trailer_alt_source = ? "
                                     "WHERE id = ?", (url, alt_kind, f"moved_from_season:{n}", target))
            if free:
                have[target] = (url, None) if field == "ru" else (None, url)
    REPORT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"checked {out['checked']}: right {out['right']}, wrong {len(out['wrong'])} "
        f"(moved {sum(1 for w in out['wrong'] if w['moved_to'])}), unknown {len(out['unknown'])} → {REPORT}")
    return out


if __name__ == "__main__":
    if sys.argv[1:2] == ["check"]:
        check(db.connect(), apply="--apply" in sys.argv)
    else:
        print(__doc__)
