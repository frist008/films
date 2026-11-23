"""[watched] not_seen (владелец, 2026-11-23): «не смотрел» — ни история Plex, ни Jellyfin, ни перенос Jellyfin → Plex
отметку «видел» не возвращают. «tt…» — фильм или все сезоны сериала, «tt…:S3» — один сезон («Кольца власти» S3;
мультфильмы DC — «если скачаны то не смотрел»: в Jellyfin отметка есть, в Plex переносить нельзя).
Run:  python -X utf8 -m unittest tests.test_not_seen
"""
import sqlite3
import unittest
from unittest import mock

from films import db, link, watched


class NotSeen(unittest.TestCase):
    def setUp(self):
        self.c = sqlite3.connect(":memory:")
        self.c.row_factory = sqlite3.Row
        db.migrate(self.c)
        ins = "INSERT INTO titles (id, kind, title_original, year, imdb_id, series_imdb_id, season_number) VALUES (?,?,?,?,?,?,?)"
        self.c.execute(ins, (1, "movie", "Bad Blood", 2016, "tt4870838", None, None))
        self.c.execute(ins, (2, "series", "Rings", 2022, None, "tt7631058", 1))
        self.c.execute(ins, (3, "series", "Rings", 2026, None, "tt7631058", 3))
        self.c.execute(ins, (4, "series", "Death Note", 2015, None, "tt4623604", 1))
        self.c.execute(ins, (5, "movie", "Other", 2016, "tt0000009", None, None))
        self.c.commit()
        self.p = mock.patch.object(link, "owner_not_seen",
                                   return_value={"tt4870838", "tt7631058:S3", "tt4623604"})
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_titles_resolved(self):
        self.assertEqual(link.not_seen_titles(self.c), {1, 3, 4})   # S1 «Колец» не тронут

    def test_jellyfin_played_movie_not_counted(self):
        self.c.execute("INSERT INTO jellyfin_played (key, kind, imdb_id, played_at) VALUES ('tt4870838', 'movie', "
                       "'tt4870838', '2026-11-12')")
        self.c.execute("INSERT INTO watched (title_id, source, watched_at) VALUES (1, 'plex', '2023-10-14')")
        self.c.commit()
        with mock.patch.object(link, "ensure_movie", side_effect=lambda conn, imdb, how: 1 if imdb == "tt4870838" else None):
            st = link.link_jellyfin(self.c)
        self.assertEqual(st.get("played_movies_owner_not_seen"), 1)
        self.assertEqual(sorted(r[0] for r in self.c.execute("SELECT source FROM watched WHERE title_id = 1")),
                         ["plex_reverted_partial"])

    def test_jellyfin_to_plex_skips_not_seen(self):
        self.c.execute("INSERT INTO jellyfin_played (key, kind, imdb_id, title_id, played_at) VALUES "
                       "('tt4870838', 'movie', 'tt4870838', 1, '2026-11-12')")
        self.c.execute("INSERT INTO jellyfin_played (key, kind, imdb_id, season, episode, played_at) VALUES "
                       "('tt7631058:3:1', 'episode', 'tt7631058', 3, 1, '2026-11-12')")
        self.c.commit()
        with mock.patch.object(watched.plexsrc, "Plex"), \
                mock.patch.object(watched, "mark", side_effect=AssertionError("в Plex писать нельзя")), \
                mock.patch.object(watched, "mark_episode", side_effect=AssertionError("в Plex писать нельзя")):
            res = watched.sync_from_jellyfin(self.c, dry_run=True)
        self.assertEqual(sorted(res["owner_not_seen_skipped"]), ["tt4870838", "tt7631058:S3E1"])
        self.assertEqual(res["movies"] + res["episodes"], [])


if __name__ == "__main__":
    unittest.main()
