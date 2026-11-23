"""Rotten Tomatoes scorecard (2026-11-13: RT renamed «score» → «scorePercent» "97%" and dropped the audience liked counts;
the night's `films ratings rt` parsed every page without a score and wrote NULL over the stored ones — 129 titles)."""
import gc
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from films import db, ratings
from films.sources import rt

NEW = {"criticsScore": {"averageRating": "8.80", "certified": True, "likedCount": 426, "notLikedCount": 15,
                        "ratingCount": 441, "reviewCount": 441, "scorePercent": "97%", "title": "Tomatometer"},
       "audienceScore": {"averageRating": "4.8", "bandedRatingCount": "25,000+ Verified Ratings", "certified": True,
                         "scorePercent": "96%", "title": "Popcornmeter"}}


class ScoreTest(unittest.TestCase):
    def test_score_percent(self):
        c, a = rt._score(NEW["criticsScore"]), rt._score(NEW["audienceScore"])
        self.assertEqual((c["score"], c["reviews"], c["ratings"]), (97, 441, 441))
        self.assertEqual((a["score"], a["ratings"], a["score_type"]), (96, 25000, "VERIFIED"))

    def test_old_format_still_read(self):
        a = rt._score({"score": "88", "likedCount": 300, "notLikedCount": 50, "scoreType": "ALL"})
        self.assertEqual((a["score"], a["ratings"], a["score_type"]), (88, 350, "ALL"))


class NoBlankingTest(unittest.TestCase):
    def test_page_without_score_keeps_stored_one(self):
        with tempfile.TemporaryDirectory() as d:
            conn = db.connect(Path(d) / "t.db")
            db.migrate(conn)
            conn.execute("INSERT INTO titles (id, kind, title_original, year, rt_url, rt_critics, rt_audience) "
                         "VALUES (1, 'movie', 'Sinners', 2025, 'https://www.rottentomatoes.com/m/sinners_2025', 97, 96)")
            conn.commit()
            url = "https://www.rottentomatoes.com/m/sinners_2025"
            page = {"url": url, "kind": "movie", "critics": None, "audience": None, "seasons": []}
            with mock.patch.object(rt, "find", return_value={"status": "matched", "match": {"url": url}}), \
                    mock.patch.object(rt, "fetch", return_value=page), \
                    mock.patch.object(ratings, "_store_page", lambda *a, **k: None):
                st = ratings.rt_ratings(conn, never_checked=True)
            self.assertEqual(st["matched"], 1)
            self.assertEqual(conn.execute("SELECT rt_critics, rt_audience FROM titles WHERE id = 1").fetchone()[:], (97, 96))
            # another page (a new match): its own empty scores, not the old page's
            other = "https://www.rottentomatoes.com/m/sinners_1985"
            conn.execute("UPDATE titles SET rt_checked_at = NULL")
            conn.commit()
            with mock.patch.object(rt, "find", return_value={"status": "matched", "match": {"url": other}}), \
                    mock.patch.object(rt, "fetch", return_value=dict(page, url=other)), \
                    mock.patch.object(ratings, "_store_page", lambda *a, **k: None):
                ratings.rt_ratings(conn, never_checked=True)
            self.assertEqual(conn.execute("SELECT rt_url, rt_critics FROM titles WHERE id = 1").fetchone()[:], (other, None))
            conn.close()
            gc.collect()


class ParserBrokenStopsTest(unittest.TestCase):
    def test_many_pages_no_score_stops(self):
        with tempfile.TemporaryDirectory() as d:
            conn = db.connect(Path(d) / "t.db")
            db.migrate(conn)
            conn.executemany("INSERT INTO titles (id, kind, title_original, year) VALUES (?, 'movie', ?, 2025)",
                             [(i, f"T{i}") for i in range(1, 61)])
            conn.commit()
            page = {"url": "u", "kind": "movie", "critics": None, "audience": None, "seasons": []}
            with mock.patch.object(rt, "find", return_value={"status": "matched", "match": {"url": "u"}}), \
                    mock.patch.object(rt, "fetch", return_value=page), \
                    mock.patch.object(ratings, "_store_page", lambda *a, **k: None):
                with self.assertRaises(ratings.RTParserBroken):
                    ratings.rt_ratings(conn, never_checked=True)
            conn.close()
            gc.collect()


if __name__ == "__main__":
    unittest.main()
