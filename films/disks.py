"""Every video file on the local drives -> titles (R85, owner 2026-10-23: «проверить каждый файл. Каждый!»).

Run: python -X utf8 -m films disks scan    (walk the drives, then link)
     python -X utf8 -m films disks link    (link again without walking)
     python -X utf8 -m films disks report

Downloaded = not watched yet (the owner watches and deletes): a title with a file on disk is «скачан» and is never
asked in the «что видел» survey — even when neither Plex nor Jellyfin knows the file (Terminator Zero in
E:\\Сериалы, Pacific Rim: The Black in E:\\Films\\Without Sasha 2 were asked twice before this).

Read-only on the disks: os.walk + stat + reading .nfo / .torrent files; nothing is moved, renamed or written there.

Evidence, strongest first (disk_files.how):
  nfo            .nfo next to the file (same stem / movie.nfo / tvshow.nfo up to 2 folders up) with <uniqueid>
  folder_tmdbid  «[tmdbid-N]» / «{tmdb-N}» / «[imdbid-tt…]» in the path
  plex/jellyfin  the same path in plex_items.file_path / jellyfin_items.path with a title (their metadata wins
                 over the file name: «Justice League - War 3.mkv» is the owner's numbering, Plex knows the film)
  qbt            the same path under a qBittorrent torrent (save_path + name) with a title
  torrent        a .torrent in E:\\temp (copies of every torrent ever added) named like the file / its folder:
                 info-hash = releases.btih or topic URL = releases.url
  name_match     the file (or the show folder) name parsed like a torrent name (qbittorrent.parse_name) → TMDB search;
                 a year in the name must match; same-year namesakes → the far more voted (×5, ≥ 50 votes)
  manual         data/disk_map.json {"<folder or file path>": {"tmdb_movie"|"tmdb_tv"+"season"|"imdb": …}}
  personal / not_film   home videos, the owner's own YouTube / editing projects, game and SDK assets
  season_clash   link.ensure_season hit UNIQUE(series_key, season_number) (a namesake show already holds it)
  unmatched      nothing decided — listed in the report by folder
A wrong match is worse than unmatched.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from . import db

DRIVES = ["C", "D", "E", "F", "G"]
VIDEO_EXT = {"mkv", "mp4", "avi", "m2ts", "ts", "mov", "wmv", "m4v", "webm", "iso", "vob"}
SKIP_DIRS = {"windows", "program files", "program files (x86)", "programdata", "appdata", "$recycle.bin",
             "system volume information", "node_modules", "$windows.~ws", "$windows.~bt", "$winreagent", "recovery",
             "config.msi", "$sysreset", "msocache", "perflogs", "$getcurrent"}
TORRENT_DIR = Path(r"E:\temp")
REPORT_PATH = db.ROOT / "data" / "cache" / "disk_files_report.json"
MAP_PATH = db.ROOT / "data" / "disk_map.json"
CONFLICTS_PATH = db.ROOT / "data" / "cache" / "disk_conflicts.json"


def _skip_dir(parent: str, name: str) -> bool:
    low = name.lower()
    if low in SKIP_DIRS:
        return True
    full = os.path.normcase(os.path.join(parent, name))
    return full == os.path.normcase(str(db.ROOT))


def _retry(fn, *a, tries: int = 20):
    """Other collectors write to the DB at the same time: retry a short transaction on «database is locked»."""
    for i in range(tries):
        try:
            return fn(*a)
        except sqlite3.OperationalError as e:
            if "locked" not in str(e) and "busy" not in str(e):
                raise
            time.sleep(min(30, 1 + i * 2))
    return fn(*a)


# ---------------------------------------------------------------------------------------------- walk

def _is_mpeg_ts(path: str) -> bool:
    """MPEG transport stream: sync byte 0x47 every 188 (or 192 for m2ts-style) bytes; reads 400 bytes."""
    try:
        with open(path, "rb") as f:
            b = f.read(400)
    except OSError:
        return True   # unreadable: keep it, the report shows it
    return len(b) >= 189 and (b[0] == 0x47 and b[188] == 0x47 or len(b) >= 197 and b[4] == 0x47 and b[196] == 0x47)


PROFILE_FILM_MIN = 700 * 2 ** 20   # a bigger file in a user profile (Downloads…) is linked like any other


def _profile_personal(path: str, size: int) -> tuple[str, str] | None:
    """Files in a user profile (C:\\Users\\<user>\\…: phone clips, Telegram / Viber / Zoom downloads, cloud-drive
    copies) are the owner's own. Their names can hold his name or e-mail and the DB is published, so such a row
    keeps only «C:\\Users\\<user>\\<top folder>\\#<sha1 of the path>.<ext>»."""
    parts = path.split(chr(92))
    if len(parts) < 4 or parts[1].lower() != "users" or size >= PROFILE_FILM_MIN:
        return None
    folder = chr(92).join(parts[:4]) if len(parts) > 4 else chr(92).join(parts[:3])
    folder = re.sub(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", "<email>", folder)   # «Мой диск (<email>)»
    ext = path.rsplit(".", 1)[-1].lower()
    return f"{folder}{chr(92)}#{hashlib.sha1(path.encode('utf-8')).hexdigest()[:12]}.{ext}", folder

def walk(drives=DRIVES) -> dict:
    """{'videos': [row…], 'nfo': {folder_lower: [nfo paths]}, 'stats': {drive: {...}}} — read-only."""
    videos, nfo, stats = [], defaultdict(list), {}
    for d in drives:
        root = f"{d}:\\"
        if not os.path.exists(root):
            stats[d] = {"present": False}
            continue
        t0, errors = time.time(), []
        n = not_ts = 0
        for dp, dns, fns in os.walk(root, onerror=lambda e: errors.append(str(e)[:200])):
            dns[:] = [x for x in dns if not _skip_dir(dp, x)]
            for f in fns:
                ext = f.rsplit(".", 1)[-1].lower() if "." in f else ""
                if ext == "nfo":
                    nfo[dp.lower()].append(os.path.join(dp, f))
                    continue
                if ext not in VIDEO_EXT:
                    continue
                p = os.path.join(dp, f)
                try:
                    st = os.stat(p)
                except OSError as e:
                    errors.append(f"{p}: {e}"[:200])
                    continue
                if ext == "ts" and not _is_mpeg_ts(p):
                    not_ts += 1          # TypeScript sources share the extension
                    continue
                n += 1
                path, folder = p, dp
                profile = _profile_personal(p, st.st_size)
                if profile:              # the owner's own files: no names in the (public) DB
                    path, folder = profile
                videos.append({"path": path, "drive": d, "size_bytes": st.st_size, "ext": ext, "folder": folder,
                               "mtime": datetime.fromtimestamp(st.st_mtime, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")})
        stats[d] = {"present": True, "videos": n, "seconds": round(time.time() - t0, 1), "errors": len(errors),
                    "not_mpeg_ts": not_ts, "error_samples": errors[:5]}
    return {"videos": videos, "nfo": dict(nfo), "stats": stats}


def store_walk(conn, w: dict, drives=DRIVES) -> dict:
    """Upsert the files found; files of the walked drives not found any more → present = 0 (rows are never deleted)."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    rows = w["videos"]

    def chunk(part):
        with conn:
            for r in part:
                conn.execute(
                    """INSERT INTO disk_files (path, drive, size_bytes, mtime, ext, folder, scanned_at, present)
                       VALUES (:path, :drive, :size_bytes, :mtime, :ext, :folder, :now, 1)
                       ON CONFLICT(path) DO UPDATE SET drive = excluded.drive, size_bytes = excluded.size_bytes,
                           mtime = excluded.mtime, ext = excluded.ext, folder = excluded.folder,
                           scanned_at = excluded.scanned_at, present = 1""", dict(r, now=now))
    for i in range(0, len(rows), 500):
        _retry(chunk, rows[i:i + 500])
    walked = [d for d in drives if w["stats"].get(d, {}).get("present")]

    def gone():
        with conn:
            return conn.execute("UPDATE disk_files SET present = 0 WHERE present = 1 AND scanned_at < ? AND drive IN (%s)"
                                % ",".join("?" * len(walked)), [now, *walked]).rowcount if walked else 0
    return {"stored": len(rows), "gone": _retry(gone)}


NFO_INDEX = db.ROOT / "data" / "cache" / "disk_nfo_index.json"


def walk_and_store(conn, drives=DRIVES) -> dict:
    w = walk(drives)
    st = store_walk(conn, w, drives)
    old = json.loads(NFO_INDEX.read_text(encoding="utf-8")) if NFO_INDEX.exists() else {}
    walked = {d for d in drives if w["stats"].get(d, {}).get("present")}
    old = {k: v for k, v in old.items() if k[:1].upper() not in walked}
    NFO_INDEX.write_text(json.dumps(old | w["nfo"], ensure_ascii=False), encoding="utf-8")
    return {"drives": w["stats"], **st, "nfo_files": sum(len(v) for v in w["nfo"].values())}


# ---------------------------------------------------------------------------------------------- evidence readers

_NFO_ID = {"tmdb": re.compile(r"<uniqueid[^>]*type=\"tmdb\"[^>]*>\s*(\d+)\s*<|<tmdbid>\s*(\d+)\s*<", re.I),
           "imdb": re.compile(r"<uniqueid[^>]*type=\"imdb\"[^>]*>\s*(tt\d+)\s*<|<imdbid>\s*(tt\d+)\s*<|"
                              r"<imdb_id>\s*(tt\d+)\s*<|<id>\s*(tt\d+)\s*<", re.I)}


def read_nfo(path: str) -> dict:
    """{'root': movie / tvshow / episodedetails, 'tmdb', 'imdb', 'season', 'episode'} from a Kodi / Jellyfin .nfo."""
    try:
        with open(path, "rb") as f:
            raw = f.read(400_000)
    except OSError:
        return {}
    text = raw.decode("utf-8", "replace")
    m = re.search(r"<(movie|tvshow|episodedetails|musicvideo)\b", text)
    out = {"root": m.group(1) if m else None}
    for k, rx in _NFO_ID.items():
        hit = rx.search(text)
        out[k] = next((g for g in hit.groups() if g), None) if hit else None
    for k in ("season", "episode"):
        hit = re.search(rf"<{k}>\s*(-?\d+)\s*</{k}>", text)
        out[k] = int(hit.group(1)) if hit else None
    return out


def _bdecode_info(b: bytes):
    """bencode → (value, info_span) where info_span = (start, end) of the top-level 'info' dict (for the info-hash)."""
    span = {}

    def dec(i, depth):
        c = b[i]
        if c == 0x69:
            j = b.index(b"e", i)
            return int(b[i + 1:j]), j + 1
        if c == 0x6C:
            i += 1
            out = []
            while b[i] != 0x65:
                v, i = dec(i, depth + 1)
                out.append(v)
            return out, i + 1
        if c == 0x64:
            i += 1
            out = {}
            while b[i] != 0x65:
                k, i = dec(i, depth + 1)
                s = i
                v, i = dec(i, depth + 1)
                if depth == 0 and k == b"info":
                    span["info"] = (s, i)
                out[k] = v
            return out, i + 1
        j = b.index(b":", i)
        n = int(b[i:j])
        return b[j + 1:j + 1 + n], j + 1 + n
    return dec(0, 0)[0], span.get("info")


def _key(name: str) -> str:
    """Comparable form of a file / folder / torrent name."""
    n = re.sub(r"\.(mkv|mp4|avi|m2ts|ts|mov|wmv|m4v|webm|iso|vob|torrent)$", "", name.strip(), flags=re.I)
    return re.sub(r"[\W_]+", "", n.lower())


def torrent_index() -> dict[str, dict]:
    """E:\temp holds a copy of every .torrent ever added to qBittorrent: name key (torrent name, and every file of
    a multi-file torrent) → {btih, topic_url, torrent}."""
    out = {}
    if not TORRENT_DIR.is_dir():
        return out
    for p in sorted(TORRENT_DIR.glob("*.torrent")):
        try:
            b = p.read_bytes()
            t, span = _bdecode_info(b)
        except (OSError, ValueError, IndexError):
            continue
        info = t.get(b"info") or {}
        btih = hashlib.sha1(b[span[0]:span[1]]).hexdigest() if span else None
        comment = (t.get(b"comment") or b"").decode("utf-8", "replace")
        name = (info.get(b"name.utf-8") or info.get(b"name") or b"").decode("utf-8", "replace")
        e = {"btih": btih, "topic_url": comment if comment.startswith("http") else None, "torrent": p.name, "name": name}
        names = [name] + [((f.get(b"path.utf-8") or f.get(b"path") or [b""])[-1]).decode("utf-8", "replace")
                          for f in info.get(b"files") or []]
        for n in names:
            k = _key(n)
            if len(k) >= 8 and re.search(r"[a-zа-яё]", k):   # «01.mkv» of a multi-file torrent says nothing
                out.setdefault(_key(n), e)
    return out


# ---------------------------------------------------------------------------------------------- names

EP_RES = [  # (regex on the file stem, season group, episode group)
    (re.compile(r"\bS(\d{1,2})[ ._-]?E(\d{1,3})\b", re.I), 1, 2),
    (re.compile(r"(?<![\dx])(\d{1,2})x(\d{2,3})\b", re.I), 1, 2),
    (re.compile(r"\b(?:EP?|Ep\.)\s?(\d{1,3})\b(?!p)", re.I), None, 1),
    (re.compile(r"(\d{1,3})\s*(?:серия|эпизод)|(?:серия|эпизод)\s*(\d{1,3})", re.I), None, (1, 2)),
    (re.compile(r"\s-\s(\d{1,3})(?=\s|$|\(|\[|\.)"), None, 1),
    (re.compile(r"\[(\d{1,4})(?:\.5)?(?:_of_\d+| of \d+)?\]", re.I), None, 1),   # «[693]», «[01_of_24]»
    (re.compile(r"_(\d{2,3})_"), None, 1),
]
BRACKET = 5   # index of the «[N]» pattern: «[720]» there is an episode, not a resolution
SEASON_DIR = re.compile(r"^(?:season|сезон|s)\s*\d{1,2}$|^\d{1,2}\s*сезон$|^specials?$", re.I)
MOVIE_TAG = re.compile(r"[\[{](tmdbid|tmdb|imdbid|imdb)[-=](tt\d+|\d+)[\]}]", re.I)


def episode_of(stem: str) -> tuple[int | None, int | None, int | None, int | None]:
    """(season, episode, start, end of the marker) from a file stem; all None when it is not episode-like."""
    for i, (rx, sg, eg) in enumerate(EP_RES):
        for m in rx.finditer(stem):
            if isinstance(eg, tuple):
                ep = next(int(m.group(g)) for g in eg if m.group(g))
            else:
                ep = int(m.group(eg))
            if 1900 <= ep <= 2100 or (i != BRACKET and ep in (264, 265, 480, 576, 720, 1080)):   # a year / x264 / 720p
                continue
            return (int(m.group(sg)) if sg else None), ep, m.start(), m.end()
    return None, None, None, None


def folder_season(*names: str) -> int | None:
    from .link import season_of
    for n in names:
        s = season_of(n)
        if s is not None:
            return s
        m = re.search(r"\(S(\d{1,2})\)|\bСезон\s*(\d{1,2})|\b(\d{1,2})\s*сезон", n or "", re.I)
        if m:
            return int(next(g for g in m.groups() if g))
    return None


def clean_title(t: str) -> str:
    t = re.sub(r"\s+", " ", re.sub(r"[_]+", " ", t or "")).strip(" -.,[]()")
    return t


def name_variants(title: str) -> list[str]:
    from .sources.qbittorrent import translit_ru
    out = [title]
    if " - " in title or ": " in title:
        out.append(re.sub(r"\s+-\s+", ": ", title))
    if re.fullmatch(r"[A-Za-z0-9 '\-:!?&.,]+", title or "") and re.search(r"[a-z]{3}", title or "") \
            and re.search(r"(?i)(?:yy|iy|kh|zh|ts|ch|sh|ya|yu|yo)", title or ""):
        out.append(translit_ru(title))   # «Krasnyie komnatyi» → «красныие комнатыи» (TMDB search is fuzzy enough)
    return [x for x in dict.fromkeys(o.strip() for o in out) if x]


# ---------------------------------------------------------------------------------------------- TMDB decisions

_SEARCH: dict = {}


def _votes(conn, kind: str, tmdb_id: int) -> int:
    from .sources import tmdb
    raw = tmdb.movie_raw(conn, tmdb_id) if kind == "movie" else tmdb.tv_raw(conn, tmdb_id)
    return (raw or {}).get("vote_count") or 0


def resolve(conn, names: list[str], year: int | None, kind: str, min_episodes: int | None = None) -> tuple[int | None, str]:
    """TMDB id for a parsed name, or (None, why). A wrong match is worse than none:
    - 'matched' by tmdb.search is taken (for a movie whose name has a year: TMDB year within 1 of it);
    - 'ambiguous': only exact-name candidates (≥ 0.9; same year when the name has one), and among several the far
      more voted (≥ 50 votes and ×5 the next) — the rule qbittorrent.link / Plex shows use."""
    from .sources import tmdb
    key = (tuple(names), year, kind, min_episodes if kind == "tv" else None)
    if key in _SEARCH:
        return _SEARCH[key]
    res = tmdb.search(conn, names, year, kind) if names else {"status": "not_found"}
    out: tuple[int | None, str] = (None, res.get("status") or "not_found")
    b = res.get("best") if res.get("status") == "matched" else None
    if b and kind == "movie" and year and b.get("year") and abs(b["year"] - year) > 1:
        b = None
        out = (None, f"matched year {res['best'].get('year')} ≠ {year}")
    if b and (b.get("name_fit") or 0) < 0.9:   # «Psycho-Pass 3 First Inspector» is not «Psycho-Pass»
        b = None
        out = (None, f"only a loose name fit: {res['best'].get('title')} ({res['best'].get('year')})")
    if b:
        out = (b["tmdb_id"], f"matched {b.get('title')} ({b.get('year')})")
    elif res.get("status") == "ambiguous":
        cands = [c for c in res.get("candidates") or [] if (c.get("score") or 0) >= (0.75 if year else 0.9)
                 and (c.get("name_fit") or 0) >= 0.9]
        if year and kind == "movie":
            cands = [c for c in cands if c.get("year") == year]
        if kind == "tv" and min_episodes and len(cands) > 1:
            fit = [c for c in cands if ((tmdb.tv_raw(conn, c["tmdb_id"]) or {}).get("number_of_episodes") or 0) >= min_episodes]
            cands = fit or cands
        if len(cands) > 1:
            voted = sorted(((_votes(conn, kind, c["tmdb_id"]), i, c) for i, c in enumerate(cands)), key=lambda x: (-x[0], x[1]))
            cands = [voted[0][2]] if voted[0][0] >= 50 and voted[0][0] >= 5 * voted[1][0] else []
            why = "by votes ×5" if cands else "namesakes: " + "; ".join(
                f"{c.get('title')} ({c.get('year')}) {v}" for v, _, c in voted[:3])
        else:
            why = "only exact-name candidate" if cands else "ambiguous: " + "; ".join(
                f"{c.get('title')} ({c.get('year')})" for c in (res.get("candidates") or [])[:3])
        out = (cands[0]["tmdb_id"], f"{why}: {cands[0].get('title')} ({cands[0].get('year')})") if cands else (None, why)
    _SEARCH[key] = out
    return out


def season_for(conn, tv_id: int, season: int | None, episode: int | None,
               folder_max: int | None = None) -> tuple[int | None, str]:
    """The season of a file: given; else the only season; else absolute episode numbering over the TMDB seasons
    (an episode beyond season 1, or any episode of a folder whose numbers run past season 1); else season 1 when
    the episode fits it (no season anywhere in the names — noted «S1 assumed» for the report)."""
    from .sources import tmdb
    if season is not None:
        return season, ""
    raw = tmdb.tv_raw(conn, tv_id) or {}
    seasons = sorted((s["season_number"], s.get("episode_count") or 0) for s in raw.get("seasons") or []
                     if s.get("season_number"))
    if len(seasons) == 1:
        return seasons[0][0], ""
    if episode is not None and seasons:
        absolute = (folder_max or 0) > seasons[0][1]
        if episode == 0:
            return seasons[0][0], "episode 0 (special) → first season"
        acc = 0
        for n, cnt in seasons:
            if acc < episode <= acc + cnt:
                if n > seasons[0][0]:
                    return n, "absolute numbering"
                return n, "absolute numbering (folder runs past S1)" if absolute else "S1 assumed (no season in names)"
            acc += cnt
        return None, f"episode {episode} beyond {acc} TMDB episodes"
    return None, "season unknown"


def ensure_movie_tmdb(conn, tmdb_id: int, how: str) -> tuple[int | None, str]:
    from .link import ensure_movie
    from .sources import tmdb
    m = tmdb.movie(conn, tmdb_id)
    if not m:
        return None, f"TMDB movie {tmdb_id} not found"
    if not m.get("imdb_id"):
        return None, f"TMDB movie {tmdb_id} has no IMDb id"
    return ensure_movie(conn, m["imdb_id"], how), ""


def ensure_season_safe(conn, tv_id: int, season: int) -> tuple[int | None, str]:
    """link.ensure_season; UNIQUE(series_key, season_number) for namesake shows → (None, 'season_clash')."""
    from .link import ensure_season
    try:
        with conn:
            return ensure_season(conn, tv_id, season), ""
    except sqlite3.IntegrityError as e:
        return None, f"season_clash: tv {tv_id} S{season}: {e}"


# ---------------------------------------------------------------------------------------------- classification

# not films: game / SDK / editor assets, the owner's own videos (checked by hand in the first scan, R85)
# matched against the lowercased path with «/» separators
NOT_FILM = ["/games/", "/steamapps/", "/steam/", "/android/", "/sdk/", "/work/android/", "/emulator/", "/battle.net/",
            "/gog galaxy/", "/vortex", "/sidequest/", "/site-packages/", "/.gradle/", "/.vscode/", "/jetbrains/",
            "/unity/", "/epic games/", "/test_data/", "/test/assets/"]
EXTRA_DIRS = {"bonus", "extras", "extra", "featurettes", "nced", "ncop", "menu", "trailers", "sample", "samples"}
EXTRA_NAME = re.compile(r"(?i)\b(?:creditless|nc[eo]p?\d*|ncop|nced|trailer|sample|menu|preview)\b")
PERSONAL = ["/work/youtube/", "e:/vegas/"]


def _slash(path: str) -> str:
    return path.replace(chr(92), "/").lower()
GENERIC_NAMES = {"films", "сериалы", "serials", "series", "anime", "аниме", "movies", "movie", "video", "videos",
                 "видео", "downloads", "загрузки", "temp", "мультфильмы", "мульты", "cartoons", "tv", "shows"}


def _jellyfin_roots() -> set[str]:
    import tomllib
    out = set()
    try:
        with open(db.ROOT / "config" / "settings.toml", "rb") as f:
            data_dir = Path(tomllib.load(f)["jellyfin"]["data_dir"])
        for link in (data_dir.parent / "root" / "default").glob("*/*.mblink"):
            out.add(os.path.normcase(link.read_text(encoding="utf-8").strip().rstrip("\\")))
    except (OSError, KeyError):
        pass
    return out


def _prefix_key(stem: str) -> str:
    s, e, pos, end = episode_of(stem)
    base = stem[:pos] if pos else stem[end:] if end else re.sub(r"[\s._-]*\d{1,3}(?:[\s._-].*)?$", "", stem)
    from .sources.qbittorrent import parse_name
    return _key(parse_name(base + ".mkv")["title"] or base)[:12]


NUMBERED = re.compile(r"^(.*?[^\d\s._-])[\s._-]+(\d{1,3})(?=$|[\s._-])")   # «Berserk 26», «OverLord.03.HDTV»


def folder_profile(rows: list[dict]) -> dict[str, dict]:
    """Per folder: video count, distinct name prefixes (≥ 3 → a container of different titles, not a show),
    numbered stems «Name 26» sharing one prefix (≥ 3 → episodes numbered without a marker)."""
    by = defaultdict(list)
    for r in rows:
        by[os.path.normcase(r["folder"])].append(r)
    out = {}
    for f, rs in by.items():
        stems = [Path(r["path"]).stem for r in rs]
        prefixes = Counter(_prefix_key(s) for s in stems)
        numbered = Counter()
        max_ep = 0
        for s in stems:
            sn, ep, _, _ = episode_of(s)
            if ep is None:
                m0 = NUMBERED.match(s)
                ep = int(m0.group(2)) if m0 else None
            if ep and sn is None:
                max_ep = max(max_ep, ep)
            m = NUMBERED.match(s)
            if m and not re.search(r"(?:19|20)\d{2}\D*$", m.group(1)):
                numbered[_key(m.group(1))] += 1
        out[f] = {"videos": len(rs), "distinct": len(prefixes), "max_episode": max_ep,
                  "numbered": {k for k, n in numbered.items() if n >= 3}}
    return out


def show_folder(path: str, roots: set[str], prof: dict) -> str | None:
    """The folder that names the show: the file's folder, or its parent when the folder is «Season 2»; None when
    the file lies in a library root / a container of different titles."""
    p = Path(path).parent
    for _ in range(3):
        name = p.name
        if not name or os.path.normcase(str(p)) in roots or name.lower() in GENERIC_NAMES:
            return None
        if SEASON_DIR.match(name.strip()):
            p = p.parent
            continue
        info = prof.get(os.path.normcase(str(p)))
        if info and info["distinct"] >= 3 and not info["numbered"]:
            return None
        return str(p)
    return None


# ---------------------------------------------------------------------------------------------- link

def _parse(name: str) -> tuple[str, int | None, int | None]:
    """(title, year, season) of a file / folder / show prefix, release tags removed."""
    from .link import clean_show
    from .sources.qbittorrent import parse_name
    n = re.sub(r"^\[[^\]]*\][\s_.-]*", "", name)           # «[AniDub]_Berserk…»
    p = parse_name(n if re.search(r"\.\w{2,4}$", n) else n + ".mkv")
    title, year = p["title"], p["year"]
    season = p["season"] if p["season"] is not None else folder_season(n)
    rm = re.search(r"\s(II|III|IV|V)(?:\s+(?:BD|TV|OVA))?$", title or "")
    if rm and season is None:
        season = {"II": 2, "III": 3, "IV": 4, "V": 5}[rm.group(1)]
    t2, y2 = clean_show(title)
    title = clean_title(t2 or title)
    title = re.sub(r"\s*\b(?:TV|ТВ)[- ]?\d\b|\s+(?:TV|ТВ)$", "", title, flags=re.I).strip()
    return title, year or y2, season


_TAG = re.compile(r"(?i)^(?:hdtv|web|webrip|web-?dl|bd|bdrip|bdremux|remux|blu-?ray|dvd|dvdrip|tv|tvrip|satrip|dub|dubbed|"
                  r"mvo|dvo|avo|rus|eng|jap|jpn|ukr|sub|subs|raw|hd|fhd|uhd|4k|hdr|hdr10|sdr|dv|x26[45]|h26[45]|hevc|"
                  r"avc|aac|ac3|flac|dts|mp3|\d{3,4}p?|10bit|8bit|end|final|fin|v\d)$")


def _only_tags(rest: str) -> bool:
    """True when the text after an episode number holds only release tags / brackets."""
    rest = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", rest)
    return all(_TAG.match(t) for t in re.split(r"[\s._-]+", rest) if t)


def _meaningful(t: str | None) -> bool:
    return bool(t) and len(re.sub(r"\W", "", t)) >= 2 and not re.fullmatch(
        r"(?i)(?:episode|серия|video|movie|film|sample|trailer|extras?|featurette|\d+)", t.strip())


class Linker:
    def __init__(self, conn):
        self.conn = conn
        q = lambda s: conn.execute(s).fetchall()  # noqa: E731
        self.plex = {os.path.normcase(p): t for p, t in q(
            "SELECT file_path, title_id FROM plex_items WHERE title_id IS NOT NULL AND file_path IS NOT NULL")}
        self.jf = {os.path.normcase(p): t for p, t in q(
            "SELECT path, title_id FROM jellyfin_items WHERE title_id IS NOT NULL AND path IS NOT NULL "
            "AND kind IN ('movie', 'episode')")}
        self.qbt = [(os.path.normcase(os.path.join(sp or "", n or "")), t) for sp, n, t in q(
            "SELECT save_path, name, title_id FROM qbt_torrents WHERE title_id IS NOT NULL AND name IS NOT NULL")]
        self.rel_btih = {(h or "").lower(): t for h, t in q(
            "SELECT r.btih, rt.title_id FROM releases r JOIN release_titles rt ON rt.release_id = r.id WHERE r.btih IS NOT NULL")}
        self.rel_url = {u: t for u, t in q(
            "SELECT r.url, rt.title_id FROM releases r JOIN release_titles rt ON rt.release_id = r.id WHERE r.url IS NOT NULL")}
        self.torrents = torrent_index()
        self.nfo = json.loads(NFO_INDEX.read_text(encoding="utf-8")) if NFO_INDEX.exists() else {}
        self.manual = json.loads(MAP_PATH.read_text(encoding="utf-8")).get("entries", []) if MAP_PATH.exists() else []
        self.roots = _jellyfin_roots() | {os.path.normcase(f"{d}:\\") for d in DRIVES}
        self._nfo_cache: dict[str, dict] = {}
        self.conflicts: list[dict] = []      # .nfo / folder tag vs Plex / Jellyfin for the same file

    # -- evidence
    def _nfo(self, path: str) -> dict:
        if path not in self._nfo_cache:
            self._nfo_cache[path] = read_nfo(path)
        return self._nfo_cache[path]

    def nfo_for(self, path: str) -> tuple[str | None, dict, str | None, dict]:
        """(own nfo path, its data, tvshow.nfo path, its data) — own = same stem or movie.nfo in the folder."""
        p = Path(path)
        folder = self.nfo.get(str(p.parent).lower(), [])
        own = next((n for n in folder if Path(n).stem.lower() == p.stem.lower()), None) \
            or next((n for n in folder if Path(n).name.lower() == "movie.nfo"), None)
        show = None
        q = p.parent
        for _ in range(3):
            show = next((n for n in self.nfo.get(str(q).lower(), []) if Path(n).name.lower() == "tvshow.nfo"), None)
            if show or q.parent == q:
                break
            q = q.parent
        return own, (self._nfo(own) if own else {}), show, (self._nfo(show) if show else {})

    def series_title(self, tv_id: int, season: int | None, episode: int | None, how: str, folder_max: int | None = None):
        s, why = season_for(self.conn, tv_id, season, episode, folder_max)
        if s is None:
            return None, how, f"tv {tv_id}: {why}"
        tid, err = _retry(ensure_season_safe, self.conn, tv_id, s)
        if err:
            return None, "season_clash", err
        if not tid:
            return None, how, f"tv {tv_id} S{s}: not on TMDB"
        return tid, how, f"tv {tv_id} S{s}" + (f" ({why})" if why else "")

    def movie_title(self, tmdb_id: int | None, imdb_id: str | None, how: str):
        from .link import ensure_movie
        if imdb_id:
            def go():
                with self.conn:
                    return ensure_movie(self.conn, imdb_id, how)
            tid = _retry(go)
            if tid:
                return tid, how, imdb_id
        if tmdb_id:
            def go2():
                with self.conn:
                    return ensure_movie_tmdb(self.conn, tmdb_id, how)
            tid, err = _retry(go2)
            return tid, how, err or f"tmdb movie {tmdb_id}"
        return None, how, f"{imdb_id}: not on TMDB"

    def arbitrate(self, cands: list[tuple[str, int]], stem: str, folder: str, is_ep: bool) -> tuple[str, int]:
        """The candidate title whose names / year fit the file (or its folder) name best; ties → the first of
        plex > nfo / folder_tmdbid > jellyfin. An episode-like file never prefers a movie title over a season."""
        from .sources.qbittorrent import translit_ru
        from .sources.tmdb import name_fit
        order = {"plex": 0, "nfo": 1, "folder_tmdbid": 1, "jellyfin": 2}
        queries = []
        for n in (stem, folder):
            t, y, _ = _parse(n)
            if _meaningful(t):
                queries.append((t, y))
        scored = []
        for how, tid in cands:
            row = self.conn.execute("SELECT kind, title_ru, title_original, title_latin, title_uk, year FROM titles "
                                    "WHERE id = ?", (tid,)).fetchone()
            if not row:
                continue
            names = [x for x in (row["title_ru"], row["title_original"], row["title_latin"], row["title_uk"]) if x]
            fit = 0.0
            for t, y in queries:
                f = max(name_fit(q, names)[0] for q in {t, translit_ru(t)})
                if y and row["year"]:
                    f += 0.5 if abs(y - row["year"]) == 0 else 0.3 if abs(y - row["year"]) == 1 else -0.5
                fit = max(fit, f)
            if is_ep and row["kind"] == "movie":
                fit -= 2
            scored.append((-round(fit, 2), order.get(how, 3), how, tid))
        scored.sort()
        return (scored[0][2], scored[0][3]) if scored else cands[0]

    # -- one file
    def decide(self, r: dict, prof: dict) -> dict:
        from .sources import tmdb
        path = r["path"]
        np = os.path.normcase(path)
        stem = Path(path).stem
        season, episode, pos, end = episode_of(stem)
        weak = False      # episode only from «Name 26» numbering in the folder: a movie series «Пила 3/4/5» too
        info = prof.get(os.path.normcase(r["folder"])) or {}
        if episode is None and info.get("numbered"):
            m = NUMBERED.match(stem)
            # only release tags may follow the number: «OverLord.03.HDTV.1080.Dub» yes, «…Case.1.Tsumi.to.Bachi» no
            if m and _key(m.group(1)) in info["numbered"] and _only_tags(stem[m.end(2):]):
                episode, pos, end, weak = int(m.group(2)), m.start(2), m.end(2), True
        parent = Path(path).parent
        fs = folder_season(parent.name, parent.parent.name if SEASON_DIR.match(parent.name) else "")
        if season is None:
            season = fs
        out = {"season": season, "episode": episode, "title_id": None, "how": "unmatched", "note": None,
               "extra_title_ids": None, "nfo_path": None, "nfo_tmdb_id": None, "nfo_imdb_id": None,
               "parsed_title": None, "parsed_year": None}

        px, jx = self.plex.get(np), self.jf.get(np)

        def done(tid, how, note):
            # .nfo / folder tag / Plex / Jellyfin can each be wrong for a file: «Shaolin 2011» .nfo → a Chhota Bheem
            # cartoon (Plex, Jellyfin: Шаолинь), «CURE 1997» Jellyfin → «Say It, Fight It, Cure It» (Plex: Исцеление),
            # «Talk.to.Me.2023» Plex → «Sprich mit mir», «Wednesday S01E02» in a movies library → «Wednesday's Child».
            # When they disagree, the file's own name and year decide (arbitrate); a tie goes to Plex (owner-curated)
            if tid and how in ("nfo", "folder_tmdbid", "plex", "jellyfin"):
                cands = [(how, tid)] + [(h, x) for h, x in (("plex", px), ("jellyfin", jx)) if x and x != tid]
                if len(cands) > 1:
                    best = self.arbitrate(cands, stem, parent.name, episode is not None or bool(season))
                    self.conflicts.append({"path": path, **{h: x for h, x in cands}, "chosen": best[0]})
                    if best[1] != tid:
                        note = f"{how} said title {tid}; the name / year fit {best[0]}'s title {best[1]} better"
                        how, tid = best
            ok = bool(tid) or how == "season_clash"
            out.update(title_id=tid, how=how if ok else "unmatched", note=note if ok else f"{how}: {note}")
            return out

        # manual map (data/disk_map.json): a file, a folder (its own files; «recursive» for the tree), «match» regex
        for m in self.manual:
            mp = os.path.normcase(m.get("path") or "")
            fold = os.path.normcase(r["folder"])
            if not (np == mp or fold == mp or (m.get("recursive") and np.startswith(mp + chr(92)))):
                continue
            if m.get("match") and not re.search(m["match"], Path(path).name, re.I):
                continue
            if m.get("personal") or m.get("not_film"):
                out.update(how="personal" if m.get("personal") else "not_film", note=m.get("why"))
                return out
            if m.get("tmdb_tv"):
                s_no = m.get("season", season)
                out["season"] = s_no
                return done(*self.series_title(m["tmdb_tv"], s_no, episode, "manual"))
            return done(*self.movie_title(m.get("tmdb_movie"), m.get("imdb"), "manual"))
        sp = _slash(path)
        if any(x in sp for x in NOT_FILM):
            out.update(how="not_film")
            return out
        if any(x in sp for x in PERSONAL) or re.search(r"/#[0-9a-f]{12}[.]", sp):   # masked profile files
            out.update(how="personal")
            return out
        if parent.name.lower() in EXTRA_DIRS or EXTRA_NAME.search(stem):
            out.update(how="extra", note="extras / creditless / sample of a release (not a work by itself)")
            return out

        # (a) .nfo
        own, od, show, sd = self.nfo_for(path)
        is_ep = episode is not None or od.get("root") == "episodedetails" or bool(show)
        if own or show:
            out["nfo_path"] = own or show
        if od.get("root") == "episodedetails" or (show and is_ep):
            s_no = od.get("season") if od.get("season") is not None else season
            e_no = od.get("episode") if od.get("episode") is not None else episode
            out.update(season=s_no, episode=e_no)
            tv = int(sd["tmdb"]) if sd.get("tmdb") else None
            out.update(nfo_tmdb_id=tv, nfo_imdb_id=sd.get("imdb"))
            if not tv and sd.get("imdb"):
                tv = tmdb.find_by_imdb(self.conn, sd["imdb"]).get("tv")
            if not tv and od.get("imdb"):
                f = tmdb.find_by_imdb(self.conn, od["imdb"]).get("tv_episode") or {}
                tv, s_no = f.get("show_id"), f.get("season", s_no)
                out["nfo_imdb_id"] = od["imdb"]
            if tv:
                tid, how, note = self.series_title(tv, s_no, e_no, "nfo")
                if tid or how == "season_clash":
                    return done(tid, how, note)
        elif od.get("root") == "movie" and (od.get("tmdb") or od.get("imdb")) and episode is None:
            out.update(nfo_tmdb_id=int(od["tmdb"]) if od.get("tmdb") else None, nfo_imdb_id=od.get("imdb"))
            tid, how, note = self.movie_title(out["nfo_tmdb_id"], od.get("imdb"), "nfo")
            if tid:
                return done(tid, how, note)

        # (b) folder tags
        tags = MOVIE_TAG.findall(path)
        if tags:
            kind, val = tags[-1]
            if kind.lower().startswith("imdb"):
                f = tmdb.find_by_imdb(self.conn, val)
                if f.get("tv") and (episode is not None or show):
                    return done(*self.series_title(f["tv"], season, episode, "folder_tmdbid"))
                if f.get("movie") and episode is None:
                    return done(*self.movie_title(None, val, "folder_tmdbid"))
            elif episode is not None or show:
                return done(*self.series_title(int(val), season, episode, "folder_tmdbid"))
            else:
                return done(*self.movie_title(int(val), None, "folder_tmdbid"))

        # (c) the same file in Jellyfin / Plex
        if px:                     # Plex first: the owner fixes wrong matches there (CURE 1997 is right only in Plex)
            return done(px, "plex", None)
        if jx:
            return done(jx, "jellyfin", None)
        # qBittorrent torrent folder / file
        for prefix, tid in self.qbt:
            if np == prefix or np.startswith(prefix + "\\"):
                return done(tid, "qbt", None)
        # .torrent copies in E:\temp: info-hash / topic URL → a collected release with a title
        for k in (_key(Path(path).name), _key(parent.name)):
            t = self.torrents.get(k)
            if not t:
                continue
            tid = self.rel_btih.get(t["btih"] or "") or self.rel_url.get(
                (t["topic_url"] or "").replace("https://", "http://").replace("rutor.is", "rutor.info"))
            if tid:
                return done(tid, "torrent", t["torrent"])

        # (d) the name
        if is_ep or (season is not None and episode is not None):
            names = []
            if pos:
                names.append(_parse(stem[:pos]))
            elif end and re.match(r"(?i)\s*\d{1,3}\s*(?:серия|эпизод)", stem) and _meaningful(_parse(stem[end:])[0]):    # «1 серия Синий экзорцист…»
                names.append(_parse(stem[end:]))
            sf = show_folder(path, self.roots, prof)
            if sf:
                ft = _parse(Path(sf).name)
                own = [n[0] for n in names if _meaningful(n[0])]
                # the folder only when the file has no show name of its own, or names the same show (adds the year):
                # a folder «Pokemon» holding «1 серия Синий экзорцист…» says nothing about those files
                if not own or _meaningful(ft[0]) and any(_key(o) and (_key(o) in _key(ft[0]) or _key(ft[0]) in _key(o)) for o in own):
                    names.append(ft)
            last = "no show name"
            for title, year, s2 in names:
                if not _meaningful(title):
                    continue
                out.update(parsed_title=title, parsed_year=year)
                tv, why = resolve(self.conn, name_variants(title), year, "tv", min_episodes=episode)
                if tv:
                    tid, how, note = self.series_title(tv, season if season is not None else s2, episode, "name_match",
                                                       folder_max=info.get("max_episode"))
                    if tid or how == "season_clash":
                        return done(tid, how, f"{why}; {note}")
                    last = note
                else:
                    last = f"«{title}» {year or ''}: {why}"
            if not weak:
                return done(None, "unmatched", last)
            out.update(season=None, episode=None)   # not a show: try the name as a film

        parts = [p for p in re.split(r"\s\+\s", stem) if p.strip()] if " + " in stem else [stem]
        tids, notes = [], []
        for part in parts:
            title, year, _ = _parse(part + "." + r["ext"])
            if not _meaningful(title) and len(parts) == 1:
                sf = show_folder(path, self.roots, prof)
                if sf:
                    title, year, _ = _parse(Path(sf).name)
            if not _meaningful(title):
                notes.append(f"no name in «{part}»")
                continue
            out.update(parsed_title=out["parsed_title"] or title, parsed_year=out["parsed_year"] or year)
            mid, why = resolve(self.conn, name_variants(title), year, "movie")
            if not mid and re.search(r"\s\d{1,2}$", title) and info.get("distinct", 0) >= 3 and not info.get("numbered"):
                # «Justice League - War 3» among different films: the owner's own numbering of a film series
                t2 = re.sub(r"\s\d{1,2}$", "", title)
                mid2, why2 = resolve(self.conn, name_variants(t2), year, "movie")
                if mid2:
                    mid, why = mid2, f"{why2} (owner's number dropped)"
            if mid:
                tid, _, note = self.movie_title(mid, None, "name_match")
                if tid:
                    tids.append(tid)
                    notes.append(why)
                    continue
                why = note
            notes.append(f"«{title}» {year or ''}: {why}")
        if tids:
            out["extra_title_ids"] = json.dumps(tids[1:]) if len(tids) > 1 else None
            return done(tids[0], "name_match", "; ".join(notes))
        return done(None, "unmatched", "; ".join(notes))


def link(conn, verbose: bool = False) -> dict:
    rows = [dict(r) for r in conn.execute("SELECT * FROM disk_files WHERE present = 1 ORDER BY path")]
    lk = Linker(conn)
    prof = folder_profile(rows)
    stats = Counter()
    batch = []

    def flush(part):
        with conn:
            for d in part:
                conn.execute("""UPDATE disk_files SET nfo_path = :nfo_path, nfo_tmdb_id = :nfo_tmdb_id,
                                nfo_imdb_id = :nfo_imdb_id, parsed_title = :parsed_title, parsed_year = :parsed_year,
                                season = :season, episode = :episode, title_id = :title_id,
                                extra_title_ids = :extra_title_ids, how = :how, note = :note WHERE path = :path""", d)
    for i, r in enumerate(rows, 1):
        try:
            d = lk.decide(r, prof)
        except Exception as e:  # noqa: BLE001 — one odd file must not stop the inventory
            d = {k: None for k in ("nfo_path", "nfo_tmdb_id", "nfo_imdb_id", "parsed_title", "parsed_year", "season",
                                   "episode", "title_id", "extra_title_ids")}
            d.update(how="unmatched", note=f"error: {type(e).__name__}: {e}"[:300])
        d["path"] = r["path"]
        stats[d["how"]] += 1
        batch.append(d)
        if verbose and i % 200 == 0:
            print(f"{i}/{len(rows)} {dict(stats)}", flush=True)
        if len(batch) >= 200:
            _retry(flush, batch)
            batch = []
    if batch:
        _retry(flush, batch)
    CONFLICTS_PATH.write_text(json.dumps(lk.conflicts, ensure_ascii=False, indent=1), encoding="utf-8")
    db.mark_sync(conn, "disks", None, dict(stats) | {"conflicts": len(lk.conflicts)})
    return dict(stats) | {"conflicts_plex_jellyfin_nfo": len(lk.conflicts)}


# ---------------------------------------------------------------------------------------------- consumers

def linked_title_ids(conn) -> set[int]:
    """Titles with a file on a local drive now (present): «скачан» — the owner has not watched them yet."""
    try:
        rows = conn.execute("SELECT title_id, extra_title_ids FROM disk_files "
                            "WHERE present = 1 AND (title_id IS NOT NULL OR extra_title_ids IS NOT NULL)").fetchall()
    except sqlite3.OperationalError:   # table not migrated yet
        return set()
    out = set()
    for tid, extra in rows:
        if tid:
            out.add(tid)
        out.update(json.loads(extra or "[]"))
    return out


def files_by_title(conn) -> dict[int, list[dict]]:
    out: dict[int, list[dict]] = defaultdict(list)
    try:
        rows = conn.execute("SELECT * FROM disk_files WHERE present = 1 AND title_id IS NOT NULL").fetchall()
    except sqlite3.OperationalError:
        return {}
    for r in rows:
        d = dict(r)
        out[d["title_id"]].append(d)
        for x in json.loads(d.get("extra_title_ids") or "[]"):
            out[x].append(d)
    return dict(out)


def name_quality(path: str) -> dict:
    """Quality of a file Plex does not know, from its (and its folder's) name — the shape of rank.plex_quality.
    resolution None = not in the name (no upgrade is claimed then); has_ru_uk None = unknown."""
    n = path.replace(chr(92), " ")
    res = 2160 if re.search(r"(?i)2160p|\b4k\b|\buhd\b", n) else 1080 if re.search(r"(?i)1080[pi]?\b", n) \
        else 720 if re.search(r"(?i)720p", n) else None
    hdr = [x for x, rx in (("DV", r"\bDV\b|DoVi|Dolby.?Vision|DVP\d"), ("HDR10+", r"HDR10\+|HDR10P"),
                           ("HDR10", r"\bHDR(?:10)?\b")) if re.search(rx, n, re.I)]
    src = next((s for s, rx in (("BDRemux", r"remux"), ("BDRip", r"bdrip|blu-?ray"), ("WEB-DL", r"web-?dl"),
                                ("WEBRip", r"webrip"), ("HDTV", r"hdtv")) if re.search(rx, n, re.I)), None)
    voice = next((v for v, rx in (("dub", r"(?i)\bdub\b|дубляж"), ("mvo", r"(?i)\bmvo\b"), ("dvo", r"(?i)\bdvo\b"),
                                  ("avo", r"(?i)\bavo\b")) if re.search(rx, n)), None)
    ru = True if voice or re.search(r"(?i)\b(?:rus|ukr|ru|uk)\b", n) else None
    dvp = re.search(r"(?i)\bDV\s?P(\d)\b|\bP(\d)\b", n)
    return {"resolution": res, "hdr": hdr, "dv_profile": int(next(g for g in dvp.groups() if g)) if dvp else None,
            "source_type": src, "has_ru_uk": ru, "voice": voice, "from_name": True}


# ---------------------------------------------------------------------------------------------- report

def report(conn, max_unmatched_lines: int = 200) -> dict:
    q = lambda s, *a: conn.execute(s, a).fetchall()  # noqa: E731
    per_drive = {}
    for d, n, gb, present in q("""SELECT drive, count(*), round(sum(size_bytes) / 1073741824.0, 1), sum(present)
                                  FROM disk_files GROUP BY drive ORDER BY drive"""):
        per_drive[d] = {"files": n, "present": present, "gb": gb,
                        "by_how": {h: k for h, k in q("SELECT how, count(*) FROM disk_files WHERE drive = ? AND present = 1 "
                                                      "GROUP BY how ORDER BY 2 DESC", d)}}
    by_how = {h: k for h, k in q("SELECT how, count(*) FROM disk_files WHERE present = 1 GROUP BY how ORDER BY 2 DESC")}
    unmatched, lines = [], 0
    for folder, n, names, note in q("""SELECT folder, count(*), group_concat(substr(path, length(folder) + 2), ' | '),
                                       min(note) FROM disk_files WHERE present = 1 AND how IN ('unmatched', 'season_clash')
                                       GROUP BY folder ORDER BY folder"""):
        if lines >= max_unmatched_lines:
            break
        unmatched.append({"folder": folder, "files": n, "sample": names[:300], "why": note})
        lines += 1
    in_servers = {r[0] for r in q("SELECT title_id FROM plex_items WHERE title_id IS NOT NULL AND present = 1")} | \
                 {r[0] for r in q("SELECT title_id FROM jellyfin_items WHERE title_id IS NOT NULL")}
    files = files_by_title(conn)
    only_disk = []
    for tid, fs in files.items():
        if tid in in_servers:
            continue
        t = conn.execute("SELECT title_ru, title_original, year, season_number, kind FROM titles WHERE id = ?", (tid,)).fetchone()
        if not t:
            continue
        only_disk.append({"title_id": tid, "title": t["title_ru"] or t["title_original"], "original": t["title_original"],
                          "year": t["year"], "season": t["season_number"], "files": len(fs),
                          "folders": sorted({f["folder"] for f in fs})[:3], "how": sorted({f["how"] for f in fs})})
    only_disk.sort(key=lambda x: (x["folders"][0] if x["folders"] else "", x["title"] or "", x["season"] or 0))
    assumed = [{"folder": f, "files": n, "title": t, "season": s, "note": note} for f, n, t, s, note in q(
        """SELECT d.folder, count(*), coalesce(t.title_ru, t.title_original), t.season_number, min(d.note)
           FROM disk_files d JOIN titles t ON t.id = d.title_id
           WHERE d.present = 1 AND (d.note LIKE '%S1 assumed%' OR d.note LIKE '%by votes%' OR d.note LIKE '%absolute%'
                                    OR d.note LIKE '%only exact-name%')
           GROUP BY d.folder, d.title_id ORDER BY d.folder""")]
    conflicts = json.loads(CONFLICTS_PATH.read_text(encoding="utf-8")) if CONFLICTS_PATH.exists() else []
    out = {"per_drive": per_drive, "linked_by": by_how,
           "titles_on_disk": len(files), "titles_on_disk_not_in_plex_jellyfin": len(only_disk),
           "unmatched_by_folder": unmatched, "on_disk_not_in_plex_jellyfin": only_disk,
           "assumptions_to_review": assumed, "metadata_conflicts": conflicts}
    REPORT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def scan(conn, drives=DRIVES) -> dict:
    """Walk the drives (read-only), store, link, write the report. `films disks scan`."""
    w = walk_and_store(conn, drives)
    st = link(conn)
    rep = report(conn)
    return {"walk": w, "link": st, "titles_on_disk": rep["titles_on_disk"],
            "not_in_plex_jellyfin": rep["titles_on_disk_not_in_plex_jellyfin"],
            "unmatched_folders": len(rep["unmatched_by_folder"]), "report": str(REPORT_PATH)}
