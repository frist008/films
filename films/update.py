"""«Обнови базу» — one run of the update-db skill (its steps 1 and 13; R18, R20, R99(в)).

`films update --start` opens a `runs` row (kind = 'update_db') and snapshots what the owner's report compares against
into data/cache/update_runs/run_<id>_start.json: every title with releases — best voice / HDR / 4K / disc, what
rank.compute says about it (shown on «Главная», interesting, upgrade of the downloaded copy) — plus watched titles,
responsibility, sync_state and the highest release / title ids.

The collectors are not touched: `films update --finish` takes the same snapshot again and

* writes `events` from the difference (new_title / new_release / unmatched_releases / better_release / new_dub /
  upgrade_available / watched / responsible);
* records the date of the update — sync_state 'update_db', cursor = local date: «новинки» are what appeared on rutor
  from that date on (films.fresh.last_update, R99(в)). Every other source keeps its own cursor (mark_sync in each step);
  the report shows which of them were refreshed during the run;
* closes the run (finished_at, summary JSON) and writes the owner's report data/cache/update_report_<id>.md — the
  «Отчёт владельцу» section of .claude/skills/update-db/SKILL.md.

Nothing is deleted. A run left open (crash) is closed as 'abandoned' only by `--start --force`.
rank.compute is the sheet's own view; like every sheet rebuild it re-applies the responsibility rules first.
"""
from __future__ import annotations

import json
import sqlite3
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

from . import db, fresh, rank

KIND = "update_db"
CACHE = db.ROOT / "data" / "cache"
HDR_NAMES = ["DV", "HDR10+", "DV*", "HDR10", "HLG", "SDR"]      # rank.hdr_rank order
CATEGORY = {"foreign_films": "Зарубежные фильмы", "foreign_series": "Зарубежные сериалы", "animation": "Мультфильмы",
            "anime": "Аниме", "russian_series": "Наши сериалы", None: "без категории"}
VOICE = {"dub": "дубляж", "mvo": "многоголосый", "dvo": "двухголосый", "svo": "одноголосый", "avo": "авторский",
         "amateur": "любительский", "original": "только оригинал", "none": "нет озвучки"}
NO_VOICE = {"original", "none"}
# sources whose collectors do not call mark_sync themselves: --finish reads when they last wrote from their own tables
# (UTC, like sync_state) — last_run_at = that moment, cursor = the newest item; never moved back
DERIVED = {
    "rutor_listing": ("SELECT max(last_seen_at) FROM releases WHERE source = 'rutor'",
                      "SELECT max(added_at) FROM releases WHERE source = 'rutor'"),
    "rutor_pages": ("SELECT max(fetched_at) FROM release_raw", None),
    "tmdb": ("SELECT max(fetched_at) FROM tmdb_raw", "SELECT max(fetched_at) FROM tmdb_raw"),
    "rt": ("SELECT max(x) FROM (SELECT max(fetched_at) x FROM rt_pages UNION ALL SELECT max(rt_checked_at) FROM titles)",
           "SELECT max(fetched_at) FROM rt_pages"),
    "imdb_datasets": ("SELECT max(loaded_at) FROM imdb_ratings", "SELECT max(loaded_at) FROM imdb_ratings"),
    "plex_library": ("SELECT max(synced_at) FROM plex_items", "SELECT max(synced_at) FROM plex_items"),
    "plex_history": (None, "SELECT max(viewed_at) FROM plex_history"),   # no fetch time: advanced cursor = synced now
    "stores": ("SELECT max(checked_at) FROM store_items", "SELECT max(checked_at) FROM store_items"),
    "youtube_trailers": ("SELECT max(trailer_checked_at) FROM titles", "SELECT max(trailer_checked_at) FROM titles"),
}
# what a full run refreshes (update-db steps 2–7б); not refreshed → «Проблемы» in the report
EXPECTED = set(DERIVED) | {"jellyfin", "jellyfin_to_plex", "disks", "qbittorrent", "health", "franchise_releases",
                           "watchlist_releases", "trailer_lang"}


def _retry(fn, *args, tries: int = 12, **kw):
    """Several collectors write the DB at once: wait out «database is locked» instead of failing the run."""
    for i in range(tries):
        try:
            return fn(*args, **kw)
        except sqlite3.OperationalError as e:
            if "locked" not in str(e) or i == tries - 1:
                raise
            time.sleep(2 + 3 * i)


def _now(conn) -> str:
    return conn.execute("SELECT datetime('now')").fetchone()[0]   # UTC, the same clock as runs / sync_state


def _local(ts: str | None) -> str:
    if not ts:
        return "—"
    try:
        return datetime.fromisoformat(ts).replace(tzinfo=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return ts


def _interesting(r: dict) -> bool:
    """The owner would care about a better release: on «Главная», or picked by him / a responsible / a franchise."""
    h = r["hidden_reasons"]
    if any(x == "видел" or x.startswith("не интересен") for x in h):
        return False
    person = (r.get("responsible") or {}).get("person")
    return bool(not h or r["top"] or r["urgent"] or r["wanted"] or r["list_marker"] or r["quality_watch"]
                or r["franchise"] or r["queued"] or (person and person != "—")
                or (r.get("mark") or {}).get("mark") in ("want", "maybe", "order"))


def _have(h: dict | None) -> str:
    if not h:
        return "?"
    parts = [str(h.get("resolution") or "?"), h.get("source_type") or ""]
    hdr = rank.hdr_class(h.get("hdr") or [])
    if hdr != "SDR":
        parts.append(hdr + (f" P{h['dv_profile']}" if h.get("dv_profile") else ""))
    if h.get("voice"):
        parts.append(h["voice"])
    elif h.get("has_ru_uk") is False:
        parts.append("без RU/UK")
    return " ".join(p for p in parts if p)


def snapshot(conn) -> dict:
    """What the report compares: per title with releases, the watched / responsibility sets, sync_state."""
    voice_rank = rank.settings()["sort"]["voice_rank"]
    rows = _retry(rank.compute, conn)
    titles = {}
    for r in rows:
        if r["variant_index"]:
            continue
        titles[r["title"]["id"]] = {
            "q": r["quality"], "hidden": r["hidden_reasons"], "up": r["upgrade"],
            "have": _have(r["have"]) if r["upgrade"] else None,
            "int": _interesting(r), "rel": not any(x == "видел" or x.startswith("не интересен") for x in r["hidden_reasons"]),
            "am": r["auto_mark"], "fs": r["first_seen"]}
    # best voice / HDR / 4K / disc over ALL releases (the «Лучшее» row puts voice first, so it may not be the 4K one)
    for tid, voice, hdr, dvs, res, src in conn.execute(
            """SELECT rt.title_id, r.best_voice, r.hdr, r.dv_static_md, r.resolution, r.source_type
               FROM release_titles rt JOIN releases r ON r.id = rt.release_id"""):
        s = titles.get(tid)
        if s is None:
            continue
        v = voice_rank.index(voice or "none") if (voice or "none") in voice_rank else len(voice_rank)
        s["v"] = min(s.get("v", 99), v)
        s["h"] = min(s.get("h", 99), rank.hdr_rank(rank.hdr_class(rank._j(hdr, []), dvs)))
        s["k4"] = int(s.get("k4", 0) or (res or 0) >= 2160)
        s["bd"] = int(s.get("bd", 0) or src in ("BDRemux", "Blu-ray"))
    watched = {}
    for tid, src in conn.execute("SELECT title_id, source FROM watched WHERE substr(source, -8) != '_partial'"):
        watched.setdefault(tid, []).append(src)
    return {
        "taken_at": _now(conn),
        "max_release_id": conn.execute("SELECT coalesce(max(id), 0) FROM releases").fetchone()[0],
        "max_title_id": conn.execute("SELECT coalesce(max(id), 0) FROM titles").fetchone()[0],
        "titles": titles,
        "watched": watched,
        "resp": {tid: [p, s] for tid, p, s in conn.execute(
            "SELECT title_id, person, source FROM responsibility WHERE person != '—'")},
        "sync": {s: t for s, t in conn.execute("SELECT source, last_run_at FROM sync_state")},
        "open_issues": conn.execute("SELECT count(*) FROM match_issues WHERE resolved_at IS NULL").fetchone()[0],
        "excluded": conn.execute("SELECT count(*) FROM excluded_releases").fetchone()[0],
    }


def _load(path: Path) -> dict:
    snap = json.loads(path.read_text(encoding="utf-8"))
    for k in ("titles", "watched", "resp"):   # JSON object keys are strings
        snap[k] = {int(t): v for t, v in snap[k].items()}
    return snap


def _snap_path(out_dir: Path, run_id: int) -> Path:
    return out_dir / "update_runs" / f"run_{run_id}_start.json"


def open_run(conn) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM runs WHERE kind = ? AND finished_at IS NULL ORDER BY id DESC LIMIT 1",
                        (KIND,)).fetchone()


def start(conn, force: bool = False, out_dir: Path = CACHE) -> dict:
    cur = open_run(conn)
    if cur and not force:
        raise SystemExit(f"прогон {cur['id']} открыт с {_local(cur['started_at'])}: закончи его `films update --finish` "
                         f"или начни заново `films update --start --force` (старый останется в runs как abandoned)")
    prev = fresh.last_update(conn)
    record_sources(conn)   # activity before the run is not credited to it (refreshed = strictly after started_at)

    def _open():
        with conn:
            if cur:
                s = json.loads(cur["summary"] or "{}") | {"status": "abandoned"}
                conn.execute("UPDATE runs SET finished_at = datetime('now'), summary = ? WHERE id = ?",
                             (json.dumps(s, ensure_ascii=False), cur["id"]))
            return conn.execute("INSERT INTO runs (kind, summary) VALUES (?, ?)",
                                (KIND, json.dumps({"status": "open", "previous_update": prev}, ensure_ascii=False))).lastrowid
    run_id = _retry(_open)
    snap = snapshot(conn)
    path = _snap_path(out_dir, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
    summary = {"status": "open", "previous_update": prev, "snapshot": path.relative_to(db.ROOT).as_posix()
               if path.is_relative_to(db.ROOT) else str(path), "titles_with_releases": len(snap["titles"]),
               "releases_max_id": snap["max_release_id"]}

    def _save():
        with conn:
            conn.execute("UPDATE runs SET summary = ? WHERE id = ?", (json.dumps(summary, ensure_ascii=False), run_id))
    _retry(_save)
    return {"run_id": run_id, **summary}


def diff(conn, a: dict, b: dict) -> list[tuple[int | None, str, dict]]:
    """Events (title_id, kind, details) between the start snapshot `a` and the finish snapshot `b`."""
    voice_rank = rank.settings()["sort"]["voice_rank"]
    vname = lambda i: voice_rank[i] if i is not None and i < len(voice_rank) else "none"   # noqa: E731
    ev = []
    # new releases, per title; unmatched ones as one event
    by_title, unmatched = defaultdict(list), []
    for rid, tids in conn.execute(
            """SELECT r.id, group_concat(rt.title_id) FROM releases r LEFT JOIN release_titles rt ON rt.release_id = r.id
               WHERE r.id > ? GROUP BY r.id""", (a["max_release_id"],)):
        if tids:
            for t in {int(x) for x in tids.split(",")}:
                by_title[t].append(rid)
        else:
            unmatched.append(rid)
    for tid, rids in sorted(by_title.items()):
        ev.append((tid, "new_release", {"count": len(rids), "release_ids": sorted(rids)}))
    if unmatched:
        ev.append((None, "unmatched_releases", {"count": len(unmatched), "release_ids": sorted(unmatched)}))

    for tid, e in sorted(b["titles"].items()):
        s = a["titles"].get(tid)
        if "v" not in e:
            continue   # only a queued qBittorrent torrent, no collected release
        if s is None or "v" not in s:
            ev.append((tid, "new_title", {"best": e["q"], "first_seen": e.get("fs"), "auto_mark": e.get("am"),
                                          "created": tid > a["max_title_id"], "shown": not e["hidden"]}))
        if e["up"] and set(e["up"]) != set((s or {}).get("up") or []):
            ev.append((tid, "upgrade_available", {"have": e["have"], "best": e["q"], "why": e["up"]}))
            continue
        if s is None or "v" not in s:
            continue
        if e["rel"] and ((e["v"] == 0 and s["v"] > 0) or (vname(s["v"]) in NO_VOICE and vname(e["v"]) not in NO_VOICE)):
            ev.append((tid, "new_dub", {"from": vname(s["v"]), "to": vname(e["v"]), "best": e["q"]}))
        better = []
        if e["h"] < s["h"]:
            better.append(f"{HDR_NAMES[s['h']] if s['h'] < len(HDR_NAMES) else '?'}→{HDR_NAMES[e['h']]}")
        if e["k4"] and not s["k4"]:
            better.append("4K")
        if e["bd"] and not s["bd"]:
            better.append("BDRemux")
        if better and e["int"]:
            ev.append((tid, "better_release", {"what": better, "was": s["q"], "best": e["q"]}))

    for tid in sorted(set(b["watched"]) - set(a["watched"])):
        s = a["titles"].get(tid)
        ev.append((tid, "watched", {"sources": b["watched"][tid], "was_shown": bool(s and not s["hidden"])}))
    for tid, (person, src) in sorted(b["resp"].items()):
        if (a["resp"].get(tid) or [None])[0] != person:
            e = b["titles"].get(tid)
            ev.append((tid, "responsible", {"person": person, "source": src, "in_table": e is not None,
                                            "shown": bool(e and not e["hidden"])}))
    return ev


def record_sources(conn) -> dict[str, str]:
    """sync_state for the DERIVED sources from their own tables; returns {source: last_run_at} of the ones moved."""
    now, moved = _now(conn), {}
    for src, (last_sql, cur_sql) in DERIVED.items():
        try:
            last = conn.execute(last_sql).fetchone()[0] if last_sql else None
            cursor = conn.execute(cur_sql).fetchone()[0] if cur_sql else None
        except sqlite3.OperationalError:   # table not migrated yet
            continue
        old = conn.execute("SELECT last_run_at, cursor FROM sync_state WHERE source = ?", (src,)).fetchone()
        if last_sql is None:   # cursor only: it moved → the source was synced now
            if not cursor or (old and old["cursor"] and cursor <= old["cursor"]):
                continue
            last = now
        if not last or (old and old["last_run_at"] and last <= old["last_run_at"]):
            continue
        moved[src] = last

        def _w(src=src, last=last, cursor=cursor, how=last_sql or cur_sql):
            with conn:
                conn.execute(
                    """INSERT INTO sync_state (source, last_run_at, cursor, details) VALUES (?, ?, ?, ?)
                       ON CONFLICT(source) DO UPDATE SET last_run_at = excluded.last_run_at,
                           cursor = COALESCE(excluded.cursor, sync_state.cursor), details = excluded.details""",
                    (src, last, cursor, json.dumps({"by": "films update --finish", "from": how}, ensure_ascii=False)))
        _retry(_w)
    return moved


def finish(conn, run_id: int | None = None, out_dir: Path = CACHE) -> dict:
    run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone() if run_id else open_run(conn)
    if not run:
        raise SystemExit("нет открытого прогона: сначала `films update --start`")
    if run["finished_at"]:
        raise SystemExit(f"прогон {run['id']} уже закрыт {_local(run['finished_at'])}")
    run_id = run["id"]
    summary = json.loads(run["summary"] or "{}")
    path = _snap_path(out_dir, run_id)
    if not path.exists():
        raise SystemExit(f"нет снимка начала прогона {path} — сравнивать не с чем; начни заново `films update --start --force`")
    record_sources(conn)
    a, b = _load(path), snapshot(conn)
    events = diff(conn, a, b)
    counts = dict(Counter(k for _, k, _ in events))
    counts["new_releases"] = b["max_release_id"] - a["max_release_id"] if b["max_release_id"] > a["max_release_id"] else 0
    refreshed = sorted(s for s, t in b["sync"].items() if t and t > run["started_at"])
    today = date.today().isoformat()
    summary |= {"status": "finished", "counts": counts, "sources_refreshed": refreshed,
                "report": (out_dir / f"update_report_{run_id}.md").relative_to(db.ROOT).as_posix()
                if (out_dir / f"update_report_{run_id}.md").is_relative_to(db.ROOT) else str(out_dir / f"update_report_{run_id}.md")}

    def _close():
        with conn:
            conn.executemany("INSERT INTO events (run_id, title_id, kind, details) VALUES (?, ?, ?, ?)",
                             [(run_id, t, k, json.dumps(d, ensure_ascii=False)) for t, k, d in events])
            conn.execute("UPDATE runs SET finished_at = datetime('now'), summary = ? WHERE id = ?",
                         (json.dumps(summary, ensure_ascii=False), run_id))
    _retry(_close)
    # the date «новинки» are counted from next time (R99(в)); after the run row so a failed close leaves it untouched
    _retry(db.mark_sync, conn, KIND, today, {"run_id": run_id, **counts})
    run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    text = report(conn, run, a, b, events)
    rp = out_dir / f"update_report_{run_id}.md"
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text(text, encoding="utf-8")

    def _reported():
        with conn:
            conn.execute("UPDATE events SET reported_at = datetime('now') WHERE run_id = ? AND reported_at IS NULL", (run_id,))
    _retry(_reported)
    return {"run_id": run_id, "update_date": today, **counts, "report": str(rp)}


# ---------------------------------------------------------------------------------------------- report

def _labels(conn, ids) -> dict[int, dict]:
    ids = sorted({i for i in ids if i})
    out = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for t in conn.execute(f"SELECT * FROM titles WHERE id IN ({','.join('?' * len(chunk))})", chunk):
            out[t["id"]] = dict(t)
    return out


def _name(t: dict | None) -> str:
    if not t:
        return "?"
    n = t.get("title_ru") or t.get("title_original") or t.get("title_latin") or f"#{t['id']}"
    if t.get("title_original") and t.get("title_ru") and t["title_original"] != t["title_ru"]:
        n += f" / {t['title_original']}"
    if t.get("season_number"):
        n += f" S{t['season_number']}"
    return f"{n} ({t.get('year') or '?'}) [id {t['id']}]"


def report(conn, run: sqlite3.Row, a: dict, b: dict, events: list) -> str:
    by = defaultdict(list)
    for tid, k, d in events:
        by[k].append((tid, d))
    lab = _labels(conn, [t for t, _, _ in events] + list(b["titles"]))
    summary = json.loads(run["summary"] or "{}")
    prev = summary.get("previous_update")
    mark = {"want": "❤️", "maybe": "💛"}
    L = [f"# Обновление базы — прогон {run['id']}", "",
         f"Начало {_local(run['started_at'])}, конец {_local(run['finished_at'])}. "
         f"Прошлое обновление: {prev or 'не было (первый прогон)'}.", ""]

    # 1. new releases / titles by category and year
    new_rel = summary["counts"].get("new_releases", 0)
    L += ["## Новые раздачи и тайтлы", ""]
    if new_rel:
        rows = conn.execute("""SELECT r.category, substr(t.year, 1, 4), count(DISTINCT r.id) FROM releases r
                               LEFT JOIN release_titles rt ON rt.release_id = r.id LEFT JOIN titles t ON t.id = rt.title_id
                               WHERE r.id > ? GROUP BY 1, 2 ORDER BY 1, 2""", (a["max_release_id"],)).fetchall()
        L.append(f"Новых раздач: **{new_rel}** (не сопоставлено с тайтлом: "
                 f"{sum(d['count'] for _, d in by['unmatched_releases'])}).")
        L += ["", "| Категория | Год тайтла | Раздач |", "|---|---|---|"]
        L += [f"| {CATEGORY.get(c, c)} | {y or '—'} | {n} |" for c, y, n in rows]
    else:
        L.append("Новых раздач нет.")
    nt = by["new_title"]
    L += ["", f"Новых тайтлов (появилась первая раздача): **{len(nt)}**"
              + (f", из них на «Главной» {sum(1 for _, d in nt if d['shown'])}" if nt else "") + "."]
    if nt:
        cnt = Counter((CATEGORY.get(lab[t].get("tracker_category"), lab[t].get("tracker_category")), lab[t].get("year"))
                      for t, _ in nt)
        L += ["", "| Категория | Год | Тайтлов |", "|---|---|---|"]
        L += [f"| {c} | {y or '—'} | {n} |" for (c, y), n in sorted(cnt.items(), key=lambda x: (x[0][0], x[0][1] or 0))]
        L += [""] + [f"- {mark.get(d.get('auto_mark'), '')}{' ' if d.get('auto_mark') else ''}{_name(lab.get(t))} — {d['best']}"
                     f"{'' if d['shown'] else ' (скрыт фильтром)'}" for t, d in sorted(nt, key=lambda x: _name(lab.get(x[0])))]

    # R99(в): what appeared on rutor since the previous update
    L += ["", "## Появилось на rutor после прошлого обновления", ""]
    if prev:
        fresh_ids = [t for t, s in b["titles"].items() if (s.get("fs") or "") >= prev and "v" in s]
        L.append(f"Первая раздача на rutor {prev} или позже: **{len(fresh_ids)}** "
                 f"(на «Главной» {sum(1 for t in fresh_ids if not b['titles'][t]['hidden'])}).")
        L += [f"- {mark.get(b['titles'][t].get('am'), '')}{' ' if b['titles'][t].get('am') else ''}{_name(lab.get(t))} — "
              f"{b['titles'][t]['fs']}, {b['titles'][t]['q']}{'' if not b['titles'][t]['hidden'] else ' (скрыт: ' + ', '.join(b['titles'][t]['hidden']) + ')'}"
              for t in sorted(fresh_ids, key=lambda t: (b["titles"][t]["fs"], _name(lab.get(t))))]
    else:
        L.append("Прошлого обновления не было — новое за этот прогон см. «Новые тайтлы» выше; со следующего прогона здесь "
                 "будет всё, что появилось на rutor после даты этого.")

    def block(title: str, kind: str, fmt, empty: str):
        L.extend(["", f"## {title}", ""])
        items = sorted(by[kind], key=lambda x: _name(lab.get(x[0])))
        L.extend([fmt(t, d) for t, d in items] if items else [empty])

    block("Вышло лучшее качество (интересные тайтлы)", "better_release",
          lambda t, d: f"- {_name(lab.get(t))}: {', '.join(d['what'])} — было {d['was']}, теперь {d['best']}", "Нет.")
    block("Вышел дубляж там, где его не было", "new_dub",
          lambda t, d: f"- {_name(lab.get(t))}: {VOICE.get(d['from'], d['from'])} → {VOICE.get(d['to'], d['to'])} ({d['best']})",
          "Нет.")
    block("Апгрейды скачанного: что у меня → что доступно", "upgrade_available",
          lambda t, d: f"- {_name(lab.get(t))}: {d['have']} → {d['best']} ({'; '.join(d['why'])})", "Новых нет.")
    L.append(f"\nВсего апгрейдов сейчас (🟢 в таблице): {sum(1 for s in b['titles'].values() if s['up'])}.")
    block("Новые просмотренные", "watched",
          lambda t, d: f"- {_name(lab.get(t))} ({', '.join(d['sources'])}){' — ушёл с «Главной»' if d['was_shown'] else ''}",
          "Нет.")
    L.extend(["", "## Новые тайтлы ответственных", ""])
    per = defaultdict(list)
    for t, d in by["responsible"]:
        per[d["person"]].append((t, d))
    src_ru = {"auto": "авто", "auto_suggested": "предположение", "manual": "список владельца"}
    for person in sorted(per) or []:
        L.append(f"**{person}** — {len(per[person])}:")
        L += [f"- {_name(lab.get(t))} — {src_ru.get(d['source'], d['source'])}"
              f"{'' if d['in_table'] else ', без раздачи'}{', скрыт фильтром' if d['in_table'] and not d['shown'] else ''}"
              for t, d in sorted(per[person], key=lambda x: _name(lab.get(x[0])))]
    if not per:
        L.append("Нет.")

    # problems
    L += ["", "## Проблемы", ""]
    um = [r for _, d in by["unmatched_releases"] for r in d["release_ids"]]
    if um:
        L.append(f"Несопоставленные новые раздачи — {len(um)} (`match_issues`, `films match --retry-issues`):")
        for i in range(0, len(um), 500):
            chunk = um[i:i + 500]
            L += [f"- {r['name']} — {r['url']}" for r in conn.execute(
                f"SELECT name, url FROM releases WHERE id IN ({','.join('?' * len(chunk))}) ORDER BY name", chunk)]
    issues = conn.execute("SELECT count(*) FROM match_issues WHERE resolved_at IS NULL").fetchone()[0]
    L.append(f"\nОткрытых match_issues: {issues} (в начале прогона {a['open_issues']}).")
    exc = conn.execute("SELECT count(*) FROM excluded_releases").fetchone()[0]
    if exc > a["excluded"]:
        L.append(f"Не сохранено раздач только с DV Profile 5: {exc - a['excluded']}.")
    gaps = []
    for t, d in nt:
        x = lab.get(t) or {}
        miss = [n for n, ok in (("IMDb-рейтинга", x.get("imdb_rating") is not None or x.get("imdb_season_rating") is not None),
                                ("RT", x.get("rt_url") or x.get("rt_critics") is not None),
                                ("трейлера", x.get("trailer_ru_url") or x.get("trailer_alt_url"))) if not ok]
        if miss:
            gaps.append(f"- {_name(x)}: нет {', '.join(miss)}")
    L += ["", f"Новые тайтлы без рейтинга / трейлера — {len(gaps)}:" if gaps else "У новых тайтлов есть рейтинги и трейлеры."]
    L += sorted(gaps)
    sync = {s: t for s, t in conn.execute("SELECT source, last_run_at FROM sync_state")}   # incl. update_db just set
    stale = sorted((s, sync.get(s)) for s in EXPECTED if not sync.get(s) or sync[s] <= run["started_at"])
    L += ["", "Источники полного прогона, не обновлявшиеся в этом прогоне (ошибка или шаг пропущен — проверить):" if stale
          else "Все источники полного прогона обновлены."]
    L += [f"- {s} — последний раз {_local(t)}" for s, t in stale]
    L += ["", "## Новые типы метаданных", "",
          "Скрипт их не видит: если при разборе встретился повторяющийся тип, которого нет в схеме, — дописать сюда и "
          "спросить владельца по одному (CLAUDE.md, п. 4).", "",
          "## Источники", "", "| Источник | Последнее обновление | В этом прогоне |", "|---|---|---|"]
    L += [f"| {s} | {_local(t)} | {'да' if t and t > run['started_at'] else '—'} |" for s, t in sorted(sync.items())]
    return "\n".join(L) + "\n"
