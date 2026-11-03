"""Jellyfin source: the second local media server (set up 2026-09-25, same E:\\ files as Plex in part).

Run: python -X utf8 -m films.sources.jellyfin

Read-only: the server's SQLite database (jellyfin.db, with its -wal/-shm) is *copied* to data/cache/jellyfin/
and the copy is opened with mode=ro — the live database is never opened, the server keeps running.
No API key is needed (and none is stored).

What it gives:
- jellyfin_items — movies / series / seasons / episodes with Jellyfin's provider ids (Imdb, Tmdb, Tvdb) and
  file paths. Plex keeps many series as bare files without ids; the same path in Jellyfin identifies them.
- jellyfin_played — what the owner's Jellyfin user marked played. When a file is deleted Jellyfin keeps the
  mark on a PLACEHOLDER item with UserData.CustomDataKey = the IMDb id (movie) or series IMDb id + SSS + EEE
  (episode, e.g. tt6741278001002 = S01E02), so removed files still count.
The owner's Jellyfin user name is in config/private.toml [jellyfin] user (not in the repository).
"""
import json
import re
import shutil
import sqlite3
import tomllib
from pathlib import Path

from .. import db, personal

TYPES = {"MediaBrowser.Controller.Entities.Movies.Movie": "movie",
         "MediaBrowser.Controller.Entities.TV.Series": "series",
         "MediaBrowser.Controller.Entities.TV.Season": "season",
         "MediaBrowser.Controller.Entities.TV.Episode": "episode"}
EPISODE_KEY = re.compile(r"^(tt\d{7,9})(\d{3})(\d{3})$")
MOVIE_KEY = re.compile(r"^tt\d{7,9}$")


def _data_dir() -> Path:
    with open(db.ROOT / "config" / "settings.toml", "rb") as f:
        return Path(tomllib.load(f)["jellyfin"]["data_dir"])


def snapshot() -> Path:
    """Copy jellyfin.db (+ -wal/-shm, so the latest writes are included) into data/cache/jellyfin/."""
    src = _data_dir()
    dst = db.ROOT / "data" / "cache" / "jellyfin"
    dst.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        p = src / f"jellyfin.db{suffix}"
        if p.exists():
            shutil.copy2(p, dst / p.name)
        elif (dst / p.name).exists():
            (dst / p.name).unlink()
    return dst / "jellyfin.db"


def _items(j) -> list[dict]:
    libs = {r[0]: r[1] for r in j.execute(
        "SELECT Id, Name FROM BaseItems WHERE Type = 'MediaBrowser.Controller.Entities.CollectionFolder'")}
    lib_of = {}
    for item, parent in j.execute("SELECT ItemId, ParentItemId FROM AncestorIds"):
        if parent in libs:
            lib_of.setdefault(item, libs[parent])
    prov = {}
    for item, pid, val in j.execute("SELECT ItemId, ProviderId, ProviderValue FROM BaseItemProviders"):
        prov.setdefault(item, {})[pid.lower()] = val
    out = []
    for r in j.execute(f"""SELECT Id, Type, Name, OriginalTitle, ProductionYear, Path, SeriesId, IndexNumber,
                                  ParentIndexNumber, RunTimeTicks, Size
                           FROM BaseItems WHERE Type IN ({",".join("?" * len(TYPES))})""", list(TYPES)):
        iid, typ, name, orig, year, path, series, idx, pidx, ticks, size = r
        kind = TYPES[typ]
        p = prov.get(iid, {})
        out.append({
            "item_id": iid, "kind": kind, "name": name, "original_title": orig, "year": year, "path": path,
            "imdb_id": p.get("imdb"), "tmdb_id": p.get("tmdb"), "tvdb_id": p.get("tvdb"),
            "series_item_id": series if kind in ("season", "episode") else None,
            "season": pidx if kind == "episode" else (idx if kind == "season" else None),
            "episode": idx if kind == "episode" else None,
            "runtime_min": round(ticks / 600_000_000) if ticks else None, "size_bytes": size,
            "library": lib_of.get(iid)})
    return out


def _played(j, user: str, items: dict) -> list[dict]:
    """The owner's played marks, one row per watched movie / episode (keys deduplicated to the IMDb form)."""
    out = {}
    for key, item_id, played_at, typ in j.execute(
            """SELECT d.CustomDataKey, d.ItemId, d.LastPlayedDate, b.Type FROM UserData d
               JOIN Users u ON u.Id = d.UserId LEFT JOIN BaseItems b ON b.Id = d.ItemId
               WHERE u.Username = ? AND d.Played = 1""", (user,)):
        live = items.get(item_id) if typ != "PLACEHOLDER" else None
        row = None
        if live and live["kind"] == "movie" and live["imdb_id"]:
            row = {"key": live["imdb_id"], "kind": "movie", "imdb_id": live["imdb_id"]}
        elif live and live["kind"] == "episode":
            series = items.get(live["series_item_id"]) or {}
            if series.get("imdb_id") and live["season"] is not None and live["episode"] is not None:
                row = {"key": f"{series['imdb_id']}{live['season']:03d}{live['episode']:03d}", "kind": "episode",
                       "imdb_id": series["imdb_id"], "season": live["season"], "episode": live["episode"]}
        elif m := EPISODE_KEY.match(key or ""):
            row = {"key": key, "kind": "episode", "imdb_id": m.group(1), "season": int(m.group(2)),
                   "episode": int(m.group(3))}
        elif MOVIE_KEY.match(key or ""):
            row = {"key": key, "kind": "movie", "imdb_id": key}
        if not row:
            continue  # the same mark under its TMDB / TVDB / guid key — the IMDb key row carries it
        row.update(item_id=item_id if live else None, played_at=(played_at or "")[:19].replace("T", " ") or None)
        prev = out.get(row["key"])
        if not prev or (row["played_at"] or "") > (prev["played_at"] or ""):
            out[row["key"]] = row
    return list(out.values())


def sync(conn) -> dict:
    user = db.private()["jellyfin"]["user"]
    path = snapshot()
    j = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        items = _items(j)
        by_id = {i["item_id"]: i for i in items}
        played = _played(j, user, by_id)
    finally:
        j.close()
    ns = personal.names()
    with conn:
        for i in items:
            if personal.is_plex_row(i, ns):   # the owner's own videos are not kept (films.personal)
                continue
            conn.execute(
                """INSERT INTO jellyfin_items (item_id, kind, name, original_title, year, path, imdb_id, tmdb_id, tvdb_id,
                       series_item_id, season, episode, runtime_min, size_bytes, library, synced_at)
                   VALUES (:item_id, :kind, :name, :original_title, :year, :path, :imdb_id, :tmdb_id, :tvdb_id,
                       :series_item_id, :season, :episode, :runtime_min, :size_bytes, :library, datetime('now'))
                   ON CONFLICT(item_id) DO UPDATE SET kind=excluded.kind, name=excluded.name,
                       original_title=excluded.original_title, year=excluded.year, path=excluded.path,
                       imdb_id=excluded.imdb_id, tmdb_id=excluded.tmdb_id, tvdb_id=excluded.tvdb_id,
                       series_item_id=excluded.series_item_id, season=excluded.season, episode=excluded.episode,
                       runtime_min=excluded.runtime_min, size_bytes=excluded.size_bytes, library=excluded.library,
                       synced_at=excluded.synced_at""", i)
        for p in played:
            p.setdefault("season", None)
            p.setdefault("episode", None)
            conn.execute(
                """INSERT INTO jellyfin_played (key, kind, imdb_id, season, episode, item_id, played_at)
                   VALUES (:key, :kind, :imdb_id, :season, :episode, :item_id, :played_at)
                   ON CONFLICT(key) DO UPDATE SET item_id=excluded.item_id,
                       played_at=max(coalesce(jellyfin_played.played_at, ''), coalesce(excluded.played_at, ''))""", p)
    stats = {"items": {k: sum(1 for i in items if i["kind"] == k) for k in TYPES.values()},
             "items_with_imdb": sum(1 for i in items if i["imdb_id"]),
             "played_movies": sum(1 for p in played if p["kind"] == "movie"),
             "played_episodes": sum(1 for p in played if p["kind"] == "episode"),
             "last_played": max((p["played_at"] or "" for p in played), default=None)}
    db.mark_sync(conn, "jellyfin", stats["last_played"], stats)
    return stats


if __name__ == "__main__":
    c = db.connect()
    db.migrate(c)
    print(json.dumps(sync(c), ensure_ascii=False, indent=1))
