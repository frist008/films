"""update-db step 5 (films ratings rt --recent-days 90): titles released in the last 90 days and titles with releases
but no RT score are re-checked, except ones already checked in the last 20 h (step 4 of the same run).
Run:  python -X utf8 -m unittest tests.test_ratings_recheck
"""
import sqlite3
import unittest
from datetime import date, timedelta

from films import db, ratings


def mem_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.migrate(conn)
    return conn


class RecheckSelection(unittest.TestCase):
    def test_selection(self):
        c = mem_db()
        today = date.today()
        rows = [  # id, release_date, rt_critics, rt_checked_at, has release, tracker_only
            (1, (today - timedelta(days=10)).isoformat(), 90, "2026-01-01 00:00:00", 0, 0),   # recent → yes
            (2, (today - timedelta(days=200)).isoformat(), 80, "2026-01-01 00:00:00", 1, 0),  # old, has score → no
            (3, (today - timedelta(days=200)).isoformat(), None, "2026-01-01 00:00:00", 1, 0),  # no score + release → yes
            (4, (today - timedelta(days=200)).isoformat(), None, None, 0, 0),                 # no score, no release → no
            (5, (today - timedelta(days=5)).isoformat(), None, None, 1, 1),                   # tracker-only → no
            (6, (today - timedelta(days=5)).isoformat(), None, "now", 1, 0),                  # checked just now → no
            (7, (today + timedelta(days=30)).isoformat(), 70, "2026-01-01 00:00:00", 0, 0),   # not released yet → no
            (8, (today - timedelta(days=200)).isoformat(), None,                              # no score, checked 3 days
             (today - timedelta(days=3)).isoformat() + " 00:00:00", 1, 0),                   # ago → weekly → no
        ]
        for tid, rd, crit, chk, rel, tro in rows:
            c.execute("INSERT INTO titles (id, kind, title_original, year, release_date, rt_critics, rt_checked_at, tracker_only)"
                      " VALUES (?, 'movie', ?, 2025, ?, ?, " + ("datetime('now')" if chk == "now" else "?") + ", ?)",
                      (tid, f"T{tid}", rd, crit, tro) if chk == "now" else (tid, f"T{tid}", rd, crit, chk, tro))
            if rel:
                c.execute("INSERT INTO releases (id, source, source_id, url, name, category) VALUES (?, 'rutor', ?, 'u', 'n', 'movie')",
                          (tid, str(tid)))
                c.execute("INSERT INTO release_titles (release_id, title_id) VALUES (?, ?)", (tid, tid))
        got = {r[0] for r in c.execute(ratings.RECHECK_SQL, ("-90 days",))}
        self.assertEqual(got, {1, 3})


class AmbiguousRT(unittest.TestCase):
    """An ambiguous RT search goes through match_triage.rt_decide first; undecided → one rt_ambiguous issue per title ever."""

    def setUp(self):
        self.c = mem_db()
        self.c.execute("INSERT INTO titles (id, kind, title_original, year) VALUES (1, 'movie', 'Film', 2025)")
        self.c.commit()

    def issues(self):
        return self.c.execute("SELECT count(*) FROM match_issues WHERE problem = 'rt_ambiguous'").fetchone()[0]

    def test_decided_page_is_written_without_issue(self):
        page = {"url": "https://www.rottentomatoes.com/m/film_2025", "title": "Film", "year": 2025,
                "critics": 91, "audience": 85, "critics_reviews": 120, "audience_ratings": 1000}
        cands = [{"url": page["url"], "title": "Film", "year": 2025, "score": 0.95}]
        self.assertTrue(ratings._rt_ambiguous(self.c, 1, cands, fetch=lambda u: page, fetch_season=lambda u, s: page))
        self.assertEqual(self.c.execute("SELECT rt_url, rt_critics FROM titles WHERE id = 1").fetchone()[:],
                         (page["url"], 91))
        self.assertEqual(self.issues(), 0)

    def test_undecided_issue_only_once_even_after_triage_closed_it(self):
        pages = {"u1": {"url": "u1", "title": "Film A", "year": 2025, "critics": 50},
                 "u2": {"url": "u2", "title": "Film B", "year": 2025, "critics": 60}}
        cands = [{"url": "u1", "title": "Film A", "year": 2025, "score": 0.8},
                 {"url": "u2", "title": "Film B", "year": 2025, "score": 0.8}]
        self.assertFalse(ratings._rt_ambiguous(self.c, 1, cands, fetch=pages.get))
        self.assertEqual(self.issues(), 1)
        self.c.execute("UPDATE match_issues SET resolved_at = datetime('now')")   # triage closed it (rt_undecided)
        self.c.commit()
        self.assertFalse(ratings._rt_ambiguous(self.c, 1, cands, fetch=pages.get))
        self.assertEqual(self.issues(), 1)
        self.assertIsNone(self.c.execute("SELECT rt_url FROM titles WHERE id = 1").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
