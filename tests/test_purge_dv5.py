"""DV-Profile-5-only releases are not stored at all (owner: «раздачи, у которых только Dolby Vision Profile 5 … не сохраняем
вообще»; review 2026-10-28: 47 were kept in releases with their titles). db.purge_dv5 runs after every tracker sync."""
import sqlite3
import unittest

from films import db


class PurgeDv5Test(unittest.TestCase):
    def test_purged_after_tracker_sync(self):
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        db.migrate(c)
        c.execute("INSERT INTO titles (id, kind, title_original) VALUES (1, 'movie', 'x')")
        for rid, sid in ((10, "a"), (11, "b")):
            c.execute("INSERT INTO releases (id, source, source_id, name, url, title_id) VALUES (?, 'kinozal', ?, 'x', 'u', 1)",
                      (rid, sid))
            c.execute("INSERT INTO release_raw (release_id, body) VALUES (?, x'00')", (rid,))
            c.execute("INSERT INTO release_titles (release_id, title_id) VALUES (?, 1)", (rid,))
        c.execute("INSERT INTO excluded_releases (source, source_id, reason, name) VALUES ('kinozal', 'a', 'dv5_only', 'x')")
        c.execute("INSERT INTO excluded_releases (source, source_id, reason, name) VALUES ('kinozal', 'b', 'is_3d', 'x')")
        c.execute("UPDATE titles SET poster_release_id = 10")
        db.mark_sync(c, "kinozal")
        self.assertEqual([r[0] for r in c.execute("SELECT id FROM releases")], [11])   # 3D: kept, only hidden
        self.assertEqual([r[0] for r in c.execute("SELECT release_id FROM release_raw")], [11])
        self.assertIsNone(c.execute("SELECT poster_release_id FROM titles").fetchone()[0])
        self.assertEqual(c.execute("SELECT count(*) FROM excluded_releases WHERE reason = 'dv5_only'").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
