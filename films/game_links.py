"""Связь тайтла с игрой (R334, владелец 2026-11-22).

Владелец: «у нас есть франшиза Final fantasy в аниме, но там непонятно в названии к какой игре относится какая часть.
И ещё надо написать относительно игры где надо смотреть эти аниме. Перед игровой, после игры. Во время и если во время
то после какой части сюжета … И в целом такое бы желательно делать по всем сериям у которых есть игры если они не
являются экранизацией а вот именно что дополнениями. Если они экранизация или другой взгляд на франшизу то надо писать
экранизация слово».

Таблица `title_game_links` (миграция 26), одна строка на тайтл (фильм или сезон):

* relation = addition   — дополнение к канону игры (приквел, сиквел, побочная история): в text сказано, КОГДА смотреть
                           относительно игры — до / после / во время (и после какой части сюжета);
* relation = adaptation — экранизация игры или «другой взгляд» на франшизу: в text есть слово «Экранизация»;
* relation = other      — с играми связан только названием (сериал «Ведьмак» — по книгам, драма об игроках): text как есть.

`text` — готовая строка (по-русски, ≤ 90 знаков), её таблица пишет последней строкой ячейки названия (films/sheet.py,
game_line). Данные — исследование по источникам (Википедия, фан-вики, официальные сайты), поле `sources`.

    python -X utf8 -m films.game_links check <json…>   # только проверить формулировки
    python -X utf8 -m films.game_links load <json…>    # проверить и записать (INSERT OR REPLACE); ошибка — ничего не пишет
    python -X utf8 -m films.game_links list            # что записано

JSON: массив объектов {id (= titles.id), relation, game, when, text, note, sources}.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from datetime import date
from pathlib import Path

from . import db

MAX_LEN = 90
RELATIONS = ("addition", "adaptation", "other")
# «Перед игровой, после игры. Во время и если во время то после какой части сюжета» — слова, которыми сказано «когда»
WHEN_RE = re.compile(r"\b(?:до|после|во время|перед|между|вместо)\b", re.IGNORECASE)
ADAPTATION_RE = re.compile(r"(?<!не )экранизац", re.IGNORECASE)   # «не экранизация игр» — не считается
CYR_WORD_RE = re.compile(r"[А-Яа-яЁё]{2,}")
UKR_RE = re.compile(r"[ІіЇїЄєҐґ]")


# R349 (владелец 2026-11-23: «Искать информацию по франшизам перед после или во время смотреть медиа надо расширенно а
# не только в одном месте, включая мнение игроков и зрителей»): у каждой строки ≥ 3 источников с разных сайтов, и среди них
# хоть один — мнение игроков / зрителей (обсуждения, форумы, отзывы)
MIN_SOURCES = 3
COMMUNITY_RE = re.compile(
    r"reddit\.com|steamcommunity\.com|resetera\.com|gamefaqs|neogaf|forum|forums\.|/forum|/threads?/|discussions|"
    r"dtf\.ru|stopgame\.ru|pikabu\.ru|kanobu\.ru|igromania\.ru/.*comment|4pda|playground\.ru|letterboxd\.com|ixbt\.live|ixbt\.com/live|"
    r"imdb\.com/.*/reviews|rottentomatoes\.com/.*/reviews|myanimelist\.net/.*/(reviews|forum)|anidb\.net|shikimori|"
    r"youtube\.com/watch|quora\.com|stackexchange\.com|tvtropes\.org|backloggd\.com|metacritic\.com/.*/user-reviews",
    re.IGNORECASE)


def _host(url: str) -> str:
    m = re.match(r"https?://(?:www\.)?([^/]+)", url or "")
    return m.group(1).lower() if m else ""


def source_problems(row: dict) -> list[str]:
    """Источники: ≥ MIN_SOURCES разных сайтов, среди них мнение игроков / зрителей (R349)."""
    src = [s for s in row.get("sources") or [] if isinstance(s, str) and s.startswith("http")]
    out = []
    if len({_host(s) for s in src}) < MIN_SOURCES:
        out.append(f"источников с разных сайтов {len({_host(s) for s in src})} < {MIN_SOURCES} (R349: «расширенно а не "
                   "только в одном месте»)")
    if not any(COMMUNITY_RE.search(s) for s in src):
        out.append("нет мнения игроков / зрителей (обсуждение, форум, отзывы) — R349")
    return out


def text_problems(row: dict) -> list[str]:
    """Что не так со строкой `text` (пусто — всё в порядке)."""
    rel, text = row.get("relation"), (row.get("text") or "").strip()
    out = []
    if rel not in RELATIONS:
        out.append(f"relation {rel!r} — не из {RELATIONS}")
    if not text:
        return out + ["нет text"]
    if len(text) > MAX_LEN:
        out.append(f"длиннее {MAX_LEN} знаков ({len(text)})")
    if len(CYR_WORD_RE.findall(text)) < 2 or UKR_RE.search(text):
        out.append("не по-русски")
    if rel == "addition" and not WHEN_RE.search(text):
        out.append("дополнение: не сказано, когда смотреть (до / после / во время игры)")
    if rel == "adaptation" and not ADAPTATION_RE.search(text):
        out.append("нет слова «Экранизация» (владелец: «надо писать экранизация слово»)")
    return out


def read_rows(paths: list[str]) -> list[dict]:
    rows = []
    for p in paths:
        data = json.loads(Path(p).read_text(encoding="utf-8"))
        rows += data if isinstance(data, list) else [data]
    return rows


def check(conn, rows: list[dict]) -> list[str]:
    """Все замечания по строкам: формулировка, тайтл есть в базе, тайтл не повторяется."""
    problems, seen = [], set()
    known = {r[0] for r in conn.execute("SELECT id FROM titles")}
    for row in rows:
        tid = row.get("id")
        where = f"id {tid} ({row.get('text')!r})"
        if not isinstance(tid, int) or tid not in known:
            problems.append(f"{where}: нет такого тайтла в titles")
        if tid in seen:
            problems.append(f"{where}: тайтл повторяется")
        seen.add(tid)
        problems += [f"{where}: {p}" for p in text_problems(row) + source_problems(row)]
    return problems


def load(conn, rows: list[dict]) -> int:
    """Записать строки (INSERT OR REPLACE). Сначала check(): хоть одно замечание — ничего не пишет (SystemExit)."""
    if problems := check(conn, rows):
        raise SystemExit("title_game_links не записаны:\n" + "\n".join(problems))
    today = date.today().isoformat()
    with conn:
        for row in rows:
            conn.execute(
                "INSERT OR REPLACE INTO title_game_links (title_id, relation, game, watch_when, text, note, sources, checked_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (row["id"], row["relation"], row.get("game"), row.get("when"), row["text"].strip(), row.get("note"),
                 json.dumps(row.get("sources") or [], ensure_ascii=False), row.get("checked_at") or today))
    return len(rows)


def sheet_lines(conn) -> dict:
    """Одним запросом на всю пересборку таблицы: title_id → text; для фильмов ещё IMDb id и «tmdb:N» → text
    (лист «Нет на трекерах» знает только id франшизы)."""
    out: dict = {}
    try:
        rows = conn.execute("SELECT g.title_id, g.text, t.kind, t.imdb_id, t.tmdb_id FROM title_game_links g "
                            "JOIN titles t ON t.id = g.title_id").fetchall()
    except sqlite3.OperationalError:   # база ещё без миграции 26
        return out
    for tid, text, kind, imdb, tmdb in rows:
        out[tid] = text
        if kind == "movie":
            for k in (imdb, f"tmdb:{tmdb}" if tmdb else None):
                if k:
                    out.setdefault(k, text)
    return out


def listing(conn) -> list[str]:
    return [f"{r[0]:>6}  {r[1] or r[2]} ({r[3]}{', сезон ' + str(r[4]) if r[4] else ''})  [{r[5]}]  {r[6]}"
            for r in conn.execute(
                "SELECT g.title_id, t.title_ru, t.title_original, t.year, t.season_number, g.relation, g.text "
                "FROM title_game_links g JOIN titles t ON t.id = g.title_id ORDER BY g.relation, t.title_original, t.year")]


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("check", "load", "list"):
        print(__doc__)
        return 2
    conn = db.connect()
    db.migrate(conn)
    cmd, paths = argv[0], argv[1:]
    if cmd == "list":
        lines = listing(conn)
        print("\n".join(lines) + f"\nвсего: {len(lines)}")
        return 0
    if not paths:
        print("нужны JSON-файлы")
        return 2
    rows = read_rows(paths)
    if cmd == "check":
        problems = check(conn, rows)
        print("\n".join(problems) if problems else f"OK: {len(rows)} строк")
        return 1 if problems else 0
    print(f"записано: {load(conn, rows)}; всего в title_game_links: "
          f"{conn.execute('SELECT count(*) FROM title_game_links').fetchone()[0]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
