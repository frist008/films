"""films.audit: a film checked on IMDb and found not IMAX (is_imax = 0, imax_source NULL) counts as checked.
Run:  python -X utf8 -m unittest tests.test_audit
"""
import sqlite3
import unittest
from unittest import mock

from films import audit, db


class ImaxChecked(unittest.TestCase):
    def test_not_imax_is_checked(self):
        c = sqlite3.connect(":memory:")
        db.migrate(c)
        for tid, is_imax, src in [(1, 1, "imdb_sound"), (2, 0, None), (3, None, None)]:
            c.execute("INSERT INTO titles (id, kind, title_original, year, is_imax, imax_source) VALUES (?, 'movie', 'T', 2025, ?, ?)",
                      (tid, is_imax, src))
            c.execute("INSERT INTO releases (id, source, source_id, url, name, category) VALUES (?, 'rutor', ?, 'u', 'n', 'movie')",
                      (tid, str(tid)))
            c.execute("INSERT INTO release_titles (release_id, title_id) VALUES (?, ?)", (tid, tid))
        with mock.patch("pathlib.Path.write_text"):
            out = audit.run(c)
        self.assertEqual(out["missing"]["IMAX проверен"]["n"], 1)


if __name__ == "__main__":
    unittest.main()
