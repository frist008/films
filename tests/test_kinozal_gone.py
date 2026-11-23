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
            # R327 (2026-11-13): a run that fetched no page at all is a site / collector problem, not a gone topic —
            # 1387903 itself opened fine in the browser on 2026-11-13 (it had been marked gone on 10-26)
            st = kz.ingest(c, [{"v": 1, "kind": "kinozal", "run": "r1", "type": "topics", "topics": [],
                                "errors": [{"t": "1387903", "err": "HTTP 404"}]}])
            self.assertEqual(st.get("gone", 0), 0)
            self.assertEqual(st["suspect_404"], 1)
            self.assertNotIn("1387903", kz.known_topics(c))
            # a 404 while the site serves pages in the same run → gone, never put in a job again
            with mock.patch.object(kz.common, "store_topic", lambda *a, **k: None, create=True):
                try:
                    kz.ingest(c, [{"v": 1, "kind": "kinozal", "run": "r2", "type": "topics",
                                   "topics": [{"topic_id": "x1"}, {"topic_id": "x2"}],
                                   "errors": [{"t": "1387903", "err": "HTTP 404"}]}])
                except Exception:   # the fake topics carry no page; only the 404 marking is under test
                    pass
            self.assertIn("1387903", kz.known_topics(c))


if __name__ == "__main__":
    unittest.main()
