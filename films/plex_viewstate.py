"""Страж «видел»: база против живого состояния Plex (баг 2026-11-23).

Владелец снял в Plex отметки «просмотрено» у «L: Изменить мир» и трёх сериалов, а база (watched: 'plex' и 'manual' от
нашей отметки 23.10) продолжала считать их просмотренными, и ответ ему дали по базе, не спросив Plex («ты уверен что в
плекс они отмечены просмотренными ты проверил?»). Синхронизация (`films link plex`) берёт просмотры из plex_history —
снятую отметку она не видит. Этот модуль спрашивает Plex напрямую, ТОЛЬКО ЧТЕНИЕМ (ничего в Plex не пишет):

- фильм: Plex Discover (аккаунт) `userState` — viewCount 0 и не «complete», и в библиотеке viewCount 0 (ключи
  plex_items с present = 1 и живой поиск `/library/all?guid=plex://movie/<id>` — ключи базы устаревают) → владелец
  снял отметку;
- сезон: Discover — у сезона нет ни одной просмотренной серии (`viewedLeafCount` 0 и viewCount 0 у каждой серии), и в
  библиотеке у сезона ни одной просмотренной серии (ключи базы и сериал по guid → сезон, `viewedLeafCount`) → снял.
  Хоть одна серия просмотрена (частично) — не трогаем;
- Plex не нашёл тайтл (нет Discover id / сезона) — «не проверено», не трогаем.

Снятое владельцем: строки watched тайтла → `<source>_reverted_partial` (`films.link.revert_watched`, из базы ничего не
удаляется; Jellyfin не трогаем). «Снял» — только когда аккаунт Plex этот тайтл (или серию сезона) знал просмотренным
(история plex.tv в plex_history или наша отметка в Discover, `account_ids`), а теперь там 0. В Plex 0 без такого следа
(просмотр жил только в библиотеке, которую перестроили, или отметка не дошла, или сезон с нумерацией трекера —
`watched.mark_db`) — в отчёт «no_account_trace», без правки. IMDb id снятых — в отчёт: их добавить в config/settings.toml [watched] not_seen,
иначе история Plex вернёт отметку при следующем `films link plex` (правит основной поток, не этот модуль).
Строки *_partial не проверяются (просмотром не считаются).

Запросы чтения — через общий интервал `locks.pace('plex_read', 0.3)` (запись в Plex — свой `plex_write`).

CLI: python -X utf8 -m films.plex_viewstate check [--dry-run] [--limit N] [--ids 1,2,3] [--json PATH]
"""
from __future__ import annotations

import json
import sys
import time

import requests

from . import db, locks
from . import watched as W
from .link import revert_watched
from .sources import plex as plexsrc

READ_PACE = ("plex_read", 0.3)
LIB_BATCH = 40          # ratingKey через запятую в одном запросе к библиотеке

CANDIDATES = """
SELECT t.id, t.kind, t.imdb_id, t.series_imdb_id, t.season_number, t.year,
       coalesce(t.title_ru, t.title_original) AS title, group_concat(w.source) AS sources
FROM watched w JOIN titles t ON t.id = w.title_id
WHERE w.source IN ('plex', 'manual')
GROUP BY t.id ORDER BY t.id
"""


class Paced:
    """Обёртка клиента Plex: каждый GET ждёт общий интервал чтения (все процессы проекта)."""

    def __init__(self, px, pace=READ_PACE):
        self.px, self._pace = px, pace
        self.s = px.s

    def get(self, url, params=None, **kw):
        if self._pace:
            locks.pace(*self._pace)
        return self.px.get(url, params=params, **kw)


def candidates(conn, limit: int | None = None, ids: list[int] | None = None) -> list[dict]:
    """Тайтлы, которые база считает просмотренными ('plex' / 'manual'). С `ids` — эти тайтлы, даже если база их
    просмотренными не считает (ответ на «видел ли я X / отмечено ли в Plex»)."""
    if ids:
        q = CANDIDATES.replace("FROM watched w JOIN titles t ON t.id = w.title_id",
                               "FROM titles t LEFT JOIN watched w ON w.title_id = t.id AND w.source IN ('plex', 'manual')")
        q = q.replace("WHERE w.source IN ('plex', 'manual')", f"WHERE t.id IN ({','.join('?' * len(ids))})")
        rows = [dict(r) for r in conn.execute(q, list(ids)).fetchall()]
    else:
        rows = [dict(r) for r in conn.execute(CANDIDATES).fetchall()]
    for r in rows:
        r["sources"] = sorted(set((r["sources"] or "").split(",")) - {""})
    return rows[:limit] if limit else rows


def _watched(st: dict | None) -> bool:
    st = st or {}
    return (st.get("viewCount") or 0) > 0 or st.get("viewState") == "complete" or (st.get("viewedLeafCount") or 0) > 0


def _meta(data: dict) -> list[dict]:
    return (data.get("MediaContainer") or {}).get("Metadata") or []


def library_counts(conn, px, title_id: int) -> dict[str, int]:
    """viewCount файлов тайтла в библиотеке Plex по ratingKey из plex_items (present = 1): {ratingKey: viewCount}.
    Ключи пачкой через запятую: сервер отдаёт найденные, 404 — ни одного не осталось (plex_items с последнего скана
    устаревают: 2026-11-23 библиотеки перестроены, старые ключи — 404). Поэтому ещё и `library_by_guid`."""
    rks = [str(rk) for (rk,) in conn.execute(W.PRESENT_ITEMS, (title_id,)).fetchall()]
    out: dict[str, int] = {}
    for i in range(0, len(rks), LIB_BATCH):
        try:
            meta = _meta(px.get("/library/metadata/" + ",".join(rks[i:i + LIB_BATCH])))
        except requests.HTTPError as e:
            if e.response is None or e.response.status_code != 404:
                raise
            meta = []
        for m in meta:
            out[str(m.get("ratingKey"))] = int(m.get("viewCount") or m.get("viewedLeafCount") or 0)
    return out


def library_by_guid(px, plex_id: str, kind: str, season: int | None = None) -> dict[str, int]:
    """Живой поиск в библиотеке по guid Plex (plex://movie|show/<Discover id>): {ratingKey: просмотрено}. Фильм —
    viewCount; сезон — viewedLeafCount сезона с этим номером у каждого найденного сериала."""
    typ, pref = (1, "movie") if kind == "movie" else (2, "show")
    items = _meta(px.get("/library/all", params={"guid": f"plex://{pref}/{plex_id}", "type": typ}))
    out: dict[str, int] = {}
    for m in items:
        if kind == "movie":
            out[str(m["ratingKey"])] = int(m.get("viewCount") or 0)
            continue
        for s in _meta(px.get(f"/library/metadata/{m['ratingKey']}/children")):
            if s.get("index") == season:
                out[str(s["ratingKey"])] = int(s.get("viewedLeafCount") or s.get("viewCount") or 0)
    return out


def movie_state(conn, px, t: dict) -> dict:
    """Аккаунт (Discover) первым: просмотрено там — дальше не спрашиваем; иначе библиотека (ключи базы + guid)."""
    out = {"discover": None, "library": {}}
    pid = W._plex_id(px, t["imdb_id"], "movie") if t["imdb_id"] else None
    if pid:
        st = W._discover_state(px, pid)
        out["discover"] = {"plexId": pid, "viewCount": st.get("viewCount") or 0, "viewState": st.get("viewState")}
        if _watched(st):
            return dict(out, verdict="watched")
    lib = library_counts(conn, px, t["id"])
    if pid:
        lib.update(library_by_guid(px, pid, "movie"))
    out["library"] = lib
    if any(lib.values()):
        return dict(out, verdict="watched")
    if not pid:
        return dict(out, verdict="unknown", why="нет Plex Discover id")
    return dict(out, verdict="not_watched")


def season_state(conn, px, t: dict, shows: dict) -> dict:
    """Сезон: Discover (viewedLeafCount сезона, при нуле — каждая серия), затем библиотека. Хоть одна серия
    просмотрена — «watched_some» (частично — не трогаем)."""
    out = {"discover": None, "library": {}}
    imdb = t["series_imdb_id"] or t["imdb_id"]
    if not imdb or not t["season_number"]:
        lib = library_counts(conn, px, t["id"])
        return dict(out, library=lib, verdict="watched_some" if any(lib.values()) else "unknown",
                    why="нет IMDb id сериала или номера сезона")
    if imdb not in shows:
        show = W._plex_id(px, imdb, "series")
        shows[imdb] = (show, plexsrc.discover_children(px, show, {"includeUserState": 1}) if show else [])
    show, seasons = shows[imdb]
    season = next((s for s in seasons if s.get("index") == t["season_number"]), None)
    if season:
        out["discover"] = {"plexId": season["ratingKey"], "viewedLeafCount": season.get("viewedLeafCount") or 0,
                           "leafCount": season.get("leafCount"), "viewCount": season.get("viewCount") or 0}
        if _watched(season):
            return dict(out, verdict="watched_some")
        # сезон говорит «0» — перепроверяем каждую серию (владелец: «ты проверил?»)
        eps = plexsrc.discover_children(px, season["ratingKey"], {"includeUserState": 1})
        seen = [e.get("index") for e in eps if _watched(e)]
        out["discover"].update(episodes=len(eps), episodes_watched=seen,
                               episode_ids=[e["ratingKey"] for e in eps if e.get("ratingKey")])
        if seen:
            return dict(out, verdict="watched_some")
    lib = library_counts(conn, px, t["id"])
    if show:
        lib.update(library_by_guid(px, show, "series", t["season_number"]))
    out["library"] = lib
    if any(lib.values()):
        return dict(out, verdict="watched_some")
    if not show:
        return dict(out, verdict="unknown", why="нет Plex Discover id сериала")
    if not season:
        return dict(out, verdict="unknown", why=f"в Plex Discover нет сезона {t['season_number']}")
    return dict(out, verdict="not_watched")


WRITES_LOG = db.ROOT / "data" / "cache" / "logs" / "plex_writes.jsonl"


def account_ids(conn) -> set[str]:
    """Discover id всего, что когда-либо было «просмотрено» в аккаунте: история plex.tv (plex_history, source
    'plextv': фильмы, серии, сезоны — plex://<type>/<id>) и наши отметки в Discover (plex_writes.jsonl)."""
    out = {(g or "").rsplit("/", 1)[-1] for (g,) in conn.execute(
        "SELECT plex_guid FROM plex_history WHERE source = 'plextv' AND plex_guid LIKE 'plex://%'")}
    try:
        with WRITES_LOG.open(encoding="utf-8") as f:
            for line in f:
                with __import__("contextlib").suppress(ValueError):
                    e = json.loads(line)
                    if e.get("allowed") and "discover" in (e.get("url") or "") and e.get("key"):
                        out.add(str(e["key"]))
    except OSError:
        pass
    out.discard("")
    return out


def seen_confirmed() -> set[str]:
    """[watched] seen_confirmed: владелец сказал «Да, смотрел», хотя в Plex отметки нет («tt…» — фильм / весь сериал,
    «tt…:S2» — сезон). Проверка такие тайтлы не снимает (2026-11-23: «Что, если...?» S2, «Агент Картер» S2)."""
    import tomllib
    try:
        with open(db.ROOT / "config" / "settings.toml", "rb") as f:
            return set(tomllib.load(f).get("watched", {}).get("seen_confirmed", []))
    except OSError:
        return set()


def _discover_ids(st: dict) -> set[str]:
    d = st.get("discover") or {}
    return {str(x) for x in [d.get("plexId"), *d.get("episode_ids", [])] if x}


def check(conn, dry_run: bool = False, limit: int | None = None, ids: list[int] | None = None, px=None,
          log=print, progress_every: int = 50, known: set[str] | None = None) -> dict:
    """Сверить «видел» базы с живым Plex. Возвращает отчёт; без dry_run снятое владельцем → *_reverted_partial.

    «Снял владелец» = в Plex сейчас 0, а аккаунт этот тайтл (или серию сезона) знал просмотренным (`account_ids`).
    В Plex 0 и аккаунт никогда не знал (просмотр был только в библиотеке, которую потом перестроили / удалили файл,
    или наша отметка не дошла) — «no_account_trace»: не правится, только отчёт (2026-11-23: «Министерство
    неджентльменских дел» — просмотр 2024 в удалённой библиотеке «Все», а не снятая отметка)."""
    px = Paced(plexsrc.Plex()) if px is None else px
    known = account_ids(conn) if known is None else known
    rows = candidates(conn, limit, ids)
    rep = {"dry_run": dry_run, "checked": 0, "watched": 0, "reverted": [], "no_account_trace": [],
           "unknown": [], "errors": [], "owner_confirmed": []}
    confirmed = seen_confirmed()
    shows: dict = {}
    t0 = time.time()
    for i, t in enumerate(rows, 1):
        item = {k: t[k] for k in ("id", "kind", "title", "year", "season_number", "sources")}
        item["imdb_id"] = t["imdb_id"] or t["series_imdb_id"]
        try:
            st = season_state(conn, px, t, shows) if t["kind"] == "series" or t["season_number"] else movie_state(conn, px, t)
        except (requests.RequestException, KeyError, ValueError) as e:
            rep["errors"].append(dict(item, error=f"{type(e).__name__}: {e}"[:300]))
            continue
        rep["checked"] += 1
        item.update(state={k: v for k, v in st.items() if k != "verdict"}, verdict=st["verdict"])
        key = f"{item['imdb_id']}:S{t['season_number']}" if t["season_number"] else item["imdb_id"]
        item["not_seen_key"] = key
        if st["verdict"] == "not_watched" and (key in confirmed or item["imdb_id"] in confirmed):
            # владелец ответил «Да, смотрел», хотя в Plex 0 — его слово важнее, отметка в базе остаётся
            rep["owner_confirmed"].append(item)
            continue
        if st["verdict"] == "unknown":
            rep["unknown"].append(item)
        elif st["verdict"] != "not_watched":
            rep["watched"] += 1
        elif _discover_ids(st) & known:
            if not dry_run:
                with conn:
                    item["rows_reverted"] = revert_watched(conn, t["id"])
            rep["reverted"].append(item)
        else:
            rep["no_account_trace"].append(item)
        if progress_every and (i % progress_every == 0 or i == len(rows)):
            el = time.time() - t0
            eta = time.strftime("%H:%M", time.localtime(time.time() + el / i * (len(rows) - i)))
            log(f"PROGRESS {i * 100 // max(len(rows), 1)}% ETA {eta} — {i}/{len(rows)}, снято владельцем "
                f"{len(rep['reverted'])}, без следа в аккаунте {len(rep['no_account_trace'])}, "
                f"не проверено {len(rep['unknown'])}, ошибок {len(rep['errors'])}")
    rep["total"] = len(rows)
    rep["not_seen_suggest"] = sorted({r["not_seen_key"] for r in rep["reverted"] if r["imdb_id"]})
    if not dry_run and not limit and not ids:
        db.mark_sync(conn, "plex_viewstate", details={k: (len(v) if isinstance(v, list) else v) for k, v in rep.items()})
    return rep


def _fmt(r: dict) -> str:
    s = f" S{r['season_number']}" if r.get("season_number") else ""
    return f"{r['id']}\t{r['title']}{s} ({r.get('year')})\t{r.get('imdb_id')}\t{','.join(r['sources'])}"


def main(argv: list[str]) -> int:
    if not argv or argv[0] != "check":
        print(__doc__)
        return 2
    dry = "--dry-run" in argv
    limit = int(argv[argv.index("--limit") + 1]) if "--limit" in argv else None
    ids = [int(x) for x in argv[argv.index("--ids") + 1].split(",")] if "--ids" in argv else None
    conn = db.connect()
    rep = check(conn, dry_run=dry, limit=limit, ids=ids, log=lambda m: print(m, flush=True))
    if "--json" in argv:
        with open(argv[argv.index("--json") + 1], "w", encoding="utf-8") as f:
            json.dump(rep, f, ensure_ascii=False, indent=1)
    head = "ПРОБНЫЙ ПРОГОН (база не изменена)" if dry else "база исправлена"
    print(f"\n{head}: проверено {rep['checked']} из {rep['total']}; в Plex просмотрено {rep['watched']}; "
          f"снято владельцем {len(rep['reverted'])}; в Plex 0 без следа в аккаунте {len(rep['no_account_trace'])}; "
          f"не проверено {len(rep['unknown'])}; ошибок {len(rep['errors'])}")
    for name, key in (("Снято владельцем в Plex (→ *_reverted_partial)", "reverted"),
                      ("В Plex 0, аккаунт просмотра не знал — библиотека / отметка не дошла (не правится)",
                       "no_account_trace"),
                      ("В Plex 0, но владелец сказал «Да, смотрел» ([watched] seen_confirmed, не правится)",
                       "owner_confirmed"),
                      ("Не проверено (Plex не нашёл)", "unknown"), ("Ошибки", "errors")):
        if rep[key]:
            print(f"\n{name}:")
            for r in rep[key]:
                print("  " + _fmt(r) + (f"\t{r.get('why') or r['state'].get('why', '')}" if key == "unknown" else "")
                      + (f"\t{r['error']}" if key == "errors" else ""))
    if rep["not_seen_suggest"]:
        print("\nПредложение: добавить в config/settings.toml [watched] not_seen:\n  "
              + ", ".join(f'"{i}"' for i in rep["not_seen_suggest"]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
