"""Tests for trusting the IMDb id of a release page (films/match.py) and normalizing it (films/parse/rutor.py).

34 titles were made from a wrong page id (an episode, a podcast, another film, a film id for a season pack, a series id
for a film, ids padded with a leading zero) — fixed 2026-10-25; these rules keep the matcher from recreating them.
Offline: TMDB calls are mocked, the IMDb datasets are an in-memory table.
Run:  python -X utf8 -m unittest tests.test_match_page_ids
"""
import sqlite3
import unittest
from unittest import mock

from films import match
from films.parse.rutor import norm_imdb_id


def rec(ttype, year=None):
    return {"tconst": "tt1", "title_type": ttype, "primary_title": None, "original_title": None, "start_year": year,
            "end_year": None, "runtime_min": None, "genres": ""}


class TestNormImdbId(unittest.TestCase):
    def test_leading_zero(self):
        self.assertEqual(norm_imdb_id("tt03748172"), "tt3748172")     # «Игра Джералда»
        self.assertEqual(norm_imdb_id("tt09484998"), "tt9484998")     # «Палм-Спрингс»
        self.assertEqual(norm_imdb_id("tt0054135"), "tt0054135")      # 7 digits with a zero are IMDb's own
        self.assertEqual(norm_imdb_id("tt10676052"), "tt10676052")
        self.assertEqual(norm_imdb_id("tt00123456"), "tt0123456")
        self.assertIsNone(norm_imdb_id(None))

    def test_canonical_uses_it(self):
        match._REDIRECTS = {}
        self.assertEqual(match.canonical_imdb("tt03748172"), "tt3748172")


class TestDistrust(unittest.TestCase):
    def test_episode(self):
        self.assertEqual(match._page_id_distrust(rec("tvEpisode", 2020), {"name": "Пассажиры / Passengers (2016)"}, [],
                                                 2016, 2016), "episode id")
        special = {"name": "Доктор Кто: Рождественский спецвыпуск (2023)"}
        self.assertIsNone(match._page_id_distrust(rec("tvEpisode", 2023), special, [], 2023, 2023))

    def test_film_id_for_a_season_release(self):
        np = {"name": "Приключения Джимми Нейтрона [S01-03] (2002-2006)"}
        self.assertEqual(match._page_id_distrust(rec("movie", 2001), np, [1, 2, 3], 2002, 2006), "movie id for a season release")
        self.assertIsNotNone(match._page_id_distrust(rec("short", 2010), np, [1], 2010, 2010))
        self.assertIsNone(match._page_id_distrust(rec("tvSeries", 2002), np, [1, 2, 3], 2002, 2006))
        self.assertIsNone(match._page_id_distrust(rec("movie", 2001), {"name": "x"}, [], 2001, 2001))

    def test_unknown_id(self):
        self.assertIsNotNone(match._page_id_distrust(rec("unknown"), {"name": "Я краснею (2022)"}, [], 2022, 2022))
        self.assertIsNone(match._page_id_distrust(rec("unknown"), {"name": "old film (1975)"}, [], 1975, 1975))
        self.assertEqual(match._page_id_distrust(rec("tvSeason"), {"name": "x"}, [], 2024, 2024), "season id")


class TestPageRecord(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("CREATE TABLE imdb_basics (tconst TEXT PRIMARY KEY, title_type TEXT, primary_title TEXT, "
                          "original_title TEXT, start_year INTEGER, end_year INTEGER, runtime_min INTEGER, genres TEXT)")
        self.conn.execute("INSERT INTO imdb_basics VALUES ('tt10676052', 'movie', 'The Fantastic 4: First Steps', "
                          "'The Fantastic 4: First Steps', 2025, NULL, 115, 'Action')")

    def test_datasets_first(self):
        with mock.patch.object(match.tmdb, "find_by_imdb") as find:
            self.assertEqual(match._page_record(self.conn, "tt10676052")["start_year"], 2025)
            find.assert_not_called()

    def test_older_id_typed_from_tmdb(self):
        finds = {"tt0054135": {"movie": 299, "movie_year": 1960, "movie_title": "Ocean's Eleven"},
                 "tt0417299": {"tv": 246, "tv_year": 2005, "tv_title": "Avatar: The Last Airbender"},
                 "tt1368311": {"tv_episode": {"show_id": 1, "name": "Melissa and Bradley"}},
                 "tt18097030": {}}
        with mock.patch.object(match.tmdb, "find_by_imdb", side_effect=lambda conn, tt: finds[tt]):
            ocean = match._page_record(self.conn, "tt0054135")
            self.assertEqual((ocean["title_type"], ocean["start_year"]), ("movie", 1960))
            self.assertEqual(match._page_record(self.conn, "tt0417299")["title_type"], "tvSeries")
            self.assertEqual(match._page_record(self.conn, "tt1368311")["title_type"], "tvEpisode")
            self.assertEqual(match._page_record(self.conn, "tt18097030")["title_type"], "unknown")
        # «Одиннадцать друзей Оушена (2001)» with the 1960 film's id: the year check now sees the 1960 record
        self.assertFalse(match._year_ok(ocean, "movie", 2001, 2001))


class TestFilmNotSeries(unittest.TestCase):
    P = {"title_original": "Justice League: Gods and Monsters", "title_ru": "Лига справедливости: Боги и монстры",
         "name_parsed": {"name": "Лига справедливости: Боги и монстры / Justice League: Gods and Monsters (2015)"}}

    def test_film_found(self):
        found = {"status": "matched", "best": {"tmdb_id": 323027, "name_fit": 1.0, "year_diff": 0}, "candidates": []}
        with mock.patch.object(match.tmdb, "search", return_value=found):
            self.assertTrue(match._film_not_series(None, self.P, "animation", rec("tvSeries", 2015), [], 2015))
            self.assertFalse(match._film_not_series(None, self.P, "foreign_series", rec("tvSeries", 2015), [], 2015))
            self.assertFalse(match._film_not_series(None, self.P, "animation", rec("tvSeries", 2015), [1], 2015))
            self.assertFalse(match._film_not_series(None, self.P, "animation", rec("movie", 2015), [], 2015))

    def test_same_work_typed_differently(self):
        found = {"status": "matched", "best": {"tmdb_id": 1617245, "name_fit": 1.0, "year_diff": 0, "imdb_id": "tt39307240"},
                 "candidates": []}
        with mock.patch.object(match.tmdb, "search", return_value=found):   # «Зверский коп / Bêtes de Flic (2026)»
            self.assertFalse(match._film_not_series(None, self.P, "foreign_films", rec("tvSeries", 2026), [], 2026,
                                                    "tt39307240"))

    def test_no_film(self):
        with mock.patch.object(match.tmdb, "search", return_value={"status": "not_found", "best": None, "candidates": []}):
            self.assertFalse(match._film_not_series(None, self.P, "foreign_films", rec("tvMiniSeries", 2000), [], 2000))


if __name__ == "__main__":
    unittest.main()
