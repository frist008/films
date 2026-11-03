"""franchise_seen.hide_excluded_series: a user_marks row with mark = NULL must not block hiding (review 2026-10-28: 161
«не интересен» were wiped to NULL by the old «Метка» read-back 2026-10-24, and `ON CONFLICT DO NOTHING` kept 149 of them
visible — «Кобра Кай», «Люцифер», «Стрела»…). Other marks (want, order…) are never overwritten."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from films import db, franchise_seen as fs

TOML = ('[[franchise]]\nkey = "dc"\nname = "DC — всё"\nscope = "all"\nseries_only = ["tt100"]\n')


class HideNullMarksTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.root, True)   # review 2026-10-28: the folders were left in %TEMP%
        (self.root / "config").mkdir()
        (self.root / "config" / "franchises.toml").write_text(TOML, encoding="utf-8")
        c = self.c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        db.migrate(c)
        for imdb in ("tt100", "tt200", "tt300"):
            c.execute("INSERT INTO franchise_members (franchise_key, imdb_id, kind, checked_at) VALUES ('dc', ?, 'series', "
                      "datetime('now'))", (imdb,))
        for tid, sim in ((1, "tt100"), (2, "tt200"), (3, "tt300"), (4, "tt200")):
            c.execute("INSERT INTO titles (id, kind, series_imdb_id, season_number, is_animation, no_dub_needed, created_at, "
                      "updated_at, tracker_only) VALUES (?, 'series', ?, 1, 0, 0, datetime('now'), datetime('now'), 0)",
                      (tid, sim))
        # a serial filed as a movie (Batman and Robin 1949): imdb_id of the series, no season
        c.execute("INSERT INTO titles (id, kind, imdb_id, is_animation, no_dub_needed, created_at, updated_at, tracker_only) "
                  "VALUES (5, 'movie', 'tt300', 0, 0, datetime('now'), datetime('now'), 0)")
        c.execute("INSERT INTO user_marks (title_id, mark, updated_at) VALUES (2, NULL, datetime('now'))")    # wiped
        c.execute("INSERT INTO user_marks (title_id, mark, updated_at) VALUES (3, 'want', datetime('now'))")  # owner's
        c.commit()

    def marks(self):
        return {r[0]: r[1] for r in self.c.execute("SELECT title_id, mark FROM user_marks")}

    def test_null_mark_is_hidden_again_other_marks_kept(self):
        with mock.patch.object(db, "ROOT", self.root):
            fs.hide_excluded_series(self.c)
        m = self.marks()
        self.assertNotIn(1, m)                     # series_only — stays visible
        self.assertEqual(m[2], "not_interested")   # was NULL — hidden again
        self.assertEqual(m[4], "not_interested")
        self.assertEqual(m[3], "want")             # never overwrite a real mark
        self.assertEqual(m[5], "not_interested")   # serial filed as a movie


if __name__ == "__main__":
    unittest.main()
