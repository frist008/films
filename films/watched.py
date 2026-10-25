"""«Я видел X»: mark a title watched in Plex (so the owner's account keeps it), in Jellyfin (jellyfin_guard, owner
2026-10-25) and in the DB.

- In the local library → `GET /:/scrobble?identifier=com.plexapp.plugins.library&key=<ratingKey>` on the server
  (view-state sync pushes it to plex.tv for agent-matched items).
- Not in the library → Plex Discover: `PUT discover.provider.plex.tv/actions/scrobble?identifier=tv.plex.provider.discover&key=<plexId>`.
- A series season → the season's Discover id (marks all its episodes).
Every mark is verified by reading the state back; the DB gets watched(source='manual') and, when Plex confirmed,
watched(source='plex'). Recipes and how ids are resolved: docs/plex-api.md.
"""
import json

from . import db
from .sources import jellyfin_guard
from .sources import plex as plexsrc

LIB_ID = "com.plexapp.plugins.library"
DISCOVER_ID = "tv.plex.provider.discover"


def find(conn, query: str) -> list[dict]:
    """Candidate titles by IMDb id or any title (ru/uk/original/latin), newest first."""
    q = query.strip()
    if q.startswith("tt"):
        rows = conn.execute("SELECT * FROM titles WHERE imdb_id = ? OR series_imdb_id = ?", (q, q)).fetchall()
    else:
        like = f"%{q.lower()}%"
        rows = conn.execute(
            """SELECT * FROM titles WHERE lower(title_ru) LIKE ? OR lower(title_original) LIKE ?
                   OR lower(title_uk) LIKE ? OR lower(title_latin) LIKE ? ORDER BY year DESC LIMIT 20""",
            (like, like, like, like)).fetchall()
    return [dict(r) for r in rows]


def _plex_id(px, imdb_id: str, kind: str) -> str | None:
    data = px.get(f"{plexsrc.METADATA_PROVIDER}/library/metadata/matches",
                  params={"type": 1 if kind == "movie" else 2, "guid": f"imdb://{imdb_id}"})
    meta = (data.get("MediaContainer") or {}).get("Metadata") or []
    return meta[0]["ratingKey"] if meta else None


def _season_plex_id(px, show_id: str, season: int) -> str | None:
    for m in plexsrc.discover_children(px, show_id):
        if m.get("index") == season:
            return m["ratingKey"]
    return None


def _discover_state(px, plex_id: str) -> dict:
    data = px.get(f"{plexsrc.DISCOVER_PROVIDER}/library/metadata/{plex_id}/userState")
    states = (data.get("MediaContainer") or {}).get("UserState") or []
    return states[0] if states else {}


def _child_id(px, parent_id: str, index: int) -> str | None:
    for m in plexsrc.discover_children(px, parent_id):
        if m.get("index") == index:
            return m["ratingKey"]
    return None


def _discover_scrobble(px, pid: str):
    r = px.s.put(f"{plexsrc.DISCOVER_PROVIDER}/actions/scrobble", params={"identifier": DISCOVER_ID, "key": pid}, timeout=60)
    if r.status_code in (404, 405):  # documented fallback: some deployments accept GET
        r = px.s.get(f"{plexsrc.DISCOVER_PROVIDER}/actions/scrobble", params={"identifier": DISCOVER_ID, "key": pid}, timeout=60)
    r.raise_for_status()


def mark_episode(conn, series_imdb: str, season: int, episode: int, dry_run: bool = False, px=None,
                 jellyfin: bool = True) -> dict:
    """One episode watched in Plex: the library file of that episode (if any) and Plex Discover (account).
    Skips what Plex already has as watched, so repeated syncs do not inflate view counts."""
    px = px or plexsrc.Plex()
    out = {"series_imdb_id": series_imdb, "season": season, "episode": episode, "plex": [], "dry_run": dry_run}
    row = conn.execute("SELECT id FROM titles WHERE series_imdb_id = ? AND season_number = ?", (series_imdb, season)).fetchone()
    if row:
        for (rk,) in conn.execute("SELECT rating_key FROM plex_items WHERE title_id = ? AND episode = ?",
                                  (row[0], episode)).fetchall():
            meta = px.get(f"/library/metadata/{rk}")
            vc = ((meta.get("MediaContainer") or {}).get("Metadata") or [{}])[0].get("viewCount", 0)
            if not vc and not dry_run:
                px.s.get(f"{plexsrc.PLEX_URL}/:/scrobble", params={"identifier": LIB_ID, "key": rk}, timeout=60).raise_for_status()
                meta = px.get(f"/library/metadata/{rk}")
                vc = ((meta.get("MediaContainer") or {}).get("Metadata") or [{}])[0].get("viewCount", 0)
            out["plex"].append({"where": "library", "ratingKey": rk, "viewCount": vc})
    show = _plex_id(px, series_imdb, "series")
    sid = _season_plex_id(px, show, season) if show else None
    eid = _child_id(px, sid, episode) if sid else None
    if eid:
        st = _discover_state(px, eid)
        if st.get("viewState") != "complete" and not (st.get("viewCount") or 0) and not dry_run:
            _discover_scrobble(px, eid)
            st = _discover_state(px, eid)
        out["plex"].append({"where": "discover", "plexId": eid, "viewCount": st.get("viewCount"),
                            "viewState": st.get("viewState")})
    else:
        out["plex"].append({"where": "discover", "error": f"no Plex id for {series_imdb} S{season}E{episode}"})
    out["confirmed"] = any((p.get("viewCount") or 0) > 0 or p.get("viewState") == "complete" for p in out["plex"])
    if jellyfin and not dry_run:  # owner 2026-10-25: «видел» — и в Jellyfin (jellyfin_guard never re-marks)
        out["jellyfin"] = jellyfin_guard.mark_episode(conn, series_imdb, season, episode, jf=_jf())
    return out


def _jf():
    global _JF
    if _JF is None:
        _JF = jellyfin_guard.Jellyfin()
    return _JF


_JF = None


def sync_from_jellyfin(conn, dry_run: bool = False) -> dict:
    """Owner's rule (2026-10-23): whatever is watched in Jellyfin must be marked watched in Plex too.
    Movies Plex already has as watched are skipped; episodes are marked one by one (a partly watched season
    must not be marked whole). Run after `films link jellyfin`."""
    px = plexsrc.Plex()
    res = {"movies": [], "episodes": [], "dry_run": dry_run}
    for key, tid in conn.execute("""SELECT p.key, p.title_id FROM jellyfin_played p WHERE p.kind = 'movie'
                                    AND p.title_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM watched w
                                    WHERE w.title_id = p.title_id AND w.source = 'plex')""").fetchall():
        r = mark(conn, tid, dry_run=dry_run, db_source=None, jellyfin=False)
        res["movies"].append({"imdb_id": key, "title": r["title"], "confirmed": r["confirmed"]})
    for imdb, season, episode in conn.execute(
            "SELECT imdb_id, season, episode FROM jellyfin_played WHERE kind = 'episode' ORDER BY 1, 2, 3").fetchall():
        r = mark_episode(conn, imdb, season, episode, dry_run=dry_run, px=px, jellyfin=False)
        res["episodes"].append({k: r[k] for k in ("series_imdb_id", "season", "episode", "confirmed")} | {"plex": r["plex"]})
    res["not_confirmed"] = [x for x in res["movies"] + res["episodes"] if not x["confirmed"]]
    if not dry_run:
        db.mark_sync(conn, "jellyfin_to_plex", None, {"movies": len(res["movies"]), "episodes": len(res["episodes"]),
                                                      "not_confirmed": len(res["not_confirmed"])})
    return res


def mark(conn, title_id: int, dry_run: bool = False, db_source: str | None = "manual", jellyfin: bool = True) -> dict:
    t = dict(conn.execute("SELECT * FROM titles WHERE id = ?", (title_id,)).fetchone())
    px = plexsrc.Plex()
    out = {"title_id": title_id, "title": t["title_ru"] or t["title_original"], "season": t["season_number"],
           "plex": [], "dry_run": dry_run}
    # 1) local library items linked to this title.
    # NEVER re-scrobble what Plex already has as watched: every scrobble adds a history entry «now» and moves
    # lastViewedAt — it destroys the owner's real chronology (the owner, 2026-10-23: «не трогай мои фильмы, где была
    # чёткая история, когда я что посмотрел»).
    for (rk,) in conn.execute("SELECT rating_key FROM plex_items WHERE title_id = ?", (title_id,)).fetchall():
        meta = px.get(f"/library/metadata/{rk}")
        vc = ((meta.get("MediaContainer") or {}).get("Metadata") or [{}])[0].get("viewCount", 0)
        if not vc and not dry_run:
            px.s.get(f"{plexsrc.PLEX_URL}/:/scrobble", params={"identifier": LIB_ID, "key": rk}, timeout=60).raise_for_status()
            meta = px.get(f"/library/metadata/{rk}")
            vc = ((meta.get("MediaContainer") or {}).get("Metadata") or [{}])[0].get("viewCount", 0)
        out["plex"].append({"where": "library", "ratingKey": rk, "viewCount": vc, "already": bool(vc) and not dry_run})
    # 2) Plex Discover (account-level), also when the library has it — keeps the account history complete
    imdb_id = t["imdb_id"] or t["series_imdb_id"]
    if imdb_id:
        pid = _plex_id(px, imdb_id, t["kind"])
        if pid and t["kind"] == "series":
            pid = _season_plex_id(px, pid, t["season_number"]) if t["season_number"] else None
        if pid and t["kind"] == "series":
            # a season is marked episode by episode (mark_episode skips watched ones); a season-level scrobble would
            # stamp «now» on every episode, including ones the owner watched long ago
            st = _discover_state(px, pid)
            out["plex"].append({"where": "discover", "plexId": pid, "viewCount": st.get("viewCount"),
                                "viewState": st.get("viewState"), "note": "season: episodes via mark_episode"})
            pid = None
        if pid:
            before = _discover_state(px, pid)
            if not dry_run and before.get("viewState") != "complete" and not (before.get("viewCount") or 0):
                r = px.s.put(f"{plexsrc.DISCOVER_PROVIDER}/actions/scrobble",
                             params={"identifier": DISCOVER_ID, "key": pid}, timeout=60)
                if r.status_code in (404, 405):  # documented fallback: some deployments accept GET
                    r = px.s.get(f"{plexsrc.DISCOVER_PROVIDER}/actions/scrobble",
                                 params={"identifier": DISCOVER_ID, "key": pid}, timeout=60)
                r.raise_for_status()
            st = _discover_state(px, pid)
            out["plex"].append({"where": "discover", "plexId": pid, "viewCount": st.get("viewCount"),
                                "viewState": st.get("viewState"),
                                "already": before.get("viewState") == "complete" or bool(before.get("viewCount"))})
        elif t["kind"] != "series":
            out["plex"].append({"where": "discover", "error": "no Plex id for " + imdb_id})
    confirmed = any((p.get("viewCount") or 0) > 0 or p.get("viewState") == "complete" for p in out["plex"])
    out["confirmed"] = confirmed
    if jellyfin and not dry_run:  # owner 2026-10-25: «видел» — и в Jellyfin (jellyfin_guard never re-marks)
        out["jellyfin"] = jellyfin_guard.mark_title(conn, title_id, jf=_jf())
    if not dry_run:
        with conn:
            if db_source:  # 'manual' = the owner said «я видел»; a Jellyfin transfer already has its own row
                conn.execute("INSERT OR REPLACE INTO watched (title_id, source, watched_at) VALUES (?, ?, datetime('now'))",
                             (title_id, db_source))
            if confirmed:
                conn.execute("INSERT OR REPLACE INTO watched (title_id, source, watched_at) VALUES (?, 'plex', datetime('now'))",
                             (title_id,))
        db.mark_sync(conn, "plex_mark", details=out)
    return out


if __name__ == "__main__":
    import sys
    c = db.connect()
    db.migrate(c)
    for row in find(c, " ".join(sys.argv[1:])):
        print(json.dumps({k: row[k] for k in ("id", "title_ru", "title_original", "year", "season_number", "imdb_id")},
                         ensure_ascii=False))

def unmark(conn, title_id: int, reason: str) -> dict:
    """Undo a WRONG «watched» mark made by the project (never the owner's own history): Plex library + Discover
    unscrobble, and the watched rows of that title written by mark(). Only for fixing our own mistakes; log the reason."""
    t = dict(conn.execute("SELECT * FROM titles WHERE id = ?", (title_id,)).fetchone())
    px = plexsrc.Plex()
    out = {"title_id": title_id, "title": t["title_ru"] or t["title_original"], "reason": reason, "plex": []}
    for (rk,) in conn.execute("SELECT rating_key FROM plex_items WHERE title_id = ?", (title_id,)).fetchall():
        px.s.get(f"{plexsrc.PLEX_URL}/:/unscrobble", params={"identifier": LIB_ID, "key": rk}, timeout=60).raise_for_status()
        out["plex"].append({"where": "library", "ratingKey": rk})
    pid = _plex_id(px, t["imdb_id"], t["kind"]) if t["imdb_id"] else None
    if pid:
        r = px.s.put(f"{plexsrc.DISCOVER_PROVIDER}/actions/unscrobble", params={"identifier": DISCOVER_ID, "key": pid}, timeout=60)
        if r.status_code in (404, 405):
            r = px.s.get(f"{plexsrc.DISCOVER_PROVIDER}/actions/unscrobble", params={"identifier": DISCOVER_ID, "key": pid}, timeout=60)
        r.raise_for_status()
        out["plex"].append({"where": "discover", "plexId": pid, **_discover_state(px, pid)})
    with conn:
        out["db_rows_removed"] = conn.execute("DELETE FROM watched WHERE title_id = ? AND source IN ('manual', 'plex')",
                                              (title_id,)).rowcount
    return out


def mark_series(conn, tv_id: int, seasons: list[int] | None = None, log=print) -> int:
    """«Я видел сериал X (сезоны …)» — the owner's words: every season title (or the given ones) and every episode,
    in Plex library + Discover (Discover keeps the state per episode). Returns the number of episodes."""
    from . import link
    from .sources import tmdb
    raw = tmdb.tv_raw(conn, tv_id) or {}
    imdb = (tmdb.get(conn, "tv_external_ids", str(tv_id), "", f"/tv/{tv_id}/external_ids")[1] or {}).get("imdb_id")
    n = 0
    for s in raw.get("seasons", []):
        sn = s.get("season_number")
        if not sn or (seasons and sn not in seasons):
            continue
        stid = link.ensure_season(conn, tv_id, sn)
        conn.commit()
        if stid:
            mark(conn, stid)
        for ep in (tmdb.season_raw(conn, tv_id, sn) or {}).get("episodes", []):
            if imdb:
                mark_episode(conn, imdb, sn, ep["episode_number"])
                n += 1
        log(f"{raw.get('name')} S{sn}: marked")
    return n


def mark_episodes(conn, tv_id: int, season: int, first: int, last: int) -> int:
    """«Видел» part of a TMDB season (episodes first..last) — the owner's words, in Plex library + Discover. For series the
    tracker numbers differently from TMDB («Универ. Новая общага»: 15 rutor seasons = TV blocks, 3 TMDB seasons)."""
    from .sources import tmdb
    imdb = (tmdb.get(conn, "tv_external_ids", str(tv_id), "", f"/tv/{tv_id}/external_ids")[1] or {}).get("imdb_id")
    n = 0
    for ep in (tmdb.season_raw(conn, tv_id, season) or {}).get("episodes", []):
        if imdb and first <= ep["episode_number"] <= last:
            mark_episode(conn, imdb, season, ep["episode_number"])
            n += 1
    return n


def mark_db(conn, title_id: int, reason: str) -> bool:
    """The DB side of the owner's «видел» for a tracker-numbered season whose Plex episodes were marked by TMDB numbering
    (mark_series / mark_episodes): the title row only, never Plex. reason — the owner's words."""
    with conn:
        if conn.execute("SELECT 1 FROM watched WHERE title_id = ? AND substr(source, -8) != '_partial'", (title_id,)).fetchone():
            return False
        conn.execute("INSERT INTO watched (title_id, source, watched_at) VALUES (?, 'manual', datetime('now'))", (title_id,))
    log_line = f"mark_db {title_id}: {reason}"
    print(log_line)
    return True


def unmark_season(conn, series_imdb: str, season: int, reason: str, px=None) -> dict:
    """Undo a WRONG «watched» of one season made by the project: every episode in Plex (library + Discover
    unscrobble) and the watched row of the season title. For our own mistakes only; log the reason."""
    from .sources import tmdb
    px = px or plexsrc.Plex()
    out = {"series_imdb_id": series_imdb, "season": season, "reason": reason, "episodes": 0}
    row = conn.execute("SELECT id, series_tmdb_id FROM titles WHERE series_imdb_id = ? AND season_number = ?",
                       (series_imdb, season)).fetchone()
    if row:
        for (rk,) in conn.execute("SELECT rating_key FROM plex_items WHERE title_id = ?", (row[0],)).fetchall():
            px.s.get(f"{plexsrc.PLEX_URL}/:/unscrobble", params={"identifier": LIB_ID, "key": rk}, timeout=60).raise_for_status()
    show = _plex_id(px, series_imdb, "series")
    sid = _season_plex_id(px, show, season) if show else None
    eps = (tmdb.season_raw(conn, row[1], season) or {}).get("episodes", []) if row and row[1] else []
    for ep in eps:
        eid = _child_id(px, sid, ep["episode_number"]) if sid else None
        if eid:
            r = px.s.put(f"{plexsrc.DISCOVER_PROVIDER}/actions/unscrobble", params={"identifier": DISCOVER_ID, "key": eid}, timeout=60)
            if r.status_code in (404, 405):
                r = px.s.get(f"{plexsrc.DISCOVER_PROVIDER}/actions/unscrobble", params={"identifier": DISCOVER_ID, "key": eid}, timeout=60)
            r.raise_for_status()
            out["episodes"] += 1
    if row:
        with conn:
            out["db_rows_removed"] = conn.execute("DELETE FROM watched WHERE title_id = ? AND source IN ('manual', 'plex')",
                                                  (row[0],)).rowcount
    return out
