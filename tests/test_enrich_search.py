"""films.enrich.search_fallback — TMDB name search when TMDB doesn't know the IMDb id (2026-10-25 audit).
Run:  python -X utf8 -m unittest tests.test_enrich_search
"""
import unittest
from unittest import mock

from films import enrich


def cand(**kw):
    c = {"tmdb_id": 1, "title": "X", "original_title": "X", "year": 2025, "score": 0.95, "verified": True,
         "year_diff": 0, "imdb_id": None}
    c.update(kw)
    return c


class TestSearchFallback(unittest.TestCase):
    def run_with(self, res, kind="movie", imdb="tt1", year=2025):
        with mock.patch.object(enrich, "_search_names", return_value=(["Caja Rapida", "Лёгкие деньги"], year)), \
                mock.patch.object(enrich.tmdb, "search", return_value=res) as s:
            out = enrich.search_fallback(None, 7, kind, imdb)
            return out, s

    def test_clear_match_accepted(self):
        (tid, why, det), s = self.run_with({"status": "matched", "reason": "clear", "best": cand(tmdb_id=55),
                                            "candidates": [cand(tmdb_id=55)]})
        self.assertEqual(tid, 55)
        self.assertEqual(s.call_args[0][1:4], (["Caja Rapida", "Лёгкие деньги"], 2025, "movie"))

    def test_tv_kind_searched_as_tv(self):
        (tid, _, _), s = self.run_with({"status": "matched", "reason": "clear", "best": cand(tmdb_id=9, year_diff=None),
                                        "candidates": []}, kind="series")
        self.assertEqual(s.call_args[0][3], "tv")
        self.assertEqual(tid, 9)                       # a series may have started before the season's year

    def test_ambiguous_rejected(self):
        (tid, why, _), _ = self.run_with({"status": "ambiguous", "reason": "close runner-up", "best": None,
                                          "candidates": [cand(), cand(tmdb_id=2)]})
        self.assertIsNone(tid)
        self.assertIn("ambiguous", why)

    def test_film_year_off_by_two_rejected(self):
        (tid, why, _), _ = self.run_with({"status": "matched", "reason": "clear", "best": cand(year_diff=2, year=2023),
                                          "candidates": []})
        self.assertIsNone(tid)
        self.assertIn("year", why)

    def test_other_imdb_id_rejected_same_confirmed(self):
        (tid, why, _), _ = self.run_with({"status": "matched", "reason": "clear", "best": cand(imdb_id="tt999"),
                                          "candidates": []})
        self.assertIsNone(tid)
        self.assertIn("another IMDb id", why)
        (tid, why, _), _ = self.run_with({"status": "matched", "reason": "clear", "best": cand(imdb_id="tt1", tmdb_id=4),
                                          "candidates": []})
        self.assertEqual((tid, why), (4, "IMDb id confirmed"))

    def test_imdb_redirect_to_tmdb_id_accepted(self):
        res = {"status": "matched", "reason": "clear", "best": cand(imdb_id="tt999", tmdb_id=3), "candidates": []}
        with mock.patch.object(enrich, "_imdb_canonical", return_value="tt999"):
            (tid, why, _), _ = self.run_with(res)
        self.assertEqual(tid, 3)
        self.assertIn("redirects", why)
        with mock.patch.object(enrich, "_imdb_canonical", return_value=None):
            (tid, _, _), _ = self.run_with(res)
        self.assertIsNone(tid)

    def test_unverified_rejected(self):
        (tid, _, _), _ = self.run_with({"status": "matched", "reason": "clear", "best": cand(verified=False),
                                        "candidates": []})
        self.assertIsNone(tid)


if __name__ == "__main__":
    unittest.main()
