"""Parser for rutracker.org topics (R9: rutracker is searched only for what rutor lacks — 4K / HDR-DV / dub).

Pure functions only: no network, no DB. The pages are collected in the owner's Chrome by
films/browser/rutracker_collect.js and ingested by films/sources/rutracker.py.

rutracker and rutor describe releases the same way inside the post (labelled lines «Перевод:», «Аудио 1:», MediaInfo in a
spoiler), so the rutor page parser does the real work. This module only adapts the two things that differ:

* the topic title: ``RU / Original / Сезон: 27 / Серии: 1-5 из 5 (Режиссёр) [2025, США, комедия, WEB-DL 2160p, SDR]
  DVO (Кубик в Кубе) + 3 MVO + Original + Sub (Rus, Eng)`` → the rutor shape
  ``RU / Original [27x1-5 из 5] (2025) WEB-DL 2160p | SDR | DVO, MVO | Кубик в Кубе`` (``rutor_style_name``);
* the post markup: rutracker spoilers ``<div class="sp-wrap"><div class="sp-body" title="MediaInfo">…</div></div>``
  (or the script-built ``sp-head``) become rutor's ``hidewrap``/``textarea`` spoilers (``rutor_shape_html``).

``parse_topic(raw)`` accepts a full viewtopic.php page or the compact document stored in ``release_raw``
(``compact_topic_html``) and returns the rutor parser's dict plus ``rutracker`` details.
"""
from __future__ import annotations

import html as _html
import json
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any

from .rutor import _SOURCE_RE, _lat, parse_release_page

BASE = "https://rutracker.org/forum"

# --------------------------------------------------------------------------------------------
# forum -> DB category
# --------------------------------------------------------------------------------------------

_NOT_VIDEO = re.compile(r"игр|музык|аудио|книг|саундтрек|soundtrack|\bost\b|клип|концерт|трейлер|документал|спорт|"
                        r"обучающ|обои|журнал|комикс|софт|программ|юмор|караоке|опер[аы]\b|балет|театр|новост|"
                        r"обсужден|архив|флуд|wallpaper|game|music", re.I)
_VIDEO = re.compile(r"кино|фильм|сериал|мульт|аниме|видео|video|uhd|\bhd\b|\bdvd\b|3d|стерео", re.I)
_RUSSIAN = re.compile(r"наш|русск|росси|отечеств|ссср|советск", re.I)


def category_for_forum(forum: str | None) -> str | None:
    """rutracker forum name -> releases.category (the rutor names), or None for forums outside the base
    (games, music, documentaries, Russian films …). Same rules as videoForum() in rutracker_collect.js."""
    f = (forum or "").strip()
    if not f or _NOT_VIDEO.search(f) or not _VIDEO.search(f):
        return None
    if re.search(r"аниме|anime", f, re.I):
        return "anime"
    if re.search(r"мульт", f, re.I):
        return "animation"      # rutor «Мультипликация» (7) also holds cartoon series
    if re.search(r"сериал", f, re.I):
        return "russian_series" if _RUSSIAN.search(f) else "foreign_series"
    return None if _RUSSIAN.search(f) else "foreign_films"


def category_for(forum: str | None, title: str | None) -> str | None:
    """category_for_forum, and a «[ANIME] …» topic is anime whatever video forum holds it (review 2026-10-28: «[ANIME]
    Семья шпиона / Spy x Family …» in an HD Video film forum became a visible «foreign film» season)."""
    cat = category_for_forum(forum)
    if cat and re.match(r"\s*\[ANIME\]", title or "", re.I):
        return "anime"
    return cat


# --------------------------------------------------------------------------------------------
# topic title -> rutor-style name
# --------------------------------------------------------------------------------------------

_INFO_RE = re.compile(r"\[\s*((?:19|20)\d{2})(?:\s*[-–—]\s*((?:19|20)?\d{2}))?((?:\s*[, ]\s*(?:19|20)\d{2}(?!\d))*)([^\]]*)\]")
_SEASON_RE = re.compile(r"(?i)(?:^|/|\s)\s*Сезон(?:ы)?\s*:?\s*([\d][\d\s,\-–—]*)(?=\s*(?:/|\(|\[|$|\s[А-Яа-яA-Za-z]))")
_EPIS_RE = re.compile(r"(?i)(?:^|/|\s)\s*(?:Серии|Серия|Эпизоды|Выпуски)\s*:?\s*(\d{1,4})(?:\s*[-–—]\s*(\d{1,4}))?"
                      r"(?:\s*(?:из|of)\s*(\d{1,4}|\?+|xx|XX))?((?:\s*\+\s*[^/\[\]()]+?)?)(?=\s*(?:/|\(|\[|$))")
_EPIS_BRACKET_RE = re.compile(r"(?i)\[\s*(?:(?:Серии|Серия|Эпизоды)\s*:?\s*(\d{1,4})(?:\s*[-–—]\s*(\d{1,4}))?"
                              r"(?:\s*(?:из|of)\s*(\d{1,4}))?|(\d{1,4})\s*(?:Серий|Серии|эпизодов))\s*\]")
# anime «[ONA] [10 из 10]», «[TV] [1-12 из 24]» (review round 12: «Cyberpunk: Edgerunners 2 [ONA] [10 из 10]» became a
# film); not OVA / Movie — «[OVA][1 из 1]» is one work
_EPIS_ANIME_RE = re.compile(r"(?i)\[\s*(?:TV|ONA|WEB)\s*\]\s*\[\s*(?:(\d{1,4})\s*[-–—]\s*)?(\d{1,4})\s*из\s*(\d{1,4}|\?+|[xх]+)\s*\]")
_RES_RE = re.compile(r"(?<![\w])(2160|1440|1080|720|576|540|480|360)\s*[pi](?![\w])", re.I)
_HDR_TOKEN_RE = re.compile(r"(?i)^(?:hdr10\+|hdr10|hdr|sdr|hlg|hdr vivid|dolby\s*vision.*|dv(?:\s*p?\d.*)?|dovi.*)$")
_CODEC_RE = re.compile(r"(?i)^(?:hevc|avc|av1|x26[45]|h\.?26[45]|vp9|xvid)$")
_EDITION_TOKEN_RE = re.compile(r"(?i)imax|open\s*-?\s*matte|extended|director|режисс|расширенн|theatrical|театральн|"
                               r"uncut|unrated|remaster|ремастер|up\s*-?\s*scal|апск")
_VOICE_RE = re.compile(r"^(?:(\d+)\s*[xх×]\s*)?(Dub|DUB|D|MVO|DVO|AVO|VO|SVO)\b(.*)$")


def _norm_space(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _drop_parens(s: str) -> tuple[str, list[str]]:
    """Remove (possibly nested) parenthesised groups; returns (text, removed groups innermost-last)."""
    removed = []
    while True:
        m = re.search(r"\(([^()]*)\)", s)
        if not m:
            return s, removed
        removed.append(m.group(1))
        s = s[: m.start()] + " " + s[m.end():]


def _outer_parens(s: str) -> tuple[str, list[str]]:
    """Text outside top-level parentheses and the top-level groups' contents (nested parens kept inside):
    «VO (Д. Пучков (Гоблин))» -> ("VO", ["Д. Пучков (Гоблин)"])."""
    bare, groups, depth, cur = [], [], 0, []
    for ch in s:
        if ch == "(":
            depth += 1
            if depth == 1:
                cur = []
                continue
        elif ch == ")" and depth:
            depth -= 1
            if depth == 0:
                groups.append("".join(cur).strip())
                continue
        (cur if depth else bare).append(ch)
    if depth:
        groups.append("".join(cur).strip())
    return _norm_space("".join(bare)), groups


def _split_top(s: str, seps: str = "+,") -> list[str]:
    """Split on separators that are not inside parentheses."""
    out, depth, cur = [], 0, []
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch in seps and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return [x.strip() for x in out if x.strip()]


def _season_list(s: str) -> list[int]:
    out: list[int] = []
    for part in re.split(r"\s*,\s*", s.strip(" ,")):
        m = re.fullmatch(r"(\d{1,3})(?:\s*[-–—]\s*(\d{1,3}))?", part.strip())
        if not m:
            continue
        a, z = int(m.group(1)), int(m.group(2) or m.group(1))
        out += list(range(a, z + 1)) if z >= a else [a, z]
    return list(dict.fromkeys(out))


def _season_blocks(seasons: list[int], ep: tuple | None) -> list[str]:
    """rutor bracket blocks: [S27], [S14-15], [27x1-5 из 5], [14-15x1-28 из 28 + 5 фильмов], [1-8 из 8]."""
    runs: list[tuple[int, int]] = []
    for s in sorted(set(seasons)):
        if runs and s == runs[-1][1] + 1:
            runs[-1] = (runs[-1][0], s)
        else:
            runs.append((s, s))
    eps = ""
    if ep:
        a, z, total, extra = ep
        eps = f"{a}" if z in (None, a) else f"{a}-{z}"
        if total:
            eps += f" из {total}"
        if extra:
            eps += f" {extra}"
    if not runs:
        return [eps] if eps else []
    if eps and len(runs) == 1:
        a, z = runs[0]
        return [f"{a}x{eps}" if a == z else f"{a}-{z}x{eps}"]
    blocks = [f"S{a}" if a == z else f"S{a}-{z}" for a, z in runs]
    if eps:
        blocks.append(eps)
    return blocks


def _is_source(tok: str) -> bool:
    lq = _lat(tok)
    for m in _SOURCE_RE.finditer(lq):
        if (m.start() == 0 or not lq[m.start() - 1].isalnum()) and (m.end() == len(lq) or not lq[m.end()].isalnum()):
            return True
    return False


def rutor_style_name(title: str) -> tuple[str, list[str]]:
    """rutracker topic title -> (rutor-style name for films.parse.rutor.parse_release_name, warnings)."""
    w: list[str] = []
    s = _norm_space(_html.unescape(title or ""))
    while True:  # leading forum tags: [MULTSERIAL], [MULT], [Nintendo Switch] …
        m = re.match(r"^\[([^\]]*)\]\s*", s)
        if not m or re.match(r"\s*(?:19|20)\d{2}", m.group(1)):
            break
        s = s[m.end():]
    im = None
    for m in _INFO_RE.finditer(s):
        im = m
        break
    if not im:
        w.append("no [year, …] block in rutracker title")
        return s, w
    head, tail = s[: im.start()], s[im.end():]
    y1 = int(im.group(1))
    y2 = im.group(2)
    years = [y1]
    if y2:
        years.append(int(y2) if len(y2) == 4 else int(str(y1)[:2] + y2))
    years += [int(x) for x in re.findall(r"(?:19|20)\d{2}", im.group(3) or "")]

    # season / episode info anywhere before the info block (also after the directors: «(…) Сезоны: 1-28 / Серии: …»)
    seasons: list[int] = []
    for m in _SEASON_RE.finditer(head):
        seasons += _season_list(m.group(1))
    head = _SEASON_RE.sub(" / ", head)
    ep = None
    m = _EPIS_RE.search(head)
    if m:
        total = m.group(3) if (m.group(3) or "").isdigit() else None
        extra = _norm_space(m.group(4) or "")
        ep = (int(m.group(1)), int(m.group(2)) if m.group(2) else None, total, extra)
        head = head[: m.start()] + " / " + head[m.end():]
    elif not seasons and (m := _EPIS_BRACKET_RE.search(head)):   # «Сезон: 14-15 …[28 Серии]» — a count over two seasons
        # anime «[Серии 501-519]», «[378 Серий]» (review round 11: «One Piece [Серии 501-519] [2011]» became a film)
        if m.group(4):
            ep = (1, int(m.group(4)), m.group(4), "")
        else:
            ep = (int(m.group(1)), int(m.group(2)) if m.group(2) else None, m.group(3), "")
    elif not seasons and (m := _EPIS_ANIME_RE.search(head)):
        ep = (int(m.group(1)) if m.group(1) else 1, int(m.group(2)), m.group(3), "")
    head = re.sub(r"\[[^\]]*\]", " ", head)          # «[28 Серии]» and other stray brackets
    head, _ = _drop_parens(head)                     # directors «(Мэтт Стоун, Трей Паркер / Matt Stone, …)»
    titles = [_norm_space(t) for t in re.split(r"\s*/\s*", head) if _norm_space(t)]

    quality, tags = [], []
    rest = [t.strip() for t in re.split(r",", im.group(4) or "") if t.strip()]
    for tok in rest:
        if _is_source(tok) or _RES_RE.search(tok):
            quality.append(tok)
        elif _HDR_TOKEN_RE.match(tok) or _CODEC_RE.match(tok) or _EDITION_TOKEN_RE.search(tok) or \
                re.fullmatch(r"(?i)4k|uhd|3d|hybrid", tok):
            tags.append(tok)
        # else: country / genre — not part of a rutor name

    codes, studios, ukr, subs = [], [], False, False
    tail = re.sub(r"\[[^\]]*\]", " ", tail)          # «[MP4, 960x]», «[P]» …
    for part in _split_top(tail):
        part = part.strip(" .;")
        bare, inner = _outer_parens(part)
        if re.fullmatch(r"(?i)(?:\d+\s*[xх×]\s*)?(?:ukr|укр)", bare) or re.search(r"(?i)^ukr\b", bare):
            ukr = True
            continue
        if re.match(r"(?i)^sub\b", bare):
            subs = True
            continue
        if re.match(r"(?i)^(original|eng|rus|оригинал)\b", bare):
            continue
        m = _VOICE_RE.match(bare) or re.match(r"^(?:(\d+)\s*[xх×]?\s*)(Dub|DUB|D|MVO|DVO|AVO|VO|SVO)\b(.*)$", bare)
        if not m:
            if bare:
                w.append(f"unclassified audio part {part!r} in rutracker title")
            continue
        if re.search(r"(?i)\bukr\b|укр", m.group(3) or "") or any(re.fullmatch(r"(?i)\s*ukr\s*", x) for x in inner):
            ukr = True              # «MVO Ukr», «Dub (Ukr)»: a Ukrainian track
            continue
        code = m.group(2).upper()
        codes.append("DUB" if code in ("D", "DUB") else code)
        for group in inner:
            for st in _split_top(group, ","):
                st = re.sub(r"(?i)\s*и\s*др\.?$|\s*etc\.?$", "", st).strip(" .")
                if st and not re.fullmatch(r"\d+", st) and not re.fullmatch(r"(?i)rus|eng|ukr", st):
                    studios.append(st.replace("|", "/"))
    if subs and not codes:
        codes.append("Sub")

    name = " / ".join(titles)
    blocks = _season_blocks(seasons, ep)
    if blocks:
        name += " " + " ".join(f"[{b}]" for b in blocks)
    yr = f"{min(years)}" if max(years) == min(years) else f"{min(years)}-{max(years)}"
    name += f" ({yr})"
    if quality:
        name += " " + " ".join(quality)
    for group in (tags, list(dict.fromkeys(codes)), list(dict.fromkeys(studios))):
        if group:
            name += " | " + ", ".join(group)
    if ukr:
        name += " | UKR"
    return _norm_space(name), w


# --------------------------------------------------------------------------------------------
# post markup -> rutor-shaped HTML
# --------------------------------------------------------------------------------------------

_BLOCK_END = {"p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "table", "ul", "ol", "blockquote"}


class _PostFlattener(HTMLParser):
    """Collect the post text (links/images kept as empty tags so the rutor parser still finds IMDb / Kinopoisk ids);
    every spoiler — nested ones too — becomes a separate (head, text) entry."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.bufs: list[list[str]] = [[]]        # [0] = main text, then one per open spoiler body
        self.heads: list[str] = []
        self.spoilers: list[tuple[str, str]] = []
        self.divs: list[str] = []                # per open <div>: "" | "body" | "head"
        self.pending_head: list[str] | None = None
        self.last_head = ""
        self.sp_title: list[str] | None = None   # <h3 class="sp-title"> inside a body
        self.pre = 0
        self.skip = 0

    def _w(self, s: str):
        if self.pending_head is not None:
            self.pending_head.append(s)
        elif self.sp_title is not None:
            self.sp_title.append(s)
        else:
            self.bufs[-1].append(s)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class") or ""
        if tag in ("script", "style"):
            self.skip += 1
            return
        if self.skip:
            return
        if tag == "div":
            if re.search(r"\bsp-head\b", cls):
                self.divs.append("head")
                self.pending_head = []
                return
            if re.search(r"\bsp-body\b", cls):
                self.divs.append("body")
                self.heads.append(_norm_space(a.get("title") or "") or self.last_head)
                self.last_head = ""
                self.bufs.append([])
                return
            self.divs.append("")
            return
        if tag == "h3" and re.search(r"\bsp-title\b", cls) and len(self.bufs) > 1:
            self.sp_title = []
            return
        if tag == "br":
            self._w("\n")
        elif tag == "hr":
            self._w("\n")
        elif tag == "pre":
            self.pre += 1
            self._w("\n")
        elif tag == "a" and a.get("href"):
            self._w(f'<a href="{_html.escape(a["href"])}"></a>')
        elif tag in ("img", "var") and (a.get("src") or a.get("title")):
            # rutracker lazy images: <var class="postImg" title="URL">
            self._w(f'<img src="{_html.escape(a.get("src") or a.get("title"))}">')
        elif tag == "li":
            self._w("\n")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag == "div" and self.divs:
            self.handle_endtag("div")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag == "div":
            kind = self.divs.pop() if self.divs else ""
            if kind == "head":
                self.last_head = _norm_space(_html.unescape(re.sub(r"<[^>]+>", "", "".join(self.pending_head or []))))
                self.pending_head = None
                return
            if kind == "body":
                body = "".join(self.bufs.pop())
                head = self.heads.pop() if self.heads else ""
                self.spoilers.append((head, body))
                self._w("\n")
                return
            self._w("\n")
            return
        if tag == "h3" and self.sp_title is not None:
            t = _norm_space(_html.unescape(re.sub(r"<[^>]+>", "", "".join(self.sp_title))))
            self.sp_title = None
            if self.heads and not self.heads[-1]:
                self.heads[-1] = t
            return
        if tag == "pre":
            self.pre = max(0, self.pre - 1)
        if tag in _BLOCK_END:
            self._w("\n")

    def handle_data(self, data):
        if self.skip:
            return
        if not self.pre:
            data = re.sub(r"[ \t\r\n]+", " ", data)
        self._w(_html.escape(data, quote=False))

    def result(self) -> tuple[str, list[tuple[str, str]]]:
        while len(self.bufs) > 1:  # unclosed spoiler bodies
            self.spoilers.append((self.heads.pop() if self.heads else "", "".join(self.bufs.pop())))
        return "".join(self.bufs[0]), self.spoilers


def rutor_shape_html(title: str, post_html: str) -> str:
    """A document films.parse.rutor.parse_release_page understands: <h1>name</h1> + details with hidewrap spoilers."""
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


# --------------------------------------------------------------------------------------------
# topic page
# --------------------------------------------------------------------------------------------

def _div_block(page: str, start: int) -> str:
    """Inner HTML of the <div> that opens at `start` (balanced <div> counting)."""
    i = page.find(">", start) + 1
    depth, pos = 1, i
    for m in re.finditer(r"<(/?)div\b[^>]*?(/?)>", page[i:], re.I):
        if m.group(2):
            continue
        depth += -1 if m.group(1) else 1
        if depth == 0:
            return page[i: i + m.start()]
        pos = i + m.end()
    return page[i:]


MONTHS = {"янв": 1, "фев": 2, "мар": 3, "апр": 4, "май": 5, "мая": 5, "июн": 6, "июл": 7, "авг": 8, "сен": 9,
          "окт": 10, "ноя": 11, "дек": 12}


def parse_reged(s: str | None) -> str | None:
    """«24-Окт-25 12:34» -> '2025-10-24T12:34:00'; unix seconds -> ISO date-time (UTC)."""
    if not s:
        return None
    s = str(s).strip()
    if re.fullmatch(r"\d{9,11}", s):
        return datetime.fromtimestamp(int(s), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    m = re.search(r"(\d{1,2})-([А-Яа-яЁё]{3})-(\d{2,4})(?:\s+(\d{1,2}):(\d{2}))?", s)
    if not m or m.group(2).lower() not in MONTHS:
        return None
    y = int(m.group(3))
    y = y + 2000 if y < 100 else y
    hh, mm = int(m.group(4) or 0), int(m.group(5) or 0)
    return f"{y:04d}-{MONTHS[m.group(2).lower()]:02d}-{int(m.group(1)):02d}T{hh:02d}:{mm:02d}:00"


def split_topic(page: str) -> dict:
    """Title, forum, size, seeders, btih, registration date and first-post HTML of a viewtopic.php page
    (or of the compact document made by ``compact_topic_html``)."""
    page = page or ""
    res: dict[str, Any] = {"topic_id": None, "title": None, "forum": None, "forum_id": None, "size_bytes": None,
                           "seeders": None, "leechers": None, "btih": None, "reged": None, "post_html": ""}
    m = re.search(r'<script type="application/json" id="rt-meta">(.*?)</script>', page, re.S)
    if m:  # compact document stored in release_raw
        meta = json.loads(m.group(1))
        res.update({k: meta.get(k) for k in res if k != "post_html" and meta.get(k) is not None})
    m = re.search(r'<a[^>]*id="topic-title"[^>]*>(.*?)</a>', page, re.S) or \
        re.search(r'<h1[^>]*class="maintitle"[^>]*>(.*?)</h1>', page, re.S)
    if m and not res["title"]:
        res["title"] = _norm_space(_html.unescape(re.sub(r"<[^>]+>", "", m.group(1))))
    pb = re.search(r'<div[^>]*class="post_body"', page)
    first_post = pb.start() if pb else len(page)
    if not res["forum"]:
        forums = re.findall(r'href="viewforum\.php\?f=(\d+)"[^>]*>([^<]+)</a>', page[:first_post])
        if forums:
            res["forum_id"], res["forum"] = forums[-1][0], _norm_space(_html.unescape(forums[-1][1]))
    if not res["topic_id"]:
        m = re.search(r'viewtopic\.php\?t=(\d+)', page[:first_post])
        res["topic_id"] = m.group(1) if m else None
    if res["size_bytes"] is None:
        m = re.search(r'id="tor-size-humn"[^>]*title="(\d+)"', page) or re.search(r'title="(\d+)"[^>]*id="tor-size-humn"', page)
        res["size_bytes"] = int(m.group(1)) if m else None
    if res["seeders"] is None:
        m = re.search(r'class="seed"[^>]*>[^<]*<b>(\d+)</b>', page)
        res["seeders"] = int(m.group(1)) if m else None
    if res["leechers"] is None:
        m = re.search(r'class="leech"[^>]*>[^<]*<b>(\d+)</b>', page)
        res["leechers"] = int(m.group(1)) if m else None
    if not res["btih"]:
        m = re.search(r"magnet:\?xt=urn:btih:([0-9A-Fa-f]{40})", page) or re.search(r'id="tor-hash"[^>]*>\s*([0-9A-Fa-f]{40})', page)
        res["btih"] = m.group(1).lower() if m else None
    if not res["reged"]:
        m = re.search(r"Зарегистрирован[\s\S]{0,300}?(\d{1,2}-[А-Яа-яЁё]{3}-\d{2}(?:\s+\d{1,2}:\d{2})?)", page)
        res["reged"] = m.group(1) if m else None
    if pb:
        res["post_html"] = _div_block(page, pb.start())
    return res


def compact_topic_html(topic: dict) -> str:
    """What release_raw keeps for a rutracker topic: the meta collected by the script + the first post's HTML
    (description + MediaInfo). Re-parsable by ``parse_topic`` without opening the topic again."""
    meta = {k: topic.get(k) for k in ("topic_id", "title", "forum", "forum_id", "size_bytes", "seeders", "leechers",
                                      "btih", "reged", "fetched_at")}
    return (f"<!-- rutracker {BASE}/viewtopic.php?t={topic.get('topic_id')} — first post, collected by "
            f"films/browser/rutracker_collect.js -->\n"
            f'<script type="application/json" id="rt-meta">{json.dumps(meta, ensure_ascii=False).replace("</", "<\\/")}</script>\n'
            f'<h1 class="maintitle">{_html.escape(topic.get("title") or "", quote=False)}</h1>\n'
            f'<div class="post_body">{topic.get("post_html") or ""}</div>\n')


def parse_topic(page: str) -> dict:
    """Parse a rutracker topic: the rutor page parser on the adapted document + rutracker meta.
    ``name`` stays the original rutracker title; ``name_parsed`` is parsed from its rutor-style form."""
    t = split_topic(page)
    rname, w = rutor_style_name(t["title"] or "")
    d = parse_release_page(rutor_shape_html(t["title"] or "", t["post_html"]))
    d["warnings"] = w + d.get("warnings", [])
    if not t["post_html"]:
        d["warnings"].append("no first post (post_body) on rutracker page — name only")
    d["name"] = t["title"]
    d["name_rutor_style"] = rname
    d["category"] = category_for(t["forum"], t["title"])
    d["size_bytes"] = t["size_bytes"] if t["size_bytes"] is not None else d.get("size_bytes")
    d["seeders"] = t["seeders"] if t["seeders"] is not None else d.get("seeders")
    d["leechers"] = t["leechers"] if t["leechers"] is not None else d.get("leechers")
    d["btih"] = t["btih"] or d.get("btih")
    d["added_at"] = parse_reged(t["reged"]) or d.get("added_at")
    d["rutracker"] = {"topic_id": t["topic_id"], "forum": t["forum"], "forum_id": t["forum_id"]}
    _name_ru_audio(d, t["title"] or "", _html.unescape(re.sub(r"<[^>]+>", "\n", t["post_html"] or "")))
    return d


_NAME_RUS_RE = re.compile(r"\[([^\]]*?)\bRUS\b(?!\s*Sub)(?:\s*\(([^)]*)\))?([^\]]*)\]", re.I)
_NAME_CODES = (("dub", r"\bdub"), ("mvo", r"\bmvo"), ("dvo", r"\bdvo"), ("svo", r"\b(?:svo|vo)\b"), ("avo", r"\bavo"))


def name_tail_voice(d: dict, title: str) -> None:
    """rustorka names the Russian voice-over after the last bracket — «… [HEVC] MVO [1win Studio]», «… DUB | Red Head
    Sound», «DVO» — while the MediaInfo track has no or a wrong language (review 2026-10-28: 15 titles hidden «нет
    озвучки»). When the page gave no voiced Russian / Ukrainian track, the best code of the tail is the Russian track."""
    from .rutor import VOICE_RANK
    if d.get("best_voice") in VOICE_RANK:
        return
    info = re.search(r"\[(?:19|20)\d\d\b[^\]]*\]", title or "")      # the «[2024, genres, WEB-DL 1080p]» block
    tail = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", title[info.end():]) if info else ""
    tail = re.sub(r"(?i)movie\s+dubbing", "dub", tail)
    kinds = [k for k, rx in _NAME_CODES if re.search(rx, tail, re.I)]
    if not kinds:
        return
    kind = min(kinds, key=VOICE_RANK.index)
    d.setdefault("audio", []).append({"lang": "ru", "kind": kind, "studio": None, "from_name": True})
    d.setdefault("voice_by_lang", {}).setdefault("ru", []).append(kind)
    d["best_voice"] = kind
    d.setdefault("warnings", []).append(f"Russian voice-over from the title tail «{tail.strip()[:40]}»: {kind}")


def _name_ru_audio(d: dict, title: str, post_text: str) -> None:
    """A Russian track named only in the title — «[RUS(ext), JAP+Sub]» (a separate .mka file, not in the video's
    MediaInfo), «[RUS(int)]», «[RUS(Dub, 9*MVO)]» — when the page gave none (review 2026-10-28: 80 anime releases read as
    «no voice-over»). Kind: the codes in the brackets, else the «Озвучка» lines, else «amateur» (the lowest voiced kind;
    fan voice-overs of anime), marked kind_assumed."""
    m = _NAME_RUS_RE.search(title)
    if not m:
        return
    from .rutor import VOICE_RANK, _kind_in
    ru = [a for a in d.get("audio") or [] if a.get("lang") == "ru"]
    if ru:
        # Russian tracks on the page, kind not said («1)Русский,AAC … [NIKITOS] (SHIZA)»): the title's RUS tag says it is a
        # voice-over — the lowest voiced kind, unless the page named one (an unknown kind counted as «no voice-over»)
        if d.get("best_voice") is None and all(a.get("kind") is None for a in ru):
            for a in ru:
                a["kind"], a["kind_assumed"] = "amateur", True
            d.setdefault("voice_by_lang", {})["ru"] = ["amateur"]
            d["best_voice"] = "amateur"
            d.setdefault("warnings", []).append(f"Russian tracks without a kind, title «{m.group(0)}»: amateur (assumed)")
        return
    codes = m.group(2) or ""
    kinds = [k for k, rx in _NAME_CODES if re.search(rx, codes, re.I)]
    kind = min(kinds, key=VOICE_RANK.index) if kinds else None
    assumed = False
    if kind is None:
        lines = [l for l in post_text.split("\n") if re.search(r"(?i)озвуч|аудио\s*rus|rus\s*sound", l)]
        kind = _kind_in(" ".join(lines))[0]
        kind = kind if kind in VOICE_RANK else None
    if kind is None:
        kind, assumed = "amateur", True
    d.setdefault("audio", []).append({"lang": "ru", "kind": kind, "studio": None, "external": "ext" in codes.lower(),
                                      "from_name": True, **({"kind_assumed": True} if assumed else {})})
    d.setdefault("voice_by_lang", {}).setdefault("ru", []).append(kind)
    if (d.get("best_voice") or "none") in ("none", "original") or (
            d["best_voice"] in VOICE_RANK and VOICE_RANK.index(kind) < VOICE_RANK.index(d["best_voice"])):
        d["best_voice"] = kind
    d.setdefault("warnings", []).append(f"Russian track from the title «{m.group(0)}»: {kind}"
                                        + (" (kind assumed)" if assumed else ""))


def parse_title_only(title: str, forum: str | None = None) -> dict:
    """A search row without its topic page: everything the title says."""
    rname, w = rutor_style_name(title)
    d = parse_release_page(f"<h1>{_html.escape(rname, quote=False)}</h1>")
    d["warnings"] = w + ["rutracker search row only (topic page not collected)"] + d.get("warnings", [])
    d["name"] = title
    d["name_rutor_style"] = rname
    d["category"] = category_for(forum, title)
    _name_ru_audio(d, title, "")
    return d
