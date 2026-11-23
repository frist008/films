"""films.lookup — справочный ответ без сужения поиска (баг 2026-11-22: «Кендимен» — ответил «нет в списках»,
хотя он был в просмотренном и в списке Google TV). Страж: поиск по любому написанию (е = ё = э, оригинал),
все таблицы со столбцом title_id, части коллекции, раздачи без привязки."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from films import db, lookup


class LookupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = db.connect(Path(self.tmp.name) / "t.db")
        self.addCleanup(self.conn.close)  # закрыть до удаления папки (Windows держит файл)
        db.migrate(self.conn)
        c = self.conn
        c.execute("insert into collections (id, name_ru, parts) values (1, 'Кэндимэн (Коллекция)', ?)",
                  (json.dumps([{"tmdb_id": 10, "title_ru": "Кэндимэн", "release_date": "1992-10-16"},
                               {"tmdb_id": 20, "title_ru": "Кэндимен", "release_date": "2021-08-25"},
                               {"tmdb_id": 30, "title_ru": "Кэндимэн 2", "release_date": "1995-03-17"}]),))
        c.execute("insert into titles (id, kind, title_original, title_ru, year, imdb_id, tmdb_id, collection_id) "
                  "values (1, 'movie', 'Candyman', 'Кэндимен', 2021, 'tt9347730', 20, 1)")
        c.execute("insert into titles (id, kind, title_original, title_ru, year, tmdb_id, collection_id) "
                  "values (2, 'movie', 'Farewell to the Flesh', 'Прощание с плотью', 1995, 30, 1)")
        c.execute("insert into watched (title_id, source, watched_at) values (1, 'plex', '2021-10-31 17:15:43')")
        c.execute("insert into store_items (store, item_id, title_id, in_wishlist) values ('google_tv', 'title:1', 1, 1)")
        c.execute("insert into releases (id, source, source_id, url, name) values (7, 'rutor', '751676', "
                  "'http://rutor.info/torrent/751676', 'Кэндимэн 3: День мертвых / Candyman: Day of the Dead (1999)')")
        c.commit()

    def test_any_spelling_finds_title_and_collection(self):
        for q in ("кендимен", "КЭНДИМЭН", "candyman", "tt9347730"):
            self.assertIn(1, lookup.find_titles(self.conn, q), q)
        self.assertEqual(lookup.find_titles(self.conn, "кендимен"), [1, 2])  # часть коллекции — тоже

    def test_dossier_has_every_title_table(self):
        d = lookup.dossier(self.conn, 1)
        self.assertIn("watched", d)
        self.assertIn("store_items", d)
        parts = {p["tmdb_id"]: p["в базе"] for p in d["collection"]["parts"]}
        self.assertEqual(parts, {10: False, 20: True, 30: True})

    def test_no_commit_connection_never_writes(self):
        path = Path(self.tmp.name) / "t.db"
        conn = sqlite3.connect(path, factory=lookup._NoCommit)
        with conn:  # как apply_responsible в rank.compute
            conn.execute("insert into responsibility (title_id, person, source) values (1, 'Дима', 'auto')")
        conn.commit()
        conn.rollback()
        conn.close()
        self.assertEqual(self.conn.execute("select count(*) from responsibility").fetchone()[0], 0)

    def test_unlinked_releases_reported(self):
        u = lookup.unlinked(self.conn, "кендимен", {1, 2})
        self.assertEqual([r["source_id"] for r in u["releases"]], ["751676"])


if __name__ == "__main__":
    unittest.main()
