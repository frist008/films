"""Tests for the text rules of films/sources/trailer_lang.py (speech check v2, agent_trailers3 2026-10-25) — offline,
no Whisper model: the window / island records below are what faster-whisper produced on real trailers.
Run:  python -X utf8 -m unittest tests.test_trailer_lang
"""
import unittest

from films.sources import trailer_lang as tl


def win(lang, p, text, t=0):
    return {"t": t, "lang": lang, "p": p, "words": len(tl.speech_words(text)), "text": text}


class TestFillerAndWords(unittest.TestCase):
    def test_filler_phrases(self):
        for s in ("Субтитры сделал DimaTorzok", "Продолжение следует...", "Спасибо за просмотр!",
                  "Редактор субтитров А.Синецкая Корректор А.Егорова", "Thanks for watching!",
                  "Дякую за перегляд!", "Подписывайтесь на канал"):
            self.assertTrue(tl.is_filler(s), s)

    def test_real_speech_is_not_filler(self):
        for s in ("Быстрее, гребаный ублюдок!", "Я хочу домой!", "You're looking for someone.", None, ""):
            self.assertFalse(tl.is_filler(s), s)

    def test_sound_tags_dropped(self):
        # «Надежда» (CObCsRDJYeI): Whisper writes music / shots as ALL-CAPS tags
        self.assertEqual(tl.speech_words("НАПРЯЖЕННАЯ МУЗЫКА Быстрее, гребаный ублюдок! ВСКРИК ВЫСТРЕЛЫ"),
                         {"быстрее", "гребаный", "ублюдок"})
        self.assertEqual(tl.speech_words("[MUSIC] (laughs) Oh"), {"oh"})
        self.assertEqual(tl.speech_words("[Музыка] (смеётся)"), set())
        self.assertEqual(tl.speech_words("ИНТРИГУЮЩАЯ МУЗЫКА"), set())

    def test_chant_counts_once(self):
        # «Супермен» (tNioP6vFU4I): a chorus hallucinated as «Superman!» ×11
        self.assertEqual(tl.speech_words("Superman! " * 11), {"superman"})


class TestWindowVerdict(unittest.TestCase):
    def test_dubbed_trailer_under_music(self):   # «Надежда»: v1 (VAD) heard 0 s of speech
        wins = [win("en", 0.453, ""), win("ru", 0.769, "ТРЕВОЖНАЯ МУЗЫКА КРИК ВЫСТРЕЛ", 30),
                win("ru", 0.982, "НАПРЯЖЕННАЯ МУЗЫКА Быстрее, гребаный ублюдок! Оно прибежается!", 60),
                win("ru", 0.875, "Надежда.", 90)]
        self.assertEqual(tl.window_verdict(wins)["verdict"], "ru")

    def test_few_russian_words_and_low_confidence_noise(self):   # «Супермен»
        wins = [win("en", 0.631, ""), win("ru", 0.871, "ИНТРИГУЮЩАЯ МУЗЫКА Крипто! Домой! Я хочу домой!", 60),
                win("en", 0.518, "Superman! " * 11, 90)]
        self.assertEqual(tl.window_verdict(wins)["verdict"], "ru")

    def test_original_with_subtitles_is_other(self):   # «Живая ярость — Русский трейлер (Субтитры)»
        wins = [win("en", 0.889, "You're looking for someone."), win("en", 0.962, "So am I. My wife would miss it.", 30),
                win("en", 0.857, "No! You daughter, she might still be alive.", 60)]
        r = tl.window_verdict(wins)
        self.assertEqual((r["verdict"], r["language"]), ("other", "en"))

    def test_english_dialogue_beats_a_russian_tagline(self):
        wins = [win("en", 0.95, "I never wanted any of this, you know that, right?"),
                win("en", 0.9, "We leave tonight before they find us here.", 30),
                win("ru", 0.9, "Скоро в кино", 60)]
        self.assertEqual(tl.window_verdict(wins)["verdict"], "other")

    def test_low_confidence_russian_is_unknown(self):   # «Альфа» teaser: ru 0.353
        wins = [win("ru", 0.353, "Мамы! Мамы! Помнишь меня? Альфа.", 30)]
        self.assertEqual(tl.window_verdict(wins)["verdict"], "unknown")

    def test_ukrainian_letters_are_not_russian(self):
        wins = [win("ru", 0.9, "Він повернувся додому і все змінилося"), win("ru", 0.9, "Ніхто не знає правди", 30)]
        self.assertNotEqual(tl.window_verdict(wins)["verdict"], "ru")

    def test_music_only_is_unknown(self):
        self.assertEqual(tl.window_verdict([win("en", 0.5, ""), win("nn", 0.37, "", 30)])["verdict"], "unknown")


class TestIslandVerdict(unittest.TestCase):
    def test_short_russian_lines_under_music(self):   # «Супермен»: two 8 s pieces ru 0.86 / 0.90, one en 0.73
        self.assertEqual(tl.island_verdict(2, 4, {"en": 1}, 0)["verdict"], "ru")

    def test_only_english_song(self):   # «Гадкая сестра»: «I'm going crazy…» in every upload
        self.assertEqual(tl.island_verdict(0, 0, {"en": 4}, 0)["verdict"], "other")

    def test_nothing_confident(self):
        self.assertEqual(tl.island_verdict(0, 0, {}, 0)["verdict"], "unknown")
        self.assertEqual(tl.island_verdict(1, 2, {}, 0)["verdict"], "unknown")   # < 3 Russian words


class TestUploadDate(unittest.TestCase):
    def test_upload_long_before_release_is_another_film(self):
        # «Синяя коробка» (2026-04-16): a 2024-03-28 clip-channel upload with the same Russian name
        self.assertTrue(tl.uploaded_too_early("2024-03-28T05:44:44-07:00", "2026-04-16"))

    def test_normal_marketing_window(self):
        self.assertFalse(tl.uploaded_too_early("2024-07-24T00:00:00-07:00", "2025-04-30"))   # «Формула воды»
        self.assertFalse(tl.uploaded_too_early("2025-09-06", "2025-04-16"))                    # after the release
        self.assertFalse(tl.uploaded_too_early(None, "2025-04-16"))
        self.assertFalse(tl.uploaded_too_early("2025-09-06", None))
        self.assertFalse(tl.uploaded_too_early("garbage", "2025-04-16"))


class TestAlternativesBudget(unittest.TestCase):
    def test_alternatives_stop_after_budget(self):
        """2026-10-25: «Восемь сотен» held the check 12 min on alternatives — over ALT_BUDGET_S the title is left as is."""
        import sqlite3
        from unittest import mock
        from films import db
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        db.migrate(conn)
        conn.execute("INSERT INTO titles (id, kind, title_original, title_ru, year, trailer_ru_url) VALUES "
                     "(1, 'movie', 'X', 'Икс', 2020, 'https://www.youtube.com/watch?v=AAAAAAAAAAA')")
        conn.commit()
        checked = []
        clock = iter(range(0, 10_000, 100))          # every monotonic() call: +100 s
        def fake_check(yt, vid, cache):
            checked.append(vid)
            return {"verdict": "unknown", "method": "speech"}
        with mock.patch.object(tl.db, "connect", return_value=conn), \
                mock.patch.object(tl.yt_mod, "YouTube"), mock.patch.object(tl, "_load_checks", return_value={}), \
                mock.patch.object(tl, "check_video", side_effect=fake_check), \
                mock.patch.object(tl, "_alternatives", return_value=[f"ALT{i:08d}" for i in range(10)]), \
                mock.patch.object(tl, "_no_dialogue", return_value=False), \
                mock.patch.object(tl.time, "monotonic", side_effect=lambda: next(clock)), \
                mock.patch.object(tl.shutil, "rmtree"):
            st = tl.check_titles([1], dry_run=True, log=lambda m: None)
        self.assertEqual(st.get("alt_timeout"), 1)
        self.assertEqual(st["unknown"], 1)
        self.assertLess(len(checked), 1 + 10)        # not every alternative was audio-checked


class TestBlockMessages(unittest.TestCase):
    def test_bot_checks_stop_the_run(self):
        for msg in ("Sign in to confirm you're not a bot", "HTTP Error 429: Too Many Requests"):
            self.assertTrue(tl._BLOCK.search(msg), msg)

    def test_per_video_sign_in_walls_are_not_a_block(self):
        # 2026-10-25: right after p-saH5hQ8m8 refused, other videos downloaded fine — a per-video wall (skill 3б)
        for msg in ("ERROR: [youtube] i0PBnOAi-GY: Sign in to confirm your age. This video may be inappropriate for some users.",
                    "ERROR: [youtube] p-saH5hQ8m8: Please sign in. This helps protect our community. Use --cookies"):
            self.assertFalse(tl._BLOCK.search(msg), msg)


class TestConfirmedChecks(unittest.TestCase):
    def test_check_values(self):
        self.assertIn("teaser_only", tl.CONFIRMED)
        self.assertIn("no_speech", tl.CONFIRMED)
        self.assertEqual(tl.check_method({"verdict": "ru", "method": "speech_v2 (no VAD …); v1 speech said unknown"}),
                         "speech")
        self.assertEqual(tl.check_method({"verdict": "ru", "method": "watch:asr_captions"}), "asr")


HIDDEN_BLADE = {"kind": "movie", "year": 2023, "title_ru": "Скрытый клинок", "title_original": "无名",
                "title_latin": "Hidden Blade", "title_uk": None}
SHINOBI = {"kind": "movie", "year": 2005, "title_ru": "Синоби", "title_original": "Shinobi", "title_latin": None,
           "title_uk": "Шінобі"}
SCREAM2 = {"kind": "movie", "year": 1997, "title_ru": "Крик 2", "title_original": "Scream 2", "title_latin": None,
           "title_uk": "Крик 2"}


class TestCutFilters(unittest.TestCase):
    """Real search results of the 2026-10-25 run (agent_trailers5)."""

    def test_right_film(self):
        ok = [(HIDDEN_BLADE, "Скрытый клинок 2023   ТРЕЙЛЕР НА РУССКОМ"),
              (HIDDEN_BLADE, "HIDDEN BLADE (2023) Official International Trailer | Watch Online July"),
              (HIDDEN_BLADE, "[TEASER] Hidden Blade 無名 61'"),          # traditional 無 = simplified 无
              (HIDDEN_BLADE, "《无名》预告片 1  《Hidden Blade》 trailer"),
              (SHINOBI, "Shinobi: Heart Under Blade (2005) ORIGINAL TRAILER [HD]"),
              (SCREAM2, "Крик 2 (трейлер)"),
              (SCREAM2, "Scream 2 (1997) Trailer #1 | Neve Campbell, Courteney Cox, David Arquette")]
        for row, title in ok:
            self.assertTrue(tl.cut_title_ok(row, title), title)

    def test_other_videos_dropped(self):
        bad = [(HIDDEN_BLADE, "The Hidden Blade - Official Trailer"),                   # Latin name only, no year
               (HIDDEN_BLADE, "Assassin's Creed Hidden Blade Trailer ( High Quality )"),
               (HIDDEN_BLADE, "Hidden Blade in the Shadows | Cinematic Trailer | Gamflix"),
               (SHINOBI, "For Honor Shadow & Might - Синоби - Тизер"),                  # a game
               (SHINOBI, '18+ ТРЕЙЛЕР Сериала "ЧУЖАК" По мотивам Летописи "ПУТЬ СИНОБИ"'),  # another series
               (SHINOBI, "Shinobi: Heart Under Blade (2005) FAN TEASER (HD)"),
               (SHINOBI, "Shinobi III: Return of the Ninja Master - Sega Genesis trailer"),
               (SCREAM2, "GTA SA - КРИК 2 - ТРЕЙЛЕР"),
               (SCREAM2, "SCREAM 2 (1997) RE-CUT TRAILER"),
               (SCREAM2, "Scream 2 (1997) - Modern Trailer"),
               (SCREAM2, "SCREAM 2 Official Teaser Trailer | Bloxburg  Film"),
               (SCREAM2, "Крик 2 (2022) трейлер"),                                     # another year
               (SCREAM2, "Крик 2 — обзор фильма"),                                     # not a trailer
               ({**SCREAM2, "title_ru": "Туган Батыр", "title_uk": None, "title_original": "Туган Батыр",
                 "year": 2025}, "Туган Батыр | Трейлер 1 серия: Туган Батыр идёт спасать отца")]
        for row, title in bad:
            self.assertFalse(tl.cut_title_ok(row, title), title)

    def test_latin_name_with_cast(self):
        self.assertTrue(tl.cut_title_ok(HIDDEN_BLADE, "Hidden Blade trailer Tony Leung", cast={"leung"}))


def up(vid, length, views=0, title="", verified=False):
    return {"id": vid, "length": length, "views": views, "title": title,
            "owner_badges": ["BADGE_STYLE_TYPE_VERIFIED"] if verified else []}


class TestCutGrouping(unittest.TestCase):
    def test_close_lengths_are_one_cut(self):
        g = tl.group_cuts([up("a", 92), up("b", 88), up("c", 90), up("d", 134), up("e", 136), up("f", 61),
                           up("g", None)])
        self.assertEqual([[v["id"] for v in x] for x in g], [["f"], ["b", "c", "a"], ["d", "e"]])

    def test_four_seconds_apart_are_different_cuts(self):
        self.assertEqual(len(tl.group_cuts([up("a", 100), up("b", 104)])), 2)

    def test_chain_within_tolerance(self):   # 88, 90, 92: each within 3 s of the previous → one cut
        self.assertEqual(len(tl.group_cuts([up("a", 88), up("b", 90), up("c", 92)])), 1)

    def test_best_upload(self):
        g = [up("fan", 90, views=10 ** 6, title="trailer"), up("hd", 90, views=10, title="Trailer HD"),
             up("off", 90, views=5, title="Trailer", verified=True)]
        self.assertEqual(sorted(g, key=tl.best_upload_key)[0]["id"], "off")
        self.assertEqual(sorted(g[:2], key=tl.best_upload_key)[0]["id"], "hd")


class TestCutPresentation(unittest.TestCase):
    def test_lengths(self):
        self.assertEqual(tl.mmss(42), "42 с")
        self.assertEqual(tl.mmss(60), "1 мин")
        self.assertEqual(tl.mmss(130), "2 мин 10 с")

    def test_languages(self):
        self.assertEqual(tl.cut_language({"verdict": "ru"}), "русский")
        self.assertEqual(tl.cut_language({"verdict": "other", "language": "fr", "transcript": "Je me suis dit"}),
                         "французский")
        # «la» on a French trailer with no transcript = Whisper's guess on music, not Latin
        self.assertEqual(tl.cut_language({"verdict": "other", "language": "la", "transcript": ""}),
                         "речь не распознана (под музыкой)")
        self.assertEqual(tl.cut_language({"verdict": "unknown"}), "речь не распознана (под музыкой)")
        self.assertTrue(tl.cut_language({"verdict": "unknown", "error": "x", "meta_lang": "en"})
                        .startswith("язык не проверить"))

    def test_quotes(self):
        self.assertEqual(tl.cut_quote({"verdict": "ru", "transcript": "Я хочу домой!"}), "Я хочу домой!")
        self.assertTrue(len(tl.cut_quote({"verdict": "other", "language": "zh",
                                          "transcript": "华人影业的海外观众朋友们大家好我是华一波今天由我主演的电影无名"})) <= 31)
        self.assertEqual(tl.cut_quote({"verdict": "unknown", "transcript": "noise"}), "")

    def test_question(self):
        f = {"title_id": 4109, "title_ru": "Крик 2", "orig": "Scream 2", "year": 1997, "is_animation": 0, "cuts": [
            {"id": "aaaaaaaaaaa", "length": 61, "verdict": "ru", "transcript": "Хэлло", "channel": "A", "uploads": 1,
             "current": []},
            {"id": "bbbbbbbbbbb", "length": 134, "verdict": "unknown", "channel": "YouTube Movies", "uploads": 4,
             "current": ["ru"]},
            {"id": "ccccccccccc", "length": 146, "verdict": "other", "language": "en", "transcript": "Hello, Sydney.",
             "channel": "RT", "uploads": 3, "current": []},
            {"id": "ddddddddddd", "length": 30, "verdict": "other", "language": "en", "transcript": "Hi", "channel": "C",
             "uploads": 2, "current": []}]}
        q = tl.cuts_question(f)
        self.assertEqual([o["label"] for o in q["options"]],
                         ["1 мин 1 с — русский", "2 мин 14 с — речь не распознана", "2 мин 26 с — английский", "Ни один"])
        self.assertEqual(len(q["links_markdown"]), 4)
        self.assertIn("сейчас стоит", q["links_markdown"][2])
        self.assertIn("таких роликов 4", q["links_markdown"][2])
        self.assertNotIn("http", q["question"])
        self.assertIn("4 разных версий", q["question"])


if __name__ == "__main__":
    unittest.main()
