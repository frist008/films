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


class TestConfirmedChecks(unittest.TestCase):
    def test_check_values(self):
        self.assertIn("teaser_only", tl.CONFIRMED)
        self.assertIn("no_speech", tl.CONFIRMED)
        self.assertEqual(tl.check_method({"verdict": "ru", "method": "speech_v2 (no VAD …); v1 speech said unknown"}),
                         "speech")
        self.assertEqual(tl.check_method({"verdict": "ru", "method": "watch:asr_captions"}), "asr")


if __name__ == "__main__":
    unittest.main()
