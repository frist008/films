"""Parser for the Ukrainian trackers toloka.to (R206, source priority 6) and mazepa.to (R206, priority 7).

Pure functions only: no network, no DB. mazepa pages are read over plain HTTP by films/sources/mazepa.py; toloka needs
the owner's login, so its pages are collected in his Chrome by films/browser/toloka_collect.js (films/sources/toloka.py).

Both are TorrentPier / phpBB trackers with Ukrainian descriptions. As for rutracker (films/parse/rutracker.py) the rutor
page parser does the real work; this module only adapts what differs:

* the topic title: ``Шуґар / Sugar (Сезон 2, серії 1-5 з 8) (2026) WEB-DL 2160p HDR DV P8 2xUkr/Eng`` → the rutor shape
  ``Шуґар / Sugar [2x1-5 из 8] (2026) WEB-DL 2160p | HDR | DV P8 | UKR`` (``rutor_style_name``); the first name is the
  Ukrainian one (it takes the «Russian name» slot of the rutor name — matching works by IMDb link / original name);
* the post markup: TorrentPier spoilers (``sp-wrap`` / ``sp-body``, as on rutracker) → rutor's ``hidewrap`` spoilers;
* the language: Ukrainian labels and words (``Озвучення: багатоголосе закадрове DniproFilm``, ``Аудіо 1: … - українська``,
  ``Субтитри: українські (форсовані)``) are put into the Russian vocabulary the rutor parser knows (``ua_to_ru``), and a
  voice line without a language («Озвучення: дубльоване Так Треба Продакшн») is Ukrainian on these trackers, not Russian
  (the rutor parser's default) — ``_mark_ukrainian_voices``. Only the text handed to the parser is translated:
  release_raw keeps the original post.

``parse_topic(source, raw)`` accepts a full topic page or the compact document stored in ``release_raw``
(``compact_topic_html``) and returns the rutor parser's dict plus ``tracker`` details.
"""
from __future__ import annotations

import html as _html
import json
import re
from datetime import date, datetime, timedelta
from typing import Any

from .rutor import _detect_lang, _kind_in, parse_release_page
from .rutracker import (_CODEC_RE, _EDITION_TOKEN_RE, _HDR_TOKEN_RE, _RES_RE, _PostFlattener, _div_block, _is_source,
                        _norm_space, _season_blocks, _season_list)

BASE = {"mazepa": "https://mazepa.to", "toloka": "https://toloka.to"}


def topic_url(source: str, topic_id) -> str:
    return f"{BASE['mazepa']}/viewtopic.php?t={topic_id}" if source == "mazepa" else f"{BASE['toloka']}/t{topic_id}"


# --------------------------------------------------------------------------------------------
# forums -> DB category
# --------------------------------------------------------------------------------------------

#: mazepa.to video forums (read from https://mazepa.to/index.php, 2026-10-26) -> releases.category (rutor names).
#: Ukrainian films / series / cartoons are «зарубежные» for rutor too (its cat 1 holds them). Not collected: «Субтитровані
#: фільми» (174, no voice at all), «Аніме» (16, R44: anime only for the owner's franchises), documentaries, sport, music,
#: «Незабаром на трекері» (40, announcements) and «Неоформлене відео» (187, no description).
MAZEPA_FORUMS: dict[int, tuple[str, str]] = {
    175: ("foreign_films", "Новинки фільмів UHD, HD"), 147: ("foreign_films", "Фільми UHD"),
    12: ("foreign_films", "Фільми HD"), 13: ("foreign_films", "Фільми SD"),
    37: ("foreign_films", "Українські фільми HD, UHD"), 7: ("foreign_films", "Українські фільми SD"),
    152: ("foreign_series", "Серіали UHD"), 44: ("foreign_series", "Серіали HD"), 14: ("foreign_series", "Серіали SD"),
    38: ("foreign_series", "Українські серіали HD, UHD"), 8: ("foreign_series", "Українські серіали SD"),
    155: ("animation", "Мультфільми UHD"), 41: ("animation", "Мультфільми HD"), 10: ("animation", "Мультфільми SD"),
    35: ("animation", "Українські мультфільми HD, UHD"), 5: ("animation", "Українські мультфільми SD"),
    43: ("animation", "Мультсеріали UHD, HD"), 11: ("animation", "Мультсеріали SD"),
    36: ("animation", "Українські мультсеріали HD, UHD"), 6: ("animation", "Українські мультсеріали SD"),
}

#: mazepa section forums that only hold the sub-forums above (no topics of their own)
MAZEPA_PARENTS = {148, 149, 151, 153, 154, 176, 177, 178}

# keep in sync with videoForum() in films/browser/toloka_collect.js (Python decides the category; JS only selects)
_NOT_VIDEO_UA = re.compile(r"музик|аудіо|книг|літератур|ігр|гра\b|програм|софт|документ|спорт|футбол|формул|бокс|"
                           r"концерт|кліп|шоу|аніме|anime|обговор|архів|пошук|запит|правил|новин|інструкц|кодуванн|"
                           r"субтитр|мобільн|телефон|трейлер|незабаром|неоформлен|фотограф|шпалер|гумор|караоке|"
                           r"театр|опер|балет|навчальн|флуд|техпідтримк|music|game|soft", re.I)
_VIDEO_UA = re.compile(r"кіно|фільм|серіал|мульт|відео|video|\bhd\b|uhd|4k", re.I)


def category_for_forum(forum: str | int | None) -> str | None:
    """mazepa forum id, or a Ukrainian forum name (toloka) -> releases.category; None for forums outside the base."""
    if isinstance(forum, int) or (isinstance(forum, str) and forum.isdigit()):
        hit = MAZEPA_FORUMS.get(int(forum))
        return hit[0] if hit else None
    f = _norm_space(forum or "")
    if not f or _NOT_VIDEO_UA.search(f) or not _VIDEO_UA.search(f):
        return None
    if re.search(r"мульт", f, re.I):
        return "animation"       # rutor «Мультипликация» (7) also holds cartoon series
    if re.search(r"серіал", f, re.I):
        return "foreign_series"
    return "foreign_films"


# --------------------------------------------------------------------------------------------
# Ukrainian -> the Russian vocabulary of films.parse.rutor
# --------------------------------------------------------------------------------------------

#: description labels at a line start («Озвучення:», «Аудіо 2:») -> the labels of films.parse.rutor._LABELS
_UA_LABELS = [
    (r"оригінальна назва|назва оригіналу", "Оригинальное название"),
    (r"назва(?: фільму| серіалу| мультфільму)?|українська назва", "Название"),
    (r"рік випуску|рік виходу|рік виробництва|рік", "Год выпуска"),
    (r"країна виробництва|країни виробництва|країна|країни", "Страна"),
    (r"кінокомпанія|кінокомпанії|студія|студії", "Студия"),
    (r"режисер-постановник|режисери|режисер|режисёр", "Режиссер"),
    (r"у ролях|в ролях|у головних ролях|актори|ролі озвучували|ролі дублювали", "В ролях"),
    (r"тривалість|час", "Продолжительность"),
    (r"озвучення|озвучування|озвучка|переклад|переклад і озвучення|мова озвучення", "Перевод"),
    (r"аудіо|аудіодоріжка|доріжка|звук", "Аудио"),
    (r"субтитри", "Субтитры"),
    (r"формат субтитрів", "Формат субтитров"),
    (r"відео", "Видео"),
    (r"якість відео|якість", "Качество"),
    (r"відеокодек|кодек", "Кодек"),
    (r"контейнер", "Контейнер"),
    (r"видання|версія", "Издание"),
    (r"джерело|исходник", "Исходник"),
    (r"автор релізу|автор рипу|реліз від|релізер|реліз|рип", "Релиз"),
    (r"примітка|примітки|реклама|додаткова інформація", "Примечание"),
    (r"сезони|сезон", "Сезон"),
    (r"серії|кількість серій|епізоди", "Серии"),
    (r"прем[ʼ'’`]?єра у світі|світова прем[ʼ'’`]?єра|прем[ʼ'’`]?єра", "Премьера"),
    (r"цифровий реліз", "Цифровой релиз"),
]
_UA_LABEL_RES = [(re.compile(r"(?im)^([ \t*•►\-]*)(?:" + p + r")(?=[ \t]*(?:(?:#|№)?[ \t]*\d{1,2})?[ \t]*(?:\([^)\n]*\))?[ \t]*:)"),
                  r) for p, r in _UA_LABELS]

_W = r"(?<![\w])"
_E = r"(?![\w])"
#: words inside values (voice kinds, languages, countries, forced subtitles, ads)
_UA_WORDS = [
    (r"дубльован\w*|дубльоване|дублювання", "дублированный"),
    (r"багатоголос\w*", "многоголосый"),
    (r"двоголос\w*|двохголос\w*", "двухголосый"),
    (r"одноголос\w*", "одноголосый"),
    (r"закадров\w*", "закадровый"),
    (r"аматорськ\w*|любительськ\w*", "любительский"),
    (r"професійн\w*", "профессиональный"),
    (r"авторськ\w*", "авторский"),
    (r"неофіційн\w*", "неофициальный"),
    (r"офіційн\w*", "официальный"),
    (r"ліцензійн\w*", "лицензионный"),
    (r"оригінал\w*", "оригинал"),
    (r"коментар\w*", "комментарий"),
    (r"форсован\w*|примусов\w*", "форсированные"),
    (r"без реклами", "без рекламы"),
    (r"реклами", "рекламы"),
    (r"серія|серії|серій", "серия"),
    (r"загалом", "всего"),
    (r"хв\.?|хвилин\w*", "мин"),
    (r"кбіт", "кбит"), (r"мбіт", "мбит"), (r"біт", "бит"),
    (r"українськ\w*|по-українськи|укр\.", "украинский"),
    (r"російськ\w*", "русский"),
    (r"англійськ\w*", "английский"),
    (r"японськ\w*", "японский"), (r"корейськ\w*", "корейский"), (r"китайськ\w*", "китайский"),
    (r"французьк\w*", "французский"), (r"німецьк\w*", "немецкий"), (r"іспанськ\w*", "испанский"),
    (r"італійськ\w*", "итальянский"), (r"польськ\w*", "польский"), (r"португальськ\w*", "португальский"),
    (r"шведськ\w*", "шведский"), (r"норвезьк\w*", "норвежский"), (r"данськ\w*", "датский"),
    (r"фінськ\w*", "финский"), (r"нідерландськ\w*|голландськ\w*", "нидерландский"), (r"чеськ\w*", "чешский"),
    (r"турецьк\w*", "турецкий"), (r"угорськ\w*", "венгерский"), (r"хінді", "хинди"),
    (r"Україна", "Украина"), (r"Велика Британія|Великобританія", "Великобритания"), (r"Німеччина", "Германия"),
    (r"Франція", "Франция"), (r"Італія", "Италия"), (r"Іспанія", "Испания"), (r"Японія", "Япония"),
    (r"Австралія", "Австралия"), (r"Південна Корея", "Южная Корея"), (r"Індія", "Индия"), (r"Польща", "Польша"),
    (r"Швеція", "Швеция"), (r"Норвегія", "Норвегия"), (r"Данія", "Дания"), (r"Ірландія", "Ирландия"),
    (r"Бельгія", "Бельгия"), (r"Нідерланди", "Нидерланды"), (r"Бразилія", "Бразилия"),
    (r"Нова Зеландія", "Новая Зеландия"), (r"Фінляндія", "Финляндия"), (r"Чехія", "Чехия"), (r"Угорщина", "Венгрия"),
    (r"Туреччина", "Турция"), (r"Ізраїль", "Израиль"), (r"Австрія", "Австрия"), (r"Швейцарія", "Швейцария"),
    (r"Румунія", "Румыния"), (r"Болгарія", "Болгария"), (r"Литва", "Литва"), (r"Латвія", "Латвия"),
    (r"Естонія", "Эстония"), (r"Грузія", "Грузия"), (r"Хорватія", "Хорватия"), (r"Сербія", "Сербия"),
    (r"Ісландія", "Исландия"), (r"Греція", "Греция"), (r"Аргентина", "Аргентина"), (r"Мексика", "Мексика"),
    (r"ПАР|Південна Африка", "ЮАР"), (r"Таїланд", "Таиланд"), (r"Філіппіни", "Филиппины"), (r"Індонезія", "Индонезия"),
]
_UA_WORD_RES = [(re.compile(_W + r"(?:" + p + r")" + _E, re.I), r) for p, r in _UA_WORDS]
_VOICE_LINE_RE = re.compile(r"(?im)^([ \t*•►\-]*Перевод[ \t]*(?:(?:#|№)?[ \t]*\d{1,2})?[ \t]*(?:\([^)\n]*\))?[ \t]*:)([^\n]*)$")


_TRACK_NO_RE = re.compile(r"\(\s*(\d{1,2}(?:\s*(?:,|і|и|&|та)\s*\d{1,2})*)\s*\)")


def _mark_ukrainian_voices(text: str) -> str:
    """«Перевод: многоголосый закадровый (1) 2+2, двухголосый закадровый (2) Новий канал» -> every voice piece without a
    language gets «украинский» (films.parse.rutor reads such a line as Russian; on a Ukrainian tracker it is not)."""
    def line(m):
        parts = re.split(r"([,;](?![^()]*\))|\s\+\s)", m.group(2))
        last_kind = None
        for i in range(0, len(parts), 2):
            p = parts[i]
            tm = _TRACK_NO_RE.search(p)
            if _kind_in(p)[0]:
                last_kind = p[: tm.start()] if tm and _kind_in(p[: tm.start()])[0] else None
            elif tm and last_kind and not p[: tm.start()].strip():
                # «багатоголосе закадрове (1) Цікава Ідея, (2) DniproFilm»: track 2 has the same kind of voice
                p = last_kind + p
            if p.strip() and _kind_in(p)[0] not in (None, "original", "commentary") and not _detect_lang(p):
                p = re.sub(r"^(\s*)", r"\1украинский ", p, count=1)
            # «дубльоване (1,2) Le Doyen»: one voice on tracks 1 and 2 -> the piece once per track (each audio track
            # takes one voice line, films.parse.rutor step 3); «(1) 2+2» -> the track number dropped
            tm = _TRACK_NO_RE.search(p)
            if tm and _kind_in(p)[0]:
                n = len(re.findall(r"\d{1,2}", tm.group(1)))
                one = _norm_space(p[: tm.start()] + " " + p[tm.end():])
                p = " " + ", ".join([one] * n)
            parts[i] = p
        return m.group(1) + "".join(parts)
    return _VOICE_LINE_RE.sub(line, text)


# ---- toloka per-track blocks and Ukrainian MediaInfo (found on the live toloka pages, 2026-10-26) ----

#: «Аудіо # 1:» / «Аудіо # 4: (1 серія)» / «Субтитри # 1:» — a track header whose details follow on their own lines
_TRACK_HEAD_RE = re.compile(r"(?i)^([ \t*•►\-]*(?:аудіо|аудио|звук|доріжка|субтитри|відео)[ \t]*(?:(?:#|№)[ \t]*)?\d{0,2}"
                            r"[ \t]*:)[ \t]*((?:(?:#|№)[ \t]*[\d\-–]+)?[ \t]*(?:\([^)\n]*\))?)"
                            r"((?:[ \t]*(?:<img\b[^>]*>|&amp;|&|\[[^\]\n]{0,120}\])[ \t]*)*)[ \t]*$")
#: toloka marks the studio of a track with its logo: <img src="//toloka.to/img/dubbing/1+1.gif">
_DUB_LOGO_RE = re.compile(r"img/dubbing/([^\"'/>]+?)\.(?:gif|png|jpe?g|webp)", re.I)
#: the detail lines of such a block: «мова: українська», «переклад: професійний дубльований | Постмодерн»,
#: «кодек: H.265 (HEVC)», «розмір кадру: 3840 х 2160», «бітрейт: 40.9 Мбіт/с», «кольоровий простір: HDR10, Dolby Vision»
_TRACK_SUB_RE = re.compile(r"(?i)^[ \t*•►\-]*(мова|переклад|озвучення|озвучування|озвучка|студія|автор|формат|кодек|"
                           r"бітрейт|канали|частота|частота кадрів|тип|субтитри|розмір кадру|роздільна здатність|"
                           r"розширення|бітова глибина|глибина кольору|кольоровий простір|hdr|співвідношення сторін)"
                           r"[ \t]*:[ \t]*(.*)$")


def _merge_track_blocks(text: str) -> str:
    """toloka writes every audio / subtitle track as a block —
    «Аудіо # 1:» / «мова: українська» / «переклад: професійний дубльований | Постмодерн» — which the rutor parser reads as
    an empty audio line plus unknown labels (the voice kind was lost, the studio became «мова»). The block becomes one
    line: «Аудіо # 1: українська, професійний дубльований | Постмодерн» (language first)."""
    lines = text.split("\n")
    out, i = [], 0
    while i < len(lines):
        m = _TRACK_HEAD_RE.match(lines[i])
        if not m:
            out.append(lines[i])
            i += 1
            continue
        j, parts = i + 1, []
        while j < len(lines):
            sm = _TRACK_SUB_RE.match(lines[j])
            if not sm or (parts and sm.group(1).lower() == "мова" and any(p[0] == "мова" for p in parts)):
                break
            parts.append((sm.group(1).lower(), sm.group(2).strip()))
            j += 1
        if not parts:
            out.append(lines[i])
            i += 1
            continue
        parts.sort(key=lambda p: p[0] != "мова")
        # «студія» is not a studio name; «Інформація про озвучення» is the head of a spoiler glued to the line
        vals = [re.sub(r"(?i)(?<![\w])(?:студі[яїі]|студио)(?![\w])|інформація\s+про\s+(?:озвучення|реліз|переклад)\w*",
                       " ", v).strip() for _, v in parts if v]
        from urllib.parse import unquote
        logos = [unquote(x) for x in _DUB_LOGO_RE.findall(m.group(3) or "")]
        if logos and not any("|" in v for v in vals):
            vals.append("| " + " & ".join(logos))      # the studio, after the voice kind
        head = m.group(1)
        if re.match(r"(?i)[ \t*•►\-]*субтитри", head):
            head = re.sub(r"(?i)(субтитри)[^:]*:", r"\1:", head)   # «Субтитри # 1:» -> «Субтитри:» (the rutor label)
        out.append(f"{head} {m.group(2) + ' ' if m.group(2) else ''}{', '.join(v for v in vals if v)}")
        i = j
    return "\n".join(out)


#: Ukrainian MediaInfo (toloka posts it localized): section names and keys -> the English ones films.parse.rutor reads
_UA_MI_SECTIONS = {"загальне": "General", "загальна інформація": "General", "відео": "Video", "аудіо": "Audio",
                   "текст": "Text", "меню": "Menu", "субтитри": "Text"}
_UA_MI_KEYS = {
    "мова": "Language", "формат": "Format", "формат/відомості": "Format/Info", "профіль формату": "Format profile",
    "комерційна назва": "Commercial name", "формат hdr": "HDR format", "ідентифікатор кодека": "Codec ID",
    "бітрейт": "Bit rate", "номінальний бітрейт": "Nominal bit rate", "загальний бітрейт": "Overall bit rate",
    "ширина": "Width", "висота": "Height", "бітова глибина": "Bit depth", "глибина кольору": "Bit depth",
    "частота кадрів": "Frame rate", "канал(и)": "Channel(s)", "канали": "Channel(s)", "назва": "Title",
    "заголовок": "Title", "за замовчуванням": "Default", "примусово": "Forced", "примусові": "Forced",
    "тривалість": "Duration", "повна назва": "Complete name", "повне ім'я": "Complete name",
    "характеристики передачі": "Transfer characteristics", "основні кольори": "Color primaries",
    "назва фільму": "Movie name", "ширина кадру": "Width", "висота кадру": "Height",
    "ідентифікатор кодеку": "Codec ID", "ідентифікатор кодека/відомості": "Codec ID/Info",
    "характеристики трансферу": "Transfer characteristics", "характеристики перенесення": "Transfer characteristics",
    "коефіцієнти матриці": "Matrix coefficients", "основні кольори": "Color primaries",
    "додаткові особливості формату": "Format AdditionalFeatures", "загальна назва": "Complete name",
}
_UA_MI_SECTION_RE = re.compile(r"(?i)^([ \t]*)(загальне|загальна інформація|відео|аудіо|текст|меню|субтитри)"
                               r"([ \t]*(?:#|№)[ \t]*\d{1,2})?[ \t]*$")
_UA_MI_KV_RE = re.compile(r"^([ \t]*)([^:\n]{1,60}?)([ \t]{2,}:[ \t]?|[ \t]+:[ \t])(.*)$")


def _ua_mediainfo(text: str) -> str:
    """«Аудіо #1» / «Мова        : Ukrainian» -> «Audio #1» / «Language : Ukrainian» (MediaInfo pads the key with
    spaces before «:», toloka's own «мова: …» lines have none and are left to _merge_track_blocks)."""
    out = []
    for ln in text.split("\n"):
        m = _UA_MI_SECTION_RE.match(ln)
        if m:
            out.append(f"{m.group(1)}{_UA_MI_SECTIONS[m.group(2).lower()]}{(m.group(3) or '').replace('№', '#')}")
            continue
        m = _UA_MI_KV_RE.match(ln)
        if m and m.group(2).strip().lower() in _UA_MI_KEYS:
            key = _UA_MI_KEYS[m.group(2).strip().lower()]
            val = m.group(4)
            if key in ("Default", "Forced"):
                val = re.sub(r"(?i)^\s*так", "Yes", re.sub(r"(?i)^\s*ні", "No", val))
            # «41.0 Мбіт/сек», «384 Кбіт/с» -> «Mb/s», «kb/s» (films.parse.rutor reads MediaInfo units in Latin)
            val = re.sub(r"(?i)мбіт\s*/\s*с(?:ек)?\.?", "Mb/s", val)
            val = re.sub(r"(?i)кбіт\s*/\s*с(?:ек)?\.?", "kb/s", val)
            out.append(f"{m.group(1)}{key} : {val}")
            continue
        out.append(ln)
    return "\n".join(out)


_AUDIO_LINE_RE = re.compile(r"(?im)^([ \t*•►\-]*Аудио[ \t]*(?:(?:#|№)?[ \t]*\d{1,2})?[ \t]*:)([^\n]*)$")


def _mark_first_audio_ukrainian(text: str) -> str:
    """«Аудіо: AAC 2.0ch 160 Kbps» (no language) as the FIRST audio line of a post on a Ukrainian tracker is the Ukrainian
    track (posts list it first); later unlabelled lines are left unknown."""
    m = _AUDIO_LINE_RE.search(text)
    if not m or not m.group(2).strip() or _detect_lang(m.group(2)):
        return text
    return text[: m.end(2)] + " - украинский" + text[m.end(2):]


def ua_to_ru(text: str) -> str:
    """Ukrainian description text -> the vocabulary films.parse.rutor understands (labels, voice kinds, languages,
    countries); then voice lines without a language are marked Ukrainian."""
    t = _ua_mediainfo(_merge_track_blocks(text or ""))
    for rx, rep in _UA_LABEL_RES:
        t = rx.sub(lambda m, rep=rep: m.group(1) + rep, t)
    for rx, rep in _UA_WORD_RES:
        t = rx.sub(rep, t)
    return _mark_first_audio_ukrainian(_mark_ukrainian_voices(t))


# --------------------------------------------------------------------------------------------
# topic title -> rutor-style name
# --------------------------------------------------------------------------------------------

_YEAR_RE = re.compile(r"\((\s*(?:19|20)\d{2})(?:\s*[-–—]\s*((?:19|20)?\d{2}))?\s*\)")
_BRACKET_YEAR_RE = re.compile(r"\[\s*((?:19|20)\d{2})(?:\s*[-–—]\s*((?:19|20)?\d{2}))?")
_SEASON_WORD = r"(?:сезони|сезон|сезоны|seasons?|s(?=\d))"
_EP_WORD = r"(?:серії|серія|серій|серии|серия|епізоди|епізод|episodes?|eps?\.?)"
_SEASON_IN_RE = re.compile(r"(?i)(?<![\w])" + _SEASON_WORD +r"\s*:?\s*(\d{1,3}(?:\s*[-–—,]\s*\d{1,3})*)")
_EPIS_IN_RE = re.compile(r"(?i)(?<![\w])" + _EP_WORD +r"\s*:?\s*(\d{1,4})(?:\s*[-–—]\s*(\d{1,4}))?(?:\s*(?:з|із|из|of|/)\s*(\d{1,4}|\?+|xx))?")
_UKR_TOKEN_RE = re.compile(r"(?i)^(?:\d+\s*[xх×]\s*)?(?:ukr|укр|ua)$")
_DV_MERGE_RE = re.compile(r"(?i)(?<![\w])(?:dolby\s*vision|dovi|dv)(?:\s*(?:tv))?(?:\s*(?:profile|p)\s*(\d)(?:\.(\d))?)?(?:\s*(fel|mel))?(?![\w])")


def _years_of(m) -> tuple[int, int]:
    y1 = int(m.group(1))
    y2 = m.group(2)
    y2 = (int(y2) if len(y2) == 4 else int(str(y1)[:2] + y2)) if y2 else y1
    return min(y1, y2), max(y1, y2)


def rutor_style_name(title: str, assume_ukr: bool = True) -> tuple[str, list[str]]:
    """mazepa / toloka topic title -> (rutor-style name for films.parse.rutor.parse_release_name, warnings).
    assume_ukr: both trackers carry Ukrainian audio (mazepa forums «Озвучений контент», toloka «українською»), so a title
    without «Ukr» still gets the UKR tag (with a warning); the page's audio lines decide the voice."""
    w: list[str] = []
    s = _norm_space(_html.unescape(title or ""))
    s = re.sub(r"^(?:\[[^\]]*\]\s*)+(?=\S)", lambda m: "" if not re.search(r"(?:19|20)\d{2}", m.group(0)) else m.group(0), s)
    ym = _YEAR_RE.search(s)
    if ym:
        head, tail = s[: ym.start()], s[ym.end():]
        lo, hi = _years_of(ym)
    else:
        bm = _BRACKET_YEAR_RE.search(s)
        if not bm:
            w.append("no (year) in title")
            return s, w
        head, tail = s[: bm.start()], s[bm.end():].lstrip("]").lstrip(" ,")
        lo, hi = _years_of(bm)
    # seasons / episodes: «(Сезон 3, серії 1-3 з 8)», «(Сезони 1-7)», «(Season 1, episodes 1-8 of 8)», «Сезон 2 / …»
    seasons: list[int] = []
    ep = None
    groups = re.findall(r"\(([^()]*)\)", head)
    for g in groups + [head]:
        for m in _SEASON_IN_RE.finditer(g):
            seasons += _season_list(m.group(1))
        if ep is None:
            m = _EPIS_IN_RE.search(g)
            if m:
                total = m.group(3) if (m.group(3) or "").isdigit() else None
                ep = (int(m.group(1)), int(m.group(2)) if m.group(2) else None, total, "")
        if seasons and g is not head:
            break
    seasons = list(dict.fromkeys(seasons))
    head = re.sub(r"\([^()]*(?:" + _SEASON_WORD + r"|" + _EP_WORD + r")[^()]*\)", " ", head, flags=re.I)
    head = _SEASON_IN_RE.sub(" ", head)
    edition = []
    for g in re.findall(r"\(([^()]*)\)", head):
        if _EDITION_TOKEN_RE.search(g) or re.search(r"(?i)режисерськ|розширен|театральн", g):
            edition.append(_norm_space(g))
    head = re.sub(r"\([^()]*\)|\[[^\]]*\]", " ", head)
    titles = [_norm_space(t) for t in re.split(r"\s*/\s*", head) if _norm_space(t)]

    # tail: quality, HDR / DV, codec, edition, audio languages; «| Sub Ukr/Eng» and the rest are not part of a rutor name
    pieces = re.findall(r"\[([^\]]*)\]", tail) + [re.sub(r"\[[^\]]*\]", " ", tail)]   # «… | sub Eng [BDRemux 2160p]»
    t = " ".join(re.sub(r"(?i)(?:^|\s|\|)\s*(?:sub|subs|суб)\b.*$", " ", p) for p in pieces)
    t = _DV_MERGE_RE.sub(lambda m: " DV" + (f"P{m.group(1)}" + (f".{m.group(2)}" if m.group(2) else "") if m.group(1) else "")
                         + (f"-{m.group(3).upper()}" if m.group(3) else "") + " ", t)
    t = re.sub(r"[\[\]|,;]+", " ", t)
    quality, tags, ukr = [], [], False
    toks = t.split()
    i = 0
    while i < len(toks):
        tok = toks[i]
        two = f"{tok} {toks[i + 1]}" if i + 1 < len(toks) else None
        if two and _is_source(two) and not _is_source(tok):
            quality.append(two)       # «UHD BDRemux», «WEB DL»
            i += 2
            continue
        if _is_source(tok) or _RES_RE.fullmatch(tok):
            quality.append(tok)
        elif re.fullmatch(r"(?i)DV(?:P\d(?:\.\d)?)?(?:-(?:FEL|MEL))?", tok):
            m = re.fullmatch(r"(?i)DV(?:P(\d)(?:\.(\d))?)?(?:-(FEL|MEL))?", tok)
            tags.append("DV" + (f" P{m.group(1)}" + (f".{m.group(2)}" if m.group(2) else "") if m.group(1) else "")
                        + (f" {m.group(3).upper()}" if m.group(3) else ""))
        elif _HDR_TOKEN_RE.match(tok) or _CODEC_RE.match(tok) or re.fullmatch(r"(?i)4k|uhd|3d|hybrid|imax|open\s*matte", tok):
            tags.append(tok)
        elif "/" in tok or _UKR_TOKEN_RE.match(tok):
            langs = [x for x in re.split(r"[/+]", tok) if x]
            if any(_UKR_TOKEN_RE.match(x) for x in langs):
                ukr = True
            # other languages of the audio list («Eng», «Jap») — not part of a rutor name
        i += 1
    tags += edition
    if not ukr and assume_ukr:
        ukr = True
        w.append("no «Ukr» in title: Ukrainian audio assumed (Ukrainian tracker)")
    name = " / ".join(titles) or s
    blocks = _season_blocks(seasons, ep)
    if blocks:
        name += " " + " ".join(f"[{b}]" for b in blocks)
    name += f" ({lo})" if lo == hi else f" ({lo}-{hi})"
    if quality:
        name += " " + " ".join(quality)
    for tag in dict.fromkeys(tags):
        name += f" | {tag}"
    if ukr:
        name += " | UKR"
    return _norm_space(name), w


# --------------------------------------------------------------------------------------------
# dates, sizes
# --------------------------------------------------------------------------------------------

UA_MONTHS = {"січ": 1, "лют": 2, "бер": 3, "кві": 4, "квi": 4, "тра": 5, "чер": 6, "лип": 7, "сер": 8, "вер": 9,
             "жов": 10, "лис": 11, "гру": 12}


def parse_ua_date(s: str | None, today: date | None = None) -> str | None:
    """«25 Жов 2025, 21:45», «25-Жов-25 21:45», «Вчора 07:14», «Сьогодні 01:20», «2024-10-25 12:34», unix seconds
    -> 'YYYY-MM-DDTHH:MM:00' (the tracker's local time, as rutracker dates are kept)."""
    if not s:
        return None
    s = _norm_space(_html.unescape(str(s)))
    if re.fullmatch(r"\d{9,11}", s):
        return datetime.fromtimestamp(int(s)).strftime("%Y-%m-%dT%H:%M:%S")
    tm = re.search(r"(\d{1,2}):(\d{2})", s)
    hh, mm = (int(tm.group(1)), int(tm.group(2))) if tm else (0, 0)
    today = today or date.today()
    if re.search(r"(?i)сьогодні|сегодня|today", s):
        d = today
    elif re.search(r"(?i)вчора|вчера|yesterday", s):
        d = today - timedelta(days=1)
    else:
        m = re.search(r"((?:19|20)\d{2})-(\d{1,2})-(\d{1,2})", s)
        if m:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        else:
            m = re.search(r"(\d{1,2})[\s\-.]+([А-Яа-яІіЇїЄєҐґ]{3})[А-Яа-яІіЇїЄєҐґ]*\.?[\s\-.,]+(\d{2,4})", s)
            if not m or m.group(2).lower() not in UA_MONTHS:
                m2 = re.search(r"(\d{1,2})\.(\d{1,2})\.((?:19|20)?\d{2})", s)
                if not m2:
                    return None
                y = int(m2.group(3))
                d = date(y + 2000 if y < 100 else y, int(m2.group(2)), int(m2.group(1)))
            else:
                y = int(m.group(3))
                d = date(y + 2000 if y < 100 else y, UA_MONTHS[m.group(2).lower()], int(m.group(1)))
    return f"{d.isoformat()}T{hh:02d}:{mm:02d}:00"


_SIZE_UNITS = {"b": 0, "б": 0, "k": 1, "к": 1, "m": 2, "м": 2, "g": 3, "г": 3, "t": 4, "т": 4}


def parse_size(s: str | None) -> int | None:
    """«18.73&nbsp;GB», «823 MB», «1,2 ГБ» -> bytes (binary units, as TorrentPier prints them)."""
    if not s:
        return None
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*([KMGTBКМГТБ])i?[BБ]?", _html.unescape(str(s)).replace("\xa0", " "), re.I)
    if not m:
        return None
    return round(float(m.group(1).replace(",", ".")) * 1024 ** _SIZE_UNITS[m.group(2).lower()])


# --------------------------------------------------------------------------------------------
# listing (mazepa viewforum)
# --------------------------------------------------------------------------------------------

def parse_mazepa_listing(page: str) -> dict:
    """A mazepa forum page -> {'rows': [{'t', 'title', 'href'}], 'pages': N, 'page': n}. Guests see topic id + title
    only; href is the slugged topic address (``topic-…-t=104085.html`` — viewtopic.php?t= only redirects to it)."""
    page = page or ""
    rows, seen = [], set()
    for m in re.finditer(r'<a\b([^>]*\bid="tt-(\d+)"[^>]*)>(.*?)</a>', page, re.S):
        t = m.group(2)
        if t in seen:
            continue
        seen.add(t)
        h = re.search(r'href="([^"]+)"', m.group(1))
        rows.append({"t": t, "title": _norm_space(_html.unescape(re.sub(r"<[^>]+>", "", m.group(3)))),
                     "href": _html.unescape(h.group(1)).lstrip("./") if h else None})
    m = re.search(r"Сторінка\s*<b>(\d+)</b>\s*з\s*<b>(\d+)</b>", page)
    return {"rows": rows, "page": int(m.group(1)) if m else 1, "pages": int(m.group(2)) if m else 1}


def parse_mazepa_index(page: str) -> list[dict]:
    """index.php -> [{'f': id, 'name': …, 'slug': 'forum-filmy-hd-f=12', 'category': …}] for every forum link
    (category None = not collected). Page n of a forum is ``{slug}&start={50 * n}.html``."""
    out, seen = [], set()
    for m in re.finditer(r'<a\b[^>]*href="(?:\./)?(forum-[^"]*?-f=(\d+))\.html"[^>]*>(.*?)</a>', page or "", re.S):
        f = int(m.group(2))
        if f in seen:
            continue
        seen.add(f)
        name = _norm_space(_html.unescape(re.sub(r"<[^>]+>", "", m.group(3)))).lstrip("✦ ").strip()
        out.append({"f": f, "name": name, "slug": m.group(1), "category": category_for_forum(f)})
    return out


# --------------------------------------------------------------------------------------------
# topic pages
# --------------------------------------------------------------------------------------------

def _tag_block(page: str, start: int, tag: str) -> str:
    """Inner HTML of the element <tag> that opens at `start` (balanced counting of that tag)."""
    if tag == "div":
        return _div_block(page, start)
    i = page.find(">", start) + 1
    depth = 1
    for m in re.finditer(r"<(/?)" + tag + r"\b[^>]*?(/?)>", page[i:], re.I):
        if m.group(2):
            continue
        depth += -1 if m.group(1) else 1
        if depth == 0:
            return page[i: i + m.start()]
    return page[i:]


def _text(fragment: str) -> str:
    return _norm_space(_html.unescape(re.sub(r"<[^>]+>", "", fragment or "")))


def _empty_topic() -> dict[str, Any]:
    return {"topic_id": None, "title": None, "forum": None, "forum_id": None, "size_bytes": None, "seeders": None,
            "leechers": None, "btih": None, "reged": None, "post_html": ""}


def _from_compact(page: str, res: dict) -> bool:
    m = re.search(r'<script type="application/json" id="ua-meta">(.*?)</script>', page, re.S)
    if not m:
        return False
    meta = json.loads(m.group(1))
    res.update({k: meta.get(k) for k in res if k != "post_html" and meta.get(k) is not None})
    pb = re.search(r'<div class="post_body">', page)
    if pb:
        res["post_html"] = _div_block(page, pb.start())
    return True


def _common_meta(page: str, res: dict) -> None:
    """Seeds / leechers / btih — the same markup on both TorrentPier-style trackers (with fallbacks)."""
    if res["seeders"] is None:
        m = re.search(r'class="seed[^"]*"[^>]*>[^<]*<b>(\d+)</b>', page) or \
            re.search(r"(?:Сідів|Сіди|Сідери|Роздають)\s*:?\s*(?:&nbsp;|\s|<[^>]+>)*(\d+)", page)
        res["seeders"] = int(m.group(1)) if m else None
    if res["leechers"] is None:
        m = re.search(r'class="leech[^"]*"[^>]*>[^<]*<b>(\d+)</b>', page) or \
            re.search(r"(?:Лічерів|Лічери|Качають)\s*:?\s*(?:&nbsp;|\s|<[^>]+>)*(\d+)", page)
        res["leechers"] = int(m.group(1)) if m else None
    if not res["btih"]:
        m = re.search(r"magnet:\?xt=urn:btih:([0-9A-Fa-f]{40})", page) or \
            re.search(r"(?i)(?:info[\s_-]*hash|хеш)[^0-9A-Fa-f]{0,80}([0-9A-Fa-f]{40})(?![0-9A-Fa-f])", page)
        res["btih"] = m.group(1).lower() if m else None


def split_mazepa_topic(page: str) -> dict:
    """Title, forum, size, seeders, btih, registration date and first-post HTML of a mazepa topic page (or of the compact
    document made by ``compact_topic_html``)."""
    page = page or ""
    res = _empty_topic()
    if _from_compact(page, res):
        return res
    m = re.search(r'<h1[^>]*class="ipsType_pagetitle"[^>]*>(.*?)</h1>', page, re.S)
    if m:
        res["title"] = _text(m.group(1))
        tm = re.search(r"-t=(\d+)\.html", m.group(1))
        res["topic_id"] = tm.group(1) if tm else None
    if not res["topic_id"]:
        m = re.search(r"topic_id\s*:\s*(\d+)", page) or re.search(r"viewtopic\.php\?t=(\d+)", page)
        res["topic_id"] = m.group(1) if m else None
    pb = re.search(r'<div[^>]*class="post_body"', page)
    head = page[: pb.start()] if pb else page
    forums = re.findall(r'href="(?:\./)?forum-[^"]*?-f=(\d+)\.html"[^>]*>(?:\s*<span>)?([^<]+)', head)
    if forums:
        res["forum_id"], res["forum"] = forums[-1][0], _norm_space(_html.unescape(forums[-1][1]))
    m = re.search(r"Розмір:\s*</td>\s*<td[^>]*>([^<]+)</td>", page)
    res["size_bytes"] = parse_size(m.group(1)) if m else None
    m = re.search(r"Торент:\s*</td>\s*<td[^>]*>\s*\[\s*(?:<span[^>]*>)?([^<\]]+)", page)
    res["reged"] = _norm_space(_html.unescape(m.group(1))) if m else None
    _common_meta(page, res)
    if pb:
        body = _div_block(page, pb.start())
        cut = [i for i in (body.find('<div id="pc_'), body.find('<div class="boxOut')) if i >= 0]
        res["post_html"] = body[: min(cut)] if cut else body
    return res


_TOLOKA_BODY_RES = [re.compile(r'<(div|span|td)\b[^>]*class="(?:[^"]*\s)?(?:post_body|postbody|post-body)(?:\s[^"]*)?"', re.I)]


def split_toloka_topic(page: str) -> dict:
    """The same for a toloka.to topic page (phpBB 2 «Гуртом»). Written from toloka's public pages and the usual phpBB /
    TorrentPier markup — the logged-in topic page was never seen (no account): the first live run must confirm it
    (toloka_collect.js reports 'no_post_body' with a page snippet when the first post is not found)."""
    page = page or ""
    res = _empty_topic()
    if _from_compact(page, res):
        return res
    m = re.search(r'<a\b[^>]*class="[^"]*maintitle[^"]*"[^>]*>(.*?)</a>', page, re.S) or \
        re.search(r'<h1\b[^>]*>(.*?)</h1>', page, re.S)
    if m:
        res["title"] = _text(m.group(1))
    if not res["title"]:
        m = re.search(r"<title>(.*?)</title>", page, re.S)
        if m:
            res["title"] = re.split(r"\s+(?:::|—|-|\|)\s+(?:Гуртом|Толока|toloka)", _text(m.group(1)), maxsplit=1, flags=re.I)[0]
    m = re.search(r'<link[^>]*rel="canonical"[^>]*href="[^"]*/t(\d+)"', page) or \
        re.search(r'href="(?:https?://toloka\.to)?/?t(\d+)"[^>]*class="[^"]*maintitle', page) or \
        re.search(r'class="[^"]*maintitle[^"]*"[^>]*href="(?:https?://toloka\.to)?/?t(\d+)"', page) or \
        re.search(r"viewtopic\.php\?t=(\d+)", page)
    res["topic_id"] = m.group(1) if m else None
    pb = None
    for rx in _TOLOKA_BODY_RES:
        pb = rx.search(page)
        if pb:
            break
    head = page[: pb.start()] if pb else page
    forums = re.findall(r'href="(?:https?://toloka\.to)?/?(?:f|viewforum\.php\?f=)(\d+)"[^>]*>([^<]+)</a>', head)
    if forums:
        res["forum_id"], res["forum"] = forums[-1][0], _norm_space(_html.unescape(forums[-1][1]))
    m = re.search(r"Розмір\s*:?\s*(?:&nbsp;|\s|<[^>]+>)*(\d+(?:[.,]\d+)?(?:&nbsp;|\s)*[KMGTКМГТ]?i?[BБ])", page, re.I)
    res["size_bytes"] = parse_size(m.group(1)) if m else None
    m = re.search(r"(?:Зареєстрован\w*|Додано|Зареєстр\.)\s*:?\s*(?:&nbsp;|\s|<[^>]+>)*"
                  r"((?:19|20)\d{2}-\d{1,2}-\d{1,2}(?:\s+\d{1,2}:\d{2})?|\d{1,2}[\s\-][А-Яа-яІіЇїЄє]{3}\w*[\s\-,]+\d{2,4}(?:,?\s+\d{1,2}:\d{2})?"
                  r"|(?:Сьогодні|Вчора)\s+\d{1,2}:\d{2})", page)
    res["reged"] = m.group(1) if m else None
    _common_meta(page, res)
    if pb:
        res["post_html"] = _tag_block(page, pb.start(), pb.group(1).lower())
    return res


def split_topic(source: str, page: str) -> dict:
    return split_mazepa_topic(page) if source == "mazepa" else split_toloka_topic(page)


def scrub(s: str | None) -> str:
    """Announce keys are personal (toloka passkey in magnet «tr=…?h=…», download links): cut before anything is stored —
    the same rule as scrub() in toloka_collect.js."""
    s = re.sub(r"(magnet:\?xt=urn:btih:[0-9A-Za-z]+)[^\"'\s<>]*", r"\1", s or "")
    return re.sub(r"(?i)([?&;](?:h|uk|passkey|pk|key|sid|hash)=)[^&\"'\s<>]+", r"\1REDACTED", s)


def compact_topic_html(source: str, topic: dict) -> str:
    """What release_raw keeps for a mazepa / toloka topic: the meta + the first post's HTML (description + MediaInfo),
    re-parsable by ``parse_topic`` without opening the topic again. Announce keys are cut (``scrub``)."""
    topic = dict(topic, post_html=scrub(topic.get("post_html")))
    meta = {k: topic.get(k) for k in ("topic_id", "title", "forum", "forum_id", "size_bytes", "seeders", "leechers",
                                      "btih", "reged", "fetched_at")}
    how = "films/sources/mazepa.py (HTTP)" if source == "mazepa" else "films/browser/toloka_collect.js"
    return (f"<!-- {source} {topic_url(source, topic.get('topic_id'))} — first post, collected by {how} -->\n"
            f'<script type="application/json" id="ua-meta">{json.dumps(meta, ensure_ascii=False).replace("</", "<\\/")}</script>\n'
            f'<h1 class="maintitle">{_html.escape(topic.get("title") or "", quote=False)}</h1>\n'
            f'<div class="post_body">{topic.get("post_html") or ""}</div>\n')


def shape_html(title: str, post_html: str) -> str:
    """A document films.parse.rutor.parse_release_page understands: <h1>name</h1> + details with hidewrap spoilers,
    the description text translated by ``ua_to_ru``."""
    p = _PostFlattener()
    p.feed(post_html or "")
    p.close()
    main, spoilers = p.result()
    sp = "".join(
        f'<div class="hidewrap"><div class="hidehead">{_html.escape(ua_to_ru(_html.unescape(h)), quote=False)}</div>'
        f'<div class="hidebody"></div><textarea class="hidearea">{ua_to_ru(b).replace("</textarea", "&lt;/textarea")}'
        f'</textarea></div>\n' for h, b in spoilers)
    name, _ = rutor_style_name(title)
    main = ua_to_ru(main).replace("\n", "<br />\n")
    return f'<h1>{_html.escape(name, quote=False)}</h1>\n<table id="details"><tr><td>{main}\n{sp}</td></tr></table>\n'


def _assumed_russian_is_ukrainian(d: dict) -> None:
    """films.parse.rutor assumes a voice track without a language is Russian («audio track without language assumed
    Russian: …»); on a Ukrainian tracker it is Ukrainian. Only those tracks are changed; voice_by_lang is rebuilt."""
    from .rutor import VOICE_RANK
    flagged = {w.split(": ", 1)[1] for w in d.get("warnings") or [] if w.startswith("audio track without language assumed Russian: ")}
    if not flagged:
        return
    for a in d.get("audio") or []:
        if a.get("lang") == "ru" and (repr(a.get("raw") or a.get("title")) in flagged):
            a["lang"] = "uk"
    by_lang: dict[str, list[str]] = {}
    for a in d.get("audio") or []:
        if a.get("lang") in ("ru", "uk"):
            k = a.get("kind") or "unknown"
            by_lang.setdefault(a["lang"], [])
            if k not in by_lang[a["lang"]]:
                by_lang[a["lang"]].append(k)
    for lang in by_lang:
        by_lang[lang] = sorted(by_lang[lang], key=lambda k: VOICE_RANK.index(k) if k in VOICE_RANK else 99)
    d["voice_by_lang"] = by_lang
    d["warnings"] = [w.replace("assumed Russian", "taken as Ukrainian (Ukrainian tracker)")
                     if w.startswith("audio track without language assumed Russian: ") else w for w in d["warnings"]]


def parse_topic(source: str, page: str, today: date | None = None) -> dict:
    """Parse a mazepa / toloka topic: the rutor page parser on the adapted document + tracker meta.
    ``name`` stays the tracker title; ``name_parsed`` is parsed from its rutor-style form."""
    t = split_topic(source, page)
    rname, w = rutor_style_name(t["title"] or "")
    d = parse_release_page(shape_html(t["title"] or "", t["post_html"]))
    _assumed_russian_is_ukrainian(d)
    d["warnings"] = w + d.get("warnings", [])
    if not t["post_html"]:
        d["warnings"].append(f"no first post (post_body) on {source} page — name only")
    d["name"] = t["title"]
    d["name_rutor_style"] = rname
    d["category"] = category_for_forum(int(t["forum_id"]) if source == "mazepa" and t["forum_id"] else t["forum"])
    d["size_bytes"] = t["size_bytes"] if t["size_bytes"] is not None else d.get("size_bytes")
    d["seeders"] = t["seeders"] if t["seeders"] is not None else d.get("seeders")
    d["leechers"] = t["leechers"] if t["leechers"] is not None else d.get("leechers")
    d["btih"] = t["btih"] or d.get("btih")
    d["added_at"] = parse_ua_date(t["reged"], today) or d.get("added_at")
    d["tracker"] = {"source": source, "topic_id": t["topic_id"], "forum": t["forum"], "forum_id": t["forum_id"]}
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


def title_years(title: str) -> tuple[int, int] | None:
    """(first, last) year of a topic title, or None."""
    s = _html.unescape(title or "")
    m = _YEAR_RE.search(s) or _BRACKET_YEAR_RE.search(s)
    return _years_of(m) if m else None
