"""survey_gates.yana_sync: unseen cartoons of a franchise go to Яна — never those of `[responsible.yana]
exclude_franchises` (DC, Marvel…; owner 2026-10-24: «она ими не увлекается»). 2026-10-27 the survey added two DC cartoons."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from films import db, survey_gates


class YanaSyncTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "config").mkdir()
        (self.root / "config" / "settings.toml").write_text('[responsible.yana]\nexclude_franchises = ["dc"]\n',
                                                            encoding="utf-8")
        (self.root / "config" / "franchises.toml").write_text(
            '[[franchise]]\nkey = "dc"\nname = "DC — всё"\nscope = "all"\n\n'
            '[[franchise]]\nkey = "mask"\nname = "Маска"\nscope = "all"\nanimation = "ask"\n', encoding="utf-8")
        (self.root / "config" / "yana.toml").write_text("# список Яны\n", encoding="utf-8")

    def item(self, fr, imdb):
        return {"imdb_id": imdb, "franchise": fr, "title": "t", "year": 2019, "desc": "мультфильм · США"}

    def test_excluded_franchise_not_added(self):
        with mock.patch.object(db, "ROOT", self.root):
            survey_gates.yana_sync(None, [self.item("DC — всё", "tt1"), self.item("Маска", "tt2")], [])
        s = (self.root / "config" / "yana.toml").read_text(encoding="utf-8")
        self.assertNotIn("tt1", s)
        self.assertIn("tt2", s)


if __name__ == "__main__":
    unittest.main()
