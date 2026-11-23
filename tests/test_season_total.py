"""2026-11-23: «Тетрадь смерти: Новое поколение [s01-03 из 03]» — the uploader's «s01-03» are 3 episodes of the single
season (TMDB 78004), and matching without TMDB made seasons 2 and 3 out of nothing."""
import sqlite3
import unittest

from films import db, match


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    db.migrate(c)
    c.execute("INSERT INTO titles (id, kind, season_number, series_key, series_imdb_id, seasons_total, title_ru) "
              "VALUES (1, 'series', 1, 'imdb:tt6332396', 'tt6332396', 1, 'Тетрадь смерти: Новое поколение')")
    return c


class SeasonTotalTest(unittest.TestCase):
    def test_no_season_past_total(self):
        c = _conn()
        self.addCleanup(c.close)
        ids = match._season_titles(c, "tt6332396", None, {"title_ru": "Тетрадь смерти: Новое поколение"},
                                   "foreign_series", [1, 2, 3])
        self.assertEqual(ids, [1])
        self.assertEqual(c.execute("SELECT count(*) FROM titles").fetchone()[0], 1)

    def test_only_past_total_is_an_error(self):
        c = _conn()
        self.addCleanup(c.close)
        with self.assertRaises(LookupError):
            match._season_titles(c, "tt6332396", None, {}, "foreign_series", [2])

    def test_unknown_total_unchanged(self):
        c = _conn()
        self.addCleanup(c.close)
        c.execute("UPDATE titles SET seasons_total = NULL")
        ids = match._season_titles(c, "tt6332396", None, {}, "foreign_series", [1, 2])
        self.assertEqual(len(ids), 2)


if __name__ == "__main__":
    unittest.main()
