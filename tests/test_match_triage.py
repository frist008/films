"""Triage of open match_issues (films/match_triage.py, plan audit 2026-10-25): RT pages decided by data, title-level
enrich issues closed as obsolete / TMDB data gaps. Offline: an in-memory DB with the real schema, RT pages faked.
Run:  python -X utf8 -m unittest tests.test_match_triage
"""
import json
import sqlite3
import unittest

from films import db, match_triage as M


def mem_db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    db.migrate(c)
    return c


def page(url, title, year, critics=None, reviews=None, director=None, seasons=None, kind="movie"):
    return {"url": url, "title": title, "year": year, "critics": critics, "critics_reviews": reviews, "audience": None,
            "audience_ratings": None, "kind": kind, "info": {"Director": director} if director else {},
            "seasons": seasons or []}


class TestRtDecide(unittest.TestCase):
    def setUp(self):
        self.c = c = mem_db()
        c.execute("INSERT INTO titles (id, kind, title_original, year) VALUES (1, 'movie', 'Monsters vs Aliens', 2009)")
        c.execute("INSERT INTO people (id, tmdb_id, name) VALUES (1, 10, 'Rob Letterman')")
        c.execute("INSERT INTO credits (title_id, person_id, role) VALUES (1, 1, 'director')")
        self.t = dict(c.execute("SELECT * FROM titles WHERE id = 1").fetchone())

    def decide(self, cands, pages, t=None):
        return M.rt_decide(self.c, t or self.t, cands, lambda u: pages.get(u))

    def test_director_decides_among_duplicates(self):
        cands = [{"url": f"u{i}", "score": 1.0} for i in range(3)]
        pages = {"u0": page("u0", "Monsters vs. Aliens", 2009, director="Somebody Else"),
                 "u1": page("u1", "Monsters vs. Aliens", 2009, 73, 180, director="Rob Letterman, Conrad Vernon"),
                 "u2": page("u2", "Monsters vs. Aliens", 2009)}
        got, why = self.decide(cands, pages)
        self.assertEqual(got["url"], "u1")
        self.assertIn("режиссёр", why)

    def test_duplicate_stubs_without_directors(self):
        self.c.execute("DELETE FROM credits")
        cands = [{"url": "a", "score": 1.0}, {"url": "b", "score": 1.0}]
        pages = {"a": page("a", "Legend of the Guardians", 2010), "b": page("b", "Legend of the Guardians", 2010, 51, 132)}
        self.assertEqual(self.decide(cands, pages)[0]["url"], "b")
        pages["a"]["critics"], pages["a"]["critics_reviews"] = 60, 100   # scores on both: not guessed
        self.assertIsNone(self.decide(cands, pages)[0])

    def test_single_weak_candidate_needs_director(self):
        cands = [{"url": "x", "score": 0.525}]
        self.assertIsNone(self.decide(cands, {"x": page("x", "Three Steps Above Heaven", 2009, 50, 10, director="Fernando González Molina")})[0])
        self.assertEqual(self.decide(cands, {"x": page("x", "MvA", 2010, 50, 10, director="Rob Letterman")})[0]["url"], "x")
        self.assertIsNone(self.decide(cands, {"x": page("x", "MvA", 2014, 50, 10, director="Rob Letterman")})[0])   # year

    def test_series_season(self):
        self.c.execute("INSERT INTO imdb_basics (tconst, title_type, primary_title, start_year) VALUES ('tt0388629', 'tvSeries', 'One Piece', 1999)")
        t = {"id": 2, "kind": "series", "season_number": 7, "year": 2004, "series_imdb_id": "tt0388629"}
        self.assertIsNone(self.decide([{"url": "p1", "score": 1.0}], {"p1": page("p1", "One Piece", 2023, seasons=[{"season": 7}], kind="series")}, t)[0])
        s = lambda n, crit=None: {"season": n, "critics": crit}
        cands = [{"url": u, "score": 1.0} for u in ("p1", "p2", "p3")]
        pages = {"p1": page("p1", "One Piece", 1999, seasons=[s(1), s(2)], kind="series"),
                 "p2": page("p2", "One Piece", 1999, seasons=[s(i) for i in range(1, 21)], kind="series"),
                 "p3": page("p3", "One Piece", 2023, seasons=[s(1, 85)], kind="series")}
        got, why = self.decide(cands, pages, t)
        self.assertEqual(got["url"], "p2")


class TestTitles(unittest.TestCase):
    def test_obsolete_superseded_and_gaps(self):
        c = mem_db()
        c.execute("INSERT INTO titles (id, kind, imdb_id) VALUES (1, 'movie', 'tt1'), (2, 'movie', 'tt2'), (3, 'series', NULL)")
        c.execute("UPDATE titles SET duplicate_of = 1 WHERE id = 2")
        ins = lambda prob, d: c.execute("INSERT INTO match_issues (problem, candidates) VALUES (?, ?)", (prob, json.dumps(d)))
        ins("tmdb_no_movie_for_imdb", {"imdb_id": "tt1", "title_id": 1})
        ins("tmdb_no_movie_for_imdb", {"imdb_id": "tt1", "title_id": 1, "search": "newer"})
        ins("tmdb_no_movie_for_imdb", {"imdb_id": "tt2", "title_id": 2})
        ins("tmdb_no_season", {"season": 3, "title_id": 3})
        st = M.titles(c, log=lambda *_: None)
        self.assertEqual((st["superseded"], st["obsolete"]), (1, 1))
        self.assertEqual(st["data_gap"], {"tmdb_no_movie_for_imdb": 1, "tmdb_no_season": 1})
        self.assertEqual(c.execute("SELECT count(*) FROM match_issues WHERE resolved_at IS NULL").fetchone()[0], 0)
        notes = [json.loads(r[0]) for r in c.execute("SELECT candidates FROM match_issues ORDER BY id")]
        self.assertEqual(notes[0]["resolved_rule"], "superseded")          # the older duplicate
        self.assertEqual(notes[1]["resolved_rule"], "tmdb_data_gap")
        self.assertEqual(notes[1]["search"], "newer")                        # the details kept
        self.assertEqual(notes[2]["resolved_rule"], "obsolete")


if __name__ == "__main__":
    unittest.main()
