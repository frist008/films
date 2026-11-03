"""Posters in Jellyfin that are not posters (owner, 2026-11-02: «у пункт назначения 4 какой-то в джелефине дибильный постер,
это даже не постер. Почему такой баг? мы можем исправить такие дурацкие постеры? сколько у нас пробел. Вначале найди и
покажи потом правь»; «одно видео один вопрос, показывать текущие постер … и на что ты собрался править»).

Why: on the first scan a film is not identified yet, Jellyfin's «Screen Grabber» cuts a frame from the video and saves it
as the poster; after identification a refresh keeps existing images, so the frame stays. Episodes kept in a movie
library show a frame too.

    python -X utf8 -m films.sources.jellyfin_posters find              # wide / missing posters of films, series, seasons
    python -X utf8 -m films.sources.jellyfin_posters preview           # «сейчас → будет» pictures + data/cache/jellyfin_posters/plan.json
    python -X utf8 -m films.sources.jellyfin_posters code <item id> …  # the text for the Jellyfin tab (approved items only)
    python -X utf8 -m films.sources.jellyfin_posters detach <year> <item id> …   # drop another film's data (approved)

The proposal is the TMDB poster of the item's film (its TMDB id), or of the series for an episode / season (config
[series] below, by name prefix); Russian first, then English, then without text. Nothing is written here: the owner
looks at each picture and says yes; then films/browser/jellyfin_posters.js downloads the chosen poster into Jellyfin
(Items/RemoteImages/Download) and reads the item back.
"""
from __future__ import annotations

import io
import json
import sqlite3
import sys

import requests
from PIL import Image, ImageDraw, ImageFont

from .. import db
from . import jellyfin

OUT = db.ROOT / "data" / "cache" / "jellyfin_posters"
SCRIPT = db.ROOT / "films" / "browser" / "jellyfin_posters.js"
TYPES = {"MediaBrowser.Controller.Entities.Movies.Movie": "movie", "MediaBrowser.Controller.Entities.TV.Series": "series",
         "MediaBrowser.Controller.Entities.TV.Season": "season"}
# Series whose episodes / items sit in movie libraries, by the start of the Jellyfin name → TMDB tv id (+ season).
SERIES = {"Любовь, смерть и роботы": (86831, None), "Наклз": (158300, None), "Паук-Нуар": (220102, None),
          "Секретный уровень": (261579, None), "Уэнздей": (119051, None),
          "Континенталь": (72710, None), "IT.Welcome.to.Derry": (200875, None), "Оно: Добро пожаловать в Дерри": (200875, None),
"Берсерк (2016)": (66053, 1), "Берсерк (2017)": (66053, 2)}
# Films Jellyfin has not identified (no TMDB id), by Jellyfin name → TMDB movie id from the DB.
MOVIES = {"Одно целое": 1242011}
FONT = "C:/Windows/Fonts/arial.ttf"


def _key() -> str:
    return next(l.split("=", 1)[1].strip() for l in (db.ROOT / ".env").read_text(encoding="utf-8").splitlines()
                if l.startswith("TMDB_API_KEY="))


def find() -> dict:
    """Films / series / seasons whose poster is wider than 1:1.3 (a frame, not a poster) or missing."""
    j = sqlite3.connect(f"file:{jellyfin.snapshot()}?mode=ro", uri=True)
    try:
        wide, missing = [], []
        for iid, name, typ, year, series in j.execute(
                "SELECT Id, Name, Type, ProductionYear, SeriesName FROM BaseItems WHERE Type IN (%s)"
                % ",".join("?" * len(TYPES)), list(TYPES)):
            prov = dict(j.execute("SELECT ProviderId, ProviderValue FROM BaseItemProviders WHERE ItemId = ?", (iid,)))
            im = j.execute("SELECT Width, Height, Path FROM BaseItemImageInfos WHERE ItemId = ? AND ImageType = 0",
                           (iid,)).fetchone()
            x = {"item_id": iid, "kind": TYPES[typ], "name": name, "series": series, "year": year, "tmdb": prov.get("Tmdb"),
                 "imdb": prov.get("Imdb")}
            if not im:
                missing.append(x)
            elif im[0] and im[1] and im[1] / im[0] < 1.3:
                wide.append(x | {"width": im[0], "height": im[1], "path": im[2]})
        return {"wide": wide, "missing": missing}
    finally:
        j.close()


def _series_of(x: dict):
    label = (x["series"] + " " if x["kind"] == "season" and x["series"] else "") + x["name"]
    for prefix, v in SERIES.items():
        if label.startswith(prefix):
            return v
    return None


def _poster(kind: str, tmdb: int, season: int | None = None) -> str | None:
    url = (f"https://api.themoviedb.org/3/movie/{tmdb}/images" if kind == "movie" else
           f"https://api.themoviedb.org/3/tv/{tmdb}" + (f"/season/{season}" if season else "") + "/images")
    d = requests.get(url, params={"api_key": _key(), "include_image_language": "ru,en,null"}, timeout=30).json()
    rank = {"ru": 0, "en": 1, None: 2}
    best = sorted((p for p in d.get("posters", []) if p["height"] / p["width"] > 1.3),
                  key=lambda p: (rank.get(p["iso_639_1"], 3), -p["vote_average"], -p["width"]))
    return f"https://image.tmdb.org/t/p/original{best[0]['file_path']}" if best else None


def _tile(img: Image.Image | None, label: str, h: int = 450) -> Image.Image:
    w = int(h * 2 / 3) if img is None else int(img.width * h / img.height)
    t = Image.new("RGB", (max(w, 300), h + 40), "white")
    if img is None:
        ImageDraw.Draw(t).rectangle([0, 0, 300, h], fill="#ccc")
    else:
        t.paste(img.convert("RGB").resize((w, h)), (0, 0))
    ImageDraw.Draw(t).text((6, h + 8), label, fill="black", font=ImageFont.truetype(FONT, 22))
    return t


def preview(only: list[str] | None = None) -> list[dict]:
    """For each wide / missing poster: the proposal and a picture «сейчас → будет» in data/cache/jellyfin_posters/."""
    OUT.mkdir(parents=True, exist_ok=True)
    f = find()
    plan, seen_series = [], {}
    for x in f["wide"] + f["missing"]:
        if only and x["item_id"] not in only:
            continue
        sv = _series_of(x)
        if sv:
            key = sv
            url = seen_series.get(key) or _poster("tv", *sv)
            seen_series[key] = url
        elif x["kind"] == "movie" and (x["tmdb"] or x["name"] in MOVIES):
            url = _poster("movie", int(x["tmdb"] or MOVIES[x["name"]]))
        else:
            url = None
        cur = Image.open(x["path"]) if x.get("path") else None
        new = Image.open(io.BytesIO(requests.get(url.replace("/original/", "/w500/"), timeout=60).content)) if url else None
        a, b = _tile(cur, "Сейчас" if cur else "Сейчас: нет постера"), _tile(new, "Будет" if new else "Будет: не нашёл")
        pic = Image.new("RGB", (a.width + b.width + 20, a.height), "white")
        pic.paste(a, (0, 0))
        pic.paste(b, (a.width + 20, 0))
        name = (x["series"] + " — " if x["kind"] == "season" and x["series"] else "") + x["name"]
        file = OUT / f"{x['item_id'][:8]}.jpg"
        pic.save(file, quality=85)
        plan.append({"item_id": x["item_id"], "kind": x["kind"], "name": name, "jf_name": x["name"], "year": x["year"],
                     "series": sv,
                     "url": url, "picture": str(file)})
    (OUT / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    return plan


def code(item_ids: list[str]) -> str:
    """The text for one javascript_tool call in the Jellyfin tab: set the planned poster of each approved item."""
    plan = {p["item_id"]: p for p in json.loads((OUT / "plan.json").read_text(encoding="utf-8"))}
    items = [{"id": i, "name": plan[i]["jf_name"], "url": plan[i]["url"]} for i in item_ids if plan.get(i, {}).get("url")]
    user = db.private().get("jellyfin", {}).get("user")
    return (SCRIPT.read_text(encoding="utf-8")
            + f"\nawait __jfPosters.set({json.dumps(items, ensure_ascii=False)}, {json.dumps(user)});\n")


def detach_code(year: int, item_ids: list[str]) -> str:
    """The text that drops another film's data from approved items bound to the wrong film (and locks them)."""
    plan = {p["item_id"]: p for p in json.loads((OUT / "plan.json").read_text(encoding="utf-8"))}
    items = [{"id": i, "name": plan[i]["jf_name"], "year": year} for i in item_ids if i in plan]
    user = db.private().get("jellyfin", {}).get("user")
    return (SCRIPT.read_text(encoding="utf-8")
            + f"\nawait __jfPosters.detach({json.dumps(items, ensure_ascii=False)}, {json.dumps(user)});\n")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "find"
    if cmd == "code":
        print(code(sys.argv[2:]))
    elif cmd == "detach":
        print(detach_code(int(sys.argv[2]), sys.argv[3:]))
    else:
        r = preview(sys.argv[2:] or None) if cmd == "preview" else find()
        print(json.dumps(r, ensure_ascii=False, indent=1))
