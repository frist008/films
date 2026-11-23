"""[title_fix] (review 2026-10-28): TMDB gives «Братство: Последняя фантазия XV» the Russian name of «Кингсглейв» — the
config fix wins on every enrich, for a series season too (matched by series_imdb_id)."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from films import db, enrich


class TitleFixTest(unittest.TestCase):
    def test_fix_overrides_tmdb_name(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, root, True)   # review 2026-10-28: the folders were left in %TEMP%
        (root / "config").mkdir()
        (root / "config" / "settings.toml").write_text(
            '[title_fix]\n"tt5595178" = { ru = "Братство: Последняя фантазия XV" }\n', encoding="utf-8")
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        db.migrate(c)
        c.execute("INSERT INTO titles (id, kind, series_imdb_id, season_number, title_ru, is_animation, no_dub_needed, "
                  "created_at, updated_at, tracker_only) VALUES (1, 'series', 'tt5595178', 1, 'x', 1, 0, datetime('now'), "
                  "datetime('now'), 0)")
        with mock.patch.object(db, "ROOT", root):
            enrich._apply(c, 1, {"title_ru": "Кингсглейв: Последняя фантазия XV"}, {})
        self.assertEqual(c.execute("SELECT title_ru FROM titles WHERE id = 1").fetchone()[0],
                         "Братство: Последняя фантазия XV")


if __name__ == "__main__":
    unittest.main()
