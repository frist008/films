"""Parser for nnmclub.to (source priority 3 after rutor and rutracker; owner, 2026-10-26: «и вот ещё источники после ру
трекера которые должны идти https://nnmclub.to и https://kinozal.guru»).

Pure functions only: no network, no DB. Pages are fetched over plain HTTP by films/sources/nnmclub.py (guests may read
the forum listings, tracker.php search and topic pages); they are windows-1251, the caller decodes them.

nnmclub describes a release the way rutor does inside the post (labelled lines «Перевод:», «Аудио 1:», «Видео:»,
«Субтитры:»), so the rutor page parser does the real work. This module adapts what differs:

* the topic title: ``RU / Original (2026) WEB-DL [H.265/2160p] [4K, HDR10+, DV 8.1, 10-bit] (сезон 5, серии 1-8 из 8)
  HDrezka, LostFilm (обновляемая)`` → the rutor shape ``RU / Original [5x1-8 из 8] (2026) WEB-DL 2160p | H.265, 4K,
  HDR10+, DV 8.1 | HDrezka, LostFilm`` (``rutor_style_name``). Fan AI work («[handmade Upscale AI]», «[handmade
  remastered AI]») becomes the tag «AI Upscale» — never the official «remastered» edition, which ranks first;
* the post markup: nnmclub spoilers ``<div class="spoiler-wrap"><div class="spoiler-head">…</div><div
  class="spoiler-body">…</div></div>`` are read as rutracker's ``sp-head`` / ``sp-body`` and become rutor's hidewrap
  spoilers (films.parse.rutracker's flattener). Guests do not see the MediaInfo spoiler («Просмотр доступен только для
  зарегистрированных пользователей») — the description lines above it carry the video / audio / subtitles; a warning
  ``mediainfo hidden from guests`` is added;
* listings (viewforum.php) and search pages (tracker.php, 200 results at most) → rows.

``parse_topic(raw)`` accepts a full viewtopic.php page or the compact document stored in ``release_raw``
(``compact_topic_html``) and returns the rutor parser's dict plus ``tracker`` details.
"""
from __future__ import annotations

import html as _html
import json
import re
from datetime import datetime, timezone
from typing import Any

from .rutor import parse_release_page
from .rutracker import _PostFlattener, _div_block, _season_blocks, _season_list

BASE = "https://nnmclub.to/forum"
SOURCE = "nnmclub"


def topic_url(topic_id) -> str:
    return f"{BASE}/viewtopic.php?t={topic_id}"


# --------------------------------------------------------------------------------------------
# forums -> DB category (index.php of 2026-10-26; foreign films / series / cartoons, all countries for cartoons —
# CLAUDE.md «Мультипликация — все страны»; Russian films and series, CAM («Экранки»), 3D, music, documentaries are not
# collected). The listing crawl walks FORUMS; search results are kept from FORUMS + SEARCH_ONLY.
# --------------------------------------------------------------------------------------------

FORUMS: dict[int, tuple[str, str]] = {
    # films
    954: ("foreign_films", "Зарубежные Новинки (HD, FHD, UHD, 3D)"),
    218: ("foreign_films", "Зарубежные Новинки (SD, DVD)"),
    1293: ("foreign_films", "Новинки с Рекламой"),
    227: ("foreign_films", "Зарубежные Фильмы (HD, FHD)"),
    1296: ("foreign_films", "Зарубежные Фильмы (UHD)"),
    225: ("foreign_films", "Зарубежные Фильмы (SD)"),
    226: ("foreign_films", "Зарубежные Фильмы (DVD)"),
    1299: ("foreign_films", "Фильмы ближнего зарубежья"),
    682: ("foreign_films", "Азиатское кино (SD)"),
    694: ("foreign_films", "Азиатское кино (DVD)"),
    884: ("foreign_films", "Азиатское кино (HD, FHD, UHD)"),
    693: ("foreign_films", "Индийское кино"),
    913: ("foreign_films", "Фильмы с переводом на др. языках"),
    228: ("foreign_films", "Фильмы в оригинале (SD, DVD)"),
    1150: ("foreign_films", "Фильмы в оригинале (HD, FHD, UHD)"),
    1313: ("foreign_films", "Зарубежное кино (коллекции / *логии)"),
    1294: ("foreign_films", "Фильмы с Рекламой"),
    885: ("foreign_films", "Зарубежная Классика (HD, FHD, UHD, 3D)"),
    319: ("foreign_films", "Зарубежная Классика (SD)"),
    678: ("foreign_films", "Зарубежная Классика (DVD)"),
    912: ("foreign_films", "Старые Зарубежные Фильмы (HD, FHD, UHD, 3D)"),
    910: ("foreign_films", "Старые Зарубежные Фильмы (SD)"),
    911: ("foreign_films", "Старые Зарубежные Фильмы (DVD)"),
    1346: ("foreign_films", "Зарубежные Фильмы и Сериалы для детей (HD, FHD, UHD)"),
    733: ("foreign_films", "Зарубежные Фильмы и Сериалы для детей (SD)"),
    # series («Зарубежные сериалы» and its sub-forums)
    768: ("foreign_series", "Зарубежные сериалы"),
    1344: ("foreign_series", "Звездные войны / Star Wars (сериалы по франшизе)"),
    779: ("foreign_series", "Анатомия страсти / Grey's Anatomy"),
    1288: ("foreign_series", "Во все тяжкие / Breaking Bad; Лучше звоните Солу / Better Call Saul"),
    787: ("foreign_series", "Грань / Fringe"),
    1141: ("foreign_series", "Дневники вампира / Vampire Diaries; …"),
    777: ("foreign_series", "Доктор кто / Doctor Who; Торчвуд / Torchwood"),
    786: ("foreign_series", "Доктор Хаус / House M.D."),
    776: ("foreign_series", "Звездные врата / Stargate"),
    785: ("foreign_series", "Звездный Крейсер Галактика / BattleStar Galactica; Каприка / Caprica"),
    775: ("foreign_series", "Звездный путь / Star Trek; Орвилл / The Orville"),
    1265: ("foreign_series", "Игра престолов / Game of Thrones (сериалы по франшизе)"),
    1242: ("foreign_series", "Касл / Castle"),
    1140: ("foreign_series", "Кости / Bones"),
    782: ("foreign_series", "Менталист / The Mentalist; Теория Лжи / Lie To Me"),
    773: ("foreign_series", "Место преступления / CSI"),
    1142: ("foreign_series", "Морская полиция / Navy NCIS; Военно-юридическая служба / JAG"),
    772: ("foreign_series", "Побег / Prison Break"),
    771: ("foreign_series", "Пуаро / Poirot"),
    783: ("foreign_series", "Сверхъестественное / Supernatural"),
    1144: ("foreign_series", "Секретные материалы / X-Files"),
    804: ("foreign_series", "Теория Большого Взрыва / The Big Bang Theory; Детство Шелдона / Young Sheldon"),
    1290: ("foreign_series", "Ходячие мертвецы / The Walking Dead; …"),
    1300: ("foreign_series", "Сериалы ближнего зарубежья"),
    784: ("foreign_series", "Сериалы DC Comics"),
    774: ("foreign_series", "Сериалы Marvel Comics"),
    922: ("foreign_series", "Азиатские сериалы"),
    770: ("foreign_series", "Латиноамериканские сериалы"),
    1320: ("foreign_series", "Турецкие сериалы"),
    780: ("foreign_series", "Сериалы без русского перевода (украинская озвучка)"),
    781: ("foreign_series", "Сериалы без озвученного перевода"),
    1322: ("foreign_series", "Сериалы с рекламой"),
    1220: ("foreign_series", "Зарубежная классика сериалов и старое многосерийное кино до 90-х"),
    722: ("foreign_series", "Чертова служба в госпитале МЭШ / M*A*S*H"),
    # cartoons (all countries) and cartoon series
    1339: ("animation", "Зарубежные Мультфильмы 21-го века (HD, FHD, UHD)"),
    1338: ("animation", "Зарубежные Мультфильмы 21-го века (SD)"),
    1337: ("animation", "Зарубежные Мультфильмы 20-го века (HD, FHD, UHD)"),
    1336: ("animation", "Зарубежные Мультфильмы 20-го века (SD)"),
    660: ("animation", "Зарубежные Мультфильмы (коллекции, сборники / *логии)"),
    232: ("animation", "Зарубежные Мультсериалы"),
    1332: ("animation", "Отечественные Мультфильмы 21-го века (HD, FHD, UHD, 3D)"),
    1331: ("animation", "Отечественные Мультфильмы 21-го века (SD)"),
    1330: ("animation", "Отечественные Мультфильмы 20-го века (HD, FHD, UHD)"),
    1329: ("animation", "Отечественные Мультфильмы 20-го века (SD)"),
    1340: ("animation", "Отечественные Мультфильмы (коллекции, сборники / *логии)"),
    658: ("animation", "Отечественные Мультсериалы"),
}

# anime: only from searches (franchise anime queries, R44) — never walked
SEARCH_ONLY: dict[int, tuple[str, str]] = {
    623: ("anime", "Онгоинги"), 622: ("anime", "Аниме (SD)"), 621: ("anime", "Аниме (HD)"),
    632: ("anime", "Аниме (FullHD)"), 627: ("anime", "Онгоинги с озвучкой"), 626: ("anime", "Аниме с озвучкой (SD)"),
    625: ("anime", "Аниме с озвучкой (HD)"), 644: ("anime", "Аниме с озвучкой (FullHD)"), 635: ("anime", "Аниме DVD"),
    634: ("anime", "Аниме Blu-ray, Remux"), 638: ("anime", "Аниме хардсаб"), 646: ("anime", "Аниме прочее"),
}

# forums seen on index.php that are deliberately not collected (a new video-looking forum outside all three lists is
# reported by `forums`)
NOT_COLLECTED = {217, 1298, 891, 1211, 890, 270, 219, 221, 222, 882, 889, 320, 677, 1177, 908, 1310, 909, 1312, 1221,
                 769, 799, 800, 791, 793, 794, 796, 795, 731, 1345, 1155, 1156, 1099, 1098, 266, 265, 272, 1262, 257,
                 258, 883, 955, 905, 271, 1210, 20, 21, 70, 22, 212, 214, 215, 216, 318, 220, 224, 1311, 256, 264, 1219,
                 729, 725, 724, 229}


def category_for_forum(fid) -> str | None:
    try:
        f = int(fid)
    except (TypeError, ValueError):
        return None
    hit = FORUMS.get(f) or SEARCH_ONLY.get(f)
    return hit[0] if hit else None


_VIDEO_NAME = re.compile(r"кино|фильм|сериал|мульт|аниме|uhd|\bhd\b|\bdvd\b", re.I)
_NOT_VIDEO_NAME = re.compile(r"игр|музык|аудио|книг|клип|концерт|трейлер|документ|спорт|обучающ|курс|журнал|театр|"
                             r"караоке|ищу|обсужд|faq|техпо|обработка|apple|кпк|отечеств|наш|мультимедиа|аниме|\bost\b|"
                             r"livecd|windows|кинолетопись|кинопутешеств|интересно|форум", re.I)


def looks_like_video_forum(name: str) -> bool:
    n = name or ""
    return bool(_VIDEO_NAME.search(n)) and not _NOT_VIDEO_NAME.search(n)


# --------------------------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------------------------

def _norm_space(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip()


def _text(fragment: str) -> str:
    return _norm_space(_html.unescape(re.sub(r"<[^>]+>", " ", fragment or "")))


MONTHS = {"янв": 1, "фев": 2, "мар": 3, "апр": 4, "май": 5, "мая": 5, "июн": 6, "июл": 7, "авг": 8, "сен": 9,
          "окт": 10, "ноя": 11, "дек": 12}


def parse_date(s: str | None) -> str | None:
    """«26 Окт 2026 03:12:47» / «19 Май 2026 07:31» → '2026-10-26T03:12:47'; «20-09-2026 06:21» too; unix seconds → UTC."""
    if not s:
        return None
    s = _norm_space(_html.unescape(str(s)))
    if re.fullmatch(r"\d{9,11}", s):
        return datetime.fromtimestamp(int(s), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    m = re.search(r"(\d{1,2})\s+([А-Яа-яЁё]{3})[а-яё]*\.?\s+(\d{4})(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?", s)
    if m and m.group(2).lower() in MONTHS:
        d, mo, y = int(m.group(1)), MONTHS[m.group(2).lower()], int(m.group(3))
        hh, mm, ss = int(m.group(4) or 0), int(m.group(5) or 0), int(m.group(6) or 0)
        return f"{y:04d}-{mo:02d}-{d:02d}T{hh:02d}:{mm:02d}:{ss:02d}"
    m = re.search(r"(\d{1,2})-(\d{1,2})-(\d{4})(?:\s+(\d{1,2}):(\d{2}))?", s)
    if m:
        return (f"{int(m.group(3)):04d}-{int(m.group(2)):02d}-{int(m.group(1)):02d}T"
                f"{int(m.group(4) or 0):02d}:{int(m.group(5) or 0):02d}:00")
    return None


_SIZE_UNITS = {"b": 0, "kb": 1, "mb": 2, "gb": 3, "tb": 4, "б": 0, "кб": 1, "мб": 2, "гб": 3, "тб": 4}


def parse_size(s: str | None) -> int | None:
    m = re.search(r"([\d.,]+)\s*([KMGT]?B|[КМГТ]?Б)\b", _norm_space(_html.unescape(s or "")), re.I)
    if not m:
        return None
    try:
        v = float(m.group(1).replace(",", "."))
    except ValueError:
        return None
    return int(v * 1024 ** _SIZE_UNITS[m.group(2).lower()])


def page_problem(html: str) -> str | None:
    """Why a page is not a normal nnmclub page: 'cloudflare' (challenge), 'captcha', 'login' (a login form instead of
    the content), 'maintenance'. A normal guest page has the forum navigation. Never bypassed — the caller stops."""
    h = html or ""
    # guest pages carry the login box in the header: a page is a problem only when the forum content is missing
    content = bool(re.search(r'viewforum\.php\?f=\d+|viewtopic\.php\?t=\d+|Результатов поиска', h))
    if content:
        return None
    if re.search(r"cf-chl|challenge-platform|Just a moment|Один момент|cf_chl_opt", h):
        return "cloudflare"
    if re.search(r"(?i)g-recaptcha|hcaptcha|cf-turnstile|captcha_code|введите\s+код\s+с\s+картинки", h):
        return "captcha"
    if re.search(r'(?i)<form[^>]+action="[^"]*login\.php', h):
        return "login"
    if re.search(r"(?i)технические\s+работы|временно\s+недоступ|temporarily\s+unavailable|maintenance", h):
        return "maintenance"
    return None


# --------------------------------------------------------------------------------------------
# listings and search
# --------------------------------------------------------------------------------------------

def parse_index(page: str) -> list[dict]:
    """index.php → [{'f', 'name'}] of every forum link."""
    out, seen = [], set()
    for m in re.finditer(r'href="viewforum\.php\?f=(\d+)"[^>]*>([^<]+)</a>', page or ""):
        f = int(m.group(1))
        if f in seen:
            continue
        seen.add(f)
        out.append({"f": f, "name": _norm_space(_html.unescape(m.group(2)))})
    return out


def parse_listing(page: str) -> dict:
    """viewforum.php → {'page', 'pages', 'rows': [{'t', 'title', 'seeds', 'leech', 'size', 'last_post', 'torrent'}]}.
    Rows come in last-activity order (newest first) after the sticky / announcement block; ``last_post`` is the date of
    the last message (ISO), ``torrent`` = the row has a .torrent (DL) link."""
    page = page or ""
    m = re.search(r"Страница\s*<b>(\d+)</b>\s*из\s*<b>(\d+)</b>", page)
    res: dict[str, Any] = {"page": int(m.group(1)) if m else 1, "pages": int(m.group(2)) if m else 1, "rows": []}
    i = page.find('class="topicSep">Топики')   # below it the ordinary topics, above «Объявления» / stickies
    body = page[i:] if i >= 0 else page
    for tr in re.split(r"<tr\b", body)[1:]:
        if re.search(r"folder_(?:announce|sticky)", tr):
            continue
        tm = re.search(r'<a\s+href="viewtopic\.php\?t=(\d+)"\s*class="topic\w*"[^>]*>(.*?)</a>', tr, re.S)
        if not tm:
            continue
        row = {"t": tm.group(1), "title": _text(tm.group(2)), "seeds": None, "leech": None, "size": None,
               "last_post": None, "torrent": "download.php?id=" in tr or 'class="tDL"' in tr}
        sm = re.search(r'class="seedmed"[^>]*>\s*<b>(\d+)</b>', tr)
        lm = re.search(r'class="leechmed"[^>]*>\s*<b>(\d+)</b>', tr)
        zm = re.search(r'href="download\.php\?id=\d+"[^>]*>([^<]+)</a>', tr)
        dm = re.search(r'class="postdetails">\s*([^<]+?)\s*<br', tr)
        row["seeds"] = int(sm.group(1)) if sm else None
        row["leech"] = int(lm.group(1)) if lm else None
        row["size"] = parse_size(zm.group(1)) if zm else None
        row["last_post"] = parse_date(dm.group(1)) if dm else None
        res["rows"].append(row)
    return res


def parse_search(page: str) -> dict:
    """tracker.php → {'total', 'max', 'rows': [{'t', 'fid', 'forum', 'title', 'size', 'seeds', 'leech', 'added'}]}."""
    page = page or ""
    m = re.search(r"Результатов поиска:\s*(\d+)(?:\s*\(max:\s*(\d+)\))?", page)
    res: dict[str, Any] = {"total": int(m.group(1)) if m else 0, "max": int(m.group(2)) if m and m.group(2) else None,
                           "rows": []}
    seen = set()
    for tr in re.split(r'<tr\s+class="prow\d"', page)[1:]:
        tm = re.search(r'href="viewtopic\.php\?t=(\d+)"[^>]*>(.*?)</a>', tr, re.S)
        if not tm or tm.group(1) in seen:
            continue
        seen.add(tm.group(1))
        fm = re.search(r'href="tracker\.php\?f=(\d+)[^"]*"[^>]*>([^<]*)</a>', tr)
        zm = re.search(r"<u>(\d+)</u>\s*[\d.,]+\s*[KMGT]?B", tr)
        sm = re.search(r'class="seedmed"[^>]*>\s*<b>(\d+)</b>', tr)
        lm = re.search(r'class="leechmed"[^>]*>\s*<b>(\d+)</b>', tr)
        am = re.findall(r"<u>(\d{9,11})</u>", tr)
        res["rows"].append({
            "t": tm.group(1), "fid": int(fm.group(1)) if fm else None, "forum": _text(fm.group(2)) if fm else None,
            "title": _text(tm.group(2)), "size": int(zm.group(1)) if zm else None,
            "seeds": int(sm.group(1)) if sm else None, "leech": int(lm.group(1)) if lm else None,
            "added": parse_date(am[-1]) if am else None})
    return res


def title_years(title: str) -> tuple[int, int] | None:
    m = _YEARS_RE.search(_html.unescape(title or ""))
    if not m:
        return None
    ys = _years(m)
    return min(ys), max(ys)


# --------------------------------------------------------------------------------------------
# topic title -> rutor-style name
# --------------------------------------------------------------------------------------------

_YEARS_RE = re.compile(r"\(\s*((?:19|20)\d{2})(?:\s*[-–—]\s*((?:19|20)?\d{2}))?((?:\s*,\s*(?:19|20)\d{2}(?:\s*[-–—]\s*(?:19|20)\d{2})?)*)\s*\)")
_CODEC_RES_RE = re.compile(r"(?i)^(H\.?26[45]|x26[45]|AVC|HEVC|AV1|XviD|DivX|VP9|MPEG-?[24]|VC-?1)?\s*/?\s*"
                           r"(\d{3,4}[pi])?(?:\s*-\s*(?:LQ|\d{3,4}[pi]))?$")   # «1080p-LQ», «720p-1080p» (first kept)
_BIT_RE = re.compile(r"(?i)^\d{1,2}\s*-?\s*bit$")
_VOICE_TOKEN = re.compile(r"(?i)^(?:\d+\s*[xх×]\s*)?(?:Dub|D|MVO|DVO|AVO|VO|SVO|P|P1|P2|A|L|L1|L2)$")
_LANG_TOKEN = re.compile(r"(?i)^(?:UKR|UA|ENG|EN|RUS|RU|PL|KOR|JPN|JAP|FRA|FR|GER|DE|ITA|ESP|ES|CHN|ORIGINAL|ORIG)$")
_AI_RE = re.compile(r"(?i)handmade|\bAI\b|нейро")
# «сезон 1-6, 12, Серий 1-148» / «сезон 1-8, 208 серий»: a number followed by «серий» is not one more season
_SEASON_WORD_RE = re.compile(r"(?i)сезон\w*\s*:?\s*(\d{1,3}(?:\s*[-–—]\s*\d{1,3})?"
                             r"(?:\s*,\s*\d{1,3}(?:\s*[-–—]\s*\d{1,3})?(?![\d\s\-–—]*(?:сер|эпиз|выпуск)))*)"
                             r"(?:\s*из\s*\d{1,3})?")
_EPIS_RES = [
    re.compile(r"(?i)(?:серии|серия|серий|эпизоды|эпизод|выпуски|выпуск)\s*:?\s*(\d{1,4})(?:\s*[-–—]\s*(\d{1,4}))?"
               r"(?:\s*из\s*(\d{1,4}|\?+|xx))?"),
    re.compile(r"(?i)(\d{1,4})(?:\s*[-–—]\s*(\d{1,4}))?\s*(?:серии|серия|серий|эпизодов|эпизода|выпусков)"
               r"(?:\s*из\s*(\d{1,4}|\?+|xx))?"),
]


def _years(m) -> list[int]:
    y1 = int(m.group(1))
    ys = [y1]
    if m.group(2):
        y2 = m.group(2)
        ys.append(int(y2) if len(y2) == 4 else int(str(y1)[:2] + y2))
    ys += [int(x) for x in re.findall(r"(?:19|20)\d{2}", m.group(3) or "")]
    return ys


def _split_top(s: str) -> list[str]:
    out, depth, cur = [], 0, []
    for ch in s:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return [x.strip() for x in out if x.strip()]


def _season_info(text: str) -> tuple[list[int], tuple | None]:
    seasons: list[int] = []
    for m in _SEASON_WORD_RE.finditer(text):
        seasons += _season_list(m.group(1))
    rest = _SEASON_WORD_RE.sub(" ", text)
    ep = None
    for i, rx in enumerate(_EPIS_RES):
        m = rx.search(rest)
        if m:
            total = m.group(3) if (m.group(3) or "").isdigit() else None
            if i == 1 and not m.group(2) and not total:     # «208 серий»: a count — episodes 1-208 of 208
                n = int(m.group(1))
                ep = (1, n, str(n), "")
            else:
                ep = (int(m.group(1)), int(m.group(2)) if m.group(2) else None, total, "")
            break
    if re.search(r"(?i)бонус|спецвып|special", text) and ep:
        ep = (ep[0], ep[1], ep[2], "+ бонусы")
    return list(dict.fromkeys(seasons)), ep


def rutor_style_name(title: str) -> tuple[str, list[str]]:
    """nnmclub topic title -> (rutor-style name for films.parse.rutor.parse_release_name, warnings)."""
    w: list[str] = []
    s = _norm_space(_html.unescape(title or ""))
    ym = None
    for m in _YEARS_RE.finditer(s):
        ym = m
        break
    if not ym:
        w.append("no (year) in nnmclub title")
        return s, w
    head, tail = s[: ym.start()], s[ym.end():]
    years = _years(ym)

    seasons: list[int] = []
    ep = None
    # season / episodes may also sit in the head: «Название [S01] / Title»
    head = re.sub(r"\[[^\]]*\]", " ", head)
    head = re.sub(r"\([^()]*\)", " ", head)
    titles = [_norm_space(t) for t in re.split(r"\s+/\s+|\s/(?=\S)|(?<=\S)/\s", head) if _norm_space(t)]

    quality, codec_tags, tags, codes, studios = [], [], [], [], []
    ukr, subs = False, False
    # bracket and paren groups of the tail
    groups = []
    for m in re.finditer(r"\[([^\]]*)\]|\(([^()]*)\)", tail):
        groups.append(("[" if m.group(1) is not None else "(", m.group(1) if m.group(1) is not None else m.group(2)))
    bare = _norm_space(re.sub(r"\[[^\]]*\]|\([^()]*\)", " | ", tail))
    pieces = [p.strip(" ,;.") for p in bare.split("|")]
    first = pieces[0] if pieces else ""
    if first:
        quality.append(first)                                  # «WEB-DL», «UHD BDRip», «31xDVD5»
    for p in pieces[1:]:
        for st in _split_top(p):
            st = st.strip(" .;")
            if not st:
                continue
            if _VOICE_TOKEN.match(st):
                codes.append(st.upper())
            elif re.fullmatch(r"(?i)ukr|укр", st):
                ukr = True
            else:
                studios.append(st)
    res = None
    for kind, g in groups:
        g = _norm_space(g)
        if kind == "(":
            if re.search(r"(?i)сезон|сери|эпизод|выпуск", g):
                se, e = _season_info(g)
                seasons += se
                ep = ep or e
            elif re.fullmatch(r"(?i)обновля\w*|update\w*|в процессе|продолжается", g):
                pass
            else:
                w.append(f"unclassified (…) group {g!r} in nnmclub title")
            continue
        cm = _CODEC_RES_RE.match(g)
        if cm and (cm.group(1) or cm.group(2)):
            if cm.group(1):
                codec_tags.append(cm.group(1))
            if cm.group(2):
                res = res or cm.group(2).lower()
            continue
        if re.search(r"(?i)\bAI\b|up\s*-?scal|нейро", g):
            tags.append("AI Upscale")                          # fan work, never the official «remastered» edition
            continue
        if re.fullmatch(r"(?i)handmade", g):
            continue                                            # a hand-made encode, says nothing about the picture
        if re.fullmatch(r"(?i)noir\s+version|ч/?б(?:\s+версия)?|black\s*(?:&|and)?\s*white", g):
            tags.append("Black & White")
            continue
        if re.fullmatch(r"(?i)colou?r\s+version|цветная\s+версия", g):
            continue                                            # colour is the default
        for part in re.split(r"\s*/\s*", g):
            for tok in _split_top(part):
                tok = tok.strip()
                if not tok:
                    continue
                if _BIT_RE.match(tok):
                    continue
                if re.search(r"(?i)\bsub\b|субт", tok):
                    subs = True
                    continue
                if re.fullmatch(r"(?i)ukr|ua|укр", tok):
                    ukr = True
                    continue
                if _LANG_TOKEN.match(tok):
                    continue
                if _VOICE_TOKEN.match(tok):
                    codes.append("DUB" if tok.upper() in ("D", "DUB") else tok.upper())
                    continue
                tags.append(tok)                                # 4K, HDR10, DV 8.1, Dolby Vision P7, SDR, IMAX, Extended …
    if subs and not codes and not studios:
        codes.append("Sub")

    name = " / ".join(titles)
    blocks = _season_blocks(seasons, ep)
    if blocks:
        name += " " + " ".join(f"[{b}]" for b in blocks)
    yr = f"{min(years)}" if max(years) == min(years) else f"{min(years)}-{max(years)}"
    name += f" ({yr})"
    if quality:
        name += " " + " ".join(quality)
    if res:
        name += " " + res
    for group in (codec_tags + tags, list(dict.fromkeys(codes)), list(dict.fromkeys(studios))):
        if group:
            name += " | " + ", ".join(group)
    if ukr:
        name += " | UKR"
    return _norm_space(name), w


# --------------------------------------------------------------------------------------------
# topic page
# --------------------------------------------------------------------------------------------

_GUEST_MI = re.compile(r"Просмотр доступен только для зарегистрированных")


def _empty_topic() -> dict[str, Any]:
    return {"topic_id": None, "title": None, "forum": None, "forum_id": None, "size_bytes": None, "seeders": None,
            "leechers": None, "btih": None, "reged": None, "post_html": ""}


def split_topic(page: str) -> dict:
    """Title, forum, size, btih, registration date and first-post HTML of a viewtopic.php page (or of the compact
    document made by ``compact_topic_html``)."""
    page = page or ""
    res = _empty_topic()
    m = re.search(r'<script type="application/json" id="nnm-meta">(.*?)</script>', page, re.S)
    if m:
        meta = json.loads(m.group(1))
        res.update({k: meta.get(k) for k in res if k != "post_html" and meta.get(k) is not None})
    m = re.search(r'<a[^>]*class="maintitle"[^>]*>(.*?)</a>', page, re.S) or \
        re.search(r'<h1[^>]*class="maintitle"[^>]*>(.*?)</h1>', page, re.S)
    if m and not res["title"]:
        res["title"] = _text(m.group(1))
    pb = re.search(r'<div[^>]*class="(?:postbody|post_body)"', page)
    first = pb.start() if pb else len(page)
    if not res["forum_id"]:
        fr = re.findall(r'href="viewforum\.php\?f=(\d+)"[^>]*class="nav"[^>]*>([^<]+)</a>|'
                        r'class="nav"[^>]*href="viewforum\.php\?f=(\d+)"[^>]*>([^<]+)</a>', page[:first])
        if fr:
            f = fr[-1]
            res["forum_id"], res["forum"] = (f[0] or f[2]), _norm_space(_html.unescape(f[1] or f[3]))
    if not res["topic_id"]:
        m = re.search(r'class="maintitle"[^>]*href="viewtopic\.php\?t=(\d+)"|href="viewtopic\.php\?t=(\d+)"[^>]*class="maintitle"',
                      page)
        res["topic_id"] = (m.group(1) or m.group(2)) if m else None
    if not res["btih"]:
        m = re.search(r"magnet:\?xt=urn:btih:([0-9A-Fa-f]{40})", page)
        res["btih"] = m.group(1).lower() if m else None
    if not res["reged"]:
        m = re.search(r"Зарегистрирован:(?:&nbsp;|\s|<[^>]+>)*([^<]{6,40}?)\s*</td>", page)
        res["reged"] = _norm_space(_html.unescape(m.group(1))) if m else None
    if res["size_bytes"] is None:
        m = re.search(r"Размер:(?:&nbsp;|\s|<[^>]+>)*([\d.,]+(?:&nbsp;|\s)*[KMGT]?B)", page)
        res["size_bytes"] = parse_size(m.group(1)) if m else None
    if pb:
        res["post_html"] = _div_block(page, pb.start())
    return res


def compact_topic_html(topic: dict) -> str:
    """What release_raw keeps for an nnmclub topic: the meta + the first post's HTML (description; MediaInfo when the
    page shows it). Re-parsable by ``parse_topic`` without opening the topic again."""
    meta = {k: topic.get(k) for k in ("topic_id", "title", "forum", "forum_id", "size_bytes", "seeders", "leechers",
                                      "btih", "reged", "fetched_at")}
    return (f"<!-- nnmclub {topic_url(topic.get('topic_id'))} — first post, collected by films/sources/nnmclub.py (HTTP) -->\n"
            f'<script type="application/json" id="nnm-meta">{json.dumps(meta, ensure_ascii=False).replace("</", "<\\/")}</script>\n'
            f'<h1 class="maintitle">{_html.escape(topic.get("title") or "", quote=False)}</h1>\n'
            f'<div class="post_body">{topic.get("post_html") or ""}</div>\n')


def shape_html(title: str, post_html: str) -> str:
    """A document films.parse.rutor.parse_release_page understands: <h1>name</h1> + details with hidewrap spoilers."""
    post = re.sub(r'class="spoiler-head', 'class="sp-head', post_html or "")
    post = re.sub(r'class="spoiler-body', 'class="sp-body', post)
    p = _PostFlattener()
    p.feed(post)
    p.close()
    main, spoilers = p.result()
    sp = "".join(
        f'<div class="hidewrap"><div class="hidehead">{_html.escape(h, quote=False)}</div><div class="hidebody"></div>'
        f'<textarea class="hidearea">{b.replace("</textarea", "&lt;/textarea")}</textarea></div>\n' for h, b in spoilers)
    name, _ = rutor_style_name(title)
    main = main.replace("\n", "<br />\n")
    return f'<h1>{_html.escape(name, quote=False)}</h1>\n<table id="details"><tr><td>{main}\n{sp}</td></tr></table>\n'


def parse_topic(page: str) -> dict:
    """Parse an nnmclub topic: the rutor page parser on the adapted document + tracker meta.
    ``name`` stays the nnmclub title; ``name_parsed`` is parsed from its rutor-style form."""
    t = split_topic(page)
    rname, w = rutor_style_name(t["title"] or "")
    d = parse_release_page(shape_html(t["title"] or "", t["post_html"]))
    d["warnings"] = w + d.get("warnings", [])
    if not t["post_html"]:
        d["warnings"].append("no first post (postbody) on nnmclub page — name only")
    elif _GUEST_MI.search(t["post_html"]) and not d.get("mediainfo_found"):
        d["warnings"].append("mediainfo hidden from guests (description lines only)")
    d["name"] = t["title"]
    d["name_rutor_style"] = rname
    d["category"] = category_for_forum(t["forum_id"])
    d["size_bytes"] = t["size_bytes"] if t["size_bytes"] is not None else d.get("size_bytes")
    d["seeders"] = t["seeders"] if t["seeders"] is not None else d.get("seeders")
    d["leechers"] = t["leechers"] if t["leechers"] is not None else d.get("leechers")
    d["btih"] = t["btih"] or d.get("btih")
    d["added_at"] = parse_date(t["reged"]) or d.get("added_at")
    d["tracker"] = {"source": SOURCE, "topic_id": t["topic_id"], "forum": t["forum"], "forum_id": t["forum_id"]}
    return d


def parse_title_only(title: str, category: str | None = None) -> dict:
    """A listing / search row without its topic page: everything the title says."""
    rname, w = rutor_style_name(title)
    d = parse_release_page(f"<h1>{_html.escape(rname, quote=False)}</h1>")
    d["warnings"] = w + ["listing row only (topic page not read yet)"] + d.get("warnings", [])
    d["name"] = title
    d["name_rutor_style"] = rname
    d["category"] = category
    return d
