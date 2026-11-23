"""Всё, что база знает о тайтле — справочный ответ владельцу без сужения поиска (владелец, 2026-11-22:
«ты должен быть справочником на всё, а не сужать поиск и экономить»).

    python -X utf8 -m films.lookup "кендимен"          # любое написание: RU / UK / оригинал / латиница, е = ё = э
    python -X utf8 -m films.lookup tt9347730           # IMDb id, TMDB id (число) или id строки titles
    python -X utf8 -m films.lookup "кендимен" --no-rank  # без расчёта таблицы (rank.compute ≈ 1 мин)

Что выдаёт:
1. Тайтлы, чьё название (титулы titles, aka IMDb) содержит запрос, и все части их коллекций TMDB / сезоны того же сериала.
2. По каждому тайтлу — каждая строка каждой таблицы со столбцом title_id (общий обход схемы: новая таблица попадёт сама),
   раздачи (releases.title_id и release_titles), коллекция TMDB с частями, которых в базе нет, франшизы владельца.
3. Строки без привязки к тайтлу, где встречается название: раздачи, Plex, Jellyfin, диски, qBittorrent, магазины, франшизы.
4. Списки владельца и ответы опросов: config/* и data/cache/*.json, где встречается название или id.
5. Таблица: показан ли тайтл, причины скрытия, баллы (rank.compute).
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

from films import db

BIG = {"parsed", "body", "raw", "tracker_meta", "episode_dates", "guids", "files", "info", "details", "keywords",
       "wiki_links", "filming_locations", "candidates"}
SKIP_TABLES = {"release_raw", "tmdb_raw"}  # сырые страницы — только кэш
LIST_FILES_MAX = 30_000_000


def norm(s: str | None) -> str:
    s = (s or "").lower().replace("ё", "е").replace("э", "е")
    return re.sub(r"[^\w]+", " ", s).strip()


def _cols(conn, table):
    return [r[1] for r in conn.execute(f'pragma table_info("{table}")')]


def _tables(conn):
    return [r[0] for r in conn.execute("select name from sqlite_master where type='table' order by name")
            if r[0] not in SKIP_TABLES]


def _short(v):
    if isinstance(v, bytes):
        return f"<{len(v)} байт>"
    if isinstance(v, str) and len(v) > 300:
        return v[:300] + "…"
    return v


def _row(conn, table, row):
    cols = _cols(conn, table)
    return {c: _short(v) for c, v in zip(cols, row) if v not in (None, "") and c not in BIG}


def find_titles(conn, query: str) -> list[int]:
    q = query.strip()
    if re.fullmatch(r"tt\d+", q):
        return [r[0] for r in conn.execute("select id from titles where imdb_id=? or series_imdb_id=?", (q, q))]
    if q.isdigit():
        return [r[0] for r in conn.execute("select id from titles where id=? or tmdb_id=? or series_tmdb_id=?",
                                           (int(q), int(q), int(q)))]
    nq = norm(q)
    ids = set()
    for tid, *names in conn.execute("select id, title_original, title_latin, title_ru, title_uk from titles"):
        if any(nq in norm(n) for n in names if n):
            ids.add(tid)
    tconsts = set()
    for tconst, title in conn.execute("select tconst, title from imdb_akas where tconst in "
                                      "(select imdb_id from titles where imdb_id is not null)"):
        if nq in norm(title):
            tconsts.add(tconst)
    if tconsts:
        marks = ",".join("?" * len(tconsts))
        ids |= {r[0] for r in conn.execute(f"select id from titles where imdb_id in ({marks})", list(tconsts))}
    # части коллекций и сезоны того же сериала
    more = set()
    for tid in ids:
        coll, skey = conn.execute("select collection_id, series_key from titles where id=?", (tid,)).fetchone()
        if coll:
            more |= {r[0] for r in conn.execute("select id from titles where collection_id=?", (coll,))}
        if skey:
            more |= {r[0] for r in conn.execute("select id from titles where series_key=?", (skey,))}
    return sorted(ids | more)


def dossier(conn, tid: int) -> dict:
    out = {}
    trow = conn.execute("select * from titles where id=?", (tid,)).fetchone()
    out["titles"] = [_row(conn, "titles", trow)]
    for table in _tables(conn):
        if table == "titles" or "title_id" not in _cols(conn, table):
            continue
        rows = conn.execute(f'select * from "{table}" where title_id=?', (tid,)).fetchall()
        if table == "releases":
            rows = conn.execute("select * from releases where title_id=? or id in "
                                "(select release_id from release_titles where title_id=?)", (tid, tid)).fetchall()
        if rows:
            out[table] = [_row(conn, table, r) for r in rows]
    if "credits" in out:  # имена людей вместо id
        out["credits"] = [
            {"role": r[0], "name": r[1], "name_ru": r[2], "character": r[3], "job": r[4]}
            for r in conn.execute("select c.role, p.name, p.name_ru, c.character, c.job from credits c join people p "
                                  "on p.id=c.person_id where c.title_id=? order by c.role, c.ord", (tid,))]
    t = out["titles"][0]
    if t.get("collection_id"):
        c = conn.execute("select name_ru, name_original, parts from collections where id=?",
                         (t["collection_id"],)).fetchone()
        if c:
            parts = json.loads(c[2] or "[]")
            have = {r[0] for r in conn.execute("select tmdb_id from titles where collection_id=?", (t["collection_id"],))}
            out["collection"] = {"name": c[0] or c[1], "parts": [
                {**{k: p.get(k) for k in ("tmdb_id", "title_ru", "title_original", "release_date") if p.get(k)},
                 "в базе": (p.get("tmdb_id") or p.get("id")) in have}
                for p in parts]}
    keys = [k for k in (t.get("imdb_id"), t.get("series_imdb_id")) if k]
    tm = [k for k in (t.get("tmdb_id"), t.get("series_tmdb_id")) if k]
    if keys or tm:
        rows = conn.execute("select * from franchise_members where imdb_id in (%s) or tmdb_id in (%s)" % (
            ",".join("?" * len(keys)) or "''", ",".join("?" * len(tm)) or "''"), keys + tm).fetchall()
        if rows:
            out["franchise_members"] = [_row(conn, "franchise_members", r) for r in rows]
    return out


UNLINKED = [  # таблица, столбцы с названием
    ("releases", ("name",)), ("plex_items", ("plex_title", "original_title", "show_title", "file_path")),
    ("plex_history", ("title", "grandparent_title")), ("jellyfin_items", ("name", "original_title", "path")),
    ("disk_files", ("path", "parsed_title")), ("qbt_torrents", ("name",)), ("store_items", ("title",)),
    ("franchise_members", ("title", "title_ru")), ("games", ("title",)), ("game_adaptations", ("title",)),
]


def unlinked(conn, query: str, known: set[int]) -> dict:
    nq = norm(query)
    out = {}
    for table, cols in UNLINKED:
        tc = _cols(conn, table)
        has_tid = "title_id" in tc
        sel = ", ".join(cols) + (", title_id" if has_tid else "")
        linked = set()
        if table == "releases":  # раздача тайтла может быть связана только через release_titles
            linked = {r[0] for r in conn.execute("select release_id from release_titles where title_id in (%s)"
                                                 % ",".join(map(str, known)))} if known else set()
        hits = []
        for r in conn.execute(f'select rowid, {sel} from "{table}"'):
            if (has_tid and r[-1] in known) or r[0] in linked:
                continue
            if any(nq in norm(v) for v in r[1:1 + len(cols)] if isinstance(v, str)):
                hits.append(_row(conn, table, conn.execute(f'select * from "{table}" where rowid=?', (r[0],)).fetchone()))
        if hits:
            out[table] = hits
    return out


def _around(line: str, query: str, ids: list[str], width: int = 160) -> str:
    """Длинная строка (JSON в одну строку) — только окрестность первого совпадения."""
    if len(line) <= 2 * width:
        return line
    low = line.lower().replace("ё", "е").replace("э", "е")
    q = query.lower().replace("ё", "е").replace("э", "е")
    found = [p for p in [low.find(q)] + [line.find(str(k)) for k in ids if k] if p >= 0]
    pos = min(found) if found else 0
    return "…" + line[max(0, pos - width):pos + width] + "…"


def list_hits(query: str, ids: list[str]) -> dict:
    nq = norm(query)
    files = sorted((db.ROOT / "config").glob("*")) + sorted((db.ROOT / "data" / "cache").glob("*.json"))
    out = {}
    for f in files:
        if not f.is_file() or f.stat().st_size > LIST_FILES_MAX:
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        lines = [f"{i}: {_around(line.strip(), query, ids)}" for i, line in enumerate(text.splitlines(), 1)
                 if nq in norm(line) or any(k and re.search(rf"\b{re.escape(str(k))}\b", line) for k in ids)]
        if lines:
            out[str(f.relative_to(db.ROOT)).replace("\\", "/")] = lines[:40] + ([f"… ещё {len(lines) - 40}"]
                                                                               if len(lines) > 40 else [])
    return out


class _NoCommit(sqlite3.Connection):
    """Соединение, которое не фиксирует запись: rank.compute пересчитывает ответственных в `with conn:` (а это commit) —
    справка базу не меняет (2026-11-22: первый прогон так переписал 951 строку responsibility)."""

    def commit(self):
        pass

    def __exit__(self, *exc):
        return False


def connect_no_commit() -> sqlite3.Connection:
    conn = sqlite3.connect(db.DB_PATH, factory=_NoCommit)
    db.register_functions(conn)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def sheet_state(conn, tids: list[int]) -> dict:
    from films import rank
    want = set(tids)
    out = {}
    try:
        rows = rank.compute(conn)
    finally:
        conn.rollback()
    for r in rows:
        t = r.get("title") or {}
        tid = t.get("id") if isinstance(t, dict) else None
        if tid in want:
            out.setdefault(tid, []).append({k: r.get(k) for k in ("variant", "quality", "hidden_reasons", "score",
                                                                  "score_parts", "franchise") if r.get(k)})
    return out


def lookup(conn, query: str, with_rank: bool = True) -> dict:
    tids = find_titles(conn, query)
    res = {"query": query, "titles": {tid: dossier(conn, tid) for tid in tids}}
    ids = []
    for d in res["titles"].values():
        t = d["titles"][0]
        ids += [t.get(k) for k in ("imdb_id", "series_imdb_id") if t.get(k)]
    res["unlinked"] = unlinked(conn, query, set(tids))
    res["lists"] = list_hits(query, ids)
    if with_rank and tids:
        st = sheet_state(conn, tids)
        res["sheet"] = {tid: st.get(tid, "нет в расчёте таблицы (нет раздач или просмотрено)") for tid in tids}
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("query")
    ap.add_argument("--no-rank", action="store_true")
    a = ap.parse_args(argv)
    conn = connect_no_commit()
    json.dump(lookup(conn, a.query, not a.no_rank), sys.stdout, ensure_ascii=False, indent=1, default=str)
    print()


if __name__ == "__main__":
    main()
