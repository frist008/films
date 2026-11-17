"""releases.parsed is zlib-compressed since R308 (2026-11-03): every reader goes through db.load_parsed. 2026-11-13 `films match`
fell in tracker_only.reopen_in_scope on json.loads of the compressed blob («UnicodeDecodeError … byte 0xf9»)."""
import gc
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from films import db, tracker_only


class ReopenInScopeTest(unittest.TestCase):
    def test_compressed_parsed_is_read(self):
        with tempfile.TemporaryDirectory() as d:
            conn = db.connect(Path(d) / "t.db")
            db.migrate(conn)
            conn.execute("INSERT INTO releases (id, source, source_id, url, name, category, parsed) VALUES "
                         "(1, 'rutor', '1', 'u', 'Фильм / Film (2025) WEB-DL 1080p', 1, ?)",
                         (db.dump_parsed({"name": "Фильм / Film (2025) WEB-DL 1080p", "year": 2025}),))
            conn.execute("INSERT INTO match_issues (release_id, problem, candidates, resolved_at) VALUES "
                         "(1, 'no_imdb_not_found', ?, '2026-11-01 00:00:00')",
                         (json.dumps({"candidates": [], "out_of_scope": "year"}),))
            conn.commit()
            with mock.patch.object(tracker_only, "scope_reason", return_value="year") as sr:
                tracker_only.reopen_in_scope(conn)   # raised UnicodeDecodeError before the fix
            self.assertEqual(sr.call_args[0][2].get("year"), 2025)   # the decompressed dict reached the scope check
            conn.close()
            gc.collect()


if __name__ == "__main__":
    unittest.main()
