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


if __name__ == "__main__":
    unittest.main()
