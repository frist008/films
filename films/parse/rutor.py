"""Parser for rutor.info release names and release pages.

Pure functions only: no network, no DB.

* ``parse_release_name(name)`` — a listing / <h1> title such as
  ``Кей-поп-охотницы на демонов / KPop Demon Hunters (2025) UHD BDRemux 2160p | 4K | HDR10 | Dolby Vision P7 | D | Videofilm Int``
* ``parse_release_page(html)`` — the HTML of ``http://rutor.info/torrent/{id}`` (full page, or just the
  ``<h1>`` + ``<div id="download">`` + ``<table id="details">`` parts that tests/fixtures keep).

Naming convention and voice-code legend come from the official rutor guide
"Создание и оформление презентации на трекере" (http://rutor.info/torrent/140886), section
"Обозначения качества переводов на русский в Названии презентаций":

    D   - Дублированный                         (official dub, or unofficial dub — both are "D")
    P   - Профессиональный многоголосый закадровый
    P1  - Профессиональный одноголосый закадровый
    P2  - Профессиональный двухголосый закадровый
    A   - Авторский
    L   - Любительский многоголосый закадровый
    L1  - Любительский одноголосый закадровый
    L2  - Любительский двухголосый закадровый
    Sub - Только субтитры
    (extra, "Дополнительные обозначения"): CAM / Line / "Звук с TS" — sound recorded in a cinema.

The codes describe *Russian* translations. Ukrainian audio is marked with a separate ``UKR`` tag by
convention (not in the legend). Uploaders often type the codes with Cyrillic look-alike letters
("Р", "А", "Р2") — they are normalised.

Everything ambiguous is reported in ``warnings`` instead of being guessed silently.
"""
from __future__ import annotations

import html as _html
import re
from typing import Any

# --------------------------------------------------------------------------------------------
# Reference data
# --------------------------------------------------------------------------------------------

LEGEND_SOURCE = "http://rutor.info/torrent/140886 (FAQ - Оформление названия / Качества переводов)"

#: rutor voice code -> (kind used in DB, meaning from the official legend)
VOICE_CODES: dict[str, tuple[str, str]] = {
    "D": ("dub", "Дублированный"),
    "P": ("mvo", "Профессиональный многоголосый закадровый"),
    "P1": ("svo", "Профессиональный одноголосый закадровый"),
    "P2": ("dvo", "Профессиональный двухголосый закадровый"),
    "A": ("avo", "Авторский"),
    "L": ("amateur", "Любительский многоголосый закадровый"),
    "L1": ("amateur", "Любительский одноголосый закадровый"),
    "L2": ("amateur", "Любительский двухголосый закадровый"),
    "SUB": ("none", "Только субтитры"),
    # not in the rutor legend, but seen in names (tracker-agnostic abbreviations)
    "MVO": ("mvo", "Многоголосый закадровый (MVO, не из легенды rutor)"),
    "DVO": ("dvo", "Двухголосый закадровый (DVO, не из легенды rutor)"),
    "AVO": ("avo", "Авторский (AVO, не из легенды rutor)"),
    "VO": ("svo", "Одноголосый закадровый (VO, не из легенды rutor)"),
    "DUB": ("dub", "Дубляж (DUB, не из легенды rutor)"),
}

#: best_voice ranking (best first). "svo" = professional single-voice (rutor P1) — not in the
#: owner's original list; placed between dvo and avo (see report).
VOICE_RANK = ["dub", "mvo", "dvo", "svo", "avo", "amateur"]

# Studio lists from the same rutor guide ("Переводчики, студии (справка)"). Used only as a weak
# fallback to tell professional from amateur voice-over when nothing else says it.
_RUTOR_UNOFFICIAL_DUB = ["Flarrow Films", "Jaskier", "LE-Production", "Overtook Studio", "Progovory Band",
                         "Red Head Sound", "Bad Company Dub"]
_RUTOR_PRO_VO = [
    "100ТВ", "2х2", "Agatha Studdio", "AlexFilm", "Amedia", "NovaFilm", "Novamedia", "AMS", "ARS-studio",
    "Astana TV", "AzOnFilm", "AXN Sci-Fi", "CDV", "CGInfo", "CP Digital", "Disney", "DniproFilm", "DVDXpert",
    "Elrom", "Filiza Studio", "Flarrow Films", "FocusX", "FocusStudio", "FOXCrime", "FoxLife", "Gears Media",
    "Good People", "HDrezka Studio", "Honey&Haseena", "IdeaFilm", "Jaskier", "Kansai Studio", "LostFilm",
    "MC Entertaiment", "Mega-Anime", "MTV", "Neoclassica", "NewComers", "NewStudio", "NewStation",
    "Nickelodeon", "Ozz", "Paramount", "Profix Media", "Rattlebox", "Red Head Sound", "RGB", "SDI Media",
    "Sony SF", "Superbit", "TUMBLER Studio", "TVShows", "FilmsClub", "Tycoon", "Universal", "ViruseProject",
    "WestVideo", "Арена", "Арк-ТВ", "Воротилин", "Домашний", "ДТВ", "ДубльPR studio", "Екатеринбург-Арт",
    "Инис", "Лексикон", "Киномания", "Кипарис", "Кириллица", "Кравец", "Кубик в Кубе", "Кураж Бомбей",
    "Невафильм", "Новый канал", "НТВ", "НТВ+", "Омикрон", "ОРТ", "Парадиз-ВС", "Первый канал",
    "Петербург 5 канал", "Пифагор", "Позитив-Мультимедиа", "Премьер Видео Фильм", "РЕН-ТВ", "С.Р.И.",
    "СВ-Студия", "СоюзВидео", "Велес", "Нота", "СВ Дубль", "ТВ3", "ТВЦ", "ТНТ", "ТОО Прим", "Хабар",
]
_RUTOR_AMATEUR = [
    "Albion Studio", "Alternative Production", "AniDub", "AniFilm", "AniLibria", "Anilife Project", "AniMedia",
    "AnimeReactor", "AnimeVost", "AniPlay", "AniStar", "ApofysTeam", "Baibako", "BraveSound", "CACTUS TEAM",
    "ColdFilm", "DexterTV", "DreamRecords", "Eleonor Film", "E-Production", "Etvox Film", "Flux-Team",
    "F-TRAIN", "GladiolusTV", "GostFilm", "Gramalant", "GREEN TEA", "GSGroup", "HamsterStudio", "ICG",
    "Jetvis Studio", "Jimmy J", "LevshaFilm", "LugaDUB", "LE-Production", "Mallorn Studio", "MYDIMKA", "Naruto-Hokage",
    "NetLab Anima Group", "NikiStudio Records", "OmskBird", "OneFilm", "OpenDub", "Padabajour", "ParadoX",
    "RG.Paravozik", "Red Head Sound Jr.", "Rudub", "Shiza Project", "SkyeFilmTV", "STEPonee", "StopGame",
    "Sunny-Films", "To4kaTV", "VictoryFilms", "Web_Money", "ZM-SHOW", "Несмертельное оружие", "Причудики",
    "Райдо", "Синема-УС", "Сладкая парочка", "Частная Студия",
]
_RUTOR_AUTHORS = [
    "Алексеев", "Визгунов", "Важенин", "Володарский", "Гаврилов", "Горчаков", "Готлиб", "Гранкин", "Дасевич",
    "Дольский", "Дохалов", "Дьяков", "Дьяконов", "Живаго", "Живов", "Иванов", "Карповский", "Боровой", "Карцев",
    "Кашкин", "Первомайский", "Киреев", "Козлов", "Королев", "Королёв", "Кузнецов", "Латышев", "Либергал",
    "Марченко", "Махонько", "Медведев", "Михалев", "Михалёв", "Мишин", "Мудров", "Немахов", "Огородников",
    "Пронин", "Прямостанов", "Пучков", "Гоблин", "Goblin", "Рудой", "Рябов", "Санаев", "Севастьянов",
    "Сербин", "Сонькин", "Толбин", "Штейн", "Янкилевич", "Ахметов", "Воронцов", "Гаевский", "Есарев",
    "Завгородний", "Замез", "Кирдин", "Котов", "Матвеев", "Поздняков", "Рапсодов", "Романов", "Смирнов",
    "Сергеев", "Строев", "Филонов", "Хохлов", "Чадов", "Штамп", "Яковлев", "Яроцкий",
]


def _key(s: str) -> str:
    return re.sub(r"[\W_]+", "", (s or "").lower().replace("ё", "е"))


_KIND_BY_STUDIO_KEY: dict[str, str] = {}
for _n in _RUTOR_AMATEUR:
    _KIND_BY_STUDIO_KEY[_key(_n)] = "amateur"
for _n in _RUTOR_PRO_VO:
    _KIND_BY_STUDIO_KEY[_key(_n)] = "mvo"
for _n in _RUTOR_AUTHORS:
    _KIND_BY_STUDIO_KEY[_key(_n)] = "avo"

#: spelling variants seen in names -> canonical studio name
_STUDIO_ALIASES = {
    "hdrezkastudio": "HDRezka Studio", "hdrezka": "HDRezka Studio",
    "кубиквкубе": "Кубик в Кубе", "кубиквкубеko": "Кубик в Кубе",
    "videofilmint": "Videofilm Int.", "videofilm": "Videofilm Int.", "videofilmltd": "Videofilm Int.",
    "videofilminternationalltd": "Videofilm Int.",
    "redheadsound": "Red Head Sound", "rhs": "Red Head Sound", "redheadsoud": "Red Head Sound",
    "даблин": "Даблин", "omskbird": "OmskBird", "moygolos": "MOYGOLOS", "voxrecords": "VOX Records",
    "viruseproject": "ViruseProject", "продубляж": "Продубляж", "novamedia": "Novamedia",
    "новамедиа": "Novamedia", "novafilm": "NovaFilm", "кирилица": "Кириллица", "кириллица": "Кириллица",
    "wstudi": "WStudio", "wstudio": "WStudio", "rudu": "RuDub", "rudub": "RuDub",
    "moviedubbing": "Movie Dubbing", "ultradoxstudio": "Ultradox Studio", "dragonstudio": "Dragon Studio",
    "jetvis": "Jetvis Studio", "jetvisstudio": "Jetvis Studio", "кравец": "Кравец-Рекордз",
    "кравецрекордз": "Кравец-Рекордз", "tvshow": "TVShows", "tvshows": "TVShows", "tb3": "ТВ3", "тв3": "ТВ3",
    "вподполье": "ВПодполье", "tb6": "ТВ-6", "тв6": "ТВ-6", "dmstudio": "DM Studio", "контентстудио": "Content Studio",
    "contentstudio": "Content Studio", "tодубляжная": "Дубляжная", "тодубляжная": "Дубляжная",
    "дубляжная": "Дубляжная", "lostfilm": "LostFilm", "newstudio": "NewStudio", "newcomers": "NewComers",
    "lostfilmtv": "LostFilm", "кипарис": "Кипарис", "мосфильммастер": "Мосфильм-Мастер",
}

_DISC_LABELS = [
    "The Criterion Collection", "Criterion Collection", "Criterion", "BFI", "Arrow Films", "UK Arrow Films",
    "Arrow Video", "Arrow", "Eureka!", "Eureka", "Shout! Factory", "Shout Factory", "Scream Factory",
    "Kino Lorber", "Imprint", "Severin Films", "Carlotta Films", "Studiocanal", "Via Vision Entertainment",
    "Fabulous Films", "Curzon Film", "Decal Releasing", "Lionsgate Films", "Sony Pictures",
    "Universal Studios", "Paramount Pictures", "FR Pathe", "Powerhouse Films", "88 Films", "Vinegar Syndrome",
    "Second Sight", "Indicator", "Umbrella Entertainment", "Radiance Films", "Masters of Cinema",
    "Blue Underground", "Warner Archive", "Gaumont", "Pathe",
]
_DISC_LABEL_KEYS = {_key(x) for x in _DISC_LABELS}

_PLATFORMS = {
    "itunes": "iTunes", "netflix": "Netflix", "amazon": "Amazon", "kion": "KION", "okko": "Okko",
    "кинопоискhd": "Кинопоиск HD", "кинопоиск": "Кинопоиск HD", "ivi": "IVI", "иви": "IVI",
    "megogo": "Megogo", "premier": "Premier", "wink": "Wink", "viju": "Viju", "смотрикино": "Смотри кино",
    "iptv": "IPTV", "start": "START", "more.tv": "more.tv", "moretv": "more.tv", "appletv": "Apple TV+",
    "disney": "Disney+", "hbomax": "HBO Max", "max": "Max",
}

_TRANSFER_REGIONS = {
    "US": "US", "USA": "US", "UK": "UK", "GBR": "UK", "GB": "UK", "GER": "DE", "DE": "DE", "FRA": "FR",
    "FR": "FR", "ITA": "IT", "IT": "IT", "NOR": "NO", "AUS": "AU", "TWN": "TW", "CEE": "CEE", "EUR": "EU",
    "JPN": "JP", "JP": "JP", "ESP": "ES", "SPA": "ES", "KOR": "KR", "HK": "HK", "CAN": "CA", "NLD": "NL",
    "SWE": "SE", "RUS": "RU",
}

_COUNTRY_WORDS = [
    "США", "Великобритания", "Франция", "Германия", "Италия", "Испания", "Канада", "Австралия", "Япония",
    "Южная Корея", "Корея Южная", "Корея", "Китай", "Гонконг", "Тайвань", "Индия", "Россия", "СССР", "Украина",
    "Польша", "Чехия", "Венгрия", "Румыния", "Болгария", "Сербия", "Хорватия", "Словения", "Словакия",
    "Австрия", "Швейцария", "Бельгия", "Нидерланды", "Голландия", "Люксембург", "Ирландия", "Дания",
    "Швеция", "Норвегия", "Финляндия", "Исландия", "Эстония", "Латвия", "Литва", "Греция", "Турция",
    "Израиль", "Иран", "ОАЭ", "Саудовская Аравия", "Египет", "ЮАР", "Нигерия", "Марокко", "Мексика",
    "Бразилия", "Аргентина", "Чили", "Колумбия", "Перу", "Уругвай", "Куба", "Новая Зеландия", "Индонезия",
    "Таиланд", "Филиппины", "Малайзия", "Сингапур", "Вьетнам", "Казахстан", "Беларусь", "Белоруссия",
    "Грузия", "Армения", "Азербайджан", "Узбекистан", "Португалия", "Кипр", "Мальта", "Ливан", "Иордания",
    "Катар", "Пакистан", "Бангладеш", "Непал", "Монголия", "Кения", "Западная Германия", "ФРГ", "ГДР",
    "Югославия", "Чехословакия", "Великобритания (Англия)",
]
_COUNTRY_KEYS = {_key(c): c for c in _COUNTRY_WORDS}

# language detection: (regex, code). Full words, case-insensitive.
_LANG_WORDS: list[tuple[str, str]] = [
    (r"русск\w*|russian|росс?ийск\w*", "ru"),
    (r"украин\w*|українськ\w*|ukrainian|укр\.?", "uk"),
    (r"английск\w*|англ\.?|english|американск\w*", "en"),
    (r"японск\w*|japanese", "ja"), (r"корейск\w*|korean", "ko"),
    (r"китайск\w*|chinese|mandarin|cantonese|кантонск\w*|мандарин\w*", "zh"),
    (r"французск\w*|french", "fr"), (r"немецк\w*|german", "de"), (r"испанск\w*|spanish|castilian", "es"),
    (r"итальянск\w*|italian", "it"), (r"португальск\w*|portuguese", "pt"), (r"польск\w*|polish", "pl"),
    (r"турецк\w*|turkish", "tr"), (r"хинди|hindi", "hi"), (r"шведск\w*|swedish", "sv"),
    (r"датск\w*|danish", "da"), (r"норвежск\w*|norwegian", "no"), (r"финск\w*|finnish", "fi"),
    (r"нидерландск\w*|голландск\w*|dutch|flemish", "nl"), (r"чешск\w*|czech", "cs"),
    (r"венгерск\w*|hungarian", "hu"), (r"тайск\w*|thai", "th"), (r"индонезийск\w*|indonesian", "id"),
    (r"иврит\w*|hebrew", "he"), (r"арабск\w*|arabic", "ar"), (r"греческ\w*|greek", "el"),
    (r"казахск\w*|kazakh", "kk"), (r"белорусск\w*|belarusian", "be"), (r"персидск\w*|фарси|persian", "fa"),
    (r"вьетнамск\w*|vietnamese", "vi"), (r"тагальск\w*|tagalog|filipino", "tl"),
    (r"румынск\w*|romanian", "ro"), (r"болгарск\w*|bulgarian", "bg"), (r"сербск\w*|serbian", "sr"),
    (r"хорватск\w*|croatian", "hr"), (r"исландск\w*|icelandic", "is"), (r"грузинск\w*|georgian", "ka"),
    (r"армянск\w*|armenian", "hy"), (r"тамильск\w*|tamil", "ta"), (r"телугу|telugu", "te"),
    (r"латышск\w*|latvian", "lv"), (r"литовск\w*|lithuanian", "lt"), (r"эстонск\w*|estonian", "et"),
    (r"узбекск\w*|uzbek", "uz"), (r"азербайджанск\w*|azerbaijani", "az"), (r"малайск\w*|malay", "ms"),
    (r"каталанск\w*|catalan", "ca"), (r"малаялам|malayalam", "ml"), (r"бенгальск\w*|bengali", "bn"),
]
_LANG_WORD_RES = [(re.compile(r"(?<![\w])(?:" + p + r")(?![\w])", re.I), c) for p, c in _LANG_WORDS]
_LANG_CODE_RE = re.compile(
    r"(?<![\w])(RUS|Rus|rus|UKR|Ukr|ukr|UA|ENG|Eng|eng|JPN|JAP|Jap|KOR|Kor|CHI|CHN|ZHO|FRE|FRA|Fre|GER|DEU|Ger|"
    r"SPA|Spa|ITA|Ita|POR|POL|TUR|HIN|SWE|DAN|NOR|FIN|CZE|HUN|THA|HEB|ARA|GRE|KAZ|RU|EN|UK)(?![\w])")
_LANG_CODE_MAP = {
    "rus": "ru", "ru": "ru", "ukr": "uk", "ua": "uk", "uk": None, "eng": "en", "en": "en", "jpn": "ja",
    "jap": "ja", "kor": "ko", "chi": "zh", "chn": "zh", "zho": "zh", "fre": "fr", "fra": "fr", "ger": "de",
    "deu": "de", "spa": "es", "ita": "it", "por": "pt", "pol": "pl", "tur": "tr", "hin": "hi", "swe": "sv",
    "dan": "da", "nor": "no", "fin": "fi", "cze": "cs", "hun": "hu", "tha": "th", "heb": "he", "ara": "ar",
    "gre": "el", "kaz": "kk",
}

# Cyrillic look-alikes -> Latin (only applied to technical tokens, never to titles)
_LOOKALIKE = str.maketrans({
    "А": "A", "В": "B", "С": "C", "Е": "E", "Н": "H", "К": "K", "М": "M", "О": "O", "Р": "P", "Т": "T",
    "Х": "X", "У": "Y", "а": "a", "с": "c", "е": "e", "о": "o", "р": "p", "х": "x", "у": "y", "к": "k",
    "м": "m", "т": "t", "в": "b", "н": "h",
})


def _lat(s: str) -> str:
    return (s or "").translate(_LOOKALIKE)


_LOOKALIKE_CHARS = "АВСЕНКМОРТХУасеорхукмтвн"
_MIXED_TOKEN_RE = re.compile(r"(?<![A-Za-zА-Яа-яЁё])[A-Za-z0-9" + _LOOKALIKE_CHARS + r".+\-/]+(?![A-Za-zА-Яа-яЁё])")


def _fix_tech(s: str) -> str:
    """Fold Cyrillic look-alikes inside *mixed* technical tokens ('АС3', 'Е-AC3', 'МVO', '2160р', 'КHz').
    Pure-Cyrillic words (СТС, НТВ) and normal Russian words are left untouched."""
    def repl(m):
        tok = m.group(0)
        has_cyr = any(ch in _LOOKALIKE_CHARS for ch in tok)
        has_lat = bool(re.search(r"[A-Za-z0-9]", tok))
        return _lat(tok) if has_cyr and has_lat else tok
    return _MIXED_TOKEN_RE.sub(repl, s or "")


def _has_cyr(s: str) -> bool:
    return bool(re.search(r"[А-Яа-яЁёІіЇїЄєҐґ]", s or ""))


def _norm_space(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip()


def _uniq(seq):
    out = []
    for x in seq:
        if x is not None and x not in out:
            out.append(x)
    return out


# --------------------------------------------------------------------------------------------
# Small field parsers shared by name and page
# --------------------------------------------------------------------------------------------

_SOURCE_RE = re.compile(
    r"(?P<uhd>\bU(?:HD|ND)\s*)?"  # "UND" is a typo seen in real names
    r"(?P<src>"
    r"B(?:lu-?\s?ray|D)\s*-?\s*Remux|BRemux"
    r"|HD\s*-?DVD\s*Remux|HDDVDRip|HD-?DVD"
    r"|BD-?Rip|ВDRip|HybridRip|Hybrid"
    r"|Blu-?\s?ray(?:\s*Disc)?"
    r"|HDT?V?Rip|HDTRip"
    r"|WEB\s*-?\s*DL\s*-?\s*Rip|WEB\s*-?\s*DL|WEB\s*-?\s*Rip|WEB"
    r"|UHDTVRip|HDTVRip|HDTV|IPTVRip|IPTV|SATRip|DVB|TVRip"
    r"|DVDRemux|DVDRip|DVD-?5|DVD-?9|DVD"
    r"|VHS-?Rip|DCPRip|CAMRip|CAM|SuperTS|TeleSync|TS|TC|DVDScr|SCR"
    r")"
    r"(?:\s*-{1,2}\s*(?P<codec>AVC|HEVC|AV1|XviD|DivX|x264|x265|H\.?26[45]))?",
    re.I,
)

_SOURCE_NORMAL = [
    (r"^(?:b(?:lu-?\s?ray|d)\s*-?\s*remux|bremux)$", "BDRemux"),
    (r"^hd\s*-?dvd\s*remux$", "HDDVDRemux"),
    (r"^hddvdrip$", "HDDVDRip"),
    (r"^hd-?dvd$", "HD-DVD"),
    (r"^(?:bd-?rip|вdrip)$", "BDRip"),
    (r"^hybrid(?:rip)?$", "Hybrid"),
    (r"^blu-?\s?ray(?:\s*disc)?$", "Blu-ray"),
    (r"^hdrip$", "HDRip"),
    (r"^(?:hdtrip|hdtvrip|uhdtvrip|hdtrip)$", "HDTVRip"),
    (r"^hdvrip$", "HDTVRip"),
    (r"^web\s*-?\s*dl\s*-?\s*rip$", "WEB-DLRip"),
    (r"^web\s*-?\s*dl$", "WEB-DL"),
    (r"^web\s*-?\s*rip$", "WEBRip"),
    (r"^web$", "WEB-DL"),
    (r"^hdtv$", "HDTV"),
    (r"^iptv(?:rip)?$", "IPTV"),
    (r"^satrip$", "SATRip"),
    (r"^dvb$", "DVB"),
    (r"^tvrip$", "TVRip"),
    (r"^dvdremux$", "DVDRemux"),
    (r"^dvdrip$", "DVDRip"),
    (r"^dvd-?[59]?$", "DVD"),
    (r"^vhs-?rip$", "VHSRip"),
    (r"^dcprip$", "DCPRip"),
    (r"^(?:camrip|cam)$", "CAMRip"),
    (r"^(?:superts|telesync|ts)$", "TS"),
    (r"^tc$", "TC"),
    (r"^(?:dvdscr|scr)$", "Screener"),
]

# source family helps ranking: physical disc / web / tv / dvd / cinema / vhs
_SOURCE_FAMILY = {
    "BDRemux": "bluray", "Blu-ray": "bluray", "BDRip": "bluray", "HDRip": "bluray", "Hybrid": "hybrid",
    "HDDVDRemux": "hddvd", "HDDVDRip": "hddvd", "HD-DVD": "hddvd",
    "WEB-DL": "web", "WEB-DLRip": "web", "WEBRip": "web",
    "HDTV": "tv", "HDTVRip": "tv", "IPTV": "tv", "SATRip": "tv", "DVB": "tv", "TVRip": "tv",
    "DVD": "dvd", "DVDRemux": "dvd", "DVDRip": "dvd", "VHSRip": "vhs",
    "DCPRip": "cinema", "CAMRip": "cinema", "TS": "cinema", "TC": "cinema", "Screener": "screener",
}


def _norm_source(tok: str) -> str | None:
    t = _norm_space(_lat(tok)).lower()
    for pat, val in _SOURCE_NORMAL:
        if re.match(pat, t):
            return val
    return None


_VCODEC_PATTERNS = [
    (r"\bav-?1\b|dav1", "AV1"),
    (r"hevc|h\.?\s?265|x265|mpeg-?h", "HEVC"),
    (r"\bavc\b|h\.?\s?264|x264|mpeg-?4\s*avc|\bavc1\b", "AVC"),
    (r"\bvp-?9\b", "VP9"),
    (r"\bvc-?1\b|\bwvc1\b", "VC-1"),
    (r"mpeg-?2|mpeg video|mpeg2video|\bh\.?262\b", "MPEG-2"),
    (r"xvid|divx|mpeg-?4 visual|mpeg-?4 asp", "MPEG-4 ASP"),
]


def _norm_vcodec(text: str) -> str | None:
    t = _lat(text or "").lower()
    for pat, val in _VCODEC_PATTERNS:
        if re.search(pat, t):
            return val
    return None


_ACODEC_PATTERNS = [
    (r"truehd|true hd|\bmlp\b", "TrueHD"),
    (r"dts[-\s:]?x\b", "DTS:X"),
    (r"dts-?hd\s*(?:ma|master)|\bxll\b|dts-?hd\s*master", "DTS-HD MA"),
    (r"dts-?hd\s*(?:hra|high)|dts-?hd(?!\s*m)", "DTS-HD HRA"),
    (r"\bdts\b|dts-es", "DTS"),
    (r"e-?ac-?3|eac3|\bdd\+|\bddp\d?|dolby digital plus", "E-AC3"),
    (r"\bac-?3\b|ac3|dolby digital|\bdd\s?[25]\.[01]\b", "AC3"),
    (r"\baac\b|aac[-\s]?(?:lc|he)|he-?aac", "AAC"),
    (r"\bflac\b", "FLAC"),
    (r"l?pcm", "PCM"),
    (r"\bopus\b", "Opus"),
    (r"\bmp3\b|mpeg audio.*layer ?3", "MP3"),
    (r"\bmp2\b|mpeg audio.*layer ?2", "MP2"),
    (r"vorbis", "Vorbis"),
]


def _norm_acodec(text: str) -> str | None:
    t = _lat(text or "").lower()
    for pat, val in _ACODEC_PATTERNS:
        if re.search(pat, t):
            return val
    return None


def _num(s: str) -> float | None:
    """Parse MediaInfo-style numbers: '75, 8' (ru decimal comma + space), '4 484', '61.5', '~14.3'."""
    if s is None:
        return None
    s = s.replace("\xa0", " ").replace("~", "").strip()
    m = re.match(r"(\d{1,3}(?: \d{3})+|\d+)(?:\s*[.,]\s*(\d+))?", s)
    if not m:
        return None
    whole = m.group(1).replace(" ", "")
    return float(whole + ("." + m.group(2) if m.group(2) else ""))


_KBPS_RE = re.compile(
    r"(?<![\d.,])(\d{1,3}(?:[ \xa0]\d{3})+|\d+(?:[.,]\d+)?)\s*"
    r"(kbps|kb/s|kbs|kbit/s|kbit|kb\b|mbps|mb/s|mbit/s|mbit)",
    re.I,
)


def _kbps_in(text: str) -> int | None:
    t = (text or "").replace("\xa0", " ").replace("~", " ")
    # Cyrillic units -> Latin (also mixed spellings like "Mбит", "Kбит")
    t = re.sub(r"(?i)[кk]бит/?с?\w*|[кk]б/с", "kbit", t)
    t = re.sub(r"(?i)[мm]бит/?с?\w*|[мm]б/с", "mbit", t)
    m = _KBPS_RE.search(t)
    if not m:
        return None
    val = _num(m.group(1))
    if val is None:
        return None
    if m.group(2).lower().startswith("m"):
        val *= 1000
    return int(round(val))


def _channels_in(text: str) -> str | None:
    t = _lat(text or "")
    m = re.search(r"(\d)\s*/\s*(\d)(?:\s*/\s*(\d))?\s*(?:\([^)]*\))?\s*(\+\s*LFE)?", t, re.I)
    if m and re.search(r"\d\s*/\s*\d\s*(?:/\s*\d\s*)?(?:\([^)]*\))?\s*(?:\+\s*LFE)?\s*ch", t, re.I):
        n = int(m.group(1)) + int(m.group(2)) + int(m.group(3) or 0)
        return f"{n}.{1 if m.group(4) else 0}"
    m = re.search(r"(?<![\d.,])([1-9])\.([0-2])(?![\d])(?!\s*k?hz)", t, re.I)
    if m:
        return f"{m.group(1)}.{m.group(2)}"
    m = re.search(r"(\d{1,2})\s*(?:ch\b|channels?|канал\w*|кан\.)", t, re.I)
    if m:
        return _channels_from_count(int(m.group(1)))
    return None


def _channels_from_count(n: int) -> str:
    return {1: "1.0", 2: "2.0", 3: "2.1", 4: "4.0", 5: "5.0", 6: "5.1", 7: "6.1", 8: "7.1"}.get(n, f"{n}ch")


def _detect_lang(text: str) -> str | None:
    """First language mentioned in text (word or upper-case code)."""
    best = None
    for rx, code in _LANG_WORD_RES:
        m = rx.search(text or "")
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), code)
    m = _LANG_CODE_RE.search(text or "")
    if m:
        code = _LANG_CODE_MAP.get(m.group(1).lower())
        if code and (best is None or m.start() < best[0]):
            best = (m.start(), code)
    return best[1] if best else None


def _all_langs(text: str) -> list[str]:
    found = []
    for rx, code in _LANG_WORD_RES:
        for m in rx.finditer(text or ""):
            found.append((m.start(), code))
    for m in _LANG_CODE_RE.finditer(text or ""):
        code = _LANG_CODE_MAP.get(m.group(1).lower())
        if code:
            found.append((m.start(), code))
    return _uniq(c for _, c in sorted(found))


def _resolution_class(width: int | None, height: int | None) -> int | None:
    if not width and not height:
        return None
    w, h = width or 0, height or 0
    if w >= 3200 or h >= 1700:
        return 2160
    if w >= 2200 or h >= 1300:
        return 1440
    if w >= 1700 or h >= 950:
        return 1080
    if w >= 1100 or h >= 650:
        return 720
    if h >= 560 or w >= 1000:
        return 576
    return 480


# --------------------------------------------------------------------------------------------
# Voice kind from free text (track titles, "Аудио" lines, "Перевод" lines)
# --------------------------------------------------------------------------------------------

_W = r"(?<![\w])"
_E = r"(?![\w])"
_KIND_RES = [
    ("commentary", re.compile(r"comment|коммент", re.I)),
    ("dub", re.compile(_W + r"(?:dub|dubbed|дубляж|дубл\.|дублированн\w*|дублирован\w*|дублир\w*|дубл)" + _E, re.I)),
    ("amateur", re.compile(r"любительск|amateur|" + _W + r"(?:lmvo|ldvo|lvo|l1|l2)" + _E, re.I)),
    ("avo", re.compile(_W + r"(?:avo|авторск\w*|author)" + _E, re.I)),
    ("dvo", re.compile(_W + r"(?:dvo|двух\s*голос\w*|двухголос\w*|двуголос\w*|2-?х?\s*голос\w*)" + _E, re.I)),
    ("mvo", re.compile(_W + r"(?:mvo|многоголос\w*|многогол\.)" + _E, re.I)),
    ("svo", re.compile(_W + r"(?:vo|svo|одноголос\w*)" + _E, re.I)),
    ("vo", re.compile(r"закадров", re.I)),
    ("original", re.compile(_W + r"(?:original|оригинал\w*|orig|org)" + _E, re.I)),
]


def _kind_in(text: str) -> tuple[str | None, dict]:
    """Return (kind, extra) for a free-text voice description."""
    t = _fix_tech(text or "")
    extra = {}
    if re.search(r"неофициальн|unofficial", t, re.I):
        extra["official"] = False
    elif re.search(r"(?<!не)официальн|лицензи|official", t, re.I):
        extra["official"] = True
    if re.search(r"профессиональн|professional", t, re.I):
        extra["professional"] = True
    for kind, rx in _KIND_RES:
        if rx.search(t):
            if kind == "amateur":
                if re.search(r"двух|двуголос|l2|ldvo", t, re.I):
                    extra["voices"] = 2
                elif re.search(r"одноголос|l1|lvo", t, re.I):
                    extra["voices"] = 1
                elif re.search(r"многоголос|lmvo", t, re.I):
                    extra["voices"] = "multi"
                if re.search(_KIND_RES[1][1].pattern, t, re.I):
                    return "dub", extra  # "любительский дубляж" is still a dub (code D)
            if kind == "vo":
                extra["voices_unstated"] = True
                return "mvo", extra
            return kind, extra
    return None, extra


# --------------------------------------------------------------------------------------------
# Tag classification (parts after "|" in a release name)
# --------------------------------------------------------------------------------------------

_EDITION_TAGS = [
    (r"open\s*-?\s*matte", "open_matte"),
    (r"extended|расширенн", "extended"),
    (r"director'?s|режисс[её]р", "directors_cut"),
    (r"theatrical|театральн|прокатн", "theatrical"),
    (r"\bimax\b", "imax"),
    (r"remaster|mastered in 4k|restor|реставр|ремастер", "remastered"),
    (r"uncut|полная версия|international full version|full version", "uncut"),
    (r"unrated", "unrated"),
    (r"alternative cut|альтернативн", "alternative_cut"),
    (r"fan\s*cut|фан", "fan_cut"),
    (r"локализ", "localized"),
    (r"black\s*&?\s*white|ч[её]рно-?\s*бел", "black_and_white"),
    (r"цветная версия|full colou?r|colori[sz]ed|колориз", "colorized"),
    (r"fullscreen|pan\s*&?\s*scan", "fullscreen"),
    (r"\bcut\b|\bedition\b|anniversary|версия|издание", "other"),
]

_EXT_AUDIO_RE = re.compile(r"внешн\w*\s+(?:файл|дорожк)\w*|отдельн\w*\s+файл\w*|\(ext\)|\bext(?:ernal)?\b", re.I)
_UPSCALE_RE = re.compile(r"up\s*-?\s*scal\w*|апск[еэ]йл\w*|ai[\s-]*upscal\w*|ai[\s-]*enhanced|нейросет\w*\s+апск", re.I)
_NATIVE4K_RE = re.compile(r"нативн\w*\s*4\s*[KК]|native\s*4\s*k|true\s*4k|4k\s*di\b|4k\s*digital intermediate", re.I)
_2KDI_RE = re.compile(r"2k\s*(?:di|digital intermediate|мастер|master)|мастер\w*\s*2k|di\s*2k", re.I)
_DV_TAG_RE = re.compile(
    r"^(?:dolby\s*vision|dovi|dv)\s*(?:(?:profile|prof\.?|p)\s*)?(\d{1,2})?(?:\.(\d))?\s*(tv|fel|mel)?$", re.I)


_3D_TAG_RE = re.compile(
    r"^(?:3-?d(?:[\s-]*video)?|hsbs|hou|half[\s-]*(?:ou|sbs|over[\s-]*under|side[\s-]*by[\s-]*side)(?:[\s-]*crop)?"
    r"|anaglyph|анаглиф|ou|sbs|over[\s-]*under|frame[\s-]*packing|mvc)"
    r"(?:\s*[(/]\s*(?:анаглиф|anaglyph)\w*\)?)?$")


def _classify_tag(tok: str, out: dict, w: list) -> None:
    """Classify one comma-separated token from the '|' tail of a release name; mutates ``out``."""
    t = _norm_space(tok)
    if not t:
        return
    lt = _lat(t)
    up = lt.upper().replace(" ", "")
    low = lt.lower()

    # "P от Brazzass" -> code + studio
    m = re.match(r"^([A-Za-zА-Яа-я]{1,3}\d?)\s+(?:от|by)\s+(.+)$", t)
    if m and _lat(m.group(1)).upper() in VOICE_CODES:
        _classify_tag(m.group(1), out, w)
        out["studios"].append(m.group(2).strip())
        return
    if up in VOICE_CODES:
        out["voice_codes"].append(up if up != "SUB" else "Sub")
        return
    if up in ("UKR", "UKRAINIAN", "УКР"):
        out["has_ukr"] = True
        return
    if up in ("RUS", "ENG"):
        out["lang_tags"].append(up)
        return
    m = re.match(r"^(?:лицензия|license|official|licensed)\s*(?:\((.*)\))?$", t, re.I)
    if m:
        out["licensed"] = True
        if m.group(1):
            for code in re.findall(r"[A-ZА-Я]{2,3}", m.group(1)):
                if _lat(code).upper() == "UKR":
                    out["has_ukr"] = True
                out["lang_tags"].append(_lat(code).upper())
        return
    if re.match(r"^(?:звук\s+с\s+)?(ts|cam|camrip|line|tc|чистый звук)$", low, re.I) \
            or re.match(r"(?i)^чистый\s+звук$", t):   # `low` turns Cyrillic look-alikes Latin («чиcтый»)
        out["audio_source"] = re.sub(r"^звук\s+с\s+", "", t, flags=re.I)
        return
    if up == "R5":
        # R5 = a (Russian) region-5 licensed disc as the audio source — not a cinema recording
        out["misc_tags"].append("R5")
        return
    if up in ("4K", "UHD", "2160P"):
        out["uhd_tag"] = True
        return
    if re.match(r"^(\d{3,4})[pi]$", low):
        out["res_tags"].append(int(low[:-1]))
        return
    # HDR family
    if re.match(r"^(hdr10\+|hdr\+10|hdr10plus|hdr10\s*\+|hdr\s*10\+)$", low):
        out["hdr"].append("HDR10+")
        out["hdr10_explicit"] = True
        return
    if low == "hdr+":
        out["hdr"].append("HDR10+")
        w.append("tag 'HDR+' read as HDR10+ (rutor guide lists HDR+ among HDR10 variants)")
        return
    if low in ("hdr10", "hdr 10"):
        out["hdr"].append("HDR10")
        out["hdr10_explicit"] = True
        return
    if low == "hdr":
        out["hdr_generic"] = True
        return
    if low == "sdr":
        out["sdr_tag"] = True
        return
    if low == "hlg":
        out["hdr"].append("HLG")
        return
    if low in ("hdr vivid", "hdrvivid"):
        out["hdr"].append("HDR Vivid")
        return
    m = _DV_TAG_RE.match(lt.strip())
    if m:
        out["hdr"].append("DV")
        if m.group(1):
            p = int(m.group(1))
            if p in (4, 5, 7, 8, 9, 10, 20):
                if out.get("dv_profile") not in (None, p):
                    w.append(f"several DV profiles in name: {out['dv_profile']} and {p}")
                out["dv_profile"] = p
            else:
                w.append(f"unknown DV profile number in tag {t!r}")
        if m.group(3):
            out["dv_note"] = m.group(3).upper()
            if m.group(3).upper() == "TV" and not m.group(1):
                # "Dolby Vision TV" = TV-led Dolby Vision; MediaInfo of releases 1004108 and 997571 shows
                # Profile 5 (dvhe.05.06) for exactly this tag
                out["dv_profile"] = 5
                out["dv_profile_inferred"] = True
                w.append("'Dolby Vision TV' read as Profile 5 (TV-led DV; verified on 2 rutor releases)")
            else:
                w.append(f"DV tag with suffix {m.group(3)!r} — meaning not certain")
        return
    if low in ("dolby vision p",):
        out["hdr"].append("DV")
        w.append(f"incomplete DV tag {t!r}")
        return
    vc = None
    if re.match(r"^(hevc|avc|av1|x26[45]|h\.?26[45]|vp9|xvid)$", low):
        vc = _norm_vcodec(low)
    if vc:
        out["codec_tags"].append(vc)
        return
    if low in ("hybrid",):
        out["hybrid"] = True
        return
    if up in ("КПК", "KПK", "KPK", "PDA", "MOBILE", "IPOD", "IPHONE", "3GP", "PSP") or re.match(r"(?i)^(?:кпк|kpk)\s+(?:от|by)\s", t):
        out["kpk"] = True   # survey 39 (2026-10-25): «для КПК / телефона» — excluded
        return
    # «3D-Video», «HSBS», «halfOU-Crop», «halfSBS», «3D-video (Анаглиф)», «3D-Video / Анаглиф» (survey 21, 2026-10-25)
    if any(_3D_TAG_RE.match(x) for x in (low, t.lower())):   # raw too: `low` turns «Анаглиф» into Latin look-alikes
        out["is_3d"] = True
        if low not in ("3d", "3d-video"):
            out["format_3d"] = t
        return
    m = re.match(r"^(\d{2,3})\s*fps$", low)
    if m:
        out["fps_tag"] = int(m.group(1))
        return
    if _UPSCALE_RE.search(t):
        out["upscale_hints"].append(t)
        if re.search(r"remaster", t, re.I):
            out["edition"].append("remastered")
        return
    m = re.match(r"^([A-Za-z]{2,3})[\s-]+transfer$", lt.strip(), re.I)
    if m:
        out["transfer"].append(_TRANSFER_REGIONS.get(m.group(1).upper(), m.group(1).upper()))
        return
    if up in _TRANSFER_REGIONS and len(up) >= 3 and up not in ("RUS",):
        out["transfer"].append(_TRANSFER_REGIONS[up])
        w.append(f"bare region tag {t!r} read as disc transfer region")
        return
    k = _key(t)
    if k in _DISC_LABEL_KEYS:
        out["disc_labels"].append(t)
        return
    if k in _PLATFORMS:
        out["platforms"].append(_PLATFORMS[k])
        return
    if low in ("custom",):
        out["misc_tags"].append(t)
        return
    if low in ("no dnr",):
        out["misc_tags"].append(t)
        return
    for pat, flag in _EDITION_TAGS:
        if re.search(pat, t, re.I):
            if flag == "fan_cut" and not re.search(r"fan\s*cut", t, re.I):
                continue
            out["edition"].append(flag)
            out["edition_raw"].append(t)
            if flag == "imax":
                out["is_imax_cut"] = True
            if flag == "other":
                w.append(f"unrecognised edition tag {t!r} kept as edition 'other'")
            return
    # anything else: a dubbing / voice-over studio (rutor convention: studios come after the codes)
    if re.search(r"\d", t) and not _has_cyr(t) and len(t) < 4 and _key(t) not in _STUDIO_ALIASES:
        w.append(f"unclassified short tag {t!r}")
        out["misc_tags"].append(t)
        return
    out["studios"].append(t)


def canonical_studio(name: str) -> str:
    """Canonical spelling for known studio variants (otherwise the trimmed original)."""
    n = _norm_space(name).strip(" .,;-")
    return _STUDIO_ALIASES.get(_key(n), n)


# --------------------------------------------------------------------------------------------
# Season / episodes
# --------------------------------------------------------------------------------------------

def _parse_season_block(b: str, w: list) -> dict:
    raw = b
    t = b.replace("х", "x").replace("Х", "x").replace("–", "-").replace("—", "-")
    t = _norm_space(t)
    res: dict[str, Any] = {"seasons": [], "episode_from": None, "episode_to": None, "episodes_total": None,
                           "specials": False, "extras": [], "pieces": None}
    main, _, extra = t.partition("+")
    if extra.strip():
        res["extras"].append(extra.strip())
        if re.search(r"special|sp\b|бонус|extra|спецвып", extra, re.I):
            res["specials"] = True
    main = main.strip()
    if re.fullmatch(r"(?i)specials?|ova|ona|sp", main):
        res["specials"] = True
        return res
    m = re.fullmatch(r"(?i)s(\d{1,3})(?:\s*-\s*s?(\d{1,3}))?", main)
    if m:
        a = int(m.group(1)); z = int(m.group(2) or a)
        res["seasons"] = list(range(a, z + 1)) if z >= a else [a, z]
        return res
    m = re.fullmatch(r"(?i)(\d{1,3})(?:\s*-\s*(\d{1,3}))?\s*x\s*(\d{1,4})(?:\s*-\s*(\d{1,4}))?"
                     r"(?:\s*(?:из|of)\s*(\d{1,4}|xx|\?\?))?", main)
    if m:
        a = int(m.group(1)); z = int(m.group(2) or a)
        res["seasons"] = list(range(a, z + 1))
        res["episode_from"] = int(m.group(3)); res["episode_to"] = int(m.group(4) or m.group(3))
        if m.group(5) and m.group(5).isdigit():
            res["episodes_total"] = int(m.group(5))
        return res
    m = re.fullmatch(r"(?i)(\d{1,4})(?:\s*-\s*(\d{1,4}))?\s*(?:из|of)\s*(\d{1,4}|xx|\?\?)", main)
    if m:
        res["episode_from"] = int(m.group(1)); res["episode_to"] = int(m.group(2) or m.group(1))
        if m.group(3).isdigit():
            res["episodes_total"] = int(m.group(3))
        return res
    m = re.fullmatch(r"(?i)(\d{1,4})\s*-\s*(\d{1,4})(?:\s*(?:серии|серия|эпизоды|episodes))?", main)
    if m:
        res["episode_from"] = int(m.group(1)); res["episode_to"] = int(m.group(2))
        return res
    m = re.fullmatch(r"(?i)(?:эпизоды|серии|episodes)\s*(\d{1,4})\s*-\s*(\d{1,4})", main)
    if m:
        res["episode_from"] = int(m.group(1)); res["episode_to"] = int(m.group(2))
        return res
    m = re.fullmatch(r"(?i)\(?(\d{1,3})\s*сезон\s*:?\s*(\d{1,4})\s*-\s*(\d{1,4})\s*сери\w*\)?", main)
    if m:
        res["seasons"] = [int(m.group(1))]
        res["episode_from"] = int(m.group(2)); res["episode_to"] = int(m.group(3))
        return res
    ms = re.findall(r"(?i)сезон\s*(\d{1,3})", main)
    if ms:
        res["seasons"] = [int(x) for x in ms]
        w.append(f"season block {raw!r} parsed only partly (seasons {res['seasons']})")
        return res
    m = re.fullmatch(r"(?i)(\d{1,4})\s*шт\.?", main)
    if m:
        res["pieces"] = int(m.group(1))
        return res
    m = re.fullmatch(r"(\d{1,4})", main)
    if m:
        res["episode_from"] = res["episode_to"] = int(m.group(1))
        w.append(f"bare number block [{raw}] read as an episode number")
        return res
    # complex: "02x01-08, 01-02 из 10" etc. — take the first NxN group, keep raw
    m = re.match(r"(?i)(\d{1,3})\s*x\s*(\d{1,4})(?:\s*-\s*(\d{1,4}))?", main)
    if m:
        res["seasons"] = [int(m.group(1))]
        res["episode_from"] = int(m.group(2)); res["episode_to"] = int(m.group(3) or m.group(2))
        mt = re.search(r"(?:из|of)\s*(\d{1,4})\s*$", main)
        if mt:
            res["episodes_total"] = int(mt.group(1))
        nums = re.findall(r"(\d{1,3})\s*x", main)
        if len(set(nums)) > 1:
            res["seasons"] = _uniq(int(x) for x in nums)
        w.append(f"complex season/episode block {raw!r} parsed partly")
        return res
    w.append(f"unparsed bracket block {raw!r}")
    return res


def _seasons_str(seasons: list[int]) -> str | None:
    if not seasons:
        return None
    s = sorted(set(seasons))
    if len(s) == 1:
        return str(s[0])
    if s == list(range(s[0], s[-1] + 1)):
        return f"{s[0]}-{s[-1]}"
    return ",".join(str(x) for x in s)


# --------------------------------------------------------------------------------------------
# parse_release_name
# --------------------------------------------------------------------------------------------

_YEAR_RE = re.compile(r"\((\d{4})(?:\s*[-–—]\s*(\d{2,4}))?((?:\s*,\s*\d{4})*)\)")


def parse_release_name(name: str) -> dict:
    """Parse a rutor release title. Returns a dict (see module docstring / report for keys)."""
    w: list[str] = []
    s = _norm_space(_html.unescape(name or ""))
    out: dict[str, Any] = {
        "name": s, "title_ru": None, "titles_other": [], "title_original": None, "title_uk": None,
        "titles_ru_alt": [], "title_notes": [],
        "year": None, "year_from": None, "year_to": None, "years": [],
        "season_raw": None, "seasons": [], "season": None, "episodes": None, "episode_from": None,
        "episode_to": None, "episodes_total": None, "has_specials": False, "season_extras": [], "pieces": None,
        "quality_raw": None, "source_type": None, "source_raw": None, "source_types": [], "source_family": None,
        "uhd_source": False, "release_group": None,
        "resolution": None, "resolution_class": None, "interlaced": False,
        "hdr": [], "hdr_generic": False, "sdr_tag": False, "dv_profile": None, "dv_note": None,
        "video_codec": None, "codec_tags": [],
        "voice_codes": [], "voices": [], "best_voice": None, "subs_only": False, "has_ukr": False,
        "lang_tags": [], "audio_source": None,
        "studios": [], "platforms": [], "disc_labels": [], "transfer": [], "licensed": False,
        "edition": [], "edition_raw": [], "is_imax_cut": False, "is_upscale": None, "upscale_hints": [],
        "hybrid": False, "kpk": False, "is_3d": False, "format_3d": None, "fps_tag": None,
        "uhd_tag": False, "res_tags": [], "misc_tags": [], "dv_profile_inferred": False, "disc_region_raw": None, "hdr10_explicit": False, "warnings": w,
    }
    if not s:
        w.append("empty name")
        return out

    parts = s.split("|")
    head = parts[0].strip()
    tails = [p.strip() for p in parts[1:]]

    # ---- year: last "(YYYY...)" in head
    ym = None
    for m in _YEAR_RE.finditer(head):
        ym = m
    if ym:
        titles_part, quality_part = head[: ym.start()], head[ym.end():]
        years = [int(ym.group(1))]
        if ym.group(2):
            y2 = ym.group(2)
            y2i = int(y2) if len(y2) == 4 else int(ym.group(1)[:2] + y2)
            years.append(y2i)
        if ym.group(3):
            years += [int(x) for x in re.findall(r"\d{4}", ym.group(3))]
        out["years"] = years
        out["year_from"], out["year_to"] = min(years), max(years)
        out["year"] = out["year_from"]
        if len(years) > 2 or (ym.group(3) and ym.group(3).strip()):
            w.append(f"several years in name: {years}")
    elif re.search(r"\((\d{4})\s", head):
        m = re.search(r"\((\d{4})\s", head)  # malformed "(2023 BDRip ..." — closing parenthesis missing
        titles_part, quality_part = head[: m.start()], head[m.end() - 1:]
        out["years"] = [int(m.group(1))]
        out["year"] = out["year_from"] = out["year_to"] = int(m.group(1))
        w.append("year parenthesis not closed in name")
    else:
        w.append("no (year) in name")
        sm = _SOURCE_RE.search(_lat(head))
        # require a word boundary before the source token
        while sm and sm.start() > 0 and head[sm.start() - 1].isalnum():
            sm = _SOURCE_RE.search(_lat(head), sm.end())
        if sm:
            titles_part, quality_part = head[: sm.start()], head[sm.start():]
        else:
            titles_part, quality_part = head, ""

    # ---- brackets in titles part: season / episodes
    blocks = re.findall(r"\[([^\]]*)\]", titles_part)
    titles_part = re.sub(r"\[[^\]]*\]", " ", titles_part)
    # also "[...]" accidentally after the year
    blocks += re.findall(r"\[([^\]]*)\]", quality_part)
    quality_part = re.sub(r"\[[^\]]*\]", " ", quality_part)
    if blocks:
        out["season_raw"] = ", ".join(b.strip() for b in blocks)
        seasons: list[int] = []
        for b in blocks:
            r = _parse_season_block(b.strip(), w)
            seasons += r["seasons"]
            for k in ("episode_from", "episode_to", "episodes_total", "pieces"):
                if r[k] is not None and out[k] is None:
                    out[k] = r[k]
            out["has_specials"] = out["has_specials"] or r["specials"]
            out["season_extras"] += r["extras"]
        out["seasons"] = _uniq(seasons)
        out["season"] = _seasons_str(out["seasons"])
        if out["episode_from"] is not None:
            ef, et = out["episode_from"], out["episode_to"]
            out["episodes"] = str(ef) if ef == et else f"{ef}-{et}"

    # ---- titles
    titles = [_norm_space(x) for x in re.split(r"\s+/\s+", titles_part.strip()) if _norm_space(x)]
    if titles:
        # rutor convention: the Russian release title always comes first, even when it is written
        # in Latin letters or digits ("F1 / F1: The Movie", "1670 / 1670")
        out["title_ru"] = titles[0]
        rest = titles[1:]
        out["titles_other"] = rest
        for t in rest:
            if re.search(r"[ІіЇїЄєҐґ]", t):
                if out["title_uk"] is None:
                    out["title_uk"] = t
            elif _has_cyr(t):
                out["titles_ru_alt"].append(t)
            elif out["title_original"] is None:
                out["title_original"] = t
        if out["title_ru"] and not _has_cyr(out["title_ru"]) and out["title_original"] is None:
            out["title_original"] = out["title_ru"]

    # ---- quality part: sources, codec, resolution, release group
    qp = _fix_tech(_norm_space(quality_part).strip(" )]"))
    qp = re.sub(r"(?i)\b\d+\s*x\s*(DVD-?[59])\b", r"\1", qp)  # "5xDVD9" -> "DVD9" (count dropped)
    out["quality_raw"] = qp or None
    rg = re.search(r"\s+(?:от|by)\s+(.+)$", qp, re.I)
    if rg:
        out["release_group"] = rg.group(1).strip()
        qp = qp[: rg.start()]
    leftover = qp
    lq = _lat(qp)
    srcs = []
    for m in _SOURCE_RE.finditer(lq):
        if m.start() > 0 and lq[m.start() - 1].isalnum():
            continue
        if m.end() < len(lq) and lq[m.end()].isalnum():
            continue
        norm = _norm_source(m.group("src"))
        if not norm:
            continue
        srcs.append((norm, m.group(0).strip(), bool(m.group("uhd")), m.group("codec")))
        leftover = leftover.replace(qp[m.start():m.end()], " ", 1)
    if srcs:
        out["source_types"] = _uniq(x[0] for x in srcs)
        out["source_type"] = srcs[0][0]
        out["source_raw"] = srcs[0][1]
        out["uhd_source"] = any(x[2] for x in srcs)
        out["source_family"] = _SOURCE_FAMILY.get(out["source_type"])
        for x in srcs:
            if x[3]:
                out["codec_tags"].append(_norm_vcodec(x[3]))
        if len(out["source_types"]) > 1:
            w.append(f"several sources in name: {out['source_types']} (source_type = first)")
        if re.match(r"(?i)hdtrip", srcs[0][1]):
            w.append("'HDTRip' read as HDTVRip (typo)")
    elif qp:
        w.append(f"no source type recognised in {qp!r}")
    else:
        w.append("no quality part in name")
    lql = _lat(leftover)
    for m in re.finditer(r"(?<![\d])(2160|1440|1080|720|576|540|480|360)\s*([pi])?(?![\w])", lql, re.I):
        if out["resolution"] is None:
            out["resolution"] = int(m.group(1))
            out["interlaced"] = (m.group(2) or "").lower() == "i"
            if not m.group(2):
                w.append(f"resolution '{m.group(1)}' without p/i")
        leftover = leftover.replace(m.group(0), " ", 1)
    for m in re.finditer(r"(?<![\w])(?:4\s*[kк]|uhd)(?![\w])", leftover, re.I):
        out["uhd_tag"] = True
        leftover = leftover.replace(m.group(0), " ", 1)
    for m in re.finditer(r"(?<![\w])(hevc|avc|av1|x26[45]|h\.?26[45]|xvid)(?![\w])", leftover, re.I):
        out["codec_tags"].append(_norm_vcodec(m.group(1)))
        leftover = leftover.replace(m.group(0), " ", 1)
    for m in re.finditer(r"(?<![\w])(sdr|hdr10\+?|hdr|dv)(?![\w])", leftover, re.I):
        _classify_tag(m.group(1), out, w)
        leftover = leftover.replace(m.group(0), " ", 1)
    leftover = _norm_space(re.sub(r"[,/+()&-]+", " ", leftover))
    if leftover:
        # e.g. "DVDRemux / Theatrical Cut", "WEBRip Kerob", "BDRemux 1080p селезень", "Remastered Upscale",
        # "UHD Blu-Ray EUR 2160p"
        handled = False
        for pat, flag in _EDITION_TAGS[:-1]:
            if re.search(pat, leftover, re.I):
                _classify_tag(leftover, out, w)
                handled = True
                break
        if not handled and _UPSCALE_RE.search(leftover):
            _classify_tag(leftover, out, w)
            handled = True
        if not handled and leftover.upper() in _TRANSFER_REGIONS:
            out["transfer"].append(_TRANSFER_REGIONS[leftover.upper()])
            out["disc_region_raw"] = leftover
            handled = True
        if not handled:
            if out["release_group"] is None:
                out["release_group"] = leftover
                w.append(f"unlabelled text after quality {leftover!r} taken as release group")
            else:
                out["misc_tags"].append(leftover)
                w.append(f"unclassified text in quality part {leftover!r}")

    # ---- tags after '|'
    for part in tails:
        for tok in re.split(r",(?![^()]*\))", part):
            _classify_tag(tok, out, w)

    # ---- derived
    out["codec_tags"] = _uniq(out["codec_tags"])
    if out["codec_tags"]:
        out["video_codec"] = out["codec_tags"][0]
        if len(out["codec_tags"]) > 1:
            w.append(f"several codecs in name: {out['codec_tags']}")
    if out["resolution"] is None and out["res_tags"]:
        out["resolution"] = out["res_tags"][0]
    if out["resolution"] is None and (out["uhd_tag"] or (out["uhd_source"] and out["source_type"] in ("BDRemux", "Blu-ray"))):
        out["resolution"] = 2160
    if out["resolution"] is None and out["source_type"]:
        # rutor convention: "***Rip-AVC — рипы разрешением менее 720p"; a Rip without resolution is SD
        if out["source_type"] in ("BDRip", "WEB-DLRip", "WEBRip", "HDRip", "DVDRip", "HDTVRip", "SATRip",
                                  "TVRip", "VHSRip", "DCPRip", "TS", "CAMRip", "Hybrid", "IPTV", "DVD",
                                  "DVDRemux", "TC", "Screener"):
            out["resolution_class"] = "SD"
            w.append("no resolution in name; by rutor convention a Rip without resolution is SD (<720p)")
        else:
            w.append("no resolution in name")
    if out["resolution"]:
        out["resolution_class"] = {2160: "UHD", 1440: "QHD", 1080: "FHD", 720: "HD"}.get(out["resolution"], "SD")

    out["hdr"] = _uniq(out["hdr"])
    if out["hdr_generic"] and not any(h in out["hdr"] for h in ("HDR10", "HDR10+")):
        if out["dv_profile"] == 5:
            w.append("generic 'HDR' tag next to DV Profile 5 — not treated as HDR10 fallback")
        else:
            out["hdr"].append("HDR10")
            w.append("generic 'HDR' tag read as HDR10 (rutor: 'HDR (HDR10, HDR+, HDR10+)')")
    if "HDR10+" in out["hdr"] and "HDR10" not in out["hdr"]:
        out["hdr"].append("HDR10")  # HDR10+ always carries an HDR10 base
    order = ["DV", "HDR10+", "HDR10", "HLG", "HDR Vivid"]
    out["hdr"] = sorted(out["hdr"], key=lambda h: order.index(h) if h in order else 99)
    if out["sdr_tag"]:
        if out["hdr"]:
            w.append(f"name says SDR and also {out['hdr']}")
        else:
            out["hdr"] = []
    if "DV" in out["hdr"] and out["dv_profile"] is None:
        w.append("Dolby Vision without profile in name")

    out["edition"] = _uniq(out["edition"])
    if out["upscale_hints"]:
        out["is_upscale"] = True

    # voices
    codes = _uniq(out["voice_codes"])
    out["voice_codes"] = codes
    for c in codes:
        kind, meaning = VOICE_CODES[c.upper()]
        # codes describe Russian translations; with a UKR tag they may describe the Ukrainian track instead
        out["voices"].append({"code": c, "kind": kind, "meaning": meaning,
                              "lang": None if out["has_ukr"] else "ru"})
    if out["has_ukr"] and codes:
        w.append("UKR tag present: voice codes may refer to the Ukrainian track (language left unknown)")
    kinds = [v["kind"] for v in out["voices"] if v["kind"] != "none"]
    if kinds:
        out["best_voice"] = min(kinds, key=VOICE_RANK.index)
    elif "Sub" in codes:
        out["best_voice"] = "none"
        out["subs_only"] = True
    if "Sub" in codes and kinds:
        w.append("'Sub' together with voice codes")
    if not codes:
        if out["studios"]:
            w.append("no voice codes in name, only studio names")
        elif out["has_ukr"]:
            w.append("no Russian voice codes in name, only UKR")
        else:
            w.append("no voice codes in name")
    out["studios"] = _uniq(canonical_studio(x) for x in out["studios"] if _norm_space(x))
    for k in ("platforms", "disc_labels", "transfer", "lang_tags", "misc_tags", "upscale_hints"):
        out[k] = _uniq(out[k])
    if out["audio_source"]:
        w.append(f"audio from cinema recording: {out['audio_source']}")
    if out.get("dv_profile_inferred") and re.search(r"(?i)remux", s):
        # «UHD BDRemux … HDR10 | Dolby Vision TV»: a remux is the disc's own stream (47–75 GB, owner 2026-10-25: «по весу
        # можешь понять, что это за раздача»), and UHD discs carry DV on an HDR10 base — Profile 5 would need a re-encode
        out["dv_profile"], out["dv_profile_inferred"] = None, False
        w.append("'Dolby Vision TV' on a remux: not Profile 5 (disc stream, HDR10 base)")
    return out


# --------------------------------------------------------------------------------------------
# Page helpers
# --------------------------------------------------------------------------------------------

def _to_text(fragment: str) -> str:
    t = re.sub(r"(?i)<br\s*/?>|</p>|</div>|<hr[^>]*>|</li>|</tr>|</h\d>", "\n", fragment or "")
    t = re.sub(r"<[^>]+>", "", t)
    t = _html.unescape(t).replace("\xa0", " ").replace("\r", "")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in t.split("\n")]
    return "\n".join(lines)


def _split_details(page: str) -> dict:
    """Return h1 text, btih, description html and meta rows of a rutor torrent page."""
    res = {"h1": None, "btih": None, "desc_html": "", "meta": {}}
    m = re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S | re.I)
    if m:
        res["h1"] = _norm_space(_html.unescape(re.sub(r"<[^>]+>", "", m.group(1))))
    i = page.find('id="details"')
    region = page[i:] if i >= 0 else page
    m = re.search(r"magnet:\?xt=urn:btih:([0-9a-fA-F]{40}|[A-Z2-7]{32})", page[: i if i >= 0 else len(page)])
    if m:
        res["btih"] = m.group(1).lower()
    j = region.find('<td class="header"')
    desc = region[:j] if j >= 0 else region
    res["desc_html"] = desc
    meta_html = region[j:] if j >= 0 else ""
    k = meta_html.find('id="index"')  # "Связанные раздачи" — other releases, never parsed
    meta_main = meta_html[:k] if k >= 0 else meta_html
    for mm in re.finditer(r'<td class=["\']header["\'][^>]*>(.*?)</td>\s*<td[^>]*>(.*?)</td>', meta_main, re.S):
        key = _norm_space(_html.unescape(re.sub(r"<[^>]+>", "", mm.group(1))))
        val = _norm_space(_html.unescape(re.sub(r"<[^>]+>", " ", mm.group(2))))
        res["meta"][key] = val
    mf = re.search(r"Файлы\s*\((\d+)\)", meta_html)
    if mf:
        res["meta"]["files_count"] = int(mf.group(1))
    return res


def _spoilers(desc_html: str) -> tuple[str, list[tuple[str, str]]]:
    """Split description HTML into main text (spoilers removed) and [(head, body_text)]."""
    spoilers = []
    pat = re.compile(
        r'<div class="hidewrap">\s*<div class="hidehead"[^>]*>(.*?)</div>\s*<div class="hidebody">\s*</div>\s*'
        r'<textarea class="hidearea">(.*?)</textarea>\s*</div>', re.S)

    def repl(m):
        spoilers.append((_norm_space(_to_text(m.group(1))), _to_text(m.group(2))))
        return "\n"

    main = pat.sub(repl, desc_html)
    # stray textareas (malformed spoilers)
    def repl2(m):
        spoilers.append(("", _to_text(m.group(1))))
        return "\n"
    main = re.sub(r"<textarea[^>]*>(.*?)</textarea>", repl2, main, flags=re.S)
    return _to_text(main), spoilers


# ---- MediaInfo ----

_MI_SECTION_RE = re.compile(
    r"^(General|Общее|Общая информация|Video|Видео|Audio|Аудио|Text|Текст|Subtitle|Субтитры|Menu|Меню|"
    r"Chapters|Главы|Image|Изображение|Other|Другое)(?:\s*(?:#|№)\s*(\d+))?\s*:?\s*$", re.I)
_MI_KV_RE = re.compile(r"^([^:]{1,60}?)\s*:\s?(.*)$")
_MI_SECTION_KIND = {
    "general": "general", "общее": "general", "общая информация": "general", "video": "video",
    "видео": "video", "audio": "audio", "аудио": "audio", "text": "text", "текст": "text",
    "subtitle": "text", "субтитры": "text", "menu": "menu", "меню": "menu", "chapters": "menu",
    "главы": "menu", "image": "image", "изображение": "image", "other": "other", "другое": "other",
}
_MI_KEYS = {
    "format": ["format", "формат"],
    "format_info": ["format/info", "формат/информация"],
    "format_profile": ["format profile", "профиль формата"],
    "format_features": ["format additionalfeatures", "format_additionalfeatures", "дополнительные особенности формата",
                        "format settings"],
    "commercial": ["commercial name", "коммерческое название"],
    "hdr_format": ["hdr format", "формат hdr"],
    "codec_id": ["codec id", "идентификатор кодека"],
    "bitrate": ["bit rate", "битрейт"],
    "nominal_bitrate": ["nominal bit rate", "номинальный битрейт"],
    "overall_bitrate": ["overall bit rate", "общий битрейт"],
    "width": ["width", "ширина"],
    "height": ["height", "высота"],
    "bit_depth": ["bit depth", "битовая глубина"],
    "transfer": ["transfer characteristics", "характеристики трансфера", "характеристики передачи"],
    "primaries": ["color primaries", "основные цвета"],
    "matrix": ["matrix coefficients", "коэффициенты матрицы"],
    "frame_rate": ["frame rate", "частота кадров"],
    "channels": ["channel(s)", "каналы", "channels"],
    "title": ["title", "заголовок", "название"],
    "language": ["language", "язык"],
    "default": ["default", "по умолчанию"],
    "forced": ["forced", "принудительно"],
    "service_kind": ["service kind", "вид сервиса"],
    "complete_name": ["complete name", "полное имя"],
    "movie_name": ["movie name", "название фильма"],
    "duration": ["duration", "продолжительность"],
    "writing_library": ["writing library", "библиотека кодирования"],
    "encoding_settings": ["encoding settings", "параметры кодирования", "настройки кодирования"],
    "hdr10plus": ["hdr10+"],
}
_MI_KEY_LOOKUP = {alias: k for k, aliases in _MI_KEYS.items() for alias in aliases}


def _parse_mediainfo(text: str) -> list[dict]:
    """Find MediaInfo dumps in text. Returns list of {general, video[], audio[], text[]} blocks."""
    lines = [ln.strip() for ln in (text or "").split("\n")]
    blocks: list[dict] = []
    cur_block = None
    cur = None
    for idx, ln in enumerate(lines):
        if not ln:
            continue
        sm = _MI_SECTION_RE.match(ln)
        if sm:
            kind = _MI_SECTION_KIND[sm.group(1).lower()]
            # a section header must be followed by "key : value" lines
            nxt = next((x for x in lines[idx + 1: idx + 4] if x), "")
            if not _MI_KV_RE.match(nxt):
                continue
            # a new dump starts at "General", or at an un-numbered / "#1" Video header when the current dump
            # already has a video track (several files pasted without "General" sections)
            new_dump_video = kind == "video" and cur_block is not None and cur_block["video"] \
                and (sm.group(2) in (None, "1"))
            if kind == "general" or cur_block is None or new_dump_video:
                cur_block = {"general": {}, "video": [], "audio": [], "text": [], "menu": [], "_n": len(blocks)}
                blocks.append(cur_block)
            cur = {"_kind": kind}
            if kind == "general":
                cur_block["general"] = cur
            elif kind in ("video", "audio", "text"):
                cur_block[kind].append(cur)
            else:
                cur_block.setdefault(kind, []).append(cur)
            continue
        if cur is None:
            continue
        kv = _MI_KV_RE.match(ln)
        if not kv:
            continue
        key = re.sub(r"\s+", " ", kv.group(1)).strip().lower()
        k = _MI_KEY_LOOKUP.get(key)
        if k and k not in cur:
            cur[k] = kv.group(2).strip()
    # drop blocks with no useful content
    return [b for b in blocks if b["video"] or b["audio"] or b["general"].get("format")]


def _mi_kbps(val: str | None) -> int | None:
    if not val:
        return None
    n = _num(val)
    if n is None:
        return None
    v = _lat(val).lower()
    if re.search(r"mb/s|mbps|мбит|mбит|mbit", v):
        return int(round(n * 1000))
    if re.search(r"kb/s|kbps|кбит|kбит|kbit", v):
        return int(round(n))
    if re.search(r"b/s|бит/с", v):
        return int(round(n / 1000))
    return None


def _mi_int(val: str | None) -> int | None:
    n = _num(val) if val else None
    return int(n) if n is not None else None


def _mi_lang(val: str | None) -> str | None:
    if not val:
        return None
    v = val.strip()
    c = _detect_lang(v)
    if c:
        return c
    m = re.match(r"^([a-z]{2,3})(?:-[A-Za-z]{2})?$", v)
    if m:
        return _LANG_CODE_MAP.get(m.group(1), m.group(1))
    return None


def _mi_yes(val: str | None) -> bool:
    return bool(val) and bool(re.match(r"(?i)\s*(yes|да)", val))


# ---- HDR / DV ----

def _hdr_from_mi_video(v: dict) -> dict:
    """Interpret the MediaInfo video section (HDR format, transfer, matrix)."""
    hf = v.get("hdr_format") or ""
    tr = (v.get("transfer") or "").lower()
    mx = (v.get("matrix") or "").lower()
    out = {"hdr": [], "dv_profile": None, "dv_profile_full": None, "dv_compat": None, "dv_layers": None}
    if re.search(r"dolby\s*vision", hf, re.I):
        out["hdr"].append("DV")
        m = re.search(r"profile\s*(\d{1,2})(?:\.(\d{1,2}))?", hf, re.I)
        m2 = re.search(r"dv(?:he|h1|av|a1|1|hb)\.(\d{2})\.(\d{2})", hf, re.I) or re.search(r"\bdav1\.(\d{2})\.(\d{2})", hf, re.I)
        if m:
            out["dv_profile"] = int(m.group(1))
            out["dv_profile_full"] = m.group(1) + ("." + m.group(2) if m.group(2) else "")
        elif m2:
            out["dv_profile"] = int(m2.group(1))
            out["dv_profile_full"] = str(int(m2.group(1)))
        ml = re.search(r"\b(BL\+EL\+RPU|BL\+RPU|EL\+RPU|BL\+EL)\b", hf)
        if ml:
            out["dv_layers"] = ml.group(1)
        if re.search(r"hdr10\s*compatible|blu-?ray\s*compatible", hf, re.I) or re.search(r"smpte st 2086", hf, re.I):
            out["dv_compat"] = "HDR10"
        elif re.search(r"sdr\s*compatible", hf, re.I):
            out["dv_compat"] = "SDR"
        elif re.search(r"hlg\s*compatible", hf, re.I):
            out["dv_compat"] = "HLG"
        if out["dv_profile"] == 8 and out["dv_profile_full"] == "8" and out["dv_compat"]:
            out["dv_profile_full"] = {"HDR10": "8.1", "SDR": "8.2", "HLG": "8.4"}[out["dv_compat"]]
    if re.search(r"2094\s*app\s*4|hdr10\+|hdr10 plus", hf, re.I) or v.get("hdr10plus"):
        out["hdr"].append("HDR10+")
    p5 = out["dv_profile"] == 5 or "ipt" in mx
    if "pq" in tr or "2084" in tr or re.search(r"smpte st 2086|version hdr10|hdr10\b", hf, re.I):
        if not p5:
            out["hdr"].append("HDR10")
    if "hlg" in tr or "arib std-b67" in tr:
        out["hdr"].append("HLG")
    if "ipt" in mx and out["dv_profile"] is None and "DV" in out["hdr"]:
        out["dv_profile"] = 5
        out["dv_profile_full"] = "5"
    return out


# ---- R31: Dolby Vision rebuilt from HDR10 / HDR10+ (not the studio's RPU) ----
# MediaInfo cannot tell a generated RPU from a studio one (no L1 / CM version in its output; checked on all 1013 cached
# DV pages, 2026-10-24) — only the uploader's own words do. Signals seen in cached rutor descriptions:
#   «DV metadata is generated in DaVinci Resolve» (10767)                       -> 1 (rebuilt from the HDR10 grade)
#   «Dolby vision RPU from dynamic HDR+ metadata» (16578),
#   «Dolby vision RPU converted from dynamic HDR10+ metadata» (39388)          -> 2 (converted from HDR10+)
# The remaining patterns are the usual wordings of the same thing (dovi_tool generate, CM v2.9/v4.0 from static
# metadata, «DV из HDR10», «сгенерированный DV», «статические метаданные»): none occurs in the cache yet.
# Not signals: hybrid releases «Web-DL HDR + Web-DL Dolby vision RPU» (a real RPU from another source), «Blu-ray
# compatible» on Profile 8 (P7 -> P8.1 conversion keeps the studio RPU).
_DV_WORD = r"\b(?:DV|DoVi|Dolby\s*Vision|RPU)\b"
_DV_FROM_HDR10P_RE = re.compile(
    _DV_WORD + r"\W{0,3}(?:[\w-]+\W+){0,4}?(?:from|из|на\s+основе|based\s+on)\s+(?:the\s+)?(?:dynamic\s+|динамическ\w*\s+)?"
    r"(?:метадан\w*\s+)?HDR\s*(?:10)?\s*(?:\+|plus\b)"
    r"|HDR\s*10\s*(?:\+|plus\b)\s*(?:->|→|=>|>|\bto\b|\bв\b)\s*" + _DV_WORD, re.I)
_DV_FROM_HDR10_RE = re.compile(
    _DV_WORD + r"\W{0,3}(?:[\w-]+\W+){0,4}?(?:from|из|на\s+основе|based\s+on)\s+(?:the\s+)?(?:static\s+|статическ\w*\s+)?"
    r"(?:метадан\w*\s+)?HDR\s*(?:10)?(?!\s*(?:\+|plus\b))(?![\w+])"
    r"|HDR\s*10(?!\s*(?:\+|plus\b))\s*(?:->|→|=>|>|\bto\b|\bв\b)\s*" + _DV_WORD +
    r"|" + _DV_WORD + r"\W{0,3}(?:[\w-]+\W+){0,3}?(?:is\s+|was\s+)?(?:generated|сгенерир\w*|синтезир\w*)\b"
    r"|(?:generated|сгенерир\w*|синтезир\w*|fake|фейков\w*|псевдо)[\s-]+(?:[\w-]+\s+){0,2}?" + _DV_WORD +
    r"|dovi[_\s-]?tool\s+generate|\bCM\s*v?\s*(?:2\.9|4\.0)\b[^\n]{0,40}(?:static|статическ|generat|сгенерир)"
    r"|" + _DV_WORD + r"[^\n]{0,40}(?:static\s+metadata|статическ\w*\s+метадан\w*)"
    r"|(?:static\s+metadata|статическ\w*\s+метадан\w*)[^\n]{0,40}" + _DV_WORD, re.I)


def dv_rebuild(text: str) -> tuple[int | None, str | None]:
    """(dv_static_md, evidence): 2 — DV converted from HDR10+, 1 — DV rebuilt from HDR10 (static metadata / generated
    RPU), None — no sign (the RPU is presumed the studio's)."""
    if not text:
        return None, None
    m = _DV_FROM_HDR10P_RE.search(text)
    if m:
        return 2, _norm_space(m.group(0))[:160]
    m = _DV_FROM_HDR10_RE.search(text)
    if m:
        return 1, _norm_space(m.group(0))[:160]
    return None, None


_TEXT_HDR_PATTERNS = [
    (re.compile(r"hdr10\s*\+|hdr10\s*plus|hdr\+10|hdr10plus", re.I), "HDR10+"),
    (re.compile(r"dolby\s*vision|(?<![\w])dv(?![\w])|dovi", re.I), "DV"),
    (re.compile(r"(?<![\w])hdr\s*10(?![\w+])", re.I), "HDR10"),
    (re.compile(r"(?<![\w])hlg(?![\w])", re.I), "HLG"),
]


def _hdr_from_text(t: str) -> dict:
    res = {"hdr": [], "dv_profile": None, "generic_hdr": False, "sdr": False}
    if not t:
        return res
    for rx, val in _TEXT_HDR_PATTERNS:
        if rx.search(t):
            res["hdr"].append(val)
    if re.search(r"(?<![\w])hdr(?![\w\d+])", t, re.I):
        res["generic_hdr"] = True
    if re.search(r"(?<![\w])sdr(?![\w])", t, re.I):
        res["sdr"] = True
    m = re.search(r"(?:dolby\s*vision|(?<![\w])dv)\s*(?:profile|prof\.?|p)\s*(\d{1,2})", t, re.I) \
        or re.search(r"dvhe\.(\d{2})", t, re.I)
    if m:
        res["dv_profile"] = int(m.group(1))
    return res


# ---- description label normalisation ----

_LABELS = [
    ("title_original", r"оригинальное название|название оригинала|original title|оригинал\.? название|ориг\.? название"),
    ("title_ru", r"(?:русское )?название(?: фильма| сериала| мультфильма)?|title"),
    ("year", r"год(?: выхода| выпуска| производства| премьеры| релиза)?|years?"),
    ("countries", r"страна|страны|страна производства|страны производства|country"),
    ("production", r"производство"),
    ("released_by", r"выпущено"),
    ("studios", r"студия|студии|кинокомпания|кинокомпании|production company"),
    ("genres", r"жанр|жанры|genre"),
    ("directors", r"режисс[её]р|режисс[её]ры|режисс[её]р-постановщик|постановщик|director|directors"),
    ("cast", r"в ролях|в главных ролях|роли озвучивали|роли озвучивают|роли дублировали|актеры|актёры|актеры озвучивания|cast|starring"),
    ("runtime", r"продолжительность|длительность|время|runtime|duration"),
    ("translation", r"перевод(?: \d+)?|озвучивание|озвучка|перевод и озвучивание|язык озвучки|озвучание|translation"),
    ("audio", r"(?:аудио|звук|audio)(?:\s*(?:#|№)?\s*\d+)?(?:\s+[a-zа-я]{2,3})?|аудиодорожк\w*(?:\s*\d+)?|дорожк\w*\s*\d+"),
    ("subs", r"субтитры|субтитры \d+|subtitles?"),
    ("subs_format", r"формат субтитров"),
    ("video", r"видео|video"),
    ("quality", r"качество|качество видео|quality"),
    ("codec", r"кодек|видеокодек|видео кодек"),
    ("container", r"формат|контейнер|container"),
    ("edition_info", r"издание|версия|edition"),
    ("source_file", r"исходник|source"),
    ("release_author", r"автор релиза|релиз от|рип от|релиз|автор рипа|релизер|рип|релиз группы"),
    ("note", r"примечание|примечания|реклама|доп\.? информация"),
    ("season", r"сезон|сезоны|season"),
    ("episodes", r"серии|эпизоды|количество серий|кол-во серий|серий|episodes"),
    ("premiere_world", r"премьера в мире|премьера \(мир\)|мировая премьера|премьера"),
    ("digital_release", r"цифровой релиз|цифровая премьера|digital release"),
    ("format_3d", r"формат 3d"),
    ("imdb", r"imdb"),
]
_LABEL_RES = [(k, re.compile(r"^(?:" + p + r")$", re.I)) for k, p in _LABELS]
_ITEM_LABEL_RE = re.compile(r"^(?:#\s*)?(\d{1,2})[.):]?$")


def _label_key(label: str) -> str | None:
    lab = re.sub(r"^[\s*•►\-]+", "", label or "").strip().lower().replace("ё", "е")
    lab = re.sub(r"\s+", " ", lab)
    lab = re.sub(r"\s*\(.*\)$", "", lab)
    for k, rx in _LABEL_RES:
        if rx.match(lab.replace("е", "е")):
            return k
    return None


def _desc_fields(text: str) -> dict[str, list]:
    """Collect labelled values from description text. Lists keep order; numbered sub-items are attached
    to the last list-type label (audio / subs / translation)."""
    fields: dict[str, list] = {}
    last_list = None
    last_empty = False
    for ln in (text or "").split("\n"):
        ln = ln.strip()
        if not ln:
            continue
        m = re.match(r"^([^:]{1,45}?)\s*:\s*(.*)$", ln)
        key = None
        if m:
            label = m.group(1)
            if _ITEM_LABEL_RE.match(label.strip()) and last_list:
                fields[last_list].append((f"#{label.strip()}", m.group(2).strip()))
                continue
            if re.match(r"(?i)^сезон\s*\d+$", label.strip()) and last_list and not m.group(2).strip():
                fields[last_list].append(("season_header", ln))
                continue
            key = _label_key(label)
            if key == "title_ru" and re.search(r"(?i)^\W*title", label) and "title_ru" in fields:
                key = None
        if not key:
            # «<b>Перевод </b>Любительский … - Jetvis Studio», «<b>Звук </b>AC3, 192 kbps … (RU)» — a label without a colon
            # (review 2026-10-28: 14 pages, «Ферма Кларксона» S5 hidden «нет озвучки»); only the track labels, only with a
            # value that reads as a track / a voice kind
            nc = re.match(r"(?i)^(перевод|озвучка|озвучивание|звук|аудио|субтитры)\s+([^:].{2,})$", ln)
            if nc:
                k2 = _label_key(nc.group(1))
                v2 = nc.group(2).strip()
                if (k2 == "translation" and _kind_in(v2)[0]) or (k2 == "audio" and (_norm_acodec(v2) or _detect_lang(v2))) \
                        or (k2 == "subs" and _detect_lang(v2)):
                    m = re.match(r"^(.*?)\s+(.*)$", ln)
                    key = k2
        if key:
            val = m.group(2).strip()
            # "Перевод: Перевод 1: ..." -> unwrap
            mm = re.match(r"^([^:]{1,30}?)\s*:\s*(.*)$", val)
            if mm and _label_key(mm.group(1)) == key:
                val = mm.group(2).strip()
            fields.setdefault(key, []).append((m.group(1).strip(), val))
            last_list = key if key in ("audio", "subs", "translation") else None
            last_empty = not val
            continue
        if last_list:
            numbered = re.match(r"^(?:\d{1,2}[.)]?\s|#\s*\d)", ln)
            trackish = bool(_norm_acodec(ln) or _detect_lang(ln) or _kbps_in(ln) or _kind_in(ln)[0]
                            or re.search(r"(?i)forc|форс", ln))
            if numbered and (last_empty or trackish):
                fields[last_list].append(("#", ln))
                continue
            if last_empty and trackish and len(ln) < 200:
                fields[last_list].append(("#", ln))
                continue
        last_list = None
    return fields


def _split_names(val: str) -> list[str]:
    if re.search(r"(?i)укажите|через запятую", val or ""):
        return []  # template placeholder left by the uploader
    val = re.sub(r"(?i)\s*(?:и др(?:угие)?\.?|и другие|etc\.?|\.\.\.|…)\s*$", "", val or "")
    items = [_norm_space(x) for x in re.split(r",|;", val)]
    res = []
    for it in items:
        if not it or re.search(r"(?i)укажите", it):
            continue
        it = it.split(" / ")[0].strip()
        res.append(it)
    return res


def _runtime(val: str) -> dict:
    v = (val or "").replace("~", " ").strip()
    out = {"minutes": None, "episode_minutes": None, "episodes": None}
    m = re.search(r"(\d+)\s*(?:x|х|×)\s*~?\s*(\d{1,2}):(\d{2}):(\d{2})", v)
    if m:
        out["episodes"] = int(m.group(1))
        out["episode_minutes"] = round(int(m.group(2)) * 60 + int(m.group(3)) + int(m.group(4)) / 60)
        return out
    m = re.search(r"(\d+)\s*сери\w*\s*по\s*~?\s*(\d+)\s*мин", v, re.I)
    if m:
        out["episodes"] = int(m.group(1)); out["episode_minutes"] = int(m.group(2))
        return out
    per_ep = bool(re.search(r"сери[яи]|эпизод|episode|по ", v, re.I))
    m = re.search(r"(\d{1,2}):(\d{2}):(\d{2})", v)
    mins = None
    if m:
        mins = round(int(m.group(1)) * 60 + int(m.group(2)) + int(m.group(3)) / 60)
    else:
        m = re.search(r"(\d{1,2})\s*(?:ч|h)\.?\s*(\d{1,2})\s*(?:мин|м|min)", v, re.I)
        if m:
            mins = int(m.group(1)) * 60 + int(m.group(2))
        else:
            m = re.search(r"(\d{1,2}):(\d{2})(?!:)", v)
            if m:
                mins = int(m.group(1)) if int(m.group(1)) > 9 and not per_ep else int(m.group(1)) * 60 + int(m.group(2))
                if per_ep or int(m.group(1)) >= 10:
                    mins = int(m.group(1)) + round(int(m.group(2)) / 60) if int(m.group(1)) >= 10 else mins
            else:
                m = re.search(r"(\d{2,3})\s*(?:мин|min|м\b)", v, re.I)
                if m:
                    mins = int(m.group(1))
    if per_ep:
        out["episode_minutes"] = mins
    else:
        out["minutes"] = mins
    return out


def _mi_duration_min(val: str | None) -> int | None:
    if not val:
        return None
    v = val.lower()
    h = re.search(r"(\d+)\s*(?:h|ч)", v)
    # older MediaInfo writes «1h 37mn» (review round 11: read as 60 min — «Гадкий я 2», «Заложница 3» …)
    mi = re.search(r"(\d+)\s*(?:min|mn|м(?:ин)?\.?)(?!s)", v)
    s = re.search(r"(\d+)\s*(?:s\b|с\.|сек)", v)
    if not (h or mi):
        m = re.match(r"(\d{1,2}):(\d{2}):(\d{2})", v)
        if m:
            return round(int(m.group(1)) * 60 + int(m.group(2)) + int(m.group(3)) / 60)
        return None
    total = (int(h.group(1)) * 60 if h else 0) + (int(mi.group(1)) if mi else 0)
    if s and int(s.group(1)) >= 30:
        total += 1
    return total


# ---- audio line parsing ----

_TECH_STRIP = re.compile(
    r"(?i)(\d+[.,]?\d*\s*k?hz|~?\s*\d[\d\s]*(?:[.,]\d+)?\s*(?:kbps|kb/s|kbs|kbit/s|кбит/с\w*|kбит/с\w*|mbps|mb/s|мбит/с\w*)"
    r"|\b\d+\s*(?:bits?|бит)\b|\d+-bit|\b[1-9]\.[0-2](?:\+\d+\s*objects)?(?:-ex)?\b|\b\d{1,2}\s*ch\b|\d\s*/\s*\d(?:\s*/\s*\d)?\s*(?:\([^)]*\))?\s*(?:\+\s*lfe)?\s*(?:ch)?"
    r"|\bavg\b|\bjoc\b|atmos|\bcore\b|dolby\s*digital(?:\s*plus)?|dolby|truehd|true hd|dts(?:-hd)?(?:\s*(?:master\s*audio|ma|hra|high resolution(?:\s*audio)?|es|:x|-x))?|audio"
    r"|e-?ac-?3|eac3|ac-?3|aac(?:[-\s]?(?:lc|he))?|he-?aac|flac|l?pcm|opus|mp[23]|vorbis|embedded"
    r"|\b[1-9]\.[0-2]\s*ch\b|\bch\b|\blossy\b|\blossless\b|\bstereo\b|\bmono\b|\bkhz\b|\bhz\b|\br5\b|\bddp?\b|dd\+"
    r"|\bvbr\b|\bcbr\b|телеканал\w*|эпизод\w*\s*\d*|сери[яи]\s*\d*|\bsdh\b|\bchannels?\b|\bchnls\b"
    r"|\bbd\s*(?:eur|usa|us|rus|uk|ger)\b|\blinein\b|\bcustom\b|\bobjects?\b|•"
    r"|\d*[.,]?\d*\s*[кkмm]бит/?[сc]?\w*|\d*\s*[кkмm]б/[сc])")


def _studio_from_text(text: str, known: list[str]) -> str | None:
    """Pick a studio name from a free-text track description."""
    t = _fix_tech(text or "")
    tk = _key(t)
    best = None
    for name in sorted(_uniq(known), key=len, reverse=True):
        k = _key(name)
        if len(k) >= 3 and k in tk:
            best = name
            break
    if best:
        return canonical_studio(best)
    # «1+1» — the Ukrainian channel / dubbing studio (toloka logo «1+1.gif», 2026-10-26): the age-mark rule below («16+»,
    # «18+») would eat it as a number
    if re.search(r"(?<![\w+])1\s*\+\s*1(?![\w+])", t):
        return "1+1"
    s = _TECH_STRIP.sub(" ", t)
    for rx, _ in _LANG_WORD_RES:
        s = rx.sub(" ", s)
    s = _LANG_CODE_RE.sub(" ", s)
    s = re.sub(r"(?i)" + _W + r"(?:dub|dubbed|mvo|dvo|avo|svo|vo|lmvo|ldvo|lvo|original|orig|оригинал\w*|дубляж|дубл\.|"
               r"дублированн\w*|дублир\w*|многоголос\w*|многогол\.|двухголос\w*|двуголос\w*|одноголос\w*|авторск\w*|"
               r"закадров\w*|профессиональн\w*|любительск\w*|неофициальн\w*|официальн\w*|перевод|озвучк\w*|"
               r"озвучивание|по заказу|звук|дорожка|язык|оригинальная|default|forced|\d+\+|18\+|"
               r"с ненормативной лексикой|ненормативн\w*|цензур\w*|без цензуры)" + _E, " ", s)
    s = re.sub(r"[|/\[\](){}:;,\"«»]+", " ", s)
    s = re.sub(r"(?:^|\s)[-–—](?=\s|$)", " ", s)
    s = re.sub(r"(?:^|\s)\d+(?:[.,]\d+)?(?=\s|$)", " ", s)
    s = _norm_space(s).strip(" .-–—#№")
    if not s or not re.search(r"[A-Za-zА-Яа-яЁё]{2,}", s) or len(s) > 60:
        return None
    return canonical_studio(s)


def _parse_audio_line(label: str, val: str, known: list[str]) -> dict:
    full = _fix_tech(val or "")
    lab_lang = _detect_lang(label) if label and not label.startswith("#") else None
    lang = lab_lang or _detect_lang(full)
    codec = _norm_acodec(full)
    kind, extra = _kind_in(full)
    tr = {
        "lang": lang, "kind": kind, "studio": None, "codec": codec, "channels": _channels_in(full),
        "kbps": _kbps_in(full), "title": None, "raw": _norm_space(f"{label}: {val}" if label and not label.startswith("#") else val),
        "atmos": bool(re.search(r"atmos|joc", full, re.I)),
    }
    if extra.get("official") is not None:
        tr["official"] = extra["official"]
    if extra.get("voices"):
        tr["voices"] = extra["voices"]
    if kind not in ("original", "commentary") and lang in ("ru", "uk", None):
        tr["studio"] = _studio_from_text(full, known)
    elif kind == "commentary":
        tr["title"] = _norm_space(val)
    return tr


def _parse_translation_line(val: str, known: list[str]) -> list[dict]:
    """'Перевод' values. Either 'kind | studio' or a comma list of kinds."""
    v = _norm_space(_fix_tech(val))
    if not v or re.match(r"(?i)^(нет|отсутствует|не требуется|не нужен)\b", v):
        return []
    # Split on commas / ';' / ' + ' outside parentheses; a piece that names a kind starts a new entry,
    # pieces without a kind are extra studios of the previous entry ("Дубляж VSI Moscow, Videofilm Int.").
    pieces = [p.strip() for p in re.split(r"[,;](?![^()]*\))|\s\+\s", v) if p.strip()]
    groups: list[list[str]] = []
    for p in pieces:
        if _kind_in(p)[0] or not groups:
            groups.append([p])
        else:
            groups[-1].append(p)
    res = []
    for g in groups:
        text = ", ".join(g)
        k, ex = _kind_in(text)
        lang = _detect_lang(text) or "ru"
        studios = []
        for i, p in enumerate(g):
            part = p
            if i == 0:
                mm = re.split(r"\s+[|–—-]\s+|\s*\|\s*", p, maxsplit=1)
                part = mm[1] if len(mm) == 2 else p
            st = _studio_from_text(part, known)
            if st:
                studios.append(st)
        studios = [s for s in _uniq(studios) if not re.fullmatch(r"(?i)закадровый|многоголосый", s)]
        res.append({"kind": k, "lang": lang, "studio": studios[0] if studios else None,
                    "studios": studios, "raw": text, **ex})
    return res


def _sub_forced_langs(text: str) -> tuple[list[str], list[str]]:
    """Parse a 'Субтитры' description value -> (all languages, languages with forced subs)."""
    t = _norm_space(text)
    if not t or re.match(r"(?i)^(нет|отсутствуют|без субтитров|no|none)\b", t):
        return [], []
    groups = re.split(r",(?![^()]*\))|\+(?![^()]*\))|;(?![^()]*\))", t)
    langs, forced = [], []
    cur_lang = None
    for g in groups:
        g = g.strip()
        if not g:
            continue
        lang = _detect_lang(g)
        if re.search(r"(?i)оригинал|original", g) and not lang:
            lang = "orig"
        if lang:
            cur_lang = lang
        if cur_lang:
            langs.append(cur_lang)
            if re.search(r"(?i)forc|форс|принуд|надпис", g):
                forced.append(cur_lang)
    return _uniq(langs), _uniq(forced)


# --------------------------------------------------------------------------------------------
# parse_release_page
# --------------------------------------------------------------------------------------------

_EDITION_TEXT = [
    (re.compile(r"open[\s._-]?matte", re.I), "open_matte"),
    (re.compile(r"extended|расширенн", re.I), "extended"),
    (re.compile(r"director'?s[\s._-]?cut|режисс[её]рск", re.I), "directors_cut"),
    (re.compile(r"theatrical|театральн", re.I), "theatrical"),
    (re.compile(r"(?<![\w])imax(?![\w])", re.I), "imax"),
    (re.compile(r"remaster|реставр|ремастер", re.I), "remastered"),
    (re.compile(r"(?<![\w])uncut(?![\w])", re.I), "uncut"),
    (re.compile(r"(?<![\w])unrated(?![\w])", re.I), "unrated"),
]

# ---- release rules from the owner's metadata survey (2026-10-25): stored per release, used by films/rank.py ----
# (19) built-in ads: «Реклама: Отсутствует», «Без рекламы» / «Реклама - есть (пару видеовставок)», «Присутствует
# аудио-реклама», «Релиз содержит рекламу»; long lines are plot text («рекламщик», «рекламного плаката») and are skipped
_ADS_NO_RE = re.compile(
    r"без\s+(?:какой[\s-]*либо\s+)?рекламы|реклам\w*[\s:,\-–—]*(?:любого\s+вида[\s,]*)?(?:полностью\s+)?(?:отсутству|нет\b)"
    r"|рекламы\s*[-–—:]?\s*нет|нет\s+рекламы", re.I)
_ADS_YES_RE = re.compile(
    r"присутству\w*\s+(?:аудио[\s-]*|видео[\s-]*)?реклам|реклам\w*\s*[:\-–—]\s*(?:есть|присутству|одна|\d)|содержит\s+реклам"
    r"|рекламн\w*\s+(?:аудио[\s-]*|видео[\s-]*)?(?:вставк|баннер|заставк|плашк|ролик)|всплывающ\w*\s+рекламн|реклама\s+есть"
    r"|вшит\w*\s+реклам|встроенн\w*\s+реклам", re.I)
# (20) localized video: «Локализованный видеоряд», «Локализованная версия», «Частичная локализация», «Видео ряд локализован»
_LOCALIZED_RE = re.compile(
    r"локали[зс]\w*\s+(?:чист\w+\s+)?(?:русск\w+\s+)?(?:видео\s*ряд|верси|blu-?ray)|частичн\w*\s+локализ|видео\s*ряд\w*\s+локализ"
    r"|русский\s+видеоряд|локализован\w*\s+в\s+т\.?\s*ч\.?\s+и\s+видеоряд|текст\s+локализован|локализован\w*\s+кадр", re.I)
# (21) 3D: page label «Формат 3D», «Стереоформат», MVC video, «3D-halfOU» in the file name
_3D_TEXT_RE = re.compile(
    r"(?m)^(?:формат 3d|стерео\s*формат)\s*:|mvc\s+video|(?<![\w])3d[\s._-]*(?:half[\s._-]*)?(?:ou|sbs|hou|hsbs)(?![\w])"
    r"|half[\s._-]*(?:overunder|sidebyside|ou|sbs)(?![\w])", re.I)
# (22) TS / TC / CAM picture or sound: «звук с TS», «(Line)», «Дубляж, Вольга (CamRip)», «звука экранки»
_CAM_VOICE_RE = re.compile(
    r"(?<![\w])(?:ts|tc|cam|camrip|telesync|telecine|hdts|hdcam|line(?!\s*-?\s*masters?)|чистый\s+звук)(?![\w])|экранк\w*"
    r"|звук\w*\s+(?:с|из)\s+кинотеатр", re.I)
# (23) remaster / restoration; «Restore Point», «Restoran» are titles — whole words only, file names without folders
_REMASTER_RE = re.compile(
    r"remaster\w*|(?<![\w])restor(?:ed|ation)(?![\w])|реставр\w*|ремастер\w*|mastered\s+in\s+4k|criterion"
    r"|(?<![\w])arrow[\s._-]*(?:video|films|academy)|\(arrow\)|(?<![\w])bfi(?![\w])|despeciali[sz]ed|(?<![\w])4k(?:77|80|83)(?![\w])",
    re.I)
_REMASTER_LABELS = {"criterion": "Criterion", "arrow": "Arrow", "bfi": "BFI"}
# (28) black-and-white / colour versions (tracker text; IMDb colour info is titles.color, applied in films/rank.py)
_BW_RE = re.compile(r"(?<![\w])ч\s*[/\\]\s*б(?![\w])|ч[её]рно[\s-]*бел\w*\s+верси|black\s*(?:&|and)\s*(?:white|chrome)"
                    r"|noir[\s._]*(?:edition|ver(?:sion)?)(?![\w])|(?<![\w])чб(?![\w])", re.I)
_COLORIZED_RE = re.compile(r"колориз\w*|colou?ri[sz]ed|цветн\w+\s+верси|colou?r[\s._]+ver(?:sion)?(?![\w])", re.I)
# (39) «для КПК / телефона»
_KPK_RE = re.compile(r"(?<![\w])(?:кпк|pda|ipod|iphone|3gp|psp)(?![\w])|на\s+мобильн\w*|для\s+(?:мобильн|телефон|смартфон)\w*",
                     re.I)
# R168(а): an uncensored («18+») voice-over
_ADULT_RE = re.compile(r"18\s*\+|без\s+цензуры|uncensored|ненормативн\w*|нецензурн\w*", re.I)
_ADULT_NOTE_RE = re.compile(r"(?:перевод|озвучк|озвучани|верси)\w*[^\n]{0,40}(?:ненормативн|18\s*\+|без\s+цензуры)"
                            r"|(?:внимание|примечание)[^\n]{0,30}ненормативн", re.I)


def _fps(val: str | None) -> float | None:
    """'59.940 (60000/1001) FPS', '50,000 кадров/сек' → 59.94 / 50.0 (a real video frame rate: 10 … 300)."""
    n = _num(val) if val else None
    return n if n and 10 <= n <= 300 else None


def _release_rules(out: dict, nm: dict | None, main_text: str, spoilers: list, mi: dict | None, fields: dict,
                   w: list) -> None:
    """Owner's survey answers about releases (2026-10-25), stored per release (films/sources/rutor.py `store_rules`):
    has_ads (19, lowers), localized_video (20, raises), is_3d (21, excluded: «нет 3D-ТВ»), cam_source (22, TS/TC/CAM
    picture or sound — excluded completely), remaster (23, «максимальный приоритет всегда»), video_color (28, bw /
    colorized), fps (5, «60 fps — пометка в названии раздачи»), kpk (39, excluded), voice_18plus (R168(а)); per-track
    `adult` / `cam` flags. The official / unofficial dub (25) needs the studio list from config — films/rank.py dub_official."""
    name = (nm or {}).get("name") or out.get("name") or ""
    tags = name.split("|", 1)[1] if "|" in name else ""
    spoiler_text = "\n".join(b for _, b in spoilers)
    all_text = main_text + "\n" + spoiler_text
    short_lines = [ln for ln in all_text.split("\n") if ln.strip() and len(ln) <= 160]
    labelled = [v for k in ("quality", "edition_info", "source_file", "note", "video") for _, v in fields.get(k, [])]
    mi_names = []
    if mi:
        mi_names = [re.split(r"[/\\]", mi["general"].get("complete_name") or "")[-1], mi["general"].get("movie_name") or "",
                    mi["general"].get("title") or ""]
    tech = " | ".join(labelled + mi_names + [out.get("edition_info") or "", out.get("source_file") or ""])

    # (19) ads: an explicit «есть» wins over a template «без рекламы» elsewhere on the page
    # (the older «Примечание» check read «Без какой-либо рекламы» as ads — these patterns decide when they match)
    yes = [ln for ln in short_lines if _ADS_YES_RE.search(ln) and not _ADS_NO_RE.search(ln)]
    if yes:
        out["has_ads"] = True
    elif any(_ADS_NO_RE.search(ln) for ln in short_lines):
        out["has_ads"] = False

    # (20) localized video
    loc = bool(nm and "localized" in nm["edition"]) or bool(_LOCALIZED_RE.search(name)) \
        or any(_LOCALIZED_RE.search(ln) for ln in short_lines)
    out["localized_video"] = loc

    # (21) 3D
    if not out.get("is_3d"):
        mi_3d = bool(mi and any(re.search(r"(?i)\bmvc\b|multiview", " ".join(filter(None, [v.get("format"), v.get("codec_id")])))
                                for v in mi["video"]))
        if mi_3d or _3D_TEXT_RE.search(all_text) or _3D_TEXT_RE.search(" | ".join(mi_names)):
            out["is_3d"] = True

    # (22) TS / TC / CAM: the picture (source type), or the voice the release offers recorded in a cinema — a name tag
    # («| D | TS», «| Line»), or RU/UK voice tracks whose best kind exists only as a cinema recording. A BDRemux whose
    # Russian dub is clean keeps its place even if it also carries extra CamRip Ukrainian dubs (release 45632, «Терминатор 2»).
    def _cam_in(ln: str) -> str | None:
        m = _CAM_VOICE_RE.search(ln or "")
        if not m or re.search(r"(?i)не\s*кинотеатр", ln):
            return None
        if m.group(0).lower() == "line" and (online_line or re.search(r"(?i)он-?\s*лайн|online", ln)):
            return None   # «Line * он-лайн кинотеатр KinoPub» — taken from a streaming service, not recorded in a cinema
        return m.group(0)

    # rutor legend (torrent 140886): «Line (устаревшее обозначение: "Чистый звук") – звук напрямую записан с проектора»,
    # CAM — «звук записанный в кинотеатре при посетителях», «Звук с TS» — all three are cinema sound.
    cam = None
    online_line = bool(re.search(r"(?i)(?<![\w])line(?![\w])[^\n]{0,20}(?:он-?\s*лайн|online)", all_text))
    if out.get("source_type") in ("CAMRip", "TS", "TC"):
        cam = out["source_type"]
    elif nm and nm.get("audio_source") and not (re.search(r"(?i)^(?:line|чистый звук)$", nm["audio_source"]) and online_line):
        cam = "звук: " + nm["audio_source"]
    else:
        # a voice track recorded in a cinema does not count as the release's voice; the release is excluded when no clean
        # RU/UK voice remains. A «Перевод» line marks the tracks of its kind only when it names no clean source next to
        # it («Дублированный (русский "BD CEE", украинский "Line")», release 21775)
        trans_lines = [v for _, v in fields.get("translation", []) if v]
        clean_src = re.compile(r"(?i)(?<![\w])(?:bd|blu-?ray|dvd|r5|web|itunes|лицензи\w*|netflix|amazon)(?![\w])")
        cam_kinds = {_kind_in(v)[0] for v in trans_lines if _cam_in(v) and not clean_src.search(v)}
        clean_kinds = {_kind_in(v)[0] for v in trans_lines if not _cam_in(v)}
        voice = [a for a in out.get("audio") or [] if a.get("lang") in ("ru", "uk", None) and a.get("kind") in VOICE_RANK]
        for a in voice:
            hit = _cam_in(" ".join(str(a.get(k) or "") for k in ("raw", "title", "desc")))
            if hit or (a["kind"] in cam_kinds and a["kind"] not in clean_kinds):
                a["cam"] = hit or next(h for h in (_cam_in(v) for v in trans_lines) if h)
        cam_tr = [a for a in voice if a.get("cam")]
        clean_tr = [a for a in voice if not a.get("cam")]
        if cam_tr and not clean_tr:
            cam = "звук: " + cam_tr[0]["cam"]
        elif cam_tr:
            kinds = [a["kind"] for a in clean_tr]
            best = min(kinds, key=VOICE_RANK.index)
            if best != out.get("best_voice"):
                w.append(f"voice {out.get('best_voice')} only as cinema sound ({cam_tr[0]['cam']}) — best voice {best}")
                out["best_voice"] = best
        elif not voice and cam_kinds:
            cam = "звук: " + next(h for h in (_cam_in(v) for v in trans_lines) if h)
        elif not voice and not trans_lines:
            m = re.search(r"(?i)звук\w*\s+(?:с|из)\s+(ts|tc|cam\w*|экранк\w*|кинотеатр\w*)(?![\w])", all_text)
            if m:
                cam = "звук: " + m.group(1)
    out["cam_source"] = cam

    # (23) remaster / restoration
    rem = None
    if nm and "remastered" in nm["edition"]:
        rem = next((x for x in nm["edition_raw"] + nm["upscale_hints"] if re.search(r"(?i)master|restor|реставр", x)),
                   "Remastered")
    if not rem and nm:
        lab = next((x for x in nm["disc_labels"] if re.search(r"(?i)criterion|arrow|bfi", x)), None)
        if lab:
            rem = lab
    if not rem:
        # a generic «Remaster» only inside a MediaInfo file name is how fan «4K HDR Digital Remasters» of a 1080p disc are
        # named (release 17550) — file names count for the boutique labels only
        labelled_text = " | ".join(labelled + [out.get("edition_info") or "", out.get("source_file") or ""])
        m = _REMASTER_RE.search(tags) or _REMASTER_RE.search(labelled_text)
        if not m:
            m2 = _REMASTER_RE.search(" | ".join(mi_names))
            if m2 and re.search(r"(?i)criterion|arrow|bfi", m2.group(0)):
                m = m2
        if m:
            rem = m.group(0).strip()
    if rem:
        k = next((v for kk, v in _REMASTER_LABELS.items() if kk in rem.lower()), None)
        out["remaster"] = k or rem[:60]
    else:
        out["remaster"] = None

    # (28) black-and-white / colorized
    color = None
    ed = (nm or {}).get("edition") or []
    if "black_and_white" in ed or _BW_RE.search(tags) or _BW_RE.search(tech):
        color = "bw"
    if "colorized" in ed or _COLORIZED_RE.search(tags) or _COLORIZED_RE.search(tech):
        color = "colorized" if color is None else color
    out["video_color"] = color

    # (5) frame rate: MediaInfo video track, else the «Видео:» line, else a «60 fps» name tag
    fps = None
    if mi and mi["video"]:
        fps = _fps(mi["video"][0].get("frame_rate"))
    if fps is None:
        vline = " ".join(v for _, v in fields.get("video", []))
        m = re.search(r"(\d{2,3}(?:[.,]\d+)?)\s*(?:fps|кадр)", vline, re.I)
        fps = _fps(m.group(1)) if m else None
    if fps is None and nm and nm.get("fps_tag"):
        fps = float(nm["fps_tag"])
    out["fps"] = round(fps, 3) if fps else None

    # (39) КПК / phone rips: name tag, «Качество: HDRip | КПК», or a tiny picture (≤ 480 × 272: PSP / phone formats)
    kpk = bool(nm and nm.get("kpk")) or bool(_KPK_RE.search(tags))
    if not kpk:
        kpk = any(_KPK_RE.search(v) for _, v in fields.get("quality", []))
    vw, vh = out.get("video_width"), out.get("video_height")
    if not kpk and vw and vh and vw <= 480 and vh <= 272:
        kpk = True
        w.append(f"tiny picture {vw}x{vh} — treated as a КПК / phone rip")
    out["kpk"] = kpk

    # R168(а): 18+ / uncensored professional voice-over
    for a in out.get("audio") or []:
        if _ADULT_RE.search(" ".join(str(a.get(k) or "") for k in ("raw", "title", "desc"))):
            a["adult"] = True
    pro = [a for a in out.get("audio") or [] if a.get("lang") in ("ru", "uk", None) and a.get("kind") in ("mvo", "dvo", "svo")]
    adult = any(a.get("adult") for a in pro)
    if not adult:
        for _, val in fields.get("translation", []):
            k, _ex = _kind_in(val)
            if k in ("mvo", "dvo", "svo") and _ADULT_RE.search(val) and not re.search(r"(?i)любительск|amateur", val):
                adult = True
                break
    if not adult and pro and any(_ADULT_NOTE_RE.search(ln) for ln in short_lines):
        adult = True
    out["voice_18plus"] = adult


def norm_imdb_id(tconst: str | None) -> str | None:
    """An IMDb id as IMDb writes it: 7 digits, 8 from tt10000000 on. A tracker page sometimes pads it with an extra leading
    zero («tt03748172» = «Игра Джералда» tt3748172, «tt09484998» = «Палм-Спрингс» tt9484998) — a duplicate title was made."""
    if not tconst:
        return tconst
    m = re.fullmatch(r"(?i)tt(\d+)", tconst.strip())
    if not m:
        return tconst
    digits = m.group(1)
    if len(digits) > 7 and digits.startswith("0"):
        digits = digits.lstrip("0").zfill(7)
    return "tt" + digits


def parse_release_page(html: str) -> dict:
    """Parse a rutor release page (see module docstring). Returns a dict; ``warnings`` lists doubts."""
    w: list[str] = []
    page = html or ""
    parts = _split_details(page)
    nm = parse_release_name(parts["h1"]) if parts["h1"] else None
    if not nm:
        w.append("no <h1> release name on page")
    main_text, spoilers = _spoilers(parts["desc_html"])

    out: dict[str, Any] = {
        "name": parts["h1"], "name_parsed": nm, "btih": parts["btih"],
        "category": parts["meta"].get("Категория"), "seeders": None, "leechers": None, "added_at": None,
        "size_bytes": None, "files_count": parts["meta"].get("files_count"),
        # releases columns
        "season": nm["season"] if nm else None, "episodes": nm["episodes"] if nm else None,
        "resolution": None, "source_type": nm["source_type"] if nm else None, "video_codec": None,
        "video_bit_depth": None, "video_kbps": None, "hdr": [], "dv_profile": None, "is_imax_cut": False,
        "edition": [], "is_upscale": None, "audio": [], "best_voice": None, "forced_subs": [],
        # extra
        "video_width": None, "video_height": None, "dv_profile_full": None, "dv_fallback": None,
        "dv_layers": None, "dv5_only": False, "hdr10_from_generic_tag": False, "voice_by_lang": {}, "subtitle_langs": [],
        "imdb_id": None, "mediainfo_found": False, "mediainfo_blocks": 0,
        "title_ru": None, "title_ru_desc": None, "title_original": None, "year": None, "year_to": None,
        "year_desc": None, "countries": [], "genres": [],
        "directors": [], "cast": [], "runtime_min": None, "runtime_episode_min": None, "episodes_count": None,
        "production_studios": [], "release_author": None, "has_ads": None, "premiere_world": None,
        "digital_release": None, "source_file": None, "edition_info": None, "is_3d": bool(nm and nm["is_3d"]),
        "field_sources": {}, "warnings": w,
    }
    meta = parts["meta"]
    if meta.get("Раздают", "").isdigit():
        out["seeders"] = int(meta["Раздают"])
    if meta.get("Качают", "").isdigit():
        out["leechers"] = int(meta["Качают"])
    m = re.match(r"(\d{2})-(\d{2})-(\d{4})\s+(\d{1,2}):(\d{2}):(\d{2})", meta.get("Добавлен", ""))
    if m:
        out["added_at"] = f"{m.group(3)}-{m.group(2)}-{m.group(1)}T{int(m.group(4)):02d}:{m.group(5)}:{m.group(6)}"
    m = re.search(r"\((\d+)\s*Bytes\)", meta.get("Размер", ""))
    if m:
        out["size_bytes"] = int(m.group(1))

    # ---- IDs (description only; the "related releases" block is excluded)
    ids = _uniq(norm_imdb_id(t) for t in re.findall(r"imdb\.com/(?:[a-z]{2}/)?title/(tt\d{7,9})", parts["desc_html"], re.I))
    if not ids:
        ids = _uniq(norm_imdb_id(t) for t in re.findall(r"(?<![\w/])(tt\d{7,9})(?![\w])", parts["desc_html"]))
    if ids:
        out["imdb_id"] = ids[0]
        if len(ids) > 1:
            w.append(f"several IMDb ids in description: {ids}")
    # Kinopoisk is not used in any form (owner, 2026-10-25: «Я из Украины, а это вражеский сайт»)

    # ---- MediaInfo
    mi_blocks = _parse_mediainfo(main_text)
    for head, body in spoilers:
        mi_blocks += _parse_mediainfo(body)
    out["mediainfo_blocks"] = len(mi_blocks)
    mi = mi_blocks[0] if mi_blocks else None
    out["mediainfo_found"] = bool(mi)
    mi_spoiler_heads = [h for h, b in spoilers if re.search(r"(?i)media\s*info|медиа\s*инфо|^m/?i$|^ми$", h)]
    if not mi and mi_spoiler_heads:
        w.append("MediaInfo spoiler present but no parsable MediaInfo inside (empty or image/link)")

    # ---- description fields: main text first, then non-MediaInfo spoilers for missing labels
    fields = _desc_fields(main_text)
    for head, body in spoilers:
        if re.search(r"(?i)скриншот|интересно|награды|знаете ли|содержание|список серий|описание|сюжет|о фильме", head):
            continue
        if _parse_mediainfo(body):
            continue
        extra = _desc_fields(body)
        for k, v in extra.items():
            if k not in fields and k in ("video", "audio", "subs", "translation", "quality", "source_file",
                                         "edition_info", "release_author", "note"):
                fields[k] = v

    def first(key):
        v = fields.get(key)
        return v[0][1] if v else None

    # titles / year
    t_ru = first("title_ru")
    if nm and nm["title_ru"]:
        out["title_ru"] = nm["title_ru"]  # the moderated release title is cleaner than free-form descriptions
        if t_ru and _key(t_ru.split(" / ")[0]) != _key(nm["title_ru"]):
            out["title_ru_desc"] = _norm_space(t_ru)
    elif t_ru:
        out["title_ru"] = _norm_space(t_ru.split(" / ")[0])
    t_or = first("title_original")
    if t_or:
        out["title_original"] = _norm_space(t_or.split(" / ")[0]) if not _has_cyr(t_or.split(" / ")[0]) or " / " not in t_or else _norm_space(t_or)
    elif nm:
        out["title_original"] = nm["title_original"]
    yv = first("year")
    desc_years = [int(x) for x in re.findall(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)", yv or "")]
    if nm and nm["year"]:
        out["year"] = nm["year_from"]
        out["year_to"] = nm["year_to"] if nm["year_to"] != nm["year_from"] else None
        out["field_sources"]["year"] = "name"
        if desc_years and min(desc_years) != nm["year_from"]:
            out["year_desc"] = min(desc_years)
            w.append(f"year differs: name {nm['years']} vs description {yv!r} (name used)")
    elif desc_years:
        out["year"], out["year_to"] = min(desc_years), (max(desc_years) if len(desc_years) > 1 else None)
        out["field_sources"]["year"] = "description"

    # countries / studios
    for _, val in fields.get("countries", []):
        a, sep, b = val.partition(" / ")
        if sep and not all(_key(x) in _COUNTRY_KEYS for x in _split_names(b)):
            out["countries"] += _split_names(a)
            out["production_studios"] += _split_names(b)
        else:
            out["countries"] += _split_names(val.replace("/", ","))
    for _, val in fields.get("production", []):
        a, sep, b = val.partition("/")
        if sep:
            out["countries"] += _split_names(a)
            out["production_studios"] += _split_names(b)
        else:
            for it in _split_names(val):
                (out["countries"] if _key(it) in _COUNTRY_KEYS else out["production_studios"]).append(it)
    for _, val in fields.get("released_by", []):
        for it in _split_names(val):
            (out["countries"] if _key(it) in _COUNTRY_KEYS else out["production_studios"]).append(it)
    for _, val in fields.get("studios", []):
        out["production_studios"] += _split_names(val)
    out["countries"] = _uniq(out["countries"])
    out["production_studios"] = _uniq(out["production_studios"])
    unknown_c = [c for c in out["countries"] if _key(c) not in _COUNTRY_KEYS]
    if unknown_c:
        w.append(f"country names not in reference list: {unknown_c}")

    for _, val in fields.get("genres", []):
        out["genres"] += [g.lower() for g in _split_names(val)]
    out["genres"] = _uniq(out["genres"])
    for _, val in fields.get("directors", []):
        out["directors"] += _split_names(val)
    out["directors"] = _uniq(out["directors"])
    for _, val in fields.get("cast", []):
        out["cast"] += _split_names(val)
    out["cast"] = _uniq(out["cast"])

    rv = first("runtime")
    if rv:
        r = _runtime(rv)
        out["runtime_min"], out["runtime_episode_min"] = r["minutes"], r["episode_minutes"]
        out["episodes_count"] = r["episodes"]
        out["field_sources"]["runtime"] = "description"
        if r["minutes"] is None and r["episode_minutes"] is None:
            w.append(f"runtime not understood: {rv!r}")
    if out["runtime_min"] is None and out["runtime_episode_min"] is None and mi:
        d = _mi_duration_min(mi["general"].get("duration") or (mi["video"][0].get("duration") if mi["video"] else None))
        if d:
            if nm and (nm["seasons"] or nm["episode_from"]):
                out["runtime_episode_min"] = d
            else:
                out["runtime_min"] = d
            out["field_sources"]["runtime"] = "mediainfo"

    ra = first("release_author")
    if ra and not re.search(r"(?i)реклам|присоединяйся|баннер", ra):
        out["release_author"] = _norm_space(ra).strip("-=* ") or None
    for _, val in fields.get("note", []):
        if re.search(r"(?i)без рекламы|реклама отсутствует|^отсутствует|рекламы нет|нет рекламы", val):
            out["has_ads"] = False
        elif re.search(r"(?i)реклам", val):
            out["has_ads"] = True
    if re.search(r"(?i)без рекламы", main_text) and out["has_ads"] is None:
        out["has_ads"] = False
    out["premiere_world"] = first("premiere_world")
    out["digital_release"] = first("digital_release")
    out["edition_info"] = first("edition_info")
    sf = first("source_file")
    if not sf:
        m = re.search(r"(?im)^\**\s*исходник\s*[-–:]\s*(.+)$", "\n".join(b for _, b in spoilers) + "\n" + main_text)
        if m:
            sf = m.group(1)
    out["source_file"] = _norm_space(sf) if sf else None
    if first("format_3d"):
        out["is_3d"] = True

    # ---- series info from description (name is primary)
    sv = first("season")
    if sv and not out["season"]:
        ss = [int(x) for x in re.findall(r"\d+", sv)][:20]
        out["season"] = _seasons_str(ss)
    ev = first("episodes")
    if ev and not out["episodes"]:
        m = re.search(r"(\d+)\s*-\s*(\d+)", ev)
        if m:
            out["episodes"] = f"{int(m.group(1))}-{int(m.group(2))}"

    # ---- video
    qv = first("quality")
    vline = " ".join(v for _, v in fields.get("video", []))
    cline = first("codec") or ""
    if mi and mi["video"]:
        v = mi["video"][0]
        out["video_codec"] = _norm_vcodec(" ".join(filter(None, [v.get("format"), v.get("codec_id")])))
        out["video_width"] = _mi_int(v.get("width"))
        out["video_height"] = _mi_int(v.get("height"))
        out["video_bit_depth"] = _mi_int(v.get("bit_depth"))
        out["video_kbps"] = _mi_kbps(v.get("bitrate")) or _mi_kbps(v.get("nominal_bitrate"))
        out["field_sources"]["video"] = "mediainfo"
        if out["video_kbps"] is None:
            out["video_kbps"] = _kbps_in(vline)
            if out["video_kbps"]:
                out["field_sources"]["video_kbps"] = "description"
        hd = _hdr_from_mi_video(v)
        out["hdr"] = hd["hdr"]
        out["dv_profile"] = hd["dv_profile"]
        out["dv_profile_full"] = hd["dv_profile_full"]
        out["dv_layers"] = hd["dv_layers"]
        out["_dv_compat"] = hd["dv_compat"]
        if len(mi["video"]) > 1:
            w.append(f"{len(mi['video'])} video tracks in MediaInfo; first one used")
            for v2 in mi["video"][1:]:
                h2 = _hdr_from_mi_video(v2)
                if "DV" in h2["hdr"] and out["dv_profile"] is None:
                    out["dv_profile"] = h2["dv_profile"]
                out["hdr"] = _uniq(out["hdr"] + h2["hdr"])
    else:
        vt = " ".join([vline, cline])
        out["video_codec"] = _norm_vcodec(vt)
        # «1 280 x 692» (thousands separated by a space) was read as 280 x 692 (release 17536); «DivX 5 640x480» is
        # 640 x 480 (release 17344) — a spaced width counts only when it is a real frame width (≤ 4096)
        m = re.search(r"(?<![\d\w])([1-4]\s\d{3})\s*[xх×]\s*(\d\s\d{3}|\d{3,4})(?!\d)", vline)
        if m and int(m.group(1).replace(" ", "")) <= 4096:
            out["video_width"], out["video_height"] = int(m.group(1).replace(" ", "")), int(m.group(2).replace(" ", ""))
        else:
            m = re.search(r"(\d{3,4})\s*[xх×]\s*(\d{3,4})", vline)
            if m:
                out["video_width"], out["video_height"] = int(m.group(1)), int(m.group(2))
        m = re.search(r"(\d{1,2})\s*(?:бит|bits?)\b", vline, re.I)
        if m:
            out["video_bit_depth"] = int(m.group(1))
        out["video_kbps"] = _kbps_in(vline)
        if vline:
            out["field_sources"]["video"] = "description"
    # HDR fallback / cross-check with description and name
    th = _hdr_from_text(" ".join([vline, qv or "", cline]))
    if not mi or not (mi["video"]):
        hdr = list(th["hdr"])
        if th["generic_hdr"] and not any(h in hdr for h in ("HDR10", "HDR10+")):
            hdr.append("HDR10")
        out["hdr"] = hdr
        out["dv_profile"] = th["dv_profile"]
        if nm:
            for h in nm["hdr"]:
                if h not in out["hdr"]:
                    out["hdr"].append(h)
            if out["dv_profile"] is None:
                out["dv_profile"] = nm["dv_profile"]
            elif nm["dv_profile"] and nm["dv_profile"] != out["dv_profile"]:
                w.append(f"DV profile differs: name {nm['dv_profile']} vs description {out['dv_profile']}")
            if nm["dv_profile"] == 5 or out["dv_profile"] == 5:
                out["hdr"] = [h for h in out["hdr"] if h != "HDR10" or "HDR10" in th["hdr"]]
        out["field_sources"]["hdr"] = "description+name"
    else:
        out["field_sources"]["hdr"] = "mediainfo"
        if nm:
            if nm["dv_profile"] and out["dv_profile"] and nm["dv_profile"] != out["dv_profile"]:
                w.append(f"DV profile differs: name {nm['dv_profile']} vs MediaInfo {out['dv_profile']}")
            if "HDR Vivid" in nm["hdr"] and "HDR10" in out["hdr"]:
                # MediaInfo does not identify HDR Vivid metadata; the PQ base layer is consistent with it
                out["hdr"].append("HDR Vivid")
                w.append("HDR Vivid taken from name (MediaInfo shows only the PQ/HDR10 base)")
            missing = [h for h in nm["hdr"] if h not in out["hdr"]]
            if missing:
                w.append(f"name claims {missing} but MediaInfo does not show it")
            extra_hdr = [h for h in out["hdr"] if h not in nm["hdr"]]
            if extra_hdr and nm["hdr"]:
                w.append(f"MediaInfo shows {extra_hdr} not stated in name")
    order = ["DV", "HDR10+", "HDR10", "HLG", "HDR Vivid"]
    out["hdr"] = sorted(_uniq(out["hdr"]), key=lambda h: order.index(h) if h in order else 99)
    if (nm and nm["sdr_tag"] or th["sdr"]) and out["hdr"] and not (mi and mi["video"]):
        w.append(f"SDR stated but also {out['hdr']}")
    # HDR10 claimed only through a generic "HDR" word (no MediaInfo, no explicit HDR10/HDR10+): rutor uploaders
    # also write "HDR | Dolby Vision Profile 5" (release 874748), so it does not prove an HDR10 layer
    hdr10_generic_only = (not (mi and mi["video"]) and "HDR10" in out["hdr"]
                          and not ("HDR10" in th["hdr"] or "HDR10+" in th["hdr"] or (nm and nm["hdr10_explicit"])))
    out["hdr10_from_generic_tag"] = hdr10_generic_only

    # DV fallback
    if "DV" in out["hdr"]:
        comp = out.pop("_dv_compat", None)
        if out["dv_profile"] == 5:
            out["dv_fallback"] = None
        elif out["dv_profile"] == 7:
            out["dv_fallback"] = "HDR10"
        elif out["dv_profile"] == 8:
            if comp:
                out["dv_fallback"] = comp
            elif out["dv_profile_full"] in ("8.1", "8.2", "8.4"):
                out["dv_fallback"] = {"8.1": "HDR10", "8.2": "SDR", "8.4": "HLG"}[out["dv_profile_full"]]
            elif "HDR10" in out["hdr"]:
                out["dv_fallback"] = "HDR10"
            else:
                out["dv_fallback"] = "unknown"
                w.append("DV profile 8 without stated compatibility — fallback unknown")
        elif out["dv_profile"] is None:
            ok = ("HDR10" in out["hdr"] or "HDR10+" in out["hdr"]) and not hdr10_generic_only
            out["dv_fallback"] = "HDR10" if ok else "unknown"
            w.append("Dolby Vision profile not determinable" + (
                "" if ok else "; HDR10 layer not proven (only a generic 'HDR' tag or none) — could be Profile 5"))
        else:
            out["dv_fallback"] = "HDR10" if "HDR10" in out["hdr"] else (comp or "unknown")
        # Profile 5 base layer is IPTPQc2: HDR10+ SEI on it (release 35004, «dvhe.05.06, BL+RPU / SMPTE ST 2094 App 4»)
        # does not make it playable as HDR10 on a non-DV screen — only a real HDR10 / HLG layer is a fallback (R8)
        fallbacks = ("HDR10", "HLG") if mi and mi["video"] else ("HDR10", "HDR10+", "HLG")
        out["dv5_only"] = out["dv_profile"] == 5 and not any(h in out["hdr"] for h in fallbacks)
        # R31: DV that is not the studio's — rebuilt from the HDR10 grade (1) or converted from HDR10+ (2)
        dv_texts = [main_text] + [b for _, b in spoilers]
        out["dv_static_md"], out["dv_rebuild_evidence"] = dv_rebuild(" \n".join(dv_texts))
    out.pop("_dv_compat", None)

    # resolution
    out["resolution"] = _resolution_class(out["video_width"], out["video_height"])
    if out["resolution"]:
        out["field_sources"]["resolution"] = "mediainfo" if mi and mi["video"] else "description"
    else:
        m = re.search(r"(2160|1440|1080|720|576|480)\s*[pрi]", _lat(qv or "") + " " + vline)
        if m:
            out["resolution"] = int(m.group(1))
            out["field_sources"]["resolution"] = "description"
        elif re.search(r"(?i)(?<![\w])4\s*[kк](?![\w])", qv or ""):
            out["resolution"] = 2160
            out["field_sources"]["resolution"] = "description"
        elif nm and nm["resolution"]:
            out["resolution"] = nm["resolution"]
            out["field_sources"]["resolution"] = "name"
    if nm and nm["resolution"] and out["resolution"] and nm["resolution"] != out["resolution"]:
        w.append(f"resolution differs: name {nm['resolution']} vs page {out['resolution']}"
                 f" ({out['video_width']}x{out['video_height']})")
    if out["video_codec"] is None and nm and nm["video_codec"]:
        out["video_codec"] = nm["video_codec"]
        out["field_sources"]["video_codec"] = "name"
    if out["video_codec"] is None:
        w.append("video codec unknown")
    if out["video_kbps"] is None:
        w.append("video bitrate unknown")
    # source type: name first, else quality line
    if not out["source_type"] and qv:
        mq = _SOURCE_RE.search(_lat(qv))
        if mq:
            out["source_type"] = _norm_source(mq.group("src"))
            out["field_sources"]["source_type"] = "description"
    elif out["source_type"]:
        out["field_sources"]["source_type"] = "name"
        if qv:
            mq = _SOURCE_RE.search(_lat(qv))
            if mq and _norm_source(mq.group("src")) and _norm_source(mq.group("src")) != out["source_type"]:
                w.append(f"source type differs: name {out['source_type']} vs description {qv!r}")

    # ---- edition / IMAX / upscale
    ed = list(nm["edition"]) if nm else []
    texts = [out["edition_info"] or "", out["source_file"] or "", qv or ""]
    if mi:
        texts += [mi["general"].get("complete_name") or "", mi["general"].get("movie_name") or "",
                  mi["general"].get("title") or ""]
    for vline_ed in fields.get("edition_info", []):
        texts.append(vline_ed[1])
    joined = " | ".join(texts)
    for rx, flag in _EDITION_TEXT:
        if rx.search(joined):
            ed.append(flag)
    out["edition"] = _uniq(ed)
    out["is_imax_cut"] = "imax" in out["edition"] or bool(nm and nm["is_imax_cut"])
    up_texts = joined + " | " + " ".join(nm["upscale_hints"] if nm else []) + " | " + main_text
    if _UPSCALE_RE.search(up_texts) or (nm and nm["is_upscale"]):
        out["is_upscale"] = True
        out["field_sources"]["is_upscale"] = "text: " + (_UPSCALE_RE.search(up_texts).group(0) if _UPSCALE_RE.search(up_texts) else "name")
    elif _NATIVE4K_RE.search(joined + " | " + main_text):
        out["is_upscale"] = False
        out["field_sources"]["is_upscale"] = "text: " + _NATIVE4K_RE.search(joined + " | " + main_text).group(0)
    elif _2KDI_RE.search(joined + " | " + main_text):
        out["is_upscale"] = True
        w.append("2K master/DI mentioned — is_upscale=True")
    if out["is_upscale"] and out["resolution"] and out["resolution"] < 2160:
        w.append(f"upscale mentioned for a {out['resolution']}p release")

    # ---- audio
    known = []
    if nm:
        known += nm["studios"]
    trans: list[dict] = []
    for _, val in fields.get("translation", []):
        for tdesc in _parse_translation_line(val, known):
            trans.append(tdesc)
    known += [t["studio"] for t in trans if t["studio"]]
    known = _uniq(known)
    desc_audio = []
    season_header = None
    for lab, val in fields.get("audio", []):
        if lab == "season_header":
            season_header = val
            continue
        if not val:
            continue
        a = _parse_audio_line(lab, val, known)
        if season_header:
            a["group"] = season_header
        desc_audio.append(a)
    mi_audio = []
    if mi:
        for a in mi["audio"]:
            text = " ".join(filter(None, [a.get("format"), a.get("commercial"), a.get("format_profile"),
                                          a.get("format_features")]))
            title = a.get("title")
            kind, extra = _kind_in(title or "")
            if kind is None and re.search(r"(?i)original", a.get("service_kind") or ""):
                kind = "original"
            ch = _mi_int(a.get("channels"))
            mlang = _mi_lang(a.get("language"))
            tr = {
                "lang": mlang, "kind": kind,
                "studio": _studio_from_text(title, known)
                if title and kind not in ("original", "commentary") and mlang in ("ru", "uk", None) else None,
                "codec": _norm_acodec(text), "channels": _channels_from_count(ch) if ch else None,
                "kbps": _mi_kbps(a.get("bitrate")), "title": title,
                "atmos": bool(re.search(r"atmos|joc|16-ch", text, re.I)), "default": _mi_yes(a.get("default")),
                "forced": _mi_yes(a.get("forced")),
            }
            if extra.get("official") is not None:
                tr["official"] = extra["official"]
            if extra.get("voices"):
                tr["voices"] = extra["voices"]
            mi_audio.append(tr)

    audio: list[dict]
    if mi_audio:
        audio = mi_audio
        out["field_sources"]["audio"] = "mediainfo"
        if desc_audio and len(desc_audio) == len(mi_audio):
            for a, d in zip(audio, desc_audio):
                if (d.get("lang") in ("ru", "uk") and a.get("lang") not in ("ru", "uk")
                        and d.get("kind") in ("dub", "mvo", "dvo", "svo", "avo", "amateur")
                        and a.get("kind") in (None, d.get("kind"))):
                    w.append(f"MediaInfo language {a.get('lang')} overridden by description {d['lang']} "
                             f"(voice track): {d['raw']!r}")
                    a["lang"] = d["lang"]
                for k in ("kind", "studio", "lang", "official", "voices"):
                    if a.get(k) is None and d.get(k) is not None:
                        a[k] = d[k]
                if a.get("kbps") is None and d.get("kbps"):
                    a["kbps"] = d["kbps"]
                a["desc"] = d["raw"]
                if d.get("lang") and a.get("lang") and d["lang"] != a["lang"]:
                    w.append(f"audio language differs MediaInfo {a['lang']} vs description {d['lang']}: {d['raw']!r}")
            out["field_sources"]["audio"] = "mediainfo+description"
        elif desc_audio:
            w.append(f"audio count differs: MediaInfo {len(mi_audio)} vs description {len(desc_audio)}")
            if mi_blocks and len(mi_blocks) > 1:
                w.append("several MediaInfo blocks; first one used for audio")
            for a in audio:
                if a["kind"] is None or a["studio"] is None:
                    cand = [d for d in desc_audio if d.get("lang") == a.get("lang")
                            and (a["studio"] is None or d.get("studio") == a["studio"])]
                    if len(cand) == 1:
                        a["kind"] = a["kind"] or cand[0]["kind"]
                        a["studio"] = a["studio"] or cand[0]["studio"]
            # a voice-over shipped as a separate file («Язык Русский (внешним файлом)», «[RUS(ext), JAP+Sub]») is not in
            # the MediaInfo of the video file (review 2026-10-28: 80 rutracker anime releases read as «no voice-over»)
            mi_langs = {a.get("lang") for a in audio}
            for d in desc_audio:
                if d.get("lang") and d["lang"] not in mi_langs and _EXT_AUDIO_RE.search(d.get("raw") or ""):
                    audio.append(dict(d, external=True))
                    w.append(f"external audio file from the description: {d['raw']!r}")
    else:
        audio = desc_audio
        out["field_sources"]["audio"] = "description" if desc_audio else None

    # fill kind from "Перевод" lines / studio lists / name codes
    name_kinds = [v["kind"] for v in (nm["voices"] if nm else []) if v["kind"] != "none"]

    def _studio_match(t, a):
        if not a.get("studio"):
            return False
        ka = _key(a["studio"])
        return any(_key(s) and (_key(s) == ka or (len(_key(s)) > 4 and _key(s) in ka) or (len(ka) > 4 and ka in _key(s)))
                   for s in (t.get("studios") or ([t["studio"]] if t.get("studio") else [])))

    def _needs_kind(a):
        return a.get("kind") is None and a.get("lang") in ("ru", "uk", None)

    unassigned = [t for t in trans]
    # 1) studio-name matches
    for a in audio:
        for t in unassigned:
            if _studio_match(t, a):
                if _needs_kind(a):
                    a["kind"] = t["kind"]
                    a["kind_source"] = "translation line"
                    if a.get("lang") is None:
                        a["lang"] = t["lang"]
                    if t.get("official") is not None and a.get("official") is None:
                        a["official"] = t["official"]
                if len(t.get("studios") or []) <= 1:
                    unassigned.remove(t)
                break
    # 2) translation lines that name no studio
    nostudio = [t for t in unassigned if not t.get("studio")]
    if nostudio and len(nostudio) == len(unassigned):
        for lang in ("ru", "uk"):
            tl = [t for t in nostudio if t["lang"] == lang]
            cand = [a for a in audio if _needs_kind(a) and (a.get("lang") or "ru") == lang]
            known_kinds = {a.get("kind") for a in audio if a.get("lang") == lang and a.get("kind")}
            rest = [t for t in tl if t["kind"] not in known_kinds]
            if len(tl) > 1 and len(rest) == 1 and cand:
                tl = rest  # other listed kinds are already identified on tracks
            if len(tl) == 1 and cand:
                for a in cand:
                    a["kind"] = tl[0]["kind"]
                    a["kind_source"] = "translation line (single)"
                    a["lang"] = a.get("lang") or lang
            elif tl and len(tl) == len(cand):
                for a, t in zip(cand, tl):
                    a["kind"] = t["kind"]
                    a["kind_source"] = "translation line (by order)"
                    a["lang"] = a.get("lang") or lang
    # 3) remaining translation lines with studios -> tracks with neither studio nor kind, in order
    for a in audio:
        match = None
        if _needs_kind(a) and not a.get("studio"):
            same_lang = [t for t in unassigned if t["lang"] == (a.get("lang") or "ru") and t.get("kind")]
            if same_lang:
                match = same_lang[0]
        if match:
            a["kind"] = match["kind"]
            if a.get("studio") is None and match.get("studio"):
                a["studio"] = match["studio"]
            if a.get("lang") is None:
                a["lang"] = match["lang"]
            if match.get("official") is not None and a.get("official") is None:
                a["official"] = match["official"]
            unassigned.remove(match)
            a["kind_source"] = "translation line"
    # disambiguate generic multi/single-voice into professional vs amateur using name codes / studio lists
    nk = set(nm["voice_codes"]) if nm else set()
    for a in audio:
        if a.get("lang") not in ("ru", None):
            continue
        k = a.get("kind")
        if k in ("mvo", "dvo", "svo") and not a.get("professional_checked"):
            pro_code = {"mvo": "P", "dvo": "P2", "svo": "P1"}[k]
            am_code = {"mvo": "L", "dvo": "L2", "svo": "L1"}[k]
            raw = (a.get("raw") or "") + " " + (a.get("title") or "") + " " + (a.get("desc") or "")
            if re.search(r"(?i)профессиональн|professional", raw):
                continue
            if am_code in nk and pro_code not in nk:
                a["kind"] = "amateur"
                a["kind_source"] = f"name code {am_code}"
            elif am_code in nk and pro_code in nk:
                sk = _KIND_BY_STUDIO_KEY.get(_key(a.get("studio") or ""))
                if sk == "amateur":
                    a["kind"] = "amateur"
                    a["kind_source"] = "rutor amateur studio list"
                elif sk is None:
                    w.append(f"track {a.get('studio') or a.get('raw')!r}: {k} could be professional ({pro_code}) "
                             f"or amateur ({am_code}) — kept {k}")
            elif k == "svo" and "A" in nk and "P1" not in nk and "L1" not in nk:
                a["kind"] = "avo"
                a["kind_source"] = "name code A"
    # 4) single voice code in the name (and no UKR ambiguity) -> Russian tracks still without kind
    if nm and not nm["has_ukr"] and len(set(name_kinds)) == 1:
        for a in audio:
            if a.get("kind") is None and a.get("lang") == "ru":
                a["kind"] = name_kinds[0]
                a["kind_source"] = f"name code {nm['voice_codes'][0]}"
        # the only audio track, without language or kind (FlarrowFilms localized releases, «Аудио: AC-3, 192 kb/s,
        # 2 channels»): it is the voice the name's single code describes — was best_voice 'none' (release 12257)
        if len(audio) == 1 and audio[0].get("kind") is None and audio[0].get("lang") is None:
            audio[0]["lang"], audio[0]["kind"] = "ru", name_kinds[0]
            audio[0]["kind_source"] = f"name code {nm['voice_codes'][0]} (single unlabelled track)"
            if audio[0].get("studio") is None and len(nm["studios"]) == 1:
                audio[0]["studio"] = nm["studios"][0]
    # no voice code, one studio of the rutor guide lists in the name and one unlabelled track: that studio's voice
    # («Криптон / Krypton [s01] (2018) WEBRip 1080p | Flarrow Films», release 44362 — was best_voice 'none')
    elif nm and not name_kinds and not nm["has_ukr"] and len(nm["studios"]) == 1 and len(audio) == 1 \
            and audio[0].get("kind") is None and audio[0].get("lang") in (None, "ru") \
            and _KIND_BY_STUDIO_KEY.get(_key(nm["studios"][0])):
        audio[0]["lang"], audio[0]["kind"] = "ru", _KIND_BY_STUDIO_KEY[_key(nm["studios"][0])]
        audio[0]["studio"] = nm["studios"][0]
        audio[0]["kind_source"] = "rutor studio list (single unlabelled track, no voice code)"
    # 5) films produced in Russia/USSR (or Ukraine): the RU (UK) track without kind is the original
    origin = {_key(c) for c in out["countries"]}
    for a in audio:
        if a.get("kind") is None and (
                (a.get("lang") == "ru" and origin & {_key("Россия"), _key("СССР")}) or
                (a.get("lang") == "uk" and _key("Украина") in origin)):
            a["kind"] = "original"
            a["kind_source"] = "production country"
    # tracks without language: assume Russian only when a voice kind is known
    for a in audio:
        if a.get("lang") is None:
            if a.get("kind") in ("dub", "mvo", "dvo", "svo", "avo", "amateur"):
                a["lang"] = "ru"
                w.append(f"audio track without language assumed Russian: {a.get('raw') or a.get('title')!r}")
            elif a.get("kind") is None:
                w.append(f"audio track without language and kind: {a.get('raw') or a.get('title')!r}")
    for a in audio:
        if a.get("lang") in ("ru", "uk") and a.get("kind") is None:
            w.append(f"voice kind unknown for {a['lang']} track {a.get('raw') or a.get('title')!r}")
    # final clean
    for a in audio:
        if a.get("studio"):
            a["studio"] = canonical_studio(a["studio"])
        a.pop("professional_checked", None)
    out["audio"] = audio

    # best voice
    by_lang: dict[str, list[str]] = {}
    for a in audio:
        if a.get("lang") in ("ru", "uk"):
            by_lang.setdefault(a["lang"], [])
            k = a.get("kind") or "unknown"
            if k not in by_lang[a["lang"]]:
                by_lang[a["lang"]].append(k)
    if not audio:
        # nothing on the page: fall back to the name (codes are Russian by convention)
        if nm and name_kinds:
            by_lang["ru"] = _uniq(name_kinds)
            out["field_sources"]["best_voice"] = "name codes"
        if nm and nm["has_ukr"]:
            by_lang.setdefault("uk", ["unknown"])
        if not audio:
            w.append("no audio tracks found on page")
    for lang in by_lang:
        by_lang[lang] = sorted(by_lang[lang], key=lambda k: VOICE_RANK.index(k) if k in VOICE_RANK else 99)
    out["voice_by_lang"] = by_lang
    ranked = [k for ks in by_lang.values() for k in ks if k in VOICE_RANK]
    if ranked:
        out["best_voice"] = min(ranked, key=VOICE_RANK.index)
    elif audio and all(a.get("lang") not in ("ru", "uk") for a in audio):
        out["best_voice"] = "none"
    elif nm and nm["subs_only"] and not by_lang:
        out["best_voice"] = "none"
    elif by_lang and all(k in ("original", "commentary") for ks in by_lang.values() for k in ks):
        out["best_voice"] = "original"  # RU/UK is the original language (e.g. a Russian production)
    elif by_lang:
        out["best_voice"] = None
        w.append("RU/UK audio present but voice kind unknown")
    if nm and nm["best_voice"] and out["best_voice"] and nm["best_voice"] != out["best_voice"] \
            and nm["best_voice"] != "none":
        w.append(f"best voice differs: name codes -> {nm['best_voice']}, page -> {out['best_voice']}")
    if nm and "D" in nm["voice_codes"] and audio and "dub" not in by_lang.get("ru", []) + by_lang.get("uk", []):
        w.append("name has code D but no RU/UK dub track identified on page")
    if nm and nm["has_ukr"] and audio and "uk" not in by_lang:
        w.append("name has UKR tag but no Ukrainian track identified on page")

    # ---- subtitles (forced only are stored; all languages returned for information)
    langs, forced = [], []
    for lab, val in fields.get("subs", []):
        if lab == "season_header":
            continue
        l1, f1 = _sub_forced_langs(val)
        langs += l1
        forced += f1
    if mi:
        for tt in mi["text"]:
            lang = _mi_lang(tt.get("language"))
            title = tt.get("title") or ""
            if lang:
                langs.append(lang)
            if lang and (_mi_yes(tt.get("forced")) or re.search(r"(?i)forc|форс|принуд|надпис", title)):
                forced.append(lang)
    out["subtitle_langs"] = _uniq(langs)
    out["forced_subs"] = [x for x in _uniq(forced) if x != "orig"]
    if "orig" in forced:
        w.append("forced subtitles for 'original' language (language not named)")
    _release_rules(out, nm, main_text, spoilers, mi, fields, w)
    return out
