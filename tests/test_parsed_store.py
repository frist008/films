"""R308 (owner, 2026-11-03: «Шаг 2», «Шаг 3»): releases.parsed is compact JSON compressed with zlib-9 and a preset
dictionary; the releases.audio column is gone (parsed.audio is the one copy); the git copy keeps plain JSON text."""
import gc
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from films import backup, db

SAMPLE = {"name": "Дюна / Dune (2021) UHD BDRemux 2160p", "imdb_id": "tt1160419", "year": 2021, "audio": [
    {"lang": "ru", "kind": "dub", "studio": None}, {"lang": "en", "kind": "original"}], "is_3d": False, "edition": [],
    "name_parsed": {"studios": [], "licensed": None, "title_ru": "Дюна"}, "note": ""}


class TestHelpers(unittest.TestCase):
    def test_dictionary_is_pinned(self):
        # every stored row needs this exact dictionary (zlib checks its Adler-32): never edit or regenerate the file
        self.assertEqual(hashlib.sha1(db.PARSED_ZDICT.read_bytes()).hexdigest(), "1ed4d3e1ce123e37721ffa46879519d3b7912142")

    def test_round_trip_keeps_every_key(self):
        blob = db.dump_parsed(SAMPLE)
        self.assertIsInstance(blob, bytes)
        self.assertEqual(db.load_parsed(blob), SAMPLE)          # empty keys (False, [], "", None) stay
        self.assertTrue(blob[1] & 0x20)                         # written with the preset dictionary
        text = db.parsed_text(blob)
        self.assertNotIn(", ", text.replace(SAMPLE["name"], ""))   # compact separators
        self.assertIn("Дюна", text)                              # not \\u-escaped
        self.assertLess(len(blob), len(text.encode("utf-8")))

    def test_old_text_and_empty_values(self):
        self.assertEqual(db.load_parsed(json.dumps(SAMPLE, ensure_ascii=False)), SAMPLE)
        self.assertEqual(db.parsed_text('{"a": 1}'), '{"a": 1}')
        self.assertIsNone(db.load_parsed(None))
        self.assertIsNone(db.dump_parsed(None))
        self.assertIsNone(db.parsed_text(""))
        self.assertEqual(db.load_parsed({"a": 1}), {"a": 1})     # already decoded (rank.compute)
        self.assertEqual(db.load_parsed(memoryview(db.dump_parsed({"a": 1}))), {"a": 1})

    def test_unknown_dictionary_fails_loudly(self):
        import zlib
        co = zlib.compressobj(9, zlib.DEFLATED, 15, 9, zlib.Z_DEFAULT_STRATEGY, b'{"other dictionary":1}')
        with self.assertRaises(ValueError):
            db.load_parsed(co.compress(b'{"a":1}') + co.flush())
        self.assertEqual(db.load_parsed(zlib.compress(b'{"a":1}', 9)), {"a": 1})   # plain zlib reads too

    def test_release_audio(self):
        self.assertEqual(db.release_audio({"parsed": db.dump_parsed(SAMPLE)}), SAMPLE["audio"])
        self.assertEqual(db.release_audio({"parsed": SAMPLE}), SAMPLE["audio"])
        self.assertEqual(db.release_audio({"parsed": json.dumps(SAMPLE)}), SAMPLE["audio"])
        self.assertEqual(db.release_audio({"audio": '[{"lang": "uk"}]'}), [{"lang": "uk"}])   # an own copy is read first
        self.assertEqual(db.release_audio({"audio": [{"lang": "uk"}], "parsed": None}), [{"lang": "uk"}])
        self.assertEqual(db.release_audio({"parsed": None}), [])
        self.assertEqual(db.release_audio({"audio": "not json"}), [])


class TestSqlAndMigration(unittest.TestCase):
    def test_sql_function_on_blob_text_and_null(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._sql_checks(db.connect(Path(tmp) / "a.db"))   # db.connect registers parsed_json
        self._sql_checks(sqlite3.connect(":memory:"))         # so does db.migrate (the tests' connections)

    def _sql_checks(self, conn):
        db.migrate(conn)
        conn.execute("INSERT INTO releases (id, source, source_id, url, name, parsed) VALUES "
                     "(1, 'rutor', '1', 'u', 'n', ?), (2, 'rutor', '2', 'u', 'n', ?), (3, 'rutor', '3', 'u', 'n', NULL)",
                     (db.dump_parsed(SAMPLE), json.dumps({"year": 1999})))
        got = conn.execute("SELECT id, json_extract(parsed_json(parsed), '$.year'), json_valid(parsed_json(parsed)) "
                           "FROM releases ORDER BY id").fetchall()
        self.assertEqual([tuple(r) for r in got], [(1, 2021, 1), (2, 1999, 1), (3, None, None)])
        self.assertEqual(conn.execute("SELECT json_extract(parsed_json(parsed), '$.audio[0].kind') FROM releases "
                                      "WHERE id = 1").fetchone()[0], "dub")
        cols = {r[1] for r in conn.execute("PRAGMA table_info(releases)")}
        self.assertNotIn("audio", cols)
        self.assertIn("parsed", cols)
        conn.close()

    def _v24(self):
        """A DB as before migration 25: parsed TEXT, the audio column."""
        conn = sqlite3.connect(":memory:")
        db.migrate(conn)
        conn.execute("ALTER TABLE releases ADD COLUMN audio TEXT")
        conn.execute("PRAGMA user_version = 24")
        return conn

    def test_migration_25(self):
        conn = self._v24()
        keyless = {k: v for k, v in SAMPLE.items() if k != "audio"}
        rows = [(1, json.dumps(SAMPLE, ensure_ascii=False), json.dumps(SAMPLE["audio"], ensure_ascii=False)),
                (2, json.dumps(keyless), json.dumps([{"lang": "uk"}])),     # audio only in the column: kept in parsed
                (3, None, None), (4, json.dumps({"year": 2024}), None)]
        conn.executemany("INSERT INTO releases (id, source, source_id, url, name, parsed, audio) "
                         "VALUES (?, 'rutor', ?, 'u', 'n', ?, ?)", [(i, str(i), p, a) for i, p, a in rows])
        conn.commit()
        db._compress_parsed_drop_audio(conn, batch=2)          # several batches
        db.migrate(conn)                                       # the migration itself: nothing left to do, column gone
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], len(db.MIGRATIONS))
        self.assertNotIn("audio", {r[1] for r in conn.execute("PRAGMA table_info(releases)")})
        got = dict(conn.execute("SELECT id, parsed FROM releases").fetchall())
        self.assertEqual(db.load_parsed(got[1]), SAMPLE)
        self.assertEqual(db.load_parsed(got[2]), {**keyless, "audio": [{"lang": "uk"}]})
        self.assertIsNone(got[3])
        self.assertEqual(db.load_parsed(got[4]), {"year": 2024})
        self.assertEqual({type(v) for k, v in got.items() if v is not None}, {bytes})

    def test_migration_25_stops_on_a_mismatch(self):
        conn = self._v24()
        conn.execute("INSERT INTO releases (id, source, source_id, url, name, parsed, audio) VALUES "
                     "(1, 'rutor', '1', 'u', 'n', ?, ?)", (json.dumps(SAMPLE), '[{"lang": "en"}]'))
        conn.commit()
        with self.assertRaises(RuntimeError):
            db.migrate(conn)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 24)
        self.assertIn("audio", {r[1] for r in conn.execute("PRAGMA table_info(releases)")})


class TestBackupRoundTrip(unittest.TestCase):
    def test_git_copy_has_plain_json_and_restore_compresses(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            src = tmp / "src.db"
            conn = db.connect(src)
            db.migrate(conn)
            conn.execute("INSERT INTO releases (id, source, source_id, url, name, parsed) VALUES "
                         "(1, 'rutor', '1', 'u', 'n', ?), (2, 'rutor', '2', 'u', 'n', NULL)", (db.dump_parsed(SAMPLE),))
            conn.commit()
            conn.close()
            out = tmp / "db"
            with mock.patch.object(db, "DB_PATH", src), mock.patch.object(backup, "OUT", out):
                backup.export()
                lines = (out / "releases.000.jsonl").read_text(encoding="utf-8").splitlines()
                cols = json.loads(lines[0])["columns"]
                row = json.loads(lines[1])
                self.assertNotIn("audio", cols)
                self.assertIsInstance(row[cols.index("parsed")], str)              # plain JSON text in git
                self.assertEqual(json.loads(row[cols.index("parsed")]), SAMPLE)
                self.assertIsNone(json.loads(lines[2])[cols.index("parsed")])
                backup.restore(str(tmp / "new.db"))
            new = sqlite3.connect(tmp / "new.db")
            blob = new.execute("SELECT parsed FROM releases WHERE id = 1").fetchone()[0]
            self.assertIsInstance(blob, bytes)
            self.assertEqual(db.load_parsed(blob), SAMPLE)
            self.assertIsNone(new.execute("SELECT parsed FROM releases WHERE id = 2").fetchone()[0])
            new.close()
            gc.collect()   # export() / restore() leave their connections to the collector (Windows keeps the file locked)


if __name__ == "__main__":
    unittest.main()
