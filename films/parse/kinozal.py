"""Parser for kinozal.guru (source priority 4 after rutor, rutracker and nnmclub; owner, 2026-10-26: «и вот ещё
источники после ру трекера которые должны идти https://nnmclub.to и https://kinozal.guru»).

Pure functions only: no network, no DB. kinozal answers curl with a Cloudflare challenge (403), so the pages are read in
the owner's Chrome by films/browser/kinozal_collect.js (he pastes it into the DevTools console) and ingested by
films/sources/kinozal.py.

NOT VERIFIED ON A LIVE PAGE (2026-10-26: nobody could open kinozal from here). The markup used is what Jackett's
kinozal definition (Definitions/kinozal.yml) relies on — search / listing rows ``tr`` with ``td.bt`` (category icon,
``onclick="cat(N)"``), ``td.nam a[href^="/details.php?id="]`` (title), size / seeds / leechers / date in cells 4-7, dates
«сейчас», «сегодня в 09:10», «вчера в 13:04», «24.10.2017 в 23:44» — and its category ids. The topic page is taken
defensively: the title from <h1>, the side panel items («Раздают», «Скачивают», «Вес», «Залит»), every description
block the collector found and the «Тех. данные» tab when it could load it. The first live run reports what it found
(``row_parse_failed``, ``no_post_body``, ``probe``), check it.

The kinozal title: ``Медведь (5 сезон: 1-8 серии из 8) / The Bear / 2026 / ПМ (HDrezka Studio), ДБ (Red Head Sound),
СТ / WEB-DL (2160p, HDR10, Dolby Vision)`` → rutor shape ``Медведь / The Bear [5x1-8 из 8] (2026) WEB-DL 2160p |
HDR10, Dolby Vision | MVO, DUB | HDrezka Studio, Red Head Sound`` (``rutor_style_name``); kinozal voice codes: ДБ dub,
ПМ / ПД / ПО professional multi / two / one voice, АП author, ЛМ / ЛД / ЛО amateur, СТ subtitles.
"""
from __future__ import annotations

import html as _html
import json
import re
from datetime import date, datetime, timedelta
from typing import Any

from .rutor import parse_release_page
from .rutracker import _PostFlattener, _season_blocks, _season_list

BASE = "https://kinozal.guru"
SOURCE = "kinozal"


def topic_url(topic_id) -> str:
    return f"{BASE}/details.php?id={topic_id}"


# --------------------------------------------------------------------------------------------
# categories (Jackett kinozal.yml; the collector also posts the site's own <select name="c"> list — compare)
# --------------------------------------------------------------------------------------------

CATEGORIES: dict[int, tuple[str | None, str]] = {
    8: ("foreign_films", "Кино - Комедия"), 6: ("foreign_films", "Кино - Боевик / Военный"),
    15: ("foreign_films", "Кино - Триллер / Детектив"), 17: ("foreign_films", "Кино - Драма"),
    35: ("foreign_films", "Кино - Мелодрама"), 39: ("foreign_films", "Кино - Индийское"),
    13: ("foreign_films", "Кино - Фантастика"), 14: ("foreign_films", "Кино - Фэнтези"),
    24: ("foreign_films", "Кино - Ужасы / Мистика"), 11: ("foreign_films", "Кино - Приключения"),
    9: ("foreign_films", "Кино - Исторический"), 47: ("foreign_films", "Кино - Азиатское"),
    12: ("foreign_films", "Кино - Детский / Семейный"), 7: ("foreign_films", "Кино - Классика"),
    46: ("foreign_series", "Сериал - Буржуйский"),
    21: ("animation", "Мульт - Буржуйский"), 22: ("animation", "Мульт - Наш"), 20: ("anime", "Аниме"),
    # not collected (Russian films / series, documentaries, sport, concerts, shows, theatre, erotica, music, other)
    10: (None, "Кино - Русское"), 45: (None, "Сериал - Русский"), 18: (None, "Документальный"), 37: (None, "Спорт"),
    48: (None, "Концерты"), 49: (None, "Передачи / ТВ-шоу"), 50: (None, "ТВ-шоу Мир"), 38: (None, "Театр, опера, балет"),
    16: (None, "Эротика"), 3: (None, "Музыка"), 4: (None, "Музыка - Русская"), 5: (None, "Музыка - Сборники"),
    42: (None, "Музыка - Классика"), 2: (None, "Аудиокниги"), 1: (None, "Клипы"), 23: (None, "Игры"),
    32: (None, "Программы"), 40: (None, "Дизайн / Графика"), 41: (None, "Библиотека"),
}
GROUPS = {1002: "Все фильмы", 1001: "Все сериалы", 1003: "Все мульты"}   # listing walks these (browse.php?c=…)


def category_for(cat) -> str | None:
    try:
        return CATEGORIES.get(int(cat), (None, ""))[0]
    except (TypeError, ValueError):
        return None


def category_from_name(name: str | None) -> str | None:
    """For a category id not in CATEGORIES: decide from the site's own name («Сериал - Буржуйский» …)."""
    n = (name or "").lower()
    if not n or re.search(r"русск|наш(?!ел)|отечеств|документ|спорт|концерт|шоу|театр|опер|балет|эрот|музык|игр|"
                          r"программ|книг|библиот|клип|дизайн|граф", n):
        return "animation" if re.search(r"мульт", n) and re.search(r"наш|русск", n) else None
    if "аниме" in n:
        return "anime"
    if "мульт" in n:
        return "animation"
    if "сериал" in n:
        return "foreign_series"
    if re.search(r"кино|фильм", n):
        return "foreign_films"
    return None


# --------------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------------

def _norm_space(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip()


def _text(fragment: str) -> str:
    return _norm_space(_html.unescape(re.sub(r"<[^>]+>", " ", fragment or "")))


MONTHS = {"январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6, "июл": 7, "август": 8, "сентябр": 9,
          "октябр": 10, "ноябр": 11, "декабр": 12}


def _month(word: str) -> int | None:
    w = word.lower()
    for k in sorted(MONTHS, key=len, reverse=True):
        if w.startswith(k):
            return MONTHS[k]
    return None


def parse_date(s: str | None, today: date | None = None) -> str | None:
    """«сегодня в 09:10» / «вчера в 13:04» / «сейчас» / «24.10.2017 в 23:44» / «23 октября 2026 в 12:34» / ISO →
    ISO date-time (the site's — the owner's account — local time)."""
    if not s:
        return None
    s = _norm_space(_html.unescape(str(s))).lower()
    today = today or date.today()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:t\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?z?)?", s):
        return s.upper().rstrip("Z")[:19] if "t" in s else s + "T00:00:00"
    hm = re.search(r"(\d{1,2}):(\d{2})", s)
    hh, mm = (int(hm.group(1)), int(hm.group(2))) if hm else (0, 0)
    if s.startswith("сейчас"):
        return datetime.now().strftime("%Y-%m-%dT%H:%M:00")
    if s.startswith("сегодня") or s.startswith("вчера"):
        d = today - timedelta(days=1 if s.startswith("вчера") else 0)
        return f"{d:%Y-%m-%d}T{hh:02d}:{mm:02d}:00"
    m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", s)
    if m:
        return f"{int(m.group(3)):04d}-{int(m.group(2)):02d}-{int(m.group(1)):02d}T{hh:02d}:{mm:02d}:00"
    m = re.search(r"(\d{1,2})\s+([а-яё]+)\s+(\d{4})", s)
    if m and _month(m.group(2)):
        return f"{int(m.group(3)):04d}-{_month(m.group(2)):02d}-{int(m.group(1)):02d}T{hh:02d}:{mm:02d}:00"
    return None


_SIZE_UNITS = {"б": 0, "кб": 1, "мб": 2, "гб": 3, "тб": 4, "b": 0, "kb": 1, "mb": 2, "gb": 3, "tb": 4}


def parse_size(s: str | None) -> int | None:
    """«4.5 ГБ (4 851 231 744)» → 4851231744; «4.5 ГБ» → approx bytes."""
    t = _norm_space(_html.unescape(s or ""))
    m = re.search(r"\(([\d\s,.]{4,})\)", t)          # «3.9 ГБ (4,192,456,351)» (live page, 2026-10-26)
    if m:
        return int(re.sub(r"[\s,.]", "", m.group(1)))
    m = re.search(r"([\d.,]+)\s*([КМГТ]?Б|[KMGT]?B)\b", t, re.I)
    if not m:
        return None
    try:
        return int(float(m.group(1).replace(",", ".")) * 1024 ** _SIZE_UNITS[m.group(2).lower()])
    except (ValueError, KeyError):
        return None


def title_years(title: str) -> tuple[int, int] | None:
    for part in _split_slash(_html.unescape(title or "")):
        m = re.fullmatch(r"((?:19|20)\d{2})(?:\s*[-–—]\s*((?:19|20)?\d{2}))?(?:\s*,\s*(?:19|20)\d{2})*", part.strip())
        if m:
            ys = [int(y) for y in re.findall(r"(?:19|20)\d{2}", part)]
            if m.group(2) and len(m.group(2)) == 2:
                ys.append(int(m.group(1)[:2] + m.group(2)))
            return min(ys), max(ys)
    return None


# --------------------------------------------------------------------------------------------
# rows (search / listing) — the collector parses them in the page; this is the same for tests and re-parsing
# --------------------------------------------------------------------------------------------

def parse_rows(page: str, today: date | None = None) -> list[dict]:
    out, seen = [], set()
    for tr in re.split(r"<tr\b", page or "")[1:]:
        if 'class="bt"' not in tr and "class='bt'" not in tr:
            continue
        tm = re.search(r'<a[^>]*href="/?details\.php\?id=(\d+)"[^>]*>(.*?)</a>', tr, re.S)
        if not tm or tm.group(1) in seen:
            continue
        seen.add(tm.group(1))
        cm = re.search(r"cat\((\d+)\)|browse\.php\?c=(\d+)|/pic/cat/(\d+)\.", tr)
        cells = [c.split(">", 1)[1] if ">" in c else "" for c in re.split(r"<td\b", tr)[1:]]
        cells = [re.sub(r"</td>.*$", "", c, flags=re.S) for c in cells]

        def cell(i):
            return _text(cells[i]) if len(cells) > i else ""
        sm = re.search(r"class=['\"]sl_s['\"][^>]*>([^<]*)<", tr)
        lm = re.search(r"class=['\"]sl_p['\"][^>]*>([^<]*)<", tr)
        out.append({
            "t": tm.group(1), "title": _text(tm.group(2)),
            "cat": int(next(g for g in cm.groups() if g)) if cm else None,
            "size": parse_size(cell(3)),
            "seeds": int(re.sub(r"\D", "", sm.group(1)) or 0) if sm else (int(cell(4)) if cell(4).isdigit() else None),
            "leech": int(re.sub(r"\D", "", lm.group(1)) or 0) if lm else (int(cell(5)) if cell(5).isdigit() else None),
            "added": parse_date(cell(6), today)})
    return out


# --------------------------------------------------------------------------------------------
# title -> rutor-style name
# --------------------------------------------------------------------------------------------

_VOICE = {"ДБ": "DUB", "ПМ": "MVO", "ПД": "DVO", "ПО": "SVO", "АП": "AVO", "ЛМ": "L", "ЛД": "L2", "ЛО": "L1",
          "СТ": "Sub", "ОР": None, "РУ": None}     # «РУ»: Russian original (a Russian film / series)


def russian_original(title: str) -> bool:
    """«Колокол надежды / 2025 / РУ / WEB-DL (1080p)»: the voice part says the original is Russian — kinozal keeps Russian
    films in its genre categories too, the base is foreign titles only (rutor categories 1 / 4 / 7), so such rows are
    not collected. A foreign title with a Russian voice-over never has a bare «РУ» voice part."""
    for p in _split_slash(_html.unescape(title or "")):
        toks = [t.strip() for t in _split_commas(p)]
        if toks and "РУ" in toks and all(re.fullmatch(r"РУ|СТ|УКР|ENG|АНГ", t) for t in toks):
            return True
    return False
_RES_RE = re.compile(r"(?i)(?<![\w])(\d{3,4})\s*[pi](?![\w])")
_TECH_RE = re.compile(r"(?i)^(?:4k|uhd|hevc|avc|av1|h\.?26[45]|x26[45]|hdr10\+?|hdr\+?|sdr|hlg|hdr\s*vivid|"
                      r"dolby\s*vision(?:\s*(?:p|profile)?\s*\d+(?:\.\d)?)?|dv(?:\s*p?\d+(?:\.\d)?)?|\d+\s*-?\s*bit|imax|3d)$")


def _split_slash(s: str) -> list[str]:
    out, depth, cur = [], 0, []
    i = 0
    while i < len(s):
        ch = s[i]
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth = max(0, depth - 1)
        if ch == "/" and depth == 0 and (i == 0 or s[i - 1] == " ") and (i + 1 == len(s) or s[i + 1] == " "):
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    out.append("".join(cur))
    return [x.strip() for x in out if x.strip()]


def _split_commas(s: str) -> list[str]:
    out, depth, cur = [], 0, []
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return [x.strip() for x in out if x.strip()]


def _season_paren(g: str) -> tuple[list[int], tuple | None]:
    """«5 сезон: 1-8 серии из 8», «1-3 сезоны: 1-24 серии из 24», «1-8 серии из 10», «1 сезон: 1-10 выпуски»."""
    seasons: list[int] = []
    m = re.search(r"(?i)(\d{1,3}(?:\s*[-–—,]\s*\d{1,3})*)\s*сезон\w*", g)
    if m:
        seasons = _season_list(re.sub(r"[–—]", "-", m.group(1)))
    ep = None
    m = re.search(r"(?i)(\d{1,4})(?:\s*[-–—]\s*(\d{1,4}))?\s*(?:сери\w*|выпуск\w*|эпизод\w*)(?:\s*из\s*(\d{1,4}|\?+))?", g)
    if m:
        total = m.group(3) if (m.group(3) or "").isdigit() else None
        ep = (int(m.group(1)), int(m.group(2)) if m.group(2) else None, total, "")
    return seasons, ep


def rutor_style_name(title: str) -> tuple[str, list[str]]:
    """kinozal title -> (rutor-style name for films.parse.rutor.parse_release_name, warnings)."""
    w: list[str] = []
    s = _norm_space(_html.unescape(title or ""))
    parts = _split_slash(s)
    yi = next((i for i, p in enumerate(parts) if re.fullmatch(r"(?:19|20)\d{2}(?:\s*[-–—]\s*(?:19|20)?\d{2})?"
                                                                 r"(?:\s*,\s*(?:19|20)\d{2})*", p)), None)
    if yi is None:
        w.append("no / year / part in kinozal title")
        return s, w
    yrs = title_years(parts[yi]) or (0, 0)
    seasons: list[int] = []
    ep = None
    titles = []
    for p in parts[:yi]:
        for g in re.findall(r"\(([^()]*)\)", p):
            if re.search(r"(?i)сезон|сери|выпуск|эпизод", g):
                se, e = _season_paren(g)
                seasons += se
                ep = ep or e
        t = _norm_space(re.sub(r"\([^()]*\)|\[[^\]]*\]", " ", p))
        if t:
            titles.append(t)
    quality, tags, codes, studios = [], [], [], []
    res = None
    ukr = False
    for p in parts[yi + 1:]:
        toks = _split_commas(p)
        is_voice = toks and all(re.match(r"^(?:\d+\s*[xх×]\s*)?(?:ДБ|ПМ|ПД|ПО|АП|ЛМ|ЛД|ЛО|СТ|ОР|РУ|УКР|UKR|Ukr|Укр)\b", t)
                                for t in toks)
        if is_voice:
            for t in toks:
                m = re.match(r"^(?:\d+\s*[xх×]\s*)?([А-ЯA-Za-zа-я]{2,3})\s*(?:\((.*)\))?$", t)
                code = (m.group(1) if m else t).upper()
                inner = (m.group(2) or "") if m else ""
                if code in ("УКР", "UKR") or re.search(r"(?i)\bukr\b|укр", inner):
                    ukr = True
                    if code in ("УКР", "UKR"):
                        continue
                c = _VOICE.get(code)
                if c:
                    codes.append(c)
                for st in _split_commas(inner):
                    st = st.strip(" .")
                    if st and not re.fullmatch(r"(?i)ukr|укр|rus|eng|\d+", st):
                        studios.append(st)
            continue
        if toks and all(_TECH_RE.match(t) for t in toks):    # «4K, HEVC, HDR, HDR10+, Dolby Vision P8» (live titles)
            tags += [t for t in toks if not re.fullmatch(r"(?i)\d+\s*-?\s*bit", t)]
            continue
        m = re.match(r"^(.*?)\s*\(([^()]*)\)\s*$", p)
        head, inner = (m.group(1), m.group(2)) if m else (p, "")
        if re.search(r"(?i)rip|web|remux|blu-?ray|bd|dvd|hdtv|tv|cam|ts\b|tc\b|dcp|hdr|uhd", head) or _RES_RE.search(p):
            quality.append(_norm_space(head))
            for t in _split_commas(inner):
                rm = _RES_RE.fullmatch(t.strip())
                if rm:
                    res = res or f"{rm.group(1)}p"
                elif t.strip():
                    tags.append(t.strip())
            continue
        tags.append(p)                                       # «Open Matte», «IMAX», «Extended Cut», «Remastered»
    if len(set(codes)) > 1 and "Sub" in codes:
        codes = [c for c in codes if c != "Sub"]             # «ДБ, СТ»: subtitles besides a voice — not «subs only»
    name = " / ".join(titles)
    blocks = _season_blocks(list(dict.fromkeys(seasons)), ep)
    if blocks:
        name += " " + " ".join(f"[{b}]" for b in blocks)
    name += f" ({yrs[0]})" if yrs[0] == yrs[1] else f" ({yrs[0]}-{yrs[1]})"
    if quality:
        name += " " + " ".join(quality)
    if res:
        name += " " + res
    for group in (tags, list(dict.fromkeys(codes)), list(dict.fromkeys(studios))):
        if group:
            name += " | " + ", ".join(group)
    if ukr:
        name += " | UKR"
    return _norm_space(name), w


# --------------------------------------------------------------------------------------------
# topic (as posted by the collector) -> compact document -> parse
# --------------------------------------------------------------------------------------------

def scrub(s: str | None) -> str:
    """Personal keys never stored: passkey-like URL parameters and magnet trackers (announce with a key)."""
    s = re.sub(r"(magnet:\?xt=urn:btih:[0-9A-Za-z]+)[^\"'\s<>]*", r"\1", s or "")
    return re.sub(r"(?i)([?&;](?:uk|passkey|pk|key|sid|hash4u|hash)=)[^&\"'\s<>]+", r"\1REDACTED", s)


def fix_mojibake(s: str | None) -> str:
    """The AJAX tabs (get_srv_details.php) are UTF-8; the first live run (2026-10-26 05:56) decoded them as windows-1251
    («РџРµСЂРµРІРѕРґ» = «Перевод»). Reversed when it is that: cp1251-encodable and valid UTF-8 after the round trip."""
    s = s or ""
    if not re.search(r"[РС][\u0080-¿Ѐ-џ]", s):
        return s
    try:
        fixed = s.encode("cp1251").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s
    return fixed


_TAB_VOICE_RE = re.compile(r"(Перевод:(?:</b>)?\s*[^<|\r\n]*?)\s*\|\s*(?:<b>)?([^<|\r\n]+?)(?:</b>)?\s*\|?\s*(?=<br|\r|\n|$)")


def _tab_voice(s: str) -> str:
    """«Перевод: Профессиональный (полное дублирование) | <b>Movie Dubbing</b>» / «… закадровый |Союз|» → the studio
    in parentheses, the way the rutor parser reads it."""
    return _TAB_VOICE_RE.sub(lambda m: f"{m.group(1)} ({m.group(2).strip()})", s or "")


def compact_topic_html(topic: dict) -> str:
    """What release_raw keeps for a kinozal topic: the meta + the description blocks + the «Тех. данные» tab."""
    meta = {k: topic.get(k) for k in ("topic_id", "title", "cat", "size_bytes", "seeders", "leechers", "btih", "reged",
                                      "fetched_at")}
    # live (2026-10-26): the «Тех. данные»-like tab holds the release's own «Перевод: … | Студия» and «Субтитры: … forced»
    # lines — more exact than the description block, so it goes FIRST in the text (the rutor parser takes the first
    # label); a MediaInfo dump goes into a spoiler like on rutor
    tabs = [dict(t, html=_tab_voice(scrub(fix_mojibake(t.get("html"))))) for t in topic.get("tabs") or [] if t.get("html")]
    lead = "".join(f'<div class="kz-tab">{t["html"]}</div>\n<hr>\n' for t in tabs if t.get("name") != "MediaInfo")
    spoil = "".join(f'<div class="sp-wrap"><div class="sp-body" title="MediaInfo">{t["html"]}</div></div>\n'
                    for t in tabs if t.get("name") == "MediaInfo")
    return (f"<!-- kinozal {topic_url(topic.get('topic_id'))} — collected by films/browser/kinozal_collect.js -->\n"
            f'<script type="application/json" id="kz-meta">{json.dumps(meta, ensure_ascii=False).replace("</", "<\\/")}</script>\n'
            f'<h1 class="maintitle">{_html.escape(topic.get("title") or "", quote=False)}</h1>\n'
            f'<div class="post_body">{lead}{scrub(topic.get("post_html"))}\n{spoil}</div>\n')


def split_compact(page: str) -> dict:
    res: dict[str, Any] = {"topic_id": None, "title": None, "cat": None, "size_bytes": None, "seeders": None,
                           "leechers": None, "btih": None, "reged": None, "fetched_at": None, "post_html": ""}
    m = re.search(r'<script type="application/json" id="kz-meta">(.*?)</script>', page or "", re.S)
    if m:
        meta = json.loads(m.group(1))
        res.update({k: meta.get(k) for k in res if k != "post_html" and meta.get(k) is not None})
    i = (page or "").find('<div class="post_body">')
    if i >= 0:
        body = page[i + len('<div class="post_body">'):]
        res["post_html"] = body[: body.rfind("</div>")] if "</div>" in body else body
    if not res["title"]:
        m = re.search(r"<h1[^>]*>(.*?)</h1>", page or "", re.S)
        res["title"] = _text(m.group(1)) if m else None
    return res


def shape_html(title: str, post_html: str) -> str:
    p = _PostFlattener()
    p.feed(post_html or "")
    p.close()
    main, spoilers = p.result()
    sp = "".join(
        f'<div class="hidewrap"><div class="hidehead">{_html.escape(h, quote=False)}</div><div class="hidebody"></div>'
        f'<textarea class="hidearea">{b.replace("</textarea", "&lt;/textarea")}</textarea></div>\n' for h, b in spoilers)
    name, _ = rutor_style_name(title)
    main = main.replace("\n", "<br />\n")
    return f'<h1>{_html.escape(name, quote=False)}</h1>\n<table id="details"><tr><td>{main}\n{sp}</td></tr></table>\n'


def parse_topic(page: str, today: date | None = None) -> dict:
    """Parse a stored kinozal topic (compact document): the rutor page parser on the adapted document + meta."""
    t = split_compact(page)
    rname, w = rutor_style_name(t["title"] or "")
    d = parse_release_page(shape_html(t["title"] or "", t["post_html"]))
    d["warnings"] = w + d.get("warnings", [])
    if not t["post_html"].strip():
        d["warnings"].append("no description block on kinozal page — name only")
    d["name"] = t["title"]
    d["name_rutor_style"] = rname
    d["category"] = category_for(t["cat"])
    d["size_bytes"] = t["size_bytes"] if t["size_bytes"] is not None else d.get("size_bytes")
    d["seeders"] = t["seeders"] if t["seeders"] is not None else d.get("seeders")
    d["leechers"] = t["leechers"] if t["leechers"] is not None else d.get("leechers")
    d["btih"] = (t["btih"] or d.get("btih") or None)
    d["added_at"] = parse_date(t["reged"], today) or d.get("added_at")
    d["tracker"] = {"source": SOURCE, "topic_id": t["topic_id"], "cat": t["cat"]}
    return d


def parse_title_only(title: str, category: str | None = None) -> dict:
    rname, w = rutor_style_name(title)
    d = parse_release_page(f"<h1>{_html.escape(rname, quote=False)}</h1>")
    d["warnings"] = w + ["listing row only (topic page not read yet)"] + d.get("warnings", [])
    d["name"] = title
    d["name_rutor_style"] = rname
    d["category"] = category
    return d
