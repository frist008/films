"""New since the owner's manual list (R99, owner 2026-10-24).

The owner's own list (config/kostya_list_raw.txt) and the responsibles' lists were made by hand up to New Year 2026.
From then on the update algorithm decides by itself what is new:

* «появился» — the date of the title's FIRST release on rutor (min releases.added_at over all its releases). The
  release year of the film does not matter (a 2024 film first released on rutor in 2026 is new); a later duplicate or
  a better-quality re-release of a title that was already on rutor in 2025 is NOT new.
* new films (kind = movie) with a high Rotten Tomatoes score get the owner's own markers: the bigger of the two RT
  scores (Tomatometer / Popcornmeter — what the owner wrote as «%» in his list, 95 of 116 notes) ≥ 90 → ❤️ (playlist
  «Костя Хочет»), 80–89 → 💛 («Вероятно хочу»); a score needs enough votes (critics ≥ 20 reviews or audience ≥ 250
  ratings) so a handful of early reviews does not decide.
* Дима / Яна: every new comedy / cartoon that passes the main filter goes to their playlists (rank.apply_responsible).
"""
from __future__ import annotations

import tomllib
from datetime import date

from . import db


def cfg() -> dict:
    with open(db.ROOT / "config" / "settings.toml", "rb") as f:
        return tomllib.load(f).get("fresh", {})


def cutoff() -> str:
    return cfg().get("since", "2026-01-01")


def first_seen(conn) -> dict[int, str]:
    """title_id → date (YYYY-MM-DD) of its first rutor release."""
    return {tid: d[:10] for tid, d in conn.execute(
        """SELECT rt.title_id, min(r.added_at) FROM release_titles rt JOIN releases r ON r.id = rt.release_id
           WHERE r.source = 'rutor' AND r.added_at IS NOT NULL AND coalesce(rt.how, '') NOT LIKE 'wrong_link%'
           GROUP BY rt.title_id""")}


def is_new(fs: dict[int, str], tid: int) -> bool:
    return (fs.get(tid) or "0000") >= cutoff()


def last_update(conn) -> str | None:
    """Date (YYYY-MM-DD) of the last finished «обнови базу» (`films update --finish` → sync_state 'update_db', R99(в)):
    the next report shows every title whose first rutor release is from that date on."""
    return db.sync_cursor(conn, "update_db")


def new_since_last_update(conn, fs: dict[int, str] | None = None) -> list[int]:
    """Titles first seen on rutor since the last update (empty before the first finished run)."""
    since = last_update(conn)
    fs = fs if fs is not None else first_seen(conn)
    return sorted(t for t, d in fs.items() if since and d >= since)


def rt_score(t: dict) -> int | None:
    c = cfg()
    vals = []
    if t.get("rt_critics") is not None and (t.get("rt_critics_reviews") or 0) >= c.get("min_critics_reviews", 20):
        vals.append(t["rt_critics"])
    if t.get("rt_audience") is not None and (t.get("rt_audience_ratings") or 0) >= c.get("min_audience_ratings", 250):
        vals.append(t["rt_audience"])
    return max(vals) if vals else None


def auto_marks(conn, fs: dict[int, str] | None = None) -> dict[int, str]:
    """title_id → 'want' (❤️) / 'maybe' (💛) for new films with a high RT score."""
    c = cfg()
    fs = fs if fs is not None else first_seen(conn)
    out = {}
    for t in conn.execute("SELECT * FROM titles WHERE kind = 'movie'"):
        t = dict(t)
        if not is_new(fs, t["id"]):
            continue
        s = rt_score(t)
        if s is None:
            continue
        if s >= c.get("want_rt", 90):
            out[t["id"]] = "want"
        elif s >= c.get("maybe_rt", 80):
            out[t["id"]] = "maybe"
    return out


if __name__ == "__main__":
    conn = db.connect()
    fs = first_seen(conn)
    new = [t for t in fs if is_new(fs, t)]
    marks = auto_marks(conn, fs)
    print({"titles_first_seen": len(fs), "new_since": cutoff(), "new": len(new),
           "auto_want": sum(1 for v in marks.values() if v == "want"), "auto_maybe": sum(1 for v in marks.values() if v == "maybe"),
           "today": date.today().isoformat()})
