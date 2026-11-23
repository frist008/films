"""The «нет озвучки» filter hides only releases with neither Russian nor Ukrainian speech (review 2026-10-28, round 3:
rustorka «MVO [1win Studio]» only in the title, a Russian track of unknown kind, Ukrainian original speech — all hidden)."""
import unittest

from films import rank
from films.parse.rutracker import name_tail_voice


class TailVoiceTest(unittest.TestCase):
    def test_rustorka_title_tail(self):
        for title, want in (
                ("Хранители: Часть 1 / Watchmen: Chapter I (Брэндон Виетти) [2024, мультфильм, WEBRip 1080p] MVO [1WINStudio]", "mvo"),
                ("X / Y [2024, драма, WEB-DL 1080p] DUB | Red Head Sound", "dub"),
                ("X / Y [2024, драма, WEB-DL 2160p] [HEVC] [SDR] | MVO(HDrezka Studio)", "mvo"),
                ("X / Y [2024, драма, WEB-DL 1080p] Movie Dubbing", "dub"),
                ("X / Y [2024, драма, WEB-DL 1080p] + Sub Rus", None),
                ("Dubai Bling (Sezon 2) [2023, реалити, WEB-DL]", None)):
            d = {"best_voice": None, "audio": [{"lang": "en"}]}
            name_tail_voice(d, title)
            self.assertEqual(d["best_voice"], want, title)

    def test_page_voice_kept(self):
        d = {"best_voice": "dub"}
        name_tail_voice(d, "X [2024, WEB-DL] MVO")
        self.assertEqual(d["best_voice"], "dub")


class HasVoiceTest(unittest.TestCase):
    def test_russian_track_of_unknown_kind_is_a_voice(self):
        self.assertTrue(rank.has_voice({"best_voice": None, "audio": '[{"lang": "ru", "kind": null}]'}))   # «Процветание»
        self.assertTrue(rank.has_voice({"best_voice": "mvo", "audio": "[]"}))
        self.assertFalse(rank.has_voice({"best_voice": "none", "audio": '[{"lang": "en"}, {"lang": null}]'}))
        self.assertFalse(rank.has_voice({"best_voice": None, "audio": None}))

    def test_cartoons_never_hidden_for_no_voice(self):
        # owner, 2026-10-28: «Нет фильтра скрытия для отсутствующей озвучки для мультиков ты что-то придумал»
        self.assertTrue(rank.is_cartoon({"tracker_category": "animation"}))
        self.assertTrue(rank.is_cartoon({"tracker_category": "foreign_films", "genres": '["мультфильм", "приключения"]'}))
        self.assertTrue(rank.is_cartoon({"tracker_category": "anime"}))
        self.assertFalse(rank.is_cartoon({"tracker_category": "foreign_films", "genres": '["драма"]'}))

    def test_original_speech(self):
        self.assertTrue(rank.original_voice_ok({"original_language": "uk"}, False))   # «Новый год кувырком»
        self.assertTrue(rank.original_voice_ok({"original_language": "ru"}, True))    # «Универ» in the owner's «хочу»
        self.assertFalse(rank.original_voice_ok({"original_language": "ru"}, False))  # the owner's call (asked)
        self.assertFalse(rank.original_voice_ok({"original_language": "en"}, True))


class RussianOriginalTest(unittest.TestCase):
    """Owner, 2026-10-28: Russian-language productions «Скрыть все … кроме выбранных русских франшиз», Kazakh ones shot in
    Russian — «Скрывать»."""
    def test_tmdb_language(self):
        self.assertTrue(rank.russian_original({"original_language": "ru"}))
        self.assertFalse(rank.russian_original({"original_language": "ru", "is_animation": 1}))   # cartoons untouched
        self.assertFalse(rank.russian_original({"original_language": "kk"}))

    def test_tracker_only_rows(self):
        kz = {"tracker_only": 1, "title_ru": "Эскорт", "title_original": "Эскорт", "countries": '["Казахстан"]'}
        orig_ru = [{"best_voice": None, "audio": '[{"lang": "ru", "kind": null}]'}]
        self.assertTrue(rank.russian_original(kz, orig_ru))
        dubbed = [{"best_voice": "mvo", "audio": '[{"lang": "ru", "kind": "mvo"}]'}]
        self.assertFalse(rank.russian_original(kz, dubbed))                       # a Kazakh film voiced over in Russian
        self.assertFalse(rank.russian_original(dict(kz, title_original="Eskort"), orig_ru))   # a foreign original name
        self.assertTrue(rank.russian_original({"tracker_only": 1, "countries": '["Россия"]', "title_original": "X"}))
        ua = [{"best_voice": None, "audio": '[{"lang": "uk", "kind": null}, {"lang": "ru", "kind": null}]'}]
        self.assertFalse(rank.russian_original(dict(kz, countries=None), ua))


if __name__ == "__main__":
    unittest.main()
