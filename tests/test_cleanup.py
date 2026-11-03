"""films.cleanup check and films.stage done (owner, 2026-10-28: «после каждой итерации обновленяи БД и окнчания работ …
не забывать чистить за собой мусор и не нужный набор всего»): leftovers are listed and keep the stage open."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from films import cleanup, stage


class CleanupTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.root, True)   # review 2026-10-28: the folders were left in %TEMP%
        c = self.cache = self.root / "data" / "cache"
        for d in ("ingest", "people_photos", "jellyfin", "rutracker", "locks"):
            (c / d).mkdir(parents=True)
        (c / "jellyfin" / "jellyfin.db").write_bytes(b"x")              # the working copy: kept
        (c / "rutracker" / "ingested.json").write_text(json.dumps(["rutracker-1.txt"]), encoding="utf-8")
        (c / "ingest" / "rutracker-2.txt").write_text("{}", encoding="utf-8")   # not ingested yet: kept
        self.temp = self.root / "tmp"
        (self.temp / "claude" / "D--work-films" / "s1" / "scratchpad").mkdir(parents=True)
        self.p = [mock.patch.object(cleanup, "CACHE", c), mock.patch.object(cleanup.db, "ROOT", self.root),
                  mock.patch.dict("os.environ", {"TEMP": str(self.temp)}),
                  mock.patch.object(stage, "PATH", c / "locks" / "stage.json")]
        for p in self.p:
            p.start()

    def tearDown(self):
        for p in self.p:
            p.stop()

    def test_clean_tree_closes_stage(self):
        self.assertEqual(cleanup.check(), [])
        stage.start("D")
        self.assertEqual(stage.done()["stage"], "D")

    def test_leftovers_listed_and_stage_stays_open(self):
        c = self.cache
        (c / "films_before.db").write_bytes(b"x")
        (c / "ingest" / "rutracker-1.txt").write_text("{}", encoding="utf-8")
        (c / "people_photos" / "candidates.html").write_text("x", encoding="utf-8")
        (c / "trailer.m4a").write_bytes(b"x")
        (c / "locks" / "wip.txt").write_text("films/x.py\n", encoding="utf-8")
        (self.temp / "claude" / "D--work-films" / "s1" / "scratchpad" / "main_x.py").write_text("", encoding="utf-8")
        left = "\n".join(cleanup.check())
        for s in ("films_before.db", "rutracker-1.txt", "candidates.html", "trailer.m4a", "wip.txt", "main_x.py"):
            self.assertIn(s, left)
        self.assertNotIn("rutracker-2.txt", left)
        # review 2026-10-28: dated job copies and the bridge's code folder stayed behind
        (c / "kinozal").mkdir()
        for n in ("queries_run.json", "queries_run_20261026-1315.json", "queries_run_20261028-1007.json"):
            (c / "kinozal" / n).write_text("{}", encoding="utf-8")
        (c / "kinozal_code").mkdir()
        (c / "ingest" / "kinozal_status-20261028-100753-411532.txt").write_text("{}", encoding="utf-8")
        left = "\n".join(cleanup.check())
        self.assertIn("queries_run_20261026-1315.json", left)
        self.assertIn("kinozal_code", left)
        self.assertIn("kinozal_status-20261028", left)
        self.assertNotIn("queries_run_20261028-1007.json", left)   # the newest copy is kept
        self.assertNotIn("queries_run.json", left)
        self.assertNotIn("jellyfin.db", left)
        stage.start("D")
        with self.assertRaises(SystemExit):
            stage.done()
        self.assertIsNotNone(stage.current())


if __name__ == "__main__":
    unittest.main()
