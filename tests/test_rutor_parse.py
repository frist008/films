"""Tests for films/parse/rutor.py.

Expected values were checked by hand against the release names and against the saved release pages in
tests/fixtures/rutor/{id}.html (description lines + MediaInfo). Run:  python -X utf8 -m unittest tests.test_rutor_parse
"""
import unittest
from pathlib import Path

from films.parse.rutor import parse_release_name, parse_release_page

FIX = Path(__file__).resolve().parent / "fixtures" / "rutor"


def page(tid: str) -> dict:
    return parse_release_page((FIX / f"{tid}.html").read_text(encoding="utf-8"))


def tracks(r, lang):
    return [(a.get("kind"), a.get("studio")) for a in r["audio"] if a.get("lang") == lang]


class TestReleaseName(unittest.TestCase):
    def test_uhd_bdremux_dv7(self):
        r = parse_release_name("Кей-поп-охотницы на демонов / KPop Demon Hunters (2025) UHD BDRemux 2160p | 4K | "
                               "HDR10 | Dolby Vision P7 | D | Videofilm Int, Аминокотики")
        self.assertEqual(r["title_ru"], "Кей-поп-охотницы на демонов")
        self.assertEqual(r["titles_other"], ["KPop Demon Hunters"])
        self.assertEqual(r["title_original"], "KPop Demon Hunters")
        self.assertEqual((r["year_from"], r["year_to"]), (2025, 2025))
        self.assertEqual(r["source_type"], "BDRemux")
        self.assertTrue(r["uhd_source"])
        self.assertEqual(r["resolution"], 2160)
        self.assertEqual(r["hdr"], ["DV", "HDR10"])
        self.assertEqual(r["dv_profile"], 7)
        self.assertEqual(r["voice_codes"], ["D"])
        self.assertEqual(r["best_voice"], "dub")
        self.assertEqual(r["studios"], ["Videofilm Int.", "Аминокотики"])

    def test_series_two_seasons(self):
        r = parse_release_name("Джентльмены / The Gentlemen [S01-02] (2024-2026) WEB-DLRip-AVC | D, P, P2 | "
                               "Movie Dubbing, LostFilm, Кубик в Кубе")
        self.assertEqual(r["seasons"], [1, 2])
        self.assertEqual(r["season"], "1-2")
        self.assertIsNone(r["episodes"])
        self.assertEqual((r["year_from"], r["year_to"]), (2024, 2026))
        self.assertEqual(r["source_type"], "WEB-DLRip")
        self.assertEqual(r["video_codec"], "AVC")
        self.assertIsNone(r["resolution"])
        self.assertEqual(r["resolution_class"], "SD")
        self.assertEqual([v["kind"] for v in r["voices"]], ["dub", "mvo", "dvo"])
        self.assertEqual(r["studios"], ["Movie Dubbing", "LostFilm", "Кубик в Кубе"])

    def test_partial_episodes_three_titles(self):
        r = parse_release_name("Ягодная любовь / Merii Berii Rabu / Merry Berry Love [01x01-03 из 10] (2026) "
                               "WEB-DL 1080p | L | DubLikTV")
        self.assertEqual(r["titles_other"], ["Merii Berii Rabu", "Merry Berry Love"])
        self.assertEqual(r["title_original"], "Merii Berii Rabu")
        self.assertEqual((r["season"], r["episodes"], r["episodes_total"]), ("1", "1-3", 10))
        self.assertEqual(r["resolution"], 1080)
        self.assertEqual(r["best_voice"], "amateur")
        self.assertEqual(r["voices"][0]["meaning"], "Любительский многоголосый закадровый")

    def test_cyrillic_lookalike_codes_and_av1_sdr(self):
        r = parse_release_name("Matchbox. Фильм / Matchbox the Movie (2026) UHD WEB-DLrip-AV1 2160p | 4K | SDR | D, Р")
        self.assertEqual(r["voice_codes"], ["D", "P"])  # Cyrillic "Р" read as P
        self.assertEqual(r["video_codec"], "AV1")
        self.assertEqual(r["source_type"], "WEB-DLRip")
        self.assertEqual(r["hdr"], [])
        self.assertTrue(r["sdr_tag"])

    def test_editions(self):
        r = parse_release_name("Гладиатор / Gladiator (2000) BDRip 1080p | D, P, A  | Open Matte | Extended Cut | Remastered")
        self.assertEqual(r["edition"], ["open_matte", "extended", "remastered"])
        r = parse_release_name("Телекинез / Carrie (2013) WEB-DLRip-AVC от DoMiNo & селезень | D | Open Matte | Театральная версия")
        self.assertEqual(r["edition"], ["open_matte", "theatrical"])
        self.assertEqual(r["release_group"], "DoMiNo & селезень")
        r = parse_release_name("Как приручить дракона / How to Train Your Dragon (2025) UHD BDRemux 2160p | 4K | HDR | "
                               "Dolby Vision P7 | IMAX | D, P, L")
        self.assertTrue(r["is_imax_cut"])
        self.assertEqual(r["hdr"], ["DV", "HDR10"])

    def test_ukr_tag_and_ukrainian_title(self):
        r = parse_release_name("Джек Ричер / Річер / Reacher [S01-04] (2022-2026) WEB-DLRip-AVC | КПК | P | UKR")
        self.assertEqual(r["title_uk"], "Річер")
        self.assertEqual(r["title_original"], "Reacher")
        self.assertTrue(r["has_ukr"])
        self.assertTrue(r["kpk"])
        self.assertIsNone(r["voices"][0]["lang"])  # with UKR the code may describe the Ukrainian track

    def test_dv_profile5_and_tv_tag(self):
        r = parse_release_name("Девичник в спа / Spa Weekend (2026) WEB-DL-HEVC 2160p | 4K | Dolby Vision Profile 5 | D, P")
        self.assertEqual((r["hdr"], r["dv_profile"]), (["DV"], 5))
        r = parse_release_name("Головоломка 2 / Inside Out 2 (2024) UHD WEB-DL 2160p | 4K | Dolby Vision TV | D | MovieDalen, TVShows")
        self.assertEqual(r["dv_profile"], 5)
        self.assertTrue(r["dv_profile_inferred"])

    def test_hdr10plus_variants(self):
        r = parse_release_name("Астрал 6: Они уже здесь / Insidious: Out of the Further (2026) WEB-DL 2160p | 4К | "
                               "НDR+10 | Dolby Vision Profile 8 | D, Р")
        self.assertEqual(r["hdr"], ["DV", "HDR10+", "HDR10"])
        self.assertEqual(r["dv_profile"], 8)
        self.assertEqual(r["resolution"], 2160)

    def test_sub_only_and_all_codes(self):
        r = parse_release_name("Красные дни 2: Сесилия / Red Days 2: Cecilia (2026) WEBRip 1080p | Sub")
        self.assertTrue(r["subs_only"])
        self.assertEqual(r["best_voice"], "none")
        r = parse_release_name("Балерина / Ballerina (2025) UHD BDRemux 2160p | 4K | HDR | HDR10+ | Dolby Vision P7 | "
                               "Hybrid | D, P, P2, A, L, L2, L1")
        self.assertEqual(r["voice_codes"], ["D", "P", "P2", "A", "L", "L2", "L1"])
        self.assertTrue(r["hybrid"])
        self.assertEqual(r["edition"], [])  # Hybrid is not an edition
        r = parse_release_name("Полковник Редль / Colonel Redl / Oberst Redl (1984) BDRemux 1080p | P1 | Малишевский")
        self.assertEqual(r["best_voice"], "svo")

    def test_mediainfo_duration_mn(self):
        # review round 11: «Duration : 1h 37mn» (older MediaInfo) was read as 60 min
        from films.parse.rutor import _mi_duration_min
        self.assertEqual([_mi_duration_min(v) for v in ("1h 37mn", "1h 48mn ", "1 ч. 38 мин.", "01:38:20", "45mn 30s")],
                         [97, 108, 98, 98, 46])

    def test_upscale_and_other_tags(self):
        r = parse_release_name("Тариф Новогодний (2008) UHD BDRip 2160p | 4K | SDR | Upscale")
        self.assertTrue(r["is_upscale"])
        self.assertIsNone(r["title_original"])
        r = parse_release_name("Необратимость / Irréversible (2002) BDRemux 1080p | GBR Transfer | P, P2")
        self.assertEqual(r["transfer"], ["UK"])
        r = parse_release_name("Вышка 2: Мертвая точка / Fall 2: Deadpoint (2026) WEB-DLRip-AVC от DoMiNo | D | TS")
        self.assertEqual(r["audio_source"], "TS")
        r = parse_release_name("Брубейкер / Brubaker (1980) BDRip 1080р | P")  # Cyrillic "р"
        self.assertEqual(r["resolution"], 1080)
        self.assertIsNone(r["release_group"])

    def test_title_with_digits_only(self):
        r = parse_release_name("1670 / 1670 [S03] (2026) WEB-DL 1080p | D")
        self.assertEqual(r["title_ru"], "1670")
        self.assertEqual(r["season"], "3")

    def test_unclosed_year_and_disc_count(self):
        r = parse_release_name("Сирокко из страны ветров / Sirocco et le royaume des courants d'air / Sirocco and the "
                               "Kingdom of the Winds (2023 BDRip 1080p от MegaPeer | D")
        self.assertEqual(r["year"], 2023)
        self.assertEqual(r["source_type"], "BDRip")
        r = parse_release_name("Финес и Ферб / Phineas and Ferb [S01-04] (2008-2014) 5xDVD9 от New-Team | D | Лицензия")
        self.assertEqual(r["source_type"], "DVD")
        self.assertTrue(r["licensed"])


class TestReleasePage(unittest.TestCase):
    def test_1102883_labels_without_colon(self):
        # «<b>Перевод </b>Любительский … - Jetvis Studio», «<b>Звук </b>AC3 … (RU)» (review 2026-10-28: «Ферма Кларксона» S5
        # was hidden «нет озвучки»)
        r = page("1102883")
        self.assertEqual(tracks(r, "ru"), [("amateur", "Jetvis Studio")])
        self.assertEqual(r["best_voice"], "amateur")

    def test_1110909_uhd_bdremux_dv7_with_ukrainian(self):
        r = page("1110909")
        self.assertEqual(r["imdb_id"], "tt14205554")
        self.assertEqual((r["resolution"], r["source_type"], r["video_codec"]), (2160, "BDRemux", "HEVC"))
        self.assertEqual((r["video_bit_depth"], r["video_kbps"]), (10, 75800))  # "75, 8 Мбит/сек"
        self.assertEqual((r["hdr"], r["dv_profile"], r["dv_profile_full"]), (["DV", "HDR10"], 7, "7.6"))
        self.assertEqual(r["dv_fallback"], "HDR10")
        self.assertFalse(r["dv5_only"])
        self.assertEqual(tracks(r, "ru"), [("dub", "Videofilm Int.")] * 3 + [("dub", "Аминокотики")])
        self.assertEqual(tracks(r, "uk"), [("dub", "LeDoyen")])
        self.assertEqual(r["best_voice"], "dub")
        self.assertEqual(r["forced_subs"], ["ru", "uk"])
        self.assertEqual(r["runtime_min"], 96)
        self.assertEqual(r["directors"], ["Крис Аппельханс", "Мэгги Кан"])
        self.assertEqual(r["countries"], ["США"])
        self.assertEqual(r["size_bytes"], 60332181805)
        self.assertEqual(r["btih"], "7ee425ea67e84b9ddd12c695eb1dab1dbf69910d")

    def test_1099799_web_dv5_detected_from_mediainfo(self):
        r = page("1099799")
        self.assertEqual((r["hdr"], r["dv_profile"]), (["DV"], 5))
        self.assertIsNone(r["dv_fallback"])
        self.assertTrue(r["dv5_only"])
        self.assertEqual(r["video_kbps"], 14400)
        self.assertEqual(tracks(r, "ru"), [("dub", "Продубляж"), ("mvo", "TVShows"), ("mvo", "HDRezka Studio"),
                                           ("mvo", "HDRezka Studio"), ("dub", "HDRezka Studio")])
        self.assertEqual(r["forced_subs"], ["ru", "en"])  # MediaInfo has an English "Forced" track too

    def test_1107649_web_dv5_without_mediainfo(self):
        r = page("1107649")
        self.assertFalse(r["mediainfo_found"])
        self.assertEqual((r["hdr"], r["dv_profile"]), (["DV"], 5))
        self.assertTrue(r["dv5_only"])
        self.assertEqual(r["video_kbps"], 14300)
        self.assertEqual(tracks(r, "ru"), [("mvo", "TVShows"), ("dub", "Ultradox Studio")])
        self.assertEqual(r["forced_subs"], [])

    def test_1004108_dv_tv_is_profile5(self):
        r = page("1004108")
        self.assertEqual((r["dv_profile"], r["dv5_only"]), (5, True))
        self.assertEqual(r["voice_by_lang"], {"ru": ["dub"], "uk": ["dub"]})

    def test_1110899_web_dv8_hdr10plus(self):
        r = page("1110899")
        self.assertEqual(r["hdr"], ["DV", "HDR10+", "HDR10"])
        self.assertEqual((r["dv_profile"], r["dv_profile_full"], r["dv_fallback"]), (8, "8.1", "HDR10"))
        self.assertEqual(r["video_kbps"], 19500)
        self.assertEqual(tracks(r, "ru"), [("mvo", "WinMedia"), ("mvo", "TVShows")])
        self.assertEqual(r["best_voice"], "mvo")
        self.assertEqual(r["forced_subs"], ["ru", "en"])

    def test_1107350_bdremux_dv8_hybrid_many_tracks(self):
        r = page("1107350")
        self.assertEqual(r["hdr"], ["DV", "HDR10+", "HDR10"])
        self.assertEqual(r["dv_profile"], 8)
        self.assertEqual(r["voice_by_lang"], {"ru": ["dub", "mvo", "dvo", "avo"], "uk": ["dub", "mvo"]})
        self.assertEqual([a["kind"] for a in r["audio"] if a["lang"] == "en"],
                         ["original", "original", "commentary", "commentary", "commentary"])
        self.assertEqual(r["forced_subs"], ["ru", "uk"])
        self.assertEqual(r["imdb_id"], "tt0325980")

    def test_1110462_hdr10plus_without_dv(self):
        r = page("1110462")
        self.assertEqual(r["hdr"], ["HDR10+", "HDR10"])
        self.assertIsNone(r["dv_profile"])
        self.assertEqual(tracks(r, "ru"), [("dub", "Пифагор")])

    def test_1087879_hlg_series(self):
        r = page("1087879")
        self.assertEqual(r["hdr"], ["HLG"])
        self.assertEqual((r["season"], r["runtime_episode_min"], r["episodes_count"]), ("1", 45, 6))
        self.assertEqual(tracks(r, "ru"), [("dub", "Videofilm Int."), ("mvo", "HDRezka Studio"), ("mvo", "TVShows"),
                                           ("mvo", "Red Head Sound")])

    def test_1110525_4k_sdr_native(self):
        r = page("1110525")
        self.assertEqual((r["resolution"], r["hdr"]), (2160, []))
        self.assertIs(r["is_upscale"], False)  # "Издание: ... в нативном 4К"
        self.assertEqual(r["voice_by_lang"], {"ru": ["dub", "mvo", "amateur"]})

    def test_1110986_av1(self):
        r = page("1110986")
        self.assertEqual((r["video_codec"], r["video_kbps"], r["video_bit_depth"]), ("AV1", 6400, 10))
        self.assertEqual(tracks(r, "ru"), [("mvo", "TVShows"), ("mvo", "WinMedia"), ("dub", "HDRezka Studio"),
                                           ("mvo", "HDRezka Studio")])
        self.assertEqual(r["forced_subs"], ["ru"])

    def test_1042472_web_1080_avc(self):
        r = page("1042472")
        self.assertEqual((r["resolution"], r["video_codec"], r["video_kbps"], r["hdr"]), (1080, "AVC", 4671, []))
        self.assertEqual(tracks(r, "ru"), [("dub", "Videofilm Int.")])
        self.assertEqual(r["forced_subs"], ["uk", "en"])

    def test_1059243_upscale(self):
        r = page("1059243")
        self.assertIs(r["is_upscale"], True)
        self.assertEqual(r["best_voice"], "original")  # Russian production

    def test_1110489_native_4k_and_amateur_single_voice(self):
        r = page("1110489")
        self.assertIs(r["is_upscale"], False)
        self.assertEqual((r["dv_profile"], r["hdr"]), (7, ["DV", "HDR10+", "HDR10"]))
        self.assertIn(("amateur", "Данис Нурмухаметов"), tracks(r, "ru"))  # "Одноголосый" + name code L1
        self.assertEqual(tracks(r, "uk")[0], ("dub", "Cinemaker"))

    def test_open_matte_extended_directors_imax(self):
        self.assertEqual(page("748373")["edition"], ["open_matte"])
        self.assertEqual(page("253282")["edition"], ["open_matte", "extended", "remastered"])
        self.assertEqual(page("1110678")["edition"], ["directors_cut"])
        self.assertEqual(page("1109533")["edition"], ["uncut"])
        self.assertEqual(page("1109017")["edition"], ["localized"])
        self.assertEqual(page("1102730")["edition"], ["black_and_white"])
        r = page("1110500")
        self.assertTrue(r["is_imax_cut"])
        self.assertEqual(r["forced_subs"], ["ru"])

    def test_subtitles_only(self):
        for tid in ("1111110", "1109877"):
            r = page(tid)
            self.assertEqual(r["best_voice"], "none", tid)
            self.assertEqual(r["voice_by_lang"], {}, tid)

    def test_1105238_ukrainian_only(self):
        r = page("1105238")
        self.assertEqual(r["voice_by_lang"], {"uk": ["dub"]})
        self.assertEqual(tracks(r, "uk"), [("dub", "LeDoyen")])
        self.assertEqual(r["forced_subs"], ["uk"])
        self.assertEqual(r["best_voice"], "dub")

    def test_1110416_many_studios(self):
        r = page("1110416")
        ru = tracks(r, "ru")
        self.assertEqual(ru[0], ("dub", "Movie Dubbing"))
        self.assertIn(("dvo", "Кубик в Кубе"), ru)
        self.assertIn(("avo", "Чадов"), ru)
        self.assertEqual(tracks(r, "uk"), [("dub", "Postmodern"), ("mvo", "HDRezka Studio")])
        self.assertEqual((r["resolution"], r["video_codec"], r["video_kbps"]), (1080, "AVC", 31800))

    def test_1109831_series_translation_lines_matched_by_studio(self):
        r = page("1109831")
        self.assertEqual(tracks(r, "ru"), [("dub", "Movie Dubbing"), ("mvo", "LostFilm"), ("dvo", "Кубик в Кубе")])
        self.assertEqual((r["season"], r["runtime_episode_min"]), ("1-2", 50))

    def test_1109617_partial_episodes_amateur(self):
        r = page("1109617")
        self.assertEqual((r["season"], r["episodes"]), ("1", "1-3"))
        self.assertEqual(tracks(r, "ru"), [("amateur", "DubLikTV")])

    def test_1010582_p1_single_voice_and_dv5(self):
        r = page("1010582")
        self.assertEqual(tracks(r, "ru"), [("svo", "Videofilm Int.")])
        self.assertEqual(r["dv_profile"], 5)
        self.assertEqual((r["runtime_episode_min"], r["episodes_count"]), (44, 3))

    def test_1106495_ts(self):
        r = page("1106495")
        self.assertEqual((r["source_type"], r["best_voice"]), ("TS", "mvo"))

    def test_dv_without_profile_generic_hdr_is_not_proof_of_hdr10(self):
        r = page("1110623")  # "4K | HDR | Dolby Vision", no MediaInfo
        self.assertIsNone(r["dv_profile"])
        self.assertEqual(r["dv_fallback"], "unknown")
        self.assertTrue(r["hdr10_from_generic_tag"])
        r = page("874748")  # "4K | HDR | Dolby Vision Profile 5" — generic HDR next to P5
        self.assertEqual((r["dv_profile"], r["dv5_only"], r["hdr"]), (5, True, ["DV"]))

    def test_1108642_hdr_vivid(self):
        r = page("1108642")
        self.assertEqual(r["hdr"], ["HDR10", "HDR Vivid"])

    def test_1109634_l2(self):
        r = page("1109634")
        self.assertEqual(tracks(r, "ru"), [("amateur", "ГКГ")])
        self.assertEqual(r["audio"][0]["voices"], 2)


class DvRebuildTest(unittest.TestCase):
    """R31: DV rebuilt from HDR10 (1) / converted from HDR10+ (2); wordings from cached rutor pages."""

    def test_signals(self):
        from films.parse.rutor import dv_rebuild
        cases = {
            "HYBRID WEB-DL HDR + DV metadata is generated in DaVinci Resolve, Dolby Vision": 1,   # rutor 10767
            "Web-DL 2160p HDR + Web-DL 2160p Dolby vision RPU converted from dynamic HDR10+ metadata": 2,  # 39388
            "UHD WebDL 2160p + Dolby vision RPU from dynamic HDR+ metadata": 2,                   # 16578
            "Dolby Vision из HDR10": 1, "HDR10 -> DV": 1, "DV P8 (CM v4.0, static metadata)": 1,
            # not rebuilt: hybrid with a real RPU, plain MediaInfo, an audio studio name
            "Гибридный релиз получен слиянием UHD BDRemux HDR + Web-DL 2160p Dolby vision RPU": None,
            "Dolby Vision, Version 1.0, Profile 8.1, dvhe.08.06, BL+RPU, HDR10 compatible / SMPTE ST 2094 App 4, "
            "Version 1, HDR10+ Profile B compatible": None,
            "Аудио 2: AC3, 2 ch, 192 Kbps | русский (DVO RealFake)": None,
        }
        for text, want in cases.items():
            self.assertEqual(dv_rebuild(text)[0], want, text)


def mk(name: str, *lines: str) -> dict:
    """A minimal rutor page: <h1> + description lines (as the cached pages render them) + one meta row."""
    desc = "<br />" + "<br />".join(lines)
    return parse_release_page(f'<h1>{name}</h1><table id="details"><tr><td></td><td>{desc}</td></tr>'
                              f'<tr><td class="header">Раздают</td><td>5</td></tr></table>')


class ReleaseRulesTest(unittest.TestCase):
    """Owner's metadata survey, release rules (2026-10-25). Names and description lines are from cached rutor pages
    (release ids in comments)."""

    def test_ads(self):
        self.assertTrue(mk("Куда пропала Ванда / Wheres Wanda? [S01] (2024) WEBRip 1080p | P2 | ViruseProject",
                           "Реклама - есть (пару видеовставок).")["has_ads"])                                # 12036
        self.assertTrue(mk("X / X (2018) WEB-DL 1080p | P", "Присутствует аудио-реклама")["has_ads"])        # 15512
        self.assertFalse(mk("X / X (2024) WEB-DL 1080p | D", "Примечание: Без какой-либо рекламы")["has_ads"])  # was True
        self.assertFalse(mk("X / X (2024) WEB-DL 1080p | D", "Реклама: Полностью отсутствует")["has_ads"])
        plot = ("Однажды скетчбук падает в пруд, и он вспоминает, что его самым большим успехом так и осталось участие в "
                "популярной рекламе зубной пасты, а потом ему предлагают работу в фирме «Семья в аренду», где всё иначе.")
        self.assertIsNone(mk("X / X (2024) WEB-DL 1080p | D", plot)["has_ads"])                              # 3353

    def test_localized_and_single_unlabelled_track(self):
        r = mk("Сорвиголова: Рожденный заново / Daredevil: Born Again [S02] (2026) WEB-DLRip 1080p | D | "
               "Локализованная версия | Flarrow Films", "Качество: WEB-DLRip 1080p (Локализированный видеоряд)",
               "Аудио: AC-3, 192 kb/s, 2 channels, 48.0 kHz")                                             # 12257
        self.assertTrue(r["localized_video"])
        self.assertEqual(r["best_voice"], "dub")              # was 'none': one track without language or kind
        self.assertEqual(tracks(r, "ru"), [("dub", "Flarrow Films")])
        self.assertTrue(mk("X / X (2025) WEB-DL 1080p | D", "Качество: WEB-DL 1080p | Частично локализованный видеоряд")
                        ["localized_video"])                                                                # 17
        # no voice code, only «| Flarrow Films» and one unlabelled track (release 44362) — the studio's voice-over
        r = mk("Криптон / Krypton [s01] (2018) WEBRip 1080p | Flarrow Films",
               "Звук: PCM (A_PCM/INT/LIT), 48 kHz, 1 ch, ~768 kb/s")
        self.assertEqual((r["best_voice"], tracks(r, "ru")), ("mvo", [("mvo", "Flarrow Films")]))
        self.assertFalse(mk("X / X (2025) WEB-DL 1080p | D", "Регион наводнили наёмники, которые пытаются локализовать "
                            "угрозу")["localized_video"])

    def test_3d(self):
        self.assertTrue(mk("Гравитация / Gravity (2013) BDRip 1080p | 3D-Video | HSBS | Лицензия | 60 fps")["is_3d"])  # 29599
        self.assertTrue(mk("Тачки 2 / Cars 2 (2011) BDRip 720p | 3D-Video / Анаглиф")["is_3d"])           # 32781
        self.assertTrue(mk("Тролли / Trolls (2016) WEBRip 1080p от Ash61 | halfSBS | Лицензия")["is_3d"])
        self.assertTrue(mk("Аватар / Avatar (2025) BDRip 1080p | D",
                           "Формат 3D: Half OverUnder / Вертикальная анаморфная стереопара")["is_3d"])           # 2008
        self.assertFalse(mk("Проклятье 3D / Sadako 3D (2012) HDRip от Scarabey | Лицензия")["is_3d"])      # title only

    def test_ts_cam(self):
        self.assertEqual(mk("Обезьяна / The Monkey (2025) WEB-DL 1080p | D | TS")["cam_source"], "звук: TS")   # 7506
        self.assertEqual(mk("Одиссея / The Odyssey (2026) TS-HEVC 1080p | P")["cam_source"], "TS")           # 650
        self.assertEqual(mk("Голый пистолет / The Naked Gun (2025) WEB-DL 1080p | D | Line")["cam_source"],
                         "звук: Line")                                                                     # 5471
        self.assertEqual(mk("Скуби-Ду / Scooby-Doo! (2014) BDRip 720p | Чистый звук")["cam_source"],
                         "звук: Чистый звук")                  # legend: Line = «устаревшее обозначение: Чистый звук»
        self.assertEqual(mk("Байкеры / The Bikeriders (2024) WEB-DLRip-AVC | D",
                            "Перевод: профессиональный (дублированный) звук с TS")["cam_source"], "звук: TS")      # 10818
        # a clean Russian voice stays; the Ukrainian cinema dub does not count as the release's voice (32166)
        r = mk("Кот в сапогах 2 / Puss in Boots: The Last Wish (2022) WEB-DL-HEVC 1080p | Jaskier, TVShows",
               "Аудио 1: AC-3, 6 ch, 384 kbps - Русский (MVO, Jaskier)",
               "Аудио 2: AC-3, 2 ch, 192 kbps - Украинский (DUB, Line)")
        self.assertIsNone(r["cam_source"])
        self.assertEqual(r["best_voice"], "mvo")
        # «Line * он-лайн кинотеатр KinoPub» — a stream, not a cinema recording (20094)
        self.assertIsNone(mk("Райя и последний дракон / Raya and the Last Dragon (2021) WEB-DLRip | D",
                             "Перевод: Дублированный - (Line * он-лайн кинотеатр KinoPub)")["cam_source"])
        self.assertIsNone(mk("Я плюю на ваши могилы 3 / I Spit on Your Grave 3 (2015) BDRip 720p | L2",
                             "Озвучивание: Любительское (двухголосое) [Line Masters]")["cam_source"])         # 41037

    def test_remaster(self):
        self.assertEqual(mk("Банды Нью-Йорка / Gangs of New York (2002) BDRip 1080p | Remastered")["remaster"],
                         "Remastered")                                                                     # 37698
        self.assertEqual(mk("Малхолланд Драйв / Mulholland Dr. (2001) BDRemux 1080p | D, P, A | Criterion Collection | "
                            "JPN Transfer")["remaster"], "Criterion")                                      # 29375
        self.assertEqual(mk("Плохие парни 2 / Bad Boys II (2003) BDRip 1080p | D, P, P2, A | 4K Remastered")["remaster"],
                         "4K Remastered")                                                                  # 30951
        self.assertIsNone(mk("Резервная копия / Restore Point (2023) BDRip 1080p от Generalfilm | D")["remaster"])  # 38869
        self.assertIsNone(mk("Колония / Colony (2026) WEB-DL 1080p | D | Flarrow Films")["remaster"])   # not «Arrow»

    def test_black_and_white(self):
        self.assertEqual(mk("Паук-Нуар / Spider-Noir [S01] (2026) WEB-DL 1080p от Scarabey | D | Red Head Sound | "
                            "Black & White")["video_color"], "bw")                                         # 12209
        self.assertEqual(mk("Паук-Нуар / Spider-Noir [S01] (2026) UHD WEB-DL 2160p | 4K | HDR10+ | Dolby Vision Profile 8 "
                            "| D, P, P2, A, L | Чёрно-белая версия")["video_color"], "bw")                    # 12010
        self.assertEqual(mk("Паук-Нуар / Spider-Noir [S01] (2026) WEB-DL 1080p от Scarabey | D | Red Head Sound | "
                            "Цветная версия")["video_color"], "colorized")                                 # 12210
        self.assertEqual(mk("Логан / Logan (2017) BDRip 1080p | D",
                            "Качество: BDRip 1080p | NOIR Edition (черно-белая версия фильма)")["video_color"], "bw")
        self.assertIsNone(mk("X / X (2025) WEB-DL 1080p | D", "Добро и чёрно-белая мораль Мстителей")["video_color"])

    def test_fps(self):
        mi = ["Video", "ID : 1", "Format : AVC", "Width : 1 920 pixels", "Height : 800 pixels",
              "Frame rate : 59.940 (60000/1001) FPS", "Audio", "Format : AC-3", "Frame rate : 93.750 FPS (512 SPF)"]
        self.assertEqual(mk("Граф Монте-Кристо / Le Comte de Monte-Cristo (2024) BDRip 1080p от Талямбы | D", *mi)["fps"],
                         59.94)                                                                            # 9492
        self.assertEqual(mk("Гравитация / Gravity (2013) BDRip 1080p | HSBS | 60 fps")["fps"], 60.0)
        r = mk("Реквием по мечте / Requiem for a Dream (2000) BDRip 720p",
               "Видео: H.264, 1 280 x 692 (1.850); 23.976 fps; ~7 487 kbps")                                 # 17536
        self.assertEqual((r["video_width"], r["video_height"], r["fps"]), (1280, 692, 23.976))  # was read as 280 x 692

    def test_kpk(self):
        self.assertTrue(mk("Тепло наших тел / Warm Bodies (2013) BDRip от R.G.RealMT | КПК | Open Matte | НТВ+")["kpk"])
        self.assertTrue(mk("Жизнь Дэвида Гейла / The Life of David Gale (2003) HDRip-AVC | iPhone")["kpk"])  # 19069
        self.assertTrue(mk("Как приручить дракона / Dragons (2011) HDRip | КПК от Gene")["kpk"])           # 16186
        self.assertTrue(mk("X / X (2011) HDRip", "Качество: HDRip | КПК")["kpk"])
        self.assertTrue(mk("X / X (2011) DVDRip", "Видео: XviD, 480 x 208 (2.35:1), 23.976 fps, 600 kbps")["kpk"])
        self.assertFalse(mk("Секс по телефону / Sex on Phone (2025) WEBRip | L")["kpk"])                    # title word

    def test_18plus_voice(self):
        self.assertTrue(mk("Кровавая бойня / Carnage (2024) WEB-DLRip-AVC | P2 | ViruseProject",
                           "Перевод: Профессиональный (двухголосый, закадровый) | ViruseProject | 18+")["voice_18plus"])  # 9415
        self.assertTrue(mk("X / X [S01] (2025) WEB-DL 1080p | P | HDRezka Studio",
                           "Аудио: AC3, 48 kHz, 2 ch, ~192.00 kbps avg | русский | MVO | HDRezka Studio | 18+")["voice_18plus"])
        self.assertFalse(mk("X / X [S01] (2025) WEB-DL 1080p | L | LE-Production",
                            "Аудио: AC3, 2 ch, 192 kbps | Любительский закадровый, LE-Production | 18+")["voice_18plus"])
        self.assertFalse(mk("X / X (2025) WEB-DL 1080p | P", "Перевод: Профессиональный (многоголосый)",
                            "Возраст: 18+")["voice_18plus"])                                          # age rating


if __name__ == "__main__":
    unittest.main()
