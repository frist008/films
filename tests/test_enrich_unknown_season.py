"""enrich._fill_unknown_season (review 2026-10-28): a season TMDB does not have yet («Люпен» S4, «Клиника» S10) was hidden
«год None» — the year comes from its releases, the series-level columns from TMDB, nothing is overwritten."""
import json
import sqlite3
import unittest

from films import db, enrich


class UnknownSeasonTest(unittest.TestCase):
    def test_year_from_releases_and_series_fields(self):
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        db.migrate(c)
        tid = c.execute("INSERT INTO titles (kind, season_number, series_imdb_id, title_ru) "
                        "VALUES ('series', 4, 'tt2531336', 'Люпен')").lastrowid
        for rid, y in ((1, 2026), (2, 2025)):
            c.execute("INSERT INTO releases (id, source, source_id, url, name, parsed) VALUES (?, 'rutor', ?, 'u', 'n', ?)",
                      (rid, str(rid), json.dumps({"year": y})))
            c.execute("INSERT INTO release_titles (release_id, title_id, how) VALUES (?, ?, 'imdb_page')", (rid, tid))
        enrich._fill_unknown_season(c, tid, {"tmdb_id": 96677, "title_ru": "Другое имя", "title_original": "Lupin",
                                             "genres": ["криминал"], "countries": ["Франция"], "number_of_seasons": 3})
        t = c.execute("SELECT * FROM titles WHERE id = ?", (tid,)).fetchone()
        self.assertEqual((t["year"], t["series_tmdb_id"], t["title_ru"], t["title_original"], t["seasons_total"]),
                         (2025, 96677, "Люпен", "Lupin", 3))
        self.assertEqual(json.loads(t["genres"]), ["криминал"])
        # a season TMDB has with no air date («Гранчестер» S11): the year from its releases, by the general pass
        c.execute("UPDATE titles SET year = NULL WHERE id = ?", (tid,))
        self.assertEqual(enrich.fill_years_from_releases(c), 1)
        self.assertEqual(c.execute("SELECT year FROM titles WHERE id = ?", (tid,)).fetchone()[0], 2025)

    def test_season_of_a_pack(self):
        # review 2026-10-28: «Террор [S01-03] (2018-2025)» gave season 3 the year 2018
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        db.migrate(c)
        ids = {s: c.execute("INSERT INTO titles (kind, season_number, series_imdb_id) VALUES ('series', ?, 'tt2708480')",
                            (s,)).lastrowid for s in (1, 2, 3)}
        p = {"year": 2018, "name_parsed": {"year_from": 2018, "year_to": 2025, "seasons": [1, 2, 3]}}
        c.execute("INSERT INTO releases (id, source, source_id, url, name, parsed) VALUES (1, 'rutor', '1', 'u', "
                  "'Террор [S01-03] (2018-2025)', ?)", (json.dumps(p),))
        for tid in ids.values():
            c.execute("INSERT INTO release_titles (release_id, title_id, how) VALUES (1, ?, 'imdb_page')", (tid,))
        self.assertEqual([enrich.season_year(c, ids[s]) for s in (1, 2, 3)], [2018, None, 2025])
        # a year from the old «first year of any release» rule is corrected for a season TMDB lacks
        c.execute("UPDATE titles SET year = 2018 WHERE id = ?", (ids[3],))
        c.execute("INSERT INTO match_issues (problem, candidates) VALUES ('tmdb_no_season', ?)",
                  (json.dumps({"title_id": ids[3]}),))
        enrich.fill_years_from_releases(c)
        self.assertEqual(c.execute("SELECT year FROM titles WHERE id = ?", (ids[3],)).fetchone()[0], 2025)
        # … and a season TMDB has without an air date (review 2026-10-28: «Александра Эль» S7 kept 2018)
        c.execute("UPDATE titles SET year = 2018, tmdb_id = 78834 WHERE id = ?", (ids[3],))
        enrich.fill_years_from_releases(c)
        self.assertEqual(c.execute("SELECT year FROM titles WHERE id = ?", (ids[3],)).fetchone()[0], 2025)
        c.execute("UPDATE titles SET year = 2018, release_date = '2018-03-01' WHERE id = ?", (ids[3],))
        enrich.fill_years_from_releases(c)    # TMDB's air date stands
        self.assertEqual(c.execute("SELECT year FROM titles WHERE id = ?", (ids[3],)).fetchone()[0], 2018)


class RuNameTest(unittest.TestCase):
    def test_ru_name_from_releases(self):
        # review 2026-10-28: «双世异闻录» shown with hieroglyphs only — the releases call it «Принцесса сквозь время»
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        db.migrate(c)
        c.execute("INSERT INTO titles (id, kind, season_number, title_original) VALUES (1, 'series', 1, '双世异闻录')")
        c.execute("INSERT INTO titles (id, kind, title_ru, title_original) VALUES (2, 'movie', 'Своё', 'X')")
        for rid, src, tid, ru in ((1, "rutor", 1, "Принцесса сквозь время"), (2, "kinozal", 1, "Принцесса сквозь время (1 сезон: 1-27 серии из 27)"),
                                  (3, "rutor", 2, "Другое")):
            c.execute("INSERT INTO releases (id, source, source_id, url, name, parsed) VALUES (?, ?, ?, 'u', 'n', ?)",
                      (rid, src, str(rid), json.dumps({"title_ru": ru})))
            c.execute("INSERT INTO release_titles (release_id, title_id, how) VALUES (?, ?, 'imdb_page')", (rid, tid))
        self.assertEqual(enrich.fill_ru_names_from_releases(c), 1)
        self.assertEqual([r[0] for r in c.execute("SELECT title_ru FROM titles ORDER BY id")], ["Принцесса сквозь время", "Своё"])

    def test_not_from_a_film_pack(self):
        # review round 9: «Next Avengers», «Inhumans» got «Люди Икс» from «X-Men Complete Collection»
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        db.migrate(c)
        for tid, orig in ((1, "Next Avengers: Heroes of Tomorrow"), (2, "Inhumans: The First Chapter"), (3, "Logan")):
            c.execute("INSERT INTO titles (id, kind, title_original) VALUES (?, 'movie', ?)", (tid, orig))
        c.execute("INSERT INTO releases (id, source, source_id, url, name, parsed) VALUES (1, 'rutor', '1', 'u', 'n', ?)",
                  (json.dumps({"title_ru": "Люди Икс"}),))
        c.execute("INSERT INTO release_titles (release_id, title_id, how) VALUES (1, 1, 'collection_pack')")
        c.execute("INSERT INTO releases (id, source, source_id, url, name, parsed) VALUES (2, 'kinozal', '2', 'u', 'n', ?)",
                  (json.dumps({"title_ru": "Люди Икс"}),))
        c.executemany("INSERT INTO release_titles (release_id, title_id, how) VALUES (2, ?, 'imdb_resolve')", [(2,), (3,)])
        self.assertEqual(enrich.fill_ru_names_from_releases(c), 0)


if __name__ == "__main__":
    unittest.main()
