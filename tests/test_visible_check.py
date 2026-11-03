"""films/visible_check.py — the sheet refuses rows that break the owner's rules (review 2026-10-28)."""
import sqlite3
import unittest

from films import db, visible_check


def row(tid, imdb=None, series=None, hidden=None, anime_ok=False):
    return {"title": {"id": tid, "imdb_id": imdb, "series_imdb_id": series, "title_ru": f"t{tid}"},
            "hidden_reasons": hidden or [], "release": {}, "anime_ok": anime_ok}


class VisibleCheckTest(unittest.TestCase):
    def setUp(self):
        self.c = sqlite3.connect(":memory:")
        self.c.row_factory = sqlite3.Row
        db.migrate(self.c)
        for tid, cat in ((1, "movie"), (2, "movie"), (3, "anime"), (4, "anime")):
            self.c.execute("INSERT INTO titles (id, kind, title_original, tracker_category, tmdb_checked_at, rt_checked_at) "
                           "VALUES (?, 'movie', ?, ?, 'x', 'x')", (tid, f"t{tid}", cat))

    def test_rating_and_rt_filled(self):
        # review round 12: titles made by the link rechecks had no IMDb rating (shown despite «IMDb < 5») and no RT check
        self.c.execute("INSERT INTO imdb_ratings (tconst, rating, votes) VALUES ('tt9', 4.3, 100)")
        self.c.execute("UPDATE titles SET imdb_id = 'tt9', rt_checked_at = NULL WHERE id = 1")
        self.assertEqual(sorted(p.split(" (")[0] for p in visible_check.check(self.c, [row(1, imdb="tt9")], [])),
                         ["RT не проверялся", "рейтинг IMDb не перенесён"])

    def test_top_season_by_series_id_first(self):
        # «Братство: Последняя фантазия XV»: a season row has no imdb_id, [top] names the series
        rows = [row(2, series="tt5595178"), row(1, imdb="tt1")]
        self.assertEqual(visible_check.check(self.c, rows, ["tt0000001", "tt5595178"]), [])
        bad = [row(1, imdb="tt1"), row(2, series="tt5595178")]
        self.assertTrue(any("[top] не на верху" in p for p in visible_check.check(self.c, bad, ["tt5595178"])))

    def test_top_order(self):
        rows = [row(1, imdb="tt2"), row(2, imdb="tt1")]
        self.assertIn("[top] не в порядке, названном владельцем", visible_check.check(self.c, rows, ["tt1", "tt2"]))

    def test_anime_outside_franchises(self):
        rows = [row(3), row(4, anime_ok=True), row(1)]
        self.assertEqual([p.split(":")[0] for p in visible_check.check(self.c, rows, [])],
                         ["аниме не из франшиз владельца в таблице"])

    def test_short_cartoon_outside_franchises(self):
        # review 2026-10-28: «Малыш Т-34» (6 min), «Снежный медведь» (11 min) … were visible; «только полнометражные»
        short = dict(row(5), short_ok=False)
        short["title"].update(kind="movie", is_animation=1, runtime_min=6)
        member = dict(row(6), short_ok=True)
        member["title"].update(kind="movie", is_animation=1, runtime_min=20)
        feature = row(7)
        feature["title"].update(kind="movie", is_animation=1, runtime_min=90)
        probs = visible_check.check(self.c, [short, member, feature], [])
        self.assertEqual([p.split(":")[0] for p in probs], ["короткометражный мультфильм не из франшиз в таблице"])

    def test_russian_language_outside_franchises(self):
        # owner, 2026-10-28: «Скрыть все» — «Все кроме выбранных русских франшиз»
        a, b, cart = dict(row(5), ru_ok=False), dict(row(6), ru_ok=True), dict(row(7), ru_ok=False)
        for x in (a, b, cart):
            x["title"].update(kind="movie", original_language="ru")
        cart["title"]["is_animation"] = 1
        probs = visible_check.check(self.c, [a, b, cart], [])
        self.assertEqual([p.split(":")[0] for p in probs], ["русскоязычный не из франшиз и списков в таблице"])

    def test_hidden_only_for_unknown_year(self):
        r = dict(row(5, hidden=["год None"]), release={"source": "rutor", "source_id": "1"})
        self.assertEqual([p.split(":")[0] for p in visible_check.check(self.c, [r, row(1)], [])],
                         ["скрыт только «год None» при раздачах"])

    def test_season_year_not_by_its_releases(self):
        # review 2026-10-28: «Александра Эль» S7 (TMDB has it, no air date) kept 2018 of the pack «1-7 сезоны (2018-2025)»
        import json
        self.c.execute("INSERT INTO titles (id, kind, season_number, tmdb_id, year, tmdb_checked_at, rt_checked_at) "
                       "VALUES (7, 'series', 7, 78834, 2018, 'x', 'x')")
        p = {"year": 2018, "name_parsed": {"year_from": 2018, "year_to": 2025, "seasons": [1, 2, 3, 4, 5, 6, 7]}}
        self.c.execute("INSERT INTO releases (id, source, source_id, url, name, parsed) VALUES (1, 'rutor', '1', 'u', 'n', ?)",
                       (json.dumps(p),))
        self.c.execute("INSERT INTO release_titles (release_id, title_id, how) VALUES (1, 7, 'imdb_page')")
        r = {"title": {"id": 7, "kind": "series", "tmdb_id": 78834, "release_date": None, "year": 2018, "title_ru": "t7"},
             "hidden_reasons": ["год 2018"], "release": {}}
        self.assertEqual([p.split(":")[0] for p in visible_check.check(self.c, [r], [])],
                         ["год сезона не по его раздачам (2018 ≠ 2025)"])
        r["title"]["release_date"] = "2018-06-01"     # TMDB's own air date: not second-guessed
        self.assertEqual(visible_check.check(self.c, [r], []), [])

    def test_best_release_of_another_season(self):
        # review 2026-10-28: «Больница Питт» S1 showed «(Сезон: 2 / Серии: 1-15 из 15)»
        import json
        self.c.execute("INSERT INTO titles (id, kind, season_number, tmdb_id, year, tmdb_checked_at, rt_checked_at) VALUES "
                       "(8, 'series', 1, 1, 2025, 'x', 'x')")
        self.c.execute("INSERT INTO releases (id, source, source_id, url, name, parsed) VALUES (2, 'rustorka', '2', 'u', "
                       "'Больница Питт / The Pitt (Сезон: 2 / Серии: 1-15 из 15) [2026]', ?)", (json.dumps({"year": 2026}),))
        self.c.execute("INSERT INTO release_titles (release_id, title_id, how) VALUES (2, 8, 'imdb_page')")
        r = {"title": {"id": 8, "kind": "series", "season_number": 1, "tmdb_id": 1, "title_ru": "t8"},
             "hidden_reasons": [], "release": {"id": 2}}
        self.assertEqual([p.split(":")[0] for p in visible_check.check(self.c, [r], [])],
                         ["лучшая раздача — другого сезона (S1 ← [2])"])
        self.c.execute("UPDATE release_titles SET how = 'season_recheck' WHERE release_id = 2")   # moved on purpose
        self.assertEqual(visible_check.check(self.c, [r], []), [])

    def test_one_work_two_series(self):
        # «双世异闻录 / Princess Across Time» (TMDB) and its tracker-only twin «Shuang Shi Yi Wen Lu (Princess Across Time)»
        a = {"id": 11, "kind": "series", "season_number": 1, "year": 2024, "tmdb_id": 262241, "series_tmdb_id": 262241,
             "title_original": "双世异闻录", "title_latin": "Princess Across Time"}
        b = {"id": 12, "kind": "series", "season_number": 1, "year": 2024, "tracker_only": 1,
             "title_original": "Princess Across Time"}
        rows = [{"title": t, "hidden_reasons": [], "release": {}} for t in (a, b)]
        self.assertEqual([p.split(":")[0] for p in visible_check.twin_problems(self.c, rows)],
                         ["одна работа — две строки"])
        b["season_number"] = 0       # a special is not the season
        self.assertEqual(visible_check.twin_problems(self.c, rows), [])

    def test_specials_row_with_a_season_release(self):
        # review round 9: «Атака титанов» OVA row (season 0) showed «[1x1-25 из 25] (2013)» — a season pack
        import json
        self.c.execute("INSERT INTO titles (id, kind, season_number, tmdb_id, year, tmdb_checked_at, rt_checked_at) VALUES "
                       "(10, 'series', 0, 1, 2013, 'x', 'x')")
        for rid, name in ((3, "Атака титанов / Attack on Titan (2013) WEB-DLRip"),
                          (4, "Атака титанов / Attack on Titan [OVA 1-8] (2013-2018)"),
                          (5, "Ted Lasso: Спэшл (2023)")):
            self.c.execute("INSERT INTO releases (id, source, source_id, url, name, parsed) VALUES (?, 'rutor', ?, 'u', ?, ?)",
                           (rid, str(rid), name, json.dumps({})))
            self.c.execute("INSERT INTO release_titles (release_id, title_id, how) VALUES (?, 10, 'imdb_page')", (rid,))
        r = lambda rid: {"title": {"id": 10, "kind": "series", "season_number": 0, "tmdb_id": 1, "release_date": "2013-12-09",
                                   "title_ru": "t10"}, "hidden_reasons": [], "release": {"id": rid}}
        self.assertEqual([p.split(" (")[0] for p in visible_check.check(self.c, [r(3)], [])],
                         ["на спецвыпусках"])
        self.assertEqual(visible_check.check(self.c, [r(4)], []), [])
        self.assertEqual(visible_check.check(self.c, [r(5)], []), [])

    def test_film_twins(self):
        # review round 9: «Сезон охоты / Hunting Season» (2025) — a TMDB film and its tracker-only row; «Убийства в...» —
        # two French TV films of one Russian name are not twins
        rows = [{"title": t, "hidden_reasons": [], "release": {}} for t in (
            {"id": 21, "kind": "movie", "year": 2025, "imdb_id": "tt32537226", "tmdb_id": 1, "title_original": "Hunting Season"},
            {"id": 22, "kind": "movie", "year": 2025, "tracker_only": 1, "title_original": "Hunting Season"},
            {"id": 23, "kind": "movie", "year": 2026, "imdb_id": "tt1", "title_ru": "Убийства в...",
             "title_original": "Meurtres dans les Vosges"},
            {"id": 24, "kind": "movie", "year": 2026, "tmdb_id": 2, "title_ru": "Убийства в...",
             "title_original": "Petits meurtres en Corrèze"})]
        self.assertEqual(visible_check.twin_problems(self.c, rows), ["одна работа — две строки: hunting season (2025) — [21, 22]"])
        # two IMDb records are two works («Shifting Gears»: the 2024 film and the sitcom); «Meurtres à...» is an anthology's
        # shared name; an OVA is not the season of its year
        rows = [{"title": t, "hidden_reasons": [], "release": {}} for t in (
            {"id": 31, "kind": "movie", "year": 2024, "imdb_id": "tt31519881", "tmdb_id": 1, "title_original": "Shifting Gears"},
            {"id": 32, "kind": "series", "season_number": 1, "year": 2024, "series_imdb_id": "tt18071570",
             "tmdb_id": 5, "title_original": "Shifting Gears"},
            {"id": 33, "kind": "movie", "year": 2024, "imdb_id": "tt2", "tmdb_id": 3, "title_original": "Meurtres à..."},
            {"id": 34, "kind": "movie", "year": 2024, "imdb_id": "tt3", "title_original": "Meurtres à..."})]
        self.c.execute("INSERT INTO releases (id, source, source_id, url, name) VALUES "
                       "(9, 'rutor', '9', 'u', 'Атака титанов / Shingeki no Kyojin [OVA] (2017) DVDRip')")
        rows += [{"title": {"id": 35, "kind": "series", "season_number": 2, "year": 2017, "series_imdb_id": "tt2560140",
                            "tmdb_id": 4, "title_original": "Shingeki no Kyojin"}, "hidden_reasons": [], "release": {}},
                 {"title": {"id": 36, "kind": "movie", "year": 2017, "tracker_only": 1, "title_original": "Shingeki no Kyojin"},
                  "hidden_reasons": [], "release": {"id": 9}}]
        self.assertEqual(visible_check.twin_problems(self.c, rows), [])
        # review round 10: a stray IMDb id found by name is no proof of two works; a tracker year off by one
        rows[1]["title"].pop("tmdb_id")
        rows += [{"title": {"id": 37, "kind": "movie", "year": 2026, "imdb_id": "tt27681354", "tmdb_id": 6,
                            "title_original": "In the Grey"}, "hidden_reasons": [], "release": {}},
                 {"title": {"id": 38, "kind": "movie", "year": 2025, "tracker_only": 1, "title_original": "In the Grey"},
                  "hidden_reasons": [], "release": {}}]
        self.assertEqual([p.split(" (")[0] for p in visible_check.twin_problems(self.c, rows)],
                         ["одна работа — две строки: shifting gears", "одна работа — две строки: in the grey"])

    def test_film_row_says_another_film(self):
        # review round 11: «Корни (2024)» Kazakhstan 138 min on the Irish «Frewaka»; a «film» on a series' IMDb id
        import json
        self.c.execute("INSERT INTO releases (id, source, source_id, url, name, parsed) VALUES (7, 'rutor', '7', 'u', 'Корни (2024)', ?)",
                       (json.dumps({"runtime_min": 138, "countries": ["Казахстан"]}),))
        self.c.execute("INSERT INTO release_titles (release_id, title_id, how) VALUES (7, 1, 'exact_name_year')")
        self.c.execute("INSERT INTO imdb_basics (tconst, title_type, primary_title, original_title) "
                       "VALUES ('tt0409591', 'tvSeries', 'Naruto', 'Naruto')")
        a = dict(row(1), release={"id": 7})
        a["title"].update(kind="movie", runtime_min=103, countries='["Ирландия"]', tmdb_id=1)
        b = row(2, imdb="tt0409591")
        b["title"]["kind"] = "movie"
        self.assertEqual([p.split(" (")[0].split(":")[0] for p in visible_check.check(self.c, [a, b], [])],
                         ["раздача другого фильма", "фильм на IMDb id сериала"])

    def test_one_release_two_rows(self):
        a = dict(row(1), release={"id": 5}, variant_index=0)
        b = dict(row(1), release={"id": 5}, variant_index=1, variant="Оригинал без апскейла")
        self.assertEqual(visible_check.check(self.c, [a, b], []), ["одна раздача — две строки тайтла: 1 ← 5"])
        self.assertEqual(visible_check.check(self.c, [a], []), [])

    def test_not_enriched(self):
        # review 2026-10-28: 8 «Убийства в...» rows of 09:48, IMDb ids but never through TMDB
        self.c.execute("UPDATE titles SET tmdb_checked_at = NULL WHERE id = 1")
        self.c.execute("INSERT INTO titles (id, kind, title_original, tracker_only) VALUES (9, 'movie', 'tr', 1)")
        probs = visible_check.check(self.c, [row(1), row(2), row(9)], [])
        self.assertEqual([p.split(":")[0] for p in probs], ["не дополнен из TMDB (films enrich)"])

    def test_tracker_only_season_twin(self):
        a, b = row(5), row(6)
        a["title"].update(kind="series", season_number=2, title_original="Fallout")
        b["title"].update(kind="series", season_number=2, title_original="Fallout", tracker_only=1)
        probs = visible_check.check(self.c, [a, b], [])
        self.assertTrue(probs[0].startswith("сезон дважды"))     # the name-and-year guard may name the pair too
        b["title"]["season_number"] = 3
        self.assertEqual(visible_check.check(self.c, [a, b], []), [])

    def test_auto_responsible_without_runtime(self):
        self.c.execute("UPDATE titles SET tracker_only = 1 WHERE id = 1")
        self.c.execute("INSERT INTO responsibility (title_id, person, source) VALUES (1, 'Яна', 'auto'), (2, 'Яна', 'manual')")
        probs = visible_check.check(self.c, [row(1), row(2)], [])
        self.assertEqual([p.split(":")[0] for p in probs], ["автоматический ответственный у короткого / без длительности"])


if __name__ == "__main__":
    unittest.main()
