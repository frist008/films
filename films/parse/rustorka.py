"""Parser for rustorka.com (R208, source priority 3 after rutor and rutracker).

Pure functions only: no network, no DB (films/sources/rustorka.py reads the site over plain HTTP).

What a guest can read (checked 2026-10-26): rustorka hides every film / series / cartoon forum and topic from guests —
viewforum.php?f=N and viewtopic.php?t=N answer «Форума / Темы … не существует» (HTTP 404), index.php lists only games,
software, music, anime, books; tracker.php (search) asks for a captcha. The one open door is the site's RSS feed
``rss.php?t=1`` («This feed generated for Anonymous»): the 100 newest topics of ALL forums (≈ one week), each with its
forum name, the first post's id and date and the whole first post (description + MediaInfo spoiler). A forum filter
(``rss.php?f=N``) answers «For registred users only», so history cannot be read without a login.

rustorka titles are rutracker-shaped (``Name / Original (Director) [2026, genres, WEB-DL 2160p, HDR10+ | Dolby Vision]
[HEVC] MVO (WinMedia)``), sometimes with rustorka's own episode form («Crystal Lake / 1-8 серии из 8 (…)», «Lupin / 4
Сезон: 1-8 серии из 8») — ``normalize_title`` puts those into the rutracker form, then films.parse.rutracker does the
rest (title → rutor-style name, post → rutor-shaped page; the rutor page parser does the real work). The post markup
uses ``spoiler-wrap`` / ``spoiler-head`` / ``spoiler-body`` (rutracker: ``sp-*``) — renamed before flattening.

``parse_item(raw)`` accepts the compact document stored in ``release_raw`` (``compact_item_html``) and returns the rutor
parser's dict plus ``rustorka`` details; ``classify`` decides the DB category of a feed item (None = not collected).
"""
from __future__ import annotations

import html as _html
import json
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from .rutor import parse_release_page
from .rutracker import _NOT_VIDEO, name_tail_voice, rutor_shape_html, rutor_style_name

BASE = "https://rustorka.com/forum"
FEED = "rss.php?t=1&c=100"          # the guest feed: newest topics of all forums (the site caps it at 100)
# releases.added_at = the post's «Добавлено: 2026-10-25 20:48 (GMT 3)» (Moscow time, as on the site); pub = UTC


def post_url(post_id) -> str:
    return f"{BASE}/viewtopic.php?p={post_id}#{post_id}"


def topic_url(topic_id) -> str:
    return f"{BASE}/viewtopic.php?t={topic_id}"


# the meta a compact document keeps (feed items: post_id … category; topic pages also topic_id, path, size, seeds, btih)
META_KEYS = ("post_id", "topic_id", "forum", "forum_id", "forum_path", "title", "pub", "added", "category", "size_bytes",
             "seeders", "leechers", "btih")


def source_id(post_id) -> str:
    """releases.source_id of a rustorka release: the first post's id with a «p» (the feed has no topic id; a topic id
    read later after a login would be plain digits, so the two never collide)."""
    return f"p{int(post_id)}"


# --------------------------------------------------------------------------------------------
# forums that guests see on index.php (anime is open, films / series / cartoons are not)
# --------------------------------------------------------------------------------------------

# index.php category «Аниме» (c=33), as a guest sees it on 2026-10-26; `forums` re-reads it and reports changes.
ANIME_FORUMS = {"Аниме", "Новинки", "Манга", "AMV", "Основной раздел", "HDTVRip / HDTV 720 / 1080", "DVD / DVDRip",
                "BDRip / HDRip / AVC", "TVRip / VHSRip / Другие", "PSP", "OST", "mp3", "lossless", "Флудилка",
                "Обои на рабочий стол/картинки/арты", "Новости из мира аниме"}
DORAMA_FORUMS = {"Дорамы"}          # Asian live-action series: foreign series, like the Korean series of the film forums
_ANIME_TITLE = re.compile(r"\[(?:TV|ONA|OVA|OAD|Movie|ТВ|Спешл|Special|SP)[-\s\]]|\[\s*(?:19|20)\d{2}\s*г\.|\(\s*TV\s*\d", re.I)
_SKIP_FORUM = re.compile(r"pron|xxx|эроти|порно|документал|научно|познавател|концерт|клип|спорт|футбол|хоккей|юмор|"
                         r"stand\s*-?\s*up|театр|мюзикл|обучающ|видеокурс|урок|трейлер", re.I)
_RU = {"россия", "ссср", "russia", "рф", "российская федерация"}
_ANIM_GENRE = re.compile(r"мультфильм|мультсериал|анимац|animation|кукольн|мульт\b", re.I)


def parse_index(page: str) -> list[dict]:
    """index.php → [{'f', 'name', 'c', 'category'}] in page order (the category heading is ``index.php?c=N``)."""
    out, c, cname, seen = [], None, None, set()
    for m in re.finditer(r'<a[^>]*href="(?:\./)?index\.php\?c=(\d+)"[^>]*>([^<]*)</a>|'
                         r'<a[^>]*href="(?:\./)?viewforum\.php\?f=(\d+)"[^>]*>([^<]+)</a>', page or ""):
        if m.group(1):
            c, cname = int(m.group(1)), _html.unescape(m.group(2)).strip()
            continue
        f, name = int(m.group(3)), _html.unescape(m.group(4)).strip()
        if not name or f in seen:
            continue
        seen.add(f)
        out.append({"f": f, "name": name, "c": c, "category": cname})
    return out


def video_forums_visible(forums: list[dict]) -> list[dict]:
    """Forums on the guest index that look like films / series / cartoons (none on 2026-10-26: they are hidden)."""
    out = []
    for f in forums:
        n, cat = f["name"], f.get("category") or ""
        if re.search(r"(?i)аниме|музык|книг|журнал|программ|игр|общени|обои|wallpaper|мобильн|техническ|правила|новост|"
                     r"магазин|обучающ|аудиокниг|иностранн", cat):
            continue                       # «Фильмы» in «Общение» is a chat forum, «Зарубежный рок» is music …
        if re.search(r"(?i)шаблон|саундтрек|игров|концерт|видеокурс|обучающ", n):
            continue
        if re.search(r"(?i)кино\b|фильм|сериал|blu-?ray|remux|web-?dl|hdrip|bdrip|uhd|2160|telesync|camrip", n):
            out.append(f)
    return out


# --------------------------------------------------------------------------------------------
# feed
# --------------------------------------------------------------------------------------------

def _tag(item: str, tag: str) -> str | None:
    m = re.search(rf"<{tag}>(.*?)</{tag}>", item, re.S)
    return m.group(1).strip() if m else None


def parse_feed(xml: str) -> dict:
    """rss.php → {'guest': bool, 'built': ISO, 'items': [{post_id, forum, title, pub (UTC ISO), added (MSK ISO),
    post_html}]}; a «For registred users only» answer gives no items. The uploader's name is not kept."""
    xml = xml or ""
    items = []
    for raw in re.findall(r"<item>(.*?)</item>", xml, re.S):
        full = _html.unescape(_html.unescape(_tag(raw, "title") or ""))   # escaped twice: «Clive Barker&amp;#039;s»
        link = _tag(raw, "link") or ""
        m = re.search(r"[?&]p=(\d+)", link)
        if not m:
            continue
        forum, _, title = full.partition(" :: ")
        if not title:
            forum, title = (_html.unescape(_tag(raw, "dc:subject") or "")), full
        is_reply = title.startswith("RE: ")
        title = title[4:] if is_reply else title
        desc = _html.unescape(_tag(raw, "description") or "")
        pub = None
        try:
            pub = parsedate_to_datetime(_tag(raw, "pubDate") or "").astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        except (TypeError, ValueError):
            pass
        added = None
        ma = re.search(r"Добавлено:\s*(\d{4}-\d{2}-\d{2} \d{1,2}:\d{2})", desc)
        if ma:
            added = datetime.strptime(ma.group(1), "%Y-%m-%d %H:%M").strftime("%Y-%m-%dT%H:%M:00")
        body = desc
        mb = re.search(r'<span class="postbody">', desc)
        if mb:
            body = desc[mb.end():]
            body = re.sub(r"</span>\s*(?:<br\s*/?>\s*)*$", "", body.strip())
        items.append({"post_id": int(m.group(1)), "forum": forum.strip(), "title": title.strip(),
                      "reply": is_reply, "pub": pub, "added": added, "post_html": body})
    built = None
    mb = re.search(r"<lastBuildDate>(.*?)</lastBuildDate>", xml)
    if mb:
        try:
            built = parsedate_to_datetime(mb.group(1)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        except (TypeError, ValueError):
            pass
    return {"guest": "generated for Anonymous" in xml, "registered_only": "For registred users only" in xml[:200],
            "built": built, "items": items}


# --------------------------------------------------------------------------------------------
# title / post adaptation
# --------------------------------------------------------------------------------------------

_EP_RU = r"(?:серии|серия|серий|эпизоды|эпизод|выпуски|выпуск)"


def normalize_title(title: str) -> str:
    """rustorka's own season / episode forms → rutracker's «Сезон: N / Серии: a-b из n»:
    «Crystal Lake / 1-8 серии из 8 (…)» → «Crystal Lake / Серии: 1-8 из 8 (…)», «Lupin / 4 Сезон: 1-8 серии из 8» →
    «Lupin / Сезон: 4 / Серии: 1-8 из 8», «Сезон 2 / 5 серия из 10», «1 сезон / Серии: 1-3»."""
    s = re.sub(r"\s+", " ", _html.unescape(title or "")).strip()
    s = re.sub(r"(?i)(?<![\w])(\d{1,3})\s*[-–—]?\s*(?:й\s*)?сезон(?:ы)?\s*:?(?=\s|/|$)", r"Сезон: \1 /", s)
    s = re.sub(r"(?i)(?<![\w])Сезон\s+(\d{1,3}(?:\s*[-–—]\s*\d{1,3})?)(?=\s|/|$)", r"Сезон: \1", s)
    s = re.sub(rf"(?i)(?<![\w:])(\d{{1,4}}(?:\s*[-–—]\s*\d{{1,4}})?)\s*{_EP_RU}(?:\s*(?:из|of)\s*(\d{{1,4}}|\?+|xx))?",
               lambda m: "Серии: " + m.group(1) + (f" из {m.group(2)}" if m.group(2) else ""), s)
    s = re.sub(r"/\s*/", "/", s)
    s = re.sub(r"\s*/\s*(?=\(|\[|$)", " ", s)
    # cinema sources the rutor parser does not know in this spelling: «TSRip», «Super-TS», «TCRip», «CamRip-AVC»
    s = re.sub(r"(?i)(?<![\w])(?:Super\s*-\s*TS)(?![\w])", "SuperTS", s)
    s = re.sub(r"(?i)(?<![\w])(TS|TC)\s*-?\s*Rip(?:\s*-\s*AVC)?(?![\w])", lambda m: m.group(1).upper(), s)
    return re.sub(r"\s+", " ", s).strip()


def _spoilers_as_rutracker(post_html: str) -> str:
    """rustorka spoilers (spoiler-wrap / spoiler-head / spoiler-body) → rutracker's sp-wrap / sp-head / sp-body, which
    films.parse.rutracker._PostFlattener turns into rutor spoilers; MediaInfo «code» blocks keep their line breaks."""
    s = post_html or ""
    s = re.sub(r'class="spoiler-wrap', 'class="sp-wrap', s)
    s = re.sub(r'class="spoiler-head', 'class="sp-head', s)
    s = re.sub(r'class="spoiler-body', 'class="sp-body', s)
    s = re.sub(r"(?i)</br\s*>", "<br />", s)      # rustorka writes «</br>» for a line break
    # «6 ch (L R C LFE Ls Rs)» in an audio line: the rutor parser would take the layout for a voice studio
    s = re.sub(r"\(\s*(?:(?:L|R|C|LFE|Ls|Rs|Lb|Rb|Lw|Rw|Cs|Lss|Rss|Lrs|Rrs|Tfl|Tfr|Tbl|Tbr)\s*)+\)", "", s)
    s = re.sub(r"<script\b.*?</script>", "", s, flags=re.S | re.I)
    s = re.sub(r'<div class="code_head">.*?</div><!--/code_head-->', "", s, flags=re.S)
    return s.replace("\xa0", " ").replace("&nbsp;", " ")


def _plain(post_html: str) -> str:
    t = re.sub(r"<(?:/?br|/div|/p|div)[^>]*>", "\n", post_html or "", flags=re.I)
    t = _html.unescape(re.sub(r"<[^>]+>", " ", t)).replace("\xa0", " ")
    return re.sub(r"[ \t]+", " ", t)


def _field(text: str, label: str) -> str | None:
    m = re.search(rf"(?im)^\s*{label}\s*:\s*(.+)$", text)
    return m.group(1).strip() if m else None


def countries(post_html: str) -> list[str]:
    """«Страна: Франция / Netflix, Disney+, …» → ['Франция'] (studios after « / » dropped)."""
    v = _field(_plain(post_html), "Страна") or ""
    v = v.split(" / ")[0].split("|")[0]
    return [c.strip(" .") for c in re.split(r"[,;]", v) if c.strip(" .")]


def classify(item: dict) -> tuple[str | None, str]:
    """(releases.category, why) of a feed item; category None = not collected (why says what it is).
    Films / series / cartoons are the forums hidden from guests: an item there is video when its post has video
    details («Продолжительность», «Видео», MediaInfo). Anime (open forums) is not collected (CLAUDE.md, R44); neither are
    Russian films and series (rutor's «Наши фильмы / сериалы» are not collected either) — cartoons of every country are."""
    forum, title, body = item.get("forum") or "", item.get("title") or "", item.get("post_html") or ""
    if item.get("reply"):
        return None, "reply"
    text = _plain(body)
    is_video = bool(re.search(r"(?im)^\s*(?:Продолжительность|Видео|Видео кодек|Качество)\s*:", text)) or \
        bool(re.search(r"(?m)^\s*Video\s*$|Frame rate\s*:", text))
    if not is_video or not re.search(r"\[\s*(?:19|20)\d{2}", title):
        return None, "not video"
    path = item.get("forum_path") or ""            # topic page breadcrumb «Зарубежные фильмы / WEB-DL / WEB-DLRip»
    if forum in ANIME_FORUMS and (_ANIME_TITLE.search(title) or re.search(r"(?i)аниме|anime", text[:3000])) or \
            re.search(r"(?i)аниме|anime", path) and not re.search(r"(?i)дорам", path):
        return None, "anime"
    if _SKIP_FORUM.search(forum) or _SKIP_FORUM.search(path) or \
            _NOT_VIDEO.search(forum) and not re.search(r"(?i)фильм|кино|сериал|мульт", forum):
        return None, "forum " + (path or forum)
    if re.search(r"(?i)наш|русск|росси|отечеств|ссср|советск", path) and not re.search(r"(?i)мульт", path):
        return None, "russian forum"
    head = re.split(r"\[\s*(?:19|20)\d{2}", title)[0]
    genres = (_field(text, "Жанр") or "") + " " + (re.search(r"\[\s*(?:19|20)\d{2}[^\]]*\]", title) or [""])[0]
    if re.search(r"(?i)документальн", genres) and not _ANIM_GENRE.search(genres):
        return None, "documentary"
    series = bool(re.search(r"(?i)Сезон|Серии\s*:|\d\s*" + _EP_RU + r"(?![\w])", normalize_title(head)))
    if _ANIM_GENRE.search(genres) or re.search(r"(?i)мульт", forum):
        return "animation", "cartoon"
    cs = [c.casefold() for c in countries(body)]
    russian = (bool(cs) and all(c in _RU for c in cs)) or (not cs and not re.search(r"[A-Za-z]{2}", re.sub(r"\([^)]*\)", "", head)))
    if russian:
        return None, "russian series" if series else "russian film"
    if forum in DORAMA_FORUMS or series or re.search(r"(?i)сериал|дорам", path) and not re.search(r"(?i)фильм", forum):
        return "foreign_series", "series"
    return "foreign_films", "film"


def _mi_file_size(post_html: str) -> int | None:
    """Size of a single-file release from its MediaInfo («File size : 1.43 GiB»); None when several General blocks."""
    text = _plain(post_html)
    sizes = re.findall(r"(?im)^\s*File size\s*:\s*([\d.,]+)\s*([KMGT]i?B)\s*$", text)
    if len(sizes) != 1 or len(re.findall(r"(?im)^\s*General\s*$", text)) > 1:
        return None
    n, unit = float(sizes[0][0].replace(",", ".")), sizes[0][1][0].upper()
    return int(n * 1024 ** "KMGT".index(unit) * 1024)


def compact_item_html(item: dict) -> str:
    """What release_raw keeps for a rustorka topic: the feed meta + the first post's HTML (description + MediaInfo),
    re-parsable by ``parse_item`` without the feed (which forgets the topic after a week)."""
    meta = {k: item.get(k) for k in META_KEYS if item.get(k) is not None}
    where = (f"{topic_url(item['topic_id'])} — first post, from the logged-in topic page (films/browser/rustorka_collect.js)"
             if item.get("topic_id") else
             f"{post_url(item.get('post_id'))} — first post, from the guest RSS feed {BASE}/{FEED} (films/sources/rustorka.py)")
    return (f"<!-- rustorka {where} -->\n"
            f'<script type="application/json" id="rk-meta">{json.dumps(meta, ensure_ascii=False).replace("</", "<\\/")}</script>\n'
            f'<h1 class="maintitle">{_html.escape(item.get("title") or "", quote=False)}</h1>\n'
            f'<div class="post_body">{item.get("post_html") or ""}</div>\n')


def split_item(page: str) -> dict:
    """The meta and the post of a compact document (``compact_item_html``)."""
    res: dict[str, Any] = dict.fromkeys(META_KEYS)
    res["post_html"] = ""
    m = re.search(r'<script type="application/json" id="rk-meta">(.*?)</script>', page or "", re.S)
    if m:
        res.update({k: v for k, v in json.loads(m.group(1)).items() if v is not None})
    m = re.search(r'<div class="post_body">(.*)</div>\s*$', page or "", re.S)
    if m:
        res["post_html"] = m.group(1)
    return res


def parse_item(page: str) -> dict:
    """Parse a stored rustorka topic: the rutor page parser on the adapted document + rustorka meta.
    ``name`` stays the rustorka title; ``name_parsed`` is parsed from its rutor-style form."""
    t = split_item(page)
    norm = normalize_title(t["title"] or "")
    rname, w = rutor_style_name(norm)
    d = parse_release_page(rutor_shape_html(norm, _spoilers_as_rutracker(t["post_html"])))
    d["warnings"] = w + d.get("warnings", [])
    d["name"] = t["title"]
    d["name_rutor_style"] = rname
    d["category"] = t["category"] or classify(t)[0]
    if t.get("size_bytes"):                        # topic page: the magnet's exact size (xl=)
        d["size_bytes"], d["size_from"] = int(t["size_bytes"]), "topic"
    elif d.get("size_bytes") is None and d["category"] in ("foreign_films", "animation") and \
            not re.search(r"\[\d+x|\[S\d", rname):
        d["size_bytes"] = _mi_file_size(t["post_html"])
        if d["size_bytes"]:
            d["size_from"] = "mediainfo"
    for k in ("seeders", "leechers", "btih"):
        if t.get(k) is not None:
            d[k] = t[k]
    d["added_at"] = t["added"] or d.get("added_at")
    d["rustorka"] = {"post_id": t["post_id"], "topic_id": t["topic_id"], "forum": t["forum"],
                     "forum_path": t["forum_path"], "pub": t["pub"]}
    name_tail_voice(d, t["title"] or "")
    return d
