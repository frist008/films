"""`films watched from-jellyfin` / mark(): a Plex library file the last scan no longer saw (plex_items.present = 0) has
no ratingKey on the server — it must not be asked for (2026-10-25: 404 on /library/metadata/19244 stopped the run).
Offline: Plex is faked, the DB is in memory.  Run:  python -X utf8 -m unittest tests.test_watched_present
"""
import sqlite3
import unittest
from unittest import mock

from films import db, watched


class FakePlex:
    def __init__(self):
        self.asked = []

    def get(self, path, **kw):
        self.asked.append(path)
        if path.endswith("/gone"):
            raise AssertionError("asked Plex for a removed item")
        return {"MediaContainer": {"Metadata": [{"viewCount": 1}]}}


class PresentOnly(unittest.TestCase):
    def setUp(self):
        self.c = sqlite3.connect(":memory:")
        self.c.row_factory = sqlite3.Row
        db.migrate(self.c)
        self.c.execute("INSERT INTO titles (id, kind, title_original, year) VALUES (1, 'movie', 'Film', 2020)")
        self.c.execute("INSERT INTO titles (id, kind, title_original, year, series_imdb_id, season_number)"
                       " VALUES (2, 'series', 'Show', 2020, 'tt0000002', 1)")
        rows = [("live", 1, None, 1), ("gone", 1, None, 0), ("ep_live", 2, 3, 1), ("gone", 2, 3, 0)]
        for i, (rk, tid, ep, present) in enumerate(rows):
            self.c.execute("INSERT INTO plex_items (rating_key, library, title_id, episode, present) VALUES (?, 'L', ?, ?, ?)",
                           (rk if rk != "gone" else f"gone{i}", tid, ep, present))
        # the removed rows' keys end with "gone" for the fake
        self.c.execute("UPDATE plex_items SET rating_key = 'gone' WHERE present = 0 AND title_id = 1")
        self.c.commit()
        self.px = FakePlex()

    def test_mark_skips_removed_files(self):
        with mock.patch.object(watched.plexsrc, "Plex", return_value=self.px):
            out = watched.mark(self.c, 1, dry_run=True, db_source=None, jellyfin=False)
        self.assertEqual(self.px.asked, ["/library/metadata/live"])
        self.assertEqual([p["ratingKey"] for p in out["plex"] if p["where"] == "library"], ["live"])

    def test_mark_episode_skips_removed_files(self):
        with mock.patch.object(watched, "_plex_id", return_value=None):
            out = watched.mark_episode(self.c, "tt0000002", 1, 3, dry_run=True, px=self.px, jellyfin=False)
        self.assertEqual(self.px.asked, ["/library/metadata/ep_live"])
        self.assertEqual([p["ratingKey"] for p in out["plex"] if p["where"] == "library"], ["ep_live"])


if __name__ == "__main__":
    unittest.main()
