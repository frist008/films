"""The only way the project writes to Jellyfin: the owner's «я видел X» also marks it played there (owner, 2026-10-25:
«Отмечать ещё и в Jellyfin? — Да, и в Jellyfin»).

Same safety rule as films/sources/plex_guard.py: an item Jellyfin already has as played is NEVER marked again (a second
mark rewrites its «last played» date and breaks the owner's history); nothing is ever un-marked here.

Auth: a Jellyfin API key the owner created for the project — JELLYFIN_API_KEY in .env, or the key named «films» in
the server's dashboard (Панель управления → Ключи API), read from the read-only copy of jellyfin.db. Without a key every
call returns {"error": "no API key"} and nothing is written. The user is config/private.toml [jellyfin] user.
API: GET /Items/{id}?userId= → UserData.Played; POST /UserPlayedItems/{id}?userId= (Jellyfin 10.9+).
"""
import os
import sqlite3

import requests

from .. import db
from . import jellyfin

URL = "http://127.0.0.1:8096"


def _key(j) -> str | None:
    k = os.environ.get("JELLYFIN_API_KEY")
    env = db.ROOT / ".env"
    if not k and env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("JELLYFIN_API_KEY="):
                k = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not k:
        row = j.execute("SELECT AccessToken FROM ApiKeys WHERE lower(Name) = 'films' ORDER BY DateCreated DESC").fetchone()
        k = row[0] if row else None
    return k or None


class Jellyfin:
    def __init__(self):
        j = sqlite3.connect(f"file:{jellyfin.snapshot()}?mode=ro", uri=True)
        self.key = _key(j)
        user = db.private().get("jellyfin", {}).get("user")
        row = j.execute("SELECT Id FROM Users WHERE Username = ?", (user,)).fetchone() if user else None
        self.user_id = row[0] if row else None
        j.close()
        self.s = requests.Session()
        if self.key:
            self.s.headers["Authorization"] = f'MediaBrowser Token="{self.key}"'

    def played(self, item_id: str) -> bool:
        r = self.s.get(f"{URL}/Items/{item_id}", params={"userId": self.user_id}, timeout=30)
        r.raise_for_status()
        return bool((r.json().get("UserData") or {}).get("Played"))

    def mark(self, item_id: str) -> dict:
        """Mark one item played unless it already is. Returns {"item_id", "already"|"played"|"error"}."""
        out = {"item_id": item_id}
        if not self.key or not self.user_id:
            return out | {"error": "no API key" if not self.key else "no Jellyfin user"}
        try:
            if self.played(item_id):
                return out | {"already": True}
            self.s.post(f"{URL}/UserPlayedItems/{item_id}", params={"userId": self.user_id}, timeout=30).raise_for_status()
            return out | {"played": self.played(item_id)}
        except requests.RequestException as e:
            return out | {"error": str(e)[:200]}


def mark_title(conn, title_id: int, jf: Jellyfin | None = None) -> list[dict]:
    """Every Jellyfin movie item linked to this title (seasons go episode by episode via mark_episode)."""
    jf = jf or Jellyfin()
    return [jf.mark(i) for (i,) in conn.execute(
        "SELECT item_id FROM jellyfin_items WHERE title_id = ? AND kind = 'movie'", (title_id,)).fetchall()]


def mark_episode(conn, series_imdb: str, season: int, episode: int, jf: Jellyfin | None = None) -> list[dict]:
    jf = jf or Jellyfin()
    return [jf.mark(i) for (i,) in conn.execute(
        """SELECT e.item_id FROM jellyfin_items e JOIN jellyfin_items s ON s.item_id = e.series_item_id
           WHERE e.kind = 'episode' AND s.imdb_id = ? AND e.season = ? AND e.episode = ?""",
        (series_imdb, season, episode)).fetchall()]
