"""films.personal: the owner's own videos are not kept even in the local DB (owner, 2026-10-29: «Удалить сейчас»)."""
import sqlite3
import unittest
from unittest import mock

from films import personal

NAMES = {"Rep-Фарес 2-2", "Фарес SF", "Result 2"}


class PersonalTest(unittest.TestCase):
    def test_paths(self):
        self.assertTrue(personal.is_path(r"E:\Vegas\Network\Mems\anime\x.mp4"))
        self.assertTrue(personal.is_path("D:/work/youtube/clip.mp4"))
        self.assertFalse(personal.is_path(r"E:\Films\Vegas Vacation (1997)\v.mkv"))   # «Vegas» in a film name
        self.assertFalse(personal.is_path(None))

    def test_plex_rows(self):
        self.assertTrue(personal.is_plex_row({"plex_title": "Rep-Фарес 2-2_Глава-5", "title_id": None}, NAMES))
        self.assertTrue(personal.is_plex_row({"grandparent_title": "Фарес SF", "title": "Часть2"}, NAMES))
        self.assertTrue(personal.is_plex_row({"file_path": r"E:\Vegas\Network\Result 2\Фарес_3-5.mp4"}, NAMES))
        self.assertFalse(personal.is_plex_row({"plex_title": "Фарес SF", "title_id": 17}, NAMES))   # linked = a work
        self.assertFalse(personal.is_plex_row({"plex_title": "Result 20"}, NAMES))

    def test_purge(self):
        c = sqlite3.connect(":memory:")
        c.executescript("""CREATE TABLE disk_files (path TEXT PRIMARY KEY, how TEXT);
            CREATE TABLE plex_items (rating_key TEXT PRIMARY KEY, title_id INTEGER, plex_title TEXT, file_path TEXT);
            CREATE TABLE plex_history (id INTEGER PRIMARY KEY, title_id INTEGER, title TEXT, grandparent_title TEXT);
            CREATE TABLE jellyfin_items (item_id TEXT, title_id INTEGER, name TEXT, path TEXT);
            INSERT INTO disk_files VALUES ('E:\\Vegas\\a.mp4', 'personal'), ('E:\\Vegas\\b.mp4', 'unmatched'),
                                          ('E:\\Films\\c.mkv', 'nfo');
            INSERT INTO plex_items VALUES ('1', NULL, 'Rep-Фарес 2-2', ''), ('2', 5, 'Дюна', 'E:\\Films\\d.mkv');
            INSERT INTO plex_history VALUES (1, NULL, 'Глава 1', 'Фарес SF'), (2, 5, 'Дюна', NULL);
            INSERT INTO jellyfin_items VALUES ('x', NULL, 'Дюна', 'E:\\Films\\d.mkv');""")
        with mock.patch.object(personal, "names", return_value=NAMES):
            self.assertEqual(personal.purge(c, dry=True),
                             {"disk_files": 2, "plex_items": 1, "plex_history": 1, "jellyfin_items": 0})
            self.assertEqual(c.execute("SELECT count(*) FROM disk_files").fetchone()[0], 3)   # dry: nothing deleted
            personal.purge(c)
        self.assertEqual([p for (p,) in c.execute("SELECT path FROM disk_files")], ["E:\\Films\\c.mkv"])
        self.assertEqual([k for (k,) in c.execute("SELECT rating_key FROM plex_items")], ["2"])
        self.assertEqual([k for (k,) in c.execute("SELECT id FROM plex_history")], [2])


if __name__ == "__main__":
    unittest.main()
