"""Crew jobs of the owner's important people marked `crew = true` in config/people.toml (R75).

`credits` keeps only actor / director / writer, but some people matter as creator / showrunner / executive producer
(Adi Shankar: «Devil May Cry», «Castlevania», Dredd).  For them every title in `titles` where they hold one of
`JOBS` goes to `person_crew`; rank.py counts those hits like credits.

Sources, cheapest first:
  1. TMDB cache (tmdb_raw): movie bodies (credits.crew), season bodies (aggregate_credits.crew[].jobs[]),
     series bodies (created_by -> Creator, for every season of the series);
  2. one /person/{id}/combined_credits per crew person (cached in tmdb_raw as kind 'person_credits') — covers
     titles whose cache lacks the crew (series-level executive producers are not in a season's credits).
TV items map to every season title of the series (`titles.series_tmdb_id`).  Rows are only added / re-stamped,
never deleted.
"""
from __future__ import annotations

import json
import zlib

from . import db
from .sources import tmdb

JOBS = {"Creator", "Executive Producer", "Producer", "Showrunner", "Story", "Writer", "Director"}


def crew_people() -> dict[int, str]:
    from .rank import people_cfg
    return {int(p["tmdb_id"]): p["name"] for p in people_cfg() if p.get("crew") and p.get("tmdb_id")}


def _body(blob):
    return json.loads(zlib.decompress(blob).decode("utf-8")) if blob is not None else None


def refresh(conn, max_age_days: float = 30, network: bool = True) -> dict:
    people = crew_people()
    if not people:
        return {"people": 0, "rows": 0}
    movies: dict[int, list[int]] = {}
    series: dict[int, list[tuple[int, int | None]]] = {}
    for tid, kind, ttype, tm, stm, sn in conn.execute(
            "SELECT id, kind, tmdb_type, tmdb_id, series_tmdb_id, season_number FROM titles"):
        if stm and (ttype == "tv" or kind == "series"):
            series.setdefault(stm, []).append((tid, sn))
        elif tm and ttype in ("movie", None) and kind == "movie":
            movies.setdefault(tm, []).append(tid)

    hits: dict[tuple[int, int, str], str] = {}   # (title_id, person, job) -> source

    def add(title_ids, pid, job, source):
        if job in JOBS:
            for tid in title_ids:
                hits.setdefault((tid, pid, job), source)

    # 1. TMDB cache
    for key, blob in conn.execute("SELECT key, body FROM tmdb_raw WHERE kind = 'movie' AND status = 200"):
        if not key.isdigit() or int(key) not in movies:
            continue
        b = _body(blob) or {}
        for c in ((b.get("credits") or {}).get("crew")) or []:
            if c.get("id") in people:
                add(movies[int(key)], c["id"], c.get("job"), "movie_credits")
    season_of = {(stm, sn): tid for stm, lst in series.items() for tid, sn in lst}
    for key, blob in conn.execute("SELECT key, body FROM tmdb_raw WHERE kind = 'season' AND status = 200"):
        stm, _, sn = key.partition("/")
        if not (stm.isdigit() and sn.isdigit()) or (int(stm), int(sn)) not in season_of:
            continue
        b = _body(blob) or {}
        for c in ((b.get("aggregate_credits") or {}).get("crew")) or []:
            if c.get("id") in people:
                for j in c.get("jobs") or []:
                    add([season_of[(int(stm), int(sn))]], c["id"], j.get("job"), "season_credits")
    for key, blob in conn.execute("SELECT key, body FROM tmdb_raw WHERE kind = 'tv' AND status = 200"):
        if not key.isdigit() or int(key) not in series:
            continue
        b = _body(blob) or {}
        for c in b.get("created_by") or []:
            if c.get("id") in people:
                add([tid for tid, _ in series[int(key)]], c["id"], "Creator", "created_by")

    # 2. one combined_credits request per person (cached)
    errors = []
    if network:
        for pid, name in people.items():
            try:
                status, b = tmdb.get(conn, "person_credits", str(pid), "", f"/person/{pid}/combined_credits",
                                     max_age_days=max_age_days)
            except tmdb.TmdbError as e:
                errors.append(f"{name}: {e}")
                continue
            if status != 200:
                errors.append(f"{name}: HTTP {status}")
                continue
            for c in (b or {}).get("crew") or []:
                mid = c.get("id")
                if c.get("media_type") == "movie" and mid in movies:
                    add(movies[mid], pid, c.get("job"), "combined_credits")
                elif c.get("media_type") == "tv" and mid in series:
                    add([tid for tid, _ in series[mid]], pid, c.get("job"), "combined_credits")

    with conn:
        conn.executemany(
            """INSERT INTO person_crew (title_id, person_tmdb_id, job, source) VALUES (?, ?, ?, ?)
               ON CONFLICT(title_id, person_tmdb_id, job) DO UPDATE SET checked_at = datetime('now')""",
            [(tid, pid, job, src) for (tid, pid, job), src in sorted(hits.items())])
    per_person = {people[pid]: len({t for t, p, _ in hits if p == pid}) for pid in people}
    return {"people": len(people), "rows": len(hits), "titles_per_person": per_person, "errors": errors}


def main():
    conn = db.connect()
    db.migrate(conn)
    print(json.dumps(refresh(conn), ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
