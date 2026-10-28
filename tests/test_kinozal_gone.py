"""kinozal: a topic page answering HTTP 404 (the topic is gone) is remembered — releases.page_skip = 2 — and never put in a
job again (review 2026-10-28: 131 old season topics stayed «without a page» forever)."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from films import db
from films.sources import kinozal as kz


class GoneTopicTest(unittest.TestCase):
    def test_404_marks_gone_and_known(self):
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        db.migrate(c)
        c.execute("INSERT INTO releases (source, source_id, name, url) VALUES ('kinozal', '1387903', 'x', 'u')")
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, tmp, True)   # review 2026-10-28: the folders were left in %TEMP%
        with mock.patch.object(kz, "CACHE", tmp), mock.patch.object(kz, "STATE", tmp / "state.json"), \
                mock.patch.object(kz, "ROWS", tmp / "rows.json"):
            st = kz.ingest(c, [{"v": 1, "kind": "kinozal", "type": "topics", "topics": [],
                                "errors": [{"t": "1387903", "err": "HTTP 404"}]}])
            self.assertEqual(st["gone"], 1)
            self.assertIn("1387903", kz.known_topics(c))


if __name__ == "__main__":
    unittest.main()
