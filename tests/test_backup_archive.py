"""films/backup.py, yearly archive of the git copy (R309, owner 2026-11-03: «данные которым больше года и хранить их отдельно,
чтобы они почти никогда или никогда не менялись … на каждый год. Создать её когда год закончен + один месяц»)."""
import gc
import json
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from films import backup, db

TODAY = date(2026, 11, 3)


class ArchiveTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.tmp = Path(self.dir.name)
        self.src = self.tmp / "src.db"
        self.out = self.tmp / "db"
        conn = db.connect(self.src)
        db.migrate(conn)
        rows = [(1, "2024-03-01T10:00:00", 5), (2, "2025-12-31T23:00:00", 7), (3, "2026-01-05T00:00:00", 9), (4, None, 1)]
        conn.executemany("INSERT INTO releases (id, source, source_id, url, name, added_at, seeders, parsed) VALUES "
                         "(?, 'rutor', ?, 'u', 'n', ?, ?, ?)",
                         [(i, str(i), a, s, db.dump_parsed({"name": f"r{i}"})) for i, a, s in rows])
        conn.commit()
        conn.close()
        self.patches = [mock.patch.object(db, "DB_PATH", self.src), mock.patch.object(backup, "OUT", self.out)]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def sql(self, q, *a):
        conn = sqlite3.connect(self.src)
        with conn:
            conn.execute(q, a)
        conn.close()

    def export(self, today=TODAY):
        st = backup.export(today)
        gc.collect()
        return st

    def files(self):
        return {str(p.relative_to(self.out)): p.read_bytes() for p in self.out.rglob("*.jsonl")}

    def ids(self, rel):
        lines = (self.out / rel).read_text(encoding="utf-8").splitlines()[1:]
        return [json.loads(x)[0] for x in lines]

    def test_finished_years_are_frozen(self):
        st = self.export()
        self.assertEqual(st["releases_archived_years"], [2024, 2025])
        self.assertEqual(self.ids("archive/2024/releases.000.jsonl"), [1])
        self.assertEqual(self.ids("archive/2025/releases.000.jsonl"), [2])
        self.assertEqual(self.ids("releases.000.jsonl"), [3, 4])          # 2026 and no date: live
        self.assertEqual(self.ids("releases.seen.jsonl"), [1, 2])

    def test_january_is_not_frozen_yet(self):
        st = self.export(date(2026, 1, 20))
        self.assertEqual(st["releases_archived_years"], [2024])

    def test_seeders_do_not_touch_the_archive(self):
        self.export()
        before = self.files()
        self.sql("UPDATE releases SET seeders = 50, last_seen_at = '2026-11-03 12:00:00' WHERE id IN (1, 2)")
        st = self.export()
        after = self.files()
        self.assertEqual(st["releases_changes"], 0)
        self.assertEqual(st["releases_refrozen"], [])
        for k in after:
            if k.startswith("archive"):
                self.assertEqual(after[k], before[k], k)
        self.assertNotEqual(after["releases.seen.jsonl"], before["releases.seen.jsonl"])

    def test_changed_row_goes_over_the_archive_and_restores(self):
        for i in range(5, 15):   # a bigger 2024 so that one change stays under REFREEZE
            self.sql("INSERT INTO releases (id, source, source_id, url, name, added_at) VALUES (?, 'rutor', ?, 'u', 'n', "
                     "'2024-05-01')", i, str(i))
        self.export()
        frozen = (self.out / "archive/2024/releases.000.jsonl").read_bytes()
        self.sql("UPDATE releases SET name = 'renamed', seeders = 77 WHERE id = 1")
        st = self.export()
        self.assertEqual(st["releases_changes"], 1)
        self.assertEqual((self.out / "archive/2024/releases.000.jsonl").read_bytes(), frozen)
        backup.restore(str(self.tmp / "new.db"))
        gc.collect()
        new = sqlite3.connect(self.tmp / "new.db")
        self.assertEqual(new.execute("SELECT name, seeders FROM releases WHERE id = 1").fetchone(), ("renamed", 77))
        self.assertEqual(new.execute("SELECT count(*) FROM releases").fetchone()[0], 14)
        self.assertEqual(db.load_parsed(new.execute("SELECT parsed FROM releases WHERE id = 2").fetchone()[0]), {"name": "r2"})
        self.assertEqual(new.execute("PRAGMA user_version").fetchone()[0], len(db.MIGRATIONS))
        new.close()

    def test_many_changes_refreeze_the_year_once(self):
        self.export()
        self.sql("UPDATE releases SET name = 'renamed' WHERE id = 1")      # 1 of 1 row of 2024 > 30 %
        st = self.export()
        self.assertEqual(st["releases_refrozen"], [2024])
        self.assertEqual(st["releases_changes"], 0)
        self.assertIn('"renamed"', (self.out / "archive/2024/releases.000.jsonl").read_text(encoding="utf-8"))

    def test_growing_row_does_not_shift_other_chunks(self):
        for i in range(100, 160):
            self.sql("INSERT INTO releases (id, source, source_id, url, name, added_at) VALUES (?, 'rutor', ?, 'u', ?, "
                     "'2026-06-01')", i, str(i), "x" * 200)
        with mock.patch.object(backup, "CHUNK", 4000):
            self.export()
            before = self.files()
            self.sql("UPDATE releases SET name = ? WHERE id = 101", "y" * 3000)
            self.export()
            after = self.files()
        # git stores content: a later chunk renumbered by a split is the same blob
        live = [v for k, v in before.items() if backup._CHUNK_NAME.match(k) and k.startswith("releases.")]
        kept = set(after.values())
        same = [v for v in live if v in kept]
        self.assertGreaterEqual(len(same), len(live) - 1, (len(same), len(live)))   # only the chunk of id 101


if __name__ == "__main__":
    unittest.main()
