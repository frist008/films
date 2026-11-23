"""«Я видел X» in Jellyfin without an API key: the item ids from the DB → one javascript_tool call in the logged-in Jellyfin
tab (films/browser/jellyfin_guard.js: skips played items, never un-marks). The owner, 2026-10-26: «Никакого ключа у тебя
есть доступ в хром чтобы всё делать и там скрипты гонять если надо».

    python -X utf8 -m films.sources.jellyfin_chrome code --title 123            # a film
    python -X utf8 -m films.sources.jellyfin_chrome code --titles 123,456       # several films — one call, one confirmation
    python -X utf8 -m films.sources.jellyfin_chrome code --episode tt0944947 1 3  # one episode
    python -X utf8 -m films.sources.jellyfin_chrome code --season tt0944947 1     # every episode of a season, one call
    python -X utf8 -m films.sources.jellyfin_chrome from-plex                     # all watched in Plex, not in Jellyfin
Prints the exact text to run in the Jellyfin tab (the hook jellyfin-chrome-guard.sh accepts only this shape). Several items
go in one __jfGuard.mark call (owner R274: one script, not a confirmation per item; review 2026-10-28).
"""
from __future__ import annotations

import argparse
import json
import re
import sys

from .. import db

GUARD = db.ROOT / "films" / "browser" / "jellyfin_guard.js"


# an item the last `films link jellyfin` did not see any more (file deleted — «Логан» 2026-11-13) cannot be marked
LIVE = "{a}.synced_at >= (SELECT datetime(max(synced_at), '-5 minutes') FROM jellyfin_items)"


def _words(*names) -> set[str]:
    return {w for n in names if n for w in re.findall(r"\w{3,}", n.lower().replace("ё", "е")) if not w.isdigit()}


def title_items(conn, title_id: int) -> list[str]:
    """Jellyfin films of the title — only those whose name shares a word with the title's names. A file Jellyfin bound to
    someone else's film by mistake is skipped and reported (2026-11-02: «Континенталь 2 / 3» bound to «Терминатор 2» /
    «История игрушек 3» were marked played with those films on 2026-10-28; the owner: «Не видел — снять»)."""
    rows = conn.execute("""SELECT x.item_id, x.name, x.original_title, t.title_ru, t.title_original, t.title_uk
                           FROM jellyfin_items x JOIN titles t ON t.id = x.title_id
                           WHERE x.title_id = ? AND x.kind = 'movie' AND """ + LIVE.format(a="x"), (title_id,)).fetchall()
    out = []
    for iid, name, orig, ru, t_orig, uk in rows:
        if _words(name, orig) & _words(ru, t_orig, uk):
            out.append(iid)
        else:
            print(f"пропущен: в Jellyfin «{name}» привязан к «{ru or t_orig}» — названия не совпадают", file=sys.stderr)
    return out


def episode_items(conn, series_imdb: str, season: int, episode: int) -> list[str]:
    return [i for (i,) in conn.execute(
        """SELECT e.item_id FROM jellyfin_items e JOIN jellyfin_items s ON s.item_id = e.series_item_id
           WHERE e.kind = 'episode' AND s.imdb_id = ? AND e.season = ? AND e.episode = ? AND """ + LIVE.format(a="e"),
        (series_imdb, season, episode))]


def season_items(conn, series_imdb: str, season: int) -> list[str]:
    return [i for (i,) in conn.execute(
        """SELECT e.item_id FROM jellyfin_items e JOIN jellyfin_items s ON s.item_id = e.series_item_id
           WHERE e.kind = 'episode' AND s.imdb_id = ? AND e.season = ? AND """ + LIVE.format(a="e") +
        " ORDER BY e.episode", (series_imdb, season))]


def from_plex(conn) -> tuple[list[str], dict]:
    """Owner, 2026-11-13: «подсинхронизируй … джелефин и плекс … по тому что я видел и не видел» — what Plex knows as watched
    (films: `watched` plex / manual, not *_partial; episodes: the Plex view history) and Jellyfin has as a file but not as
    played. Only marks: «не видел» in one of them is never pushed as an un-mark (both guards forbid it). Films go through
    title_items (a file bound to someone else's film is skipped, R298)."""
    films, eps, skipped = [], [], []
    for tid, ru in conn.execute(
            """SELECT DISTINCT w.title_id, t.title_ru FROM watched w JOIN titles t ON t.id = w.title_id
               WHERE w.source IN ('plex', 'manual') AND t.kind = 'movie'
                 AND EXISTS (SELECT 1 FROM jellyfin_items x WHERE x.title_id = w.title_id AND x.kind = 'movie'
                             AND """ + LIVE.format(a="x") + """)
                 AND NOT EXISTS (SELECT 1 FROM jellyfin_played p WHERE p.title_id = w.title_id)""").fetchall():
        ids = title_items(conn, tid)
        (films if ids else skipped).append({"title_id": tid, "title": ru, "items": ids})
    for imdb, season, episode, name in conn.execute(
            """SELECT DISTINCT t.series_imdb_id, h.season, h.episode, h.grandparent_title FROM plex_history h
               JOIN titles t ON t.id = h.title_id
               WHERE h.kind = 'episode' AND h.season IS NOT NULL AND h.episode IS NOT NULL AND t.series_imdb_id IS NOT NULL
                 AND NOT EXISTS (SELECT 1 FROM jellyfin_played p WHERE p.kind = 'episode' AND p.imdb_id = t.series_imdb_id
                                 AND p.season = h.season AND p.episode = h.episode)
               ORDER BY 1, 2, 3""").fetchall():
        ids = episode_items(conn, imdb, season, episode)
        if ids:
            eps.append({"series": name, "imdb": imdb, "season": season, "episode": episode, "items": ids})
    items = [i for f in films for i in f["items"]] + [i for e in eps for i in e["items"]]
    return items, {"films": films, "episodes": eps, "skipped_wrong_binding": skipped}


def code(items: list[str]) -> str:
    user = db.private().get("jellyfin", {}).get("user")
    return GUARD.read_text(encoding="utf-8") + f"\n__jfGuard.mark({json.dumps(items)}, {json.dumps(user)});\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["code", "from-plex"])
    ap.add_argument("--title", type=int)
    ap.add_argument("--titles")
    ap.add_argument("--episode", nargs=3)
    ap.add_argument("--season", nargs=2)
    a = ap.parse_args()
    conn = db.connect()
    if a.cmd == "from-plex":   # the report → data/cache/jellyfin_from_plex.json, the code → stdout
        items, rep = from_plex(conn)
        (db.ROOT / "data" / "cache" / "jellyfin_from_plex.json").write_text(
            json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"фильмов {len(rep['films'])}, серий {len(rep['episodes'])}, пропущено (чужая привязка) "
              f"{len(rep['skipped_wrong_binding'])}, элементов {len(items)}", file=sys.stderr)
        if items:
            print(code(items))
        return
    if a.titles:
        items = [i for t in a.titles.split(",") if t.strip() for i in title_items(conn, int(t))]
    elif a.season:
        items = season_items(conn, a.season[0], int(a.season[1]))
    elif a.title:
        items = title_items(conn, a.title)
    else:
        items = episode_items(conn, a.episode[0], int(a.episode[1]), int(a.episode[2]))
    if not items:
        raise SystemExit("в Jellyfin этого нет — отмечать нечего")
    print(code(items))


if __name__ == "__main__":
    main()
