"""update-db steps 4/5 (films ratings rt [--recent-days 90]), R322 (2026-11-13): only titles never checked on RT and titles
released in the last 90 days, except ones already checked in the last 20 h (step 4 of the same run); the weekly re-check
of titles without an RT score and the 30-day re-check of everything — only on the owner's request.
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
            # R322: an old title once checked without an RT score is not checked again (no weekly re-check) → no
            (3, (today - timedelta(days=200)).isoformat(), None, "2026-01-01 00:00:00", 1, 0),
            (4, (today - timedelta(days=200)).isoformat(), None, None, 0, 0),                 # never checked → yes
            (5, (today - timedelta(days=5)).isoformat(), None, None, 1, 1),                   # tracker-only → no
            (6, (today - timedelta(days=5)).isoformat(), None, "now", 1, 0),                  # checked just now → no
            (7, (today + timedelta(days=30)).isoformat(), 70, "2026-01-01 00:00:00", 0, 0),   # not released yet → no
            (8, (today - timedelta(days=200)).isoformat(), None,                              # no score, checked 3 days
             (today - timedelta(days=3)).isoformat() + " 00:00:00", 1, 0),                   # ago → no
            (9, (today - timedelta(days=95)).isoformat(), 60, "2026-01-01 00:00:00", 1, 0),   # 95 days ago → no
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
        self.assertEqual(got, {1, 4})
        # only on the owner's request: the weekly re-check of titles with releases and no RT score
        self.assertEqual({r[0] for r in c.execute(ratings.RECHECK_UNSCORED_SQL)}, {3})

    def test_default_run_does_not_touch_old_titles(self):
        """rt_ratings() with no flags (update-db step 4/5): an old, checked title is not searched again (R322)."""
        from unittest import mock
        c = mem_db()
        old = (date.today() - timedelta(days=400)).isoformat()
        new = (date.today() - timedelta(days=3)).isoformat()
        c.execute("INSERT INTO titles (id, kind, title_original, year, release_date, rt_checked_at, tmdb_checked_at) VALUES "
                  "(1, 'movie', 'Old', 2024, ?, '2026-01-01 00:00:00', '2026-01-01'),"
                  " (2, 'movie', 'Fresh', 2026, ?, '2026-01-01 00:00:00', '2026-01-01'),"
                  " (3, 'movie', 'NewTitle', 2025, ?, NULL, '2026-01-01')", (old, new, old))
        c.commit()
        seen = []

        def find(names, *a, **k):
            seen.append(names[0])
            return {"status": "not_found"}
        with mock.patch.object(ratings.rt, "find", find):
            st = ratings.rt_ratings(c)
        self.assertEqual(sorted(seen), ["Fresh", "NewTitle"])
        self.assertEqual(st["titles"], 2)
        seen.clear()
        with mock.patch.object(ratings.rt, "find", find):
            ratings.rt_ratings(c, recheck_days=0)   # the owner's explicit request: everything again
        self.assertIn("Old", seen)


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
