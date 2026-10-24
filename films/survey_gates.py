"""Cartoons of a franchise: first a gate question, then the «что видел» survey, then Яна (the owner, 2026-10-23).

«Лучше бы ты спрашивал по франшизам, интересны ли мне мультики, перед тем как их предлагать» and «Яне мы отдаём
только новые, которые мы не видели». So:
  1. `gates` — franchises whose cartoons are not settled (franchise_seen.animation_decided) → one question each;
  2. `gate KEY ask|films_only|hide` — the answer goes into config/franchises.toml as `animation = "…"`;
  3. survey answers (seen_survey.apply) call `yana_sync`: unseen cartoons → config/yana.toml, seen ones leave it.

Run: python -X utf8 -m films.survey_gates gates
     python -X utf8 -m films.survey_gates gate <key> ask|films_only|hide
"""
from __future__ import annotations

import json
import re
import sys
import tomllib
from datetime import date

from . import db

GATE_VALUES = ("ask", "ask_owner", "films_only", "hide")   # ask_owner: «интересны мне, Яне не интересны»


def gates(conn) -> list[dict]:
    from . import franchise_seen as fs
    from .seen_survey import have
    fs.hide_excluded_series(conn)   # fills the cartoon-franchise set used by animation_decided
    have_ids = have(conn)   # downloaded / queued — never asked (the owner: «… же есть в Plex»)
    with open(db.ROOT / "config" / "franchises.toml", "rb") as f:
        frs = [fr for fr in tomllib.load(f)["franchise"] if fr.get("scope") == "all"]
    out = []
    for fr in frs:
        if fs.animation_decided(fr):
            continue
        rows = conn.execute("""SELECT title_ru, title, year, kind, imdb_id FROM franchise_members WHERE franchise_key = ?
                               AND confidence != 'review' AND kind LIKE '%animated%' ORDER BY year""",
                            (fr["key"],)).fetchall()
        rows = [r for r in rows if not fs._too_old(fr, {"kind": r[3], "year": r[2], "imdb_id": r[4]})
                and r[4] not in have_ids]
        if rows:
            films = [r for r in rows if "series" not in r[3]]
            out.append({"key": fr["key"], "name": fr["name"], "films": len(films), "series": len(rows) - len(films),
                        "examples": [f"{r[0] or r[1]} ({r[2]})" for r in rows[:4]]})
    return out


def set_gate(key: str, value: str) -> None:
    if value not in GATE_VALUES:
        raise SystemExit(f"gate value must be one of {GATE_VALUES}")
    p = db.ROOT / "config" / "franchises.toml"
    s = p.read_text(encoding="utf-8")
    i = s.index(f'key = "{key}"')
    j = s.find("\n[[franchise]]", i)
    j = len(s) if j < 0 else j
    block = s[i:j]
    if "\nanimation = " in block:
        block = re.sub(r'\nanimation = "[a-z_]+"', f'\nanimation = "{value}"', block, count=1)
    else:
        block = block.rstrip("\n") + f'\n# мультики франшизы — ответ владельца на вопрос-развилку ({date.today()})\nanimation = "{value}"\n'
    p.write_text(s[:i] + block + s[j:], encoding="utf-8")


def yana_sync(conn, unseen: list[dict], seen: list[dict]) -> str:
    """Survey items (seen_survey batch entries) → Яна's list: unseen cartoons in, seen cartoons out."""
    with open(db.ROOT / "config" / "franchises.toml", "rb") as f:
        # «интересны мне, Яне не интересны» and anime franchises (the owner's own: Покемоны, Тетрадь смерти…)
        owner_only = {fr["name"] for fr in tomllib.load(f)["franchise"] if fr.get("animation") == "ask_owner" or fr.get("anime")}
    p = db.ROOT / "config" / "yana.toml"
    s = p.read_text(encoding="utf-8")
    blocks = re.split(r"(?=\[\[title\]\])", s)
    seen_ids = {w["imdb_id"].split("#")[0] for w in seen}
    kept = [b for b in blocks if not any(f'imdb_id = "{i}"' in b for i in seen_ids)]
    add = []
    for w in unseen:
        imdb = w["imdb_id"].split("#")[0]
        if not w["desc"].startswith(("мультфильм", "мультсериал")) or f'imdb_id = "{imdb}"' in s \
                or any(n in owner_only for n in w["franchise"].split(" / ")) \
                or any(f'imdb_id = "{imdb}"' in a for a in add):
            continue
        name = re.sub(r" — сезон \d+$", "", w["title"]).replace('"', "")
        add.append("\n".join(["[[title]]", f'name = "{name}"', f'imdb_id = "{imdb}"', f'year = {w["year"]}',
                              f'note = "опрос «что видел»: мультик франшизы «{w["franchise"]}», не видели — Яне '
                              f'(владелец {date.today()})"', ""]))
    p.write_text("".join(kept).rstrip() + "\n" + "".join(add), encoding="utf-8")
    return f"Яна: +{len(add)}, убрано просмотренных {len(blocks) - len(kept)}"


if __name__ == "__main__":
    c = db.connect()
    if sys.argv[1] == "gates":
        print(json.dumps(gates(c), ensure_ascii=False, indent=1))
    elif sys.argv[1] == "gate":
        set_gate(sys.argv[2], sys.argv[3])
