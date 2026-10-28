"""match._shift_seasons / recheck_series_links (review 2026-10-28): kinozal «Мыслить как преступник: Эволюция (2 сезон)
(2024)» pages link Criminal Minds — the releases sat on its seasons 2–4 of 2006–2008, the visible season 17–19 rows lost
them (the official dub among them)."""
import json
import sqlite3
import unittest

from films import db, match


class SeasonShiftTest(unittest.TestCase):
    def setUp(self):
        self.c = sqlite3.connect(":memory:")
        self.c.row_factory = sqlite3.Row
        db.migrate(self.c)
        for s, y in ((1, 2005), (2, 2006), (3, 2007), (16, 2022), (17, 2024), (18, 2025)):
            self.c.execute("INSERT INTO titles (id, kind, season_number, series_imdb_id, year) VALUES (?, 'series', ?, "
                           "'tt0452046', ?)", (100 + s, s, y))

    def test_shift(self):
        self.assertEqual(match._shift_seasons(self.c, "tt0452046", [2], 2024, 2024), [17])
        self.assertIsNone(match._shift_seasons(self.c, "tt0452046", [2, 3], 2024, 2025))  # a pack: its years are often off
        self.assertIsNone(match._shift_seasons(self.c, "tt0452046", [2], 2006, 2006))      # the right season
        self.assertIsNone(match._shift_seasons(self.c, "tt0452046", [3], 2026, 2026))      # no season of 2026

    def test_two_years_only_for_a_named_part(self):
        # «Монстры: История братьев Менендес [S01] (2024)» → «Монстр» S2; «Юные Титаны, вперед! (4 сезон) / 2018» is the
        # dub's year of season 4 (2016), the series' own name — not moved
        for s, y in ((1, 2022), (2, 2024)):
            self.c.execute("INSERT INTO titles (kind, season_number, series_imdb_id, year, title_ru, title_original) "
                           "VALUES ('series', ?, 'tt13207736', ?, 'Монстр', 'Monster')", (s, y))
        self.assertEqual(match._shift_seasons(self.c, "tt13207736", [1], 2024, 2024,
                                              names=["Монстры: История братьев Менендес"]), [2])
        self.assertIsNone(match._shift_seasons(self.c, "tt13207736", [1], 2024, 2024, names=["Монстр", "Monster"]))

    def test_round14_episodes_decide(self):
        # «Тренировка столпов (ТВ-4) [8 из 8] 2024»: TMDB S4 2023 (11) → S5 2024 (8); «Деревня кузнецов (ТВ-3) [11 из 11]
        # 2023»: S3 2021 (11) → S4 2023 (11) only by the episodes and the year together
        for s, y, n in ((1, 2019, 26), (2, 2021, 7), (3, 2021, 11), (4, 2023, 11), (5, 2024, 8)):
            self.c.execute("INSERT INTO titles (kind, season_number, series_imdb_id, year, episodes_total, title_ru, "
                           "episode_dates) VALUES ('series', ?, 'tt9335498', ?, ?, 'Истребитель демонов', ?)",
                           (s, y, n, json.dumps([f"{y}-04-09", f"{y}-06-18"])))
        # «Звёздный путь: Вояджер (5 сезон: 1-26 серии из 26) / 1999»: S5 ran 1998-10 … 1999-05 — kept
        for s, y, n, dates in ((5, 1998, 25, ["1998-10-14", "1999-05-26"]), (6, 1999, 26, ["1999-09-22", "2000-05-24"])):
            self.c.execute("INSERT INTO titles (kind, season_number, series_imdb_id, year, episodes_total, episode_dates) "
                           "VALUES ('series', ?, 'tt0112178', ?, ?, ?)", (s, y, n, json.dumps(dates)))
        self.assertIsNone(match._shift_seasons(self.c, "tt0112178", [5], 1999, 1999, eps=26))
        self.assertEqual(match._shift_seasons(self.c, "tt9335498", [4], 2024, 2024, eps=8), [5])
        self.assertIsNone(match._shift_seasons(self.c, "tt9335498", [4], 2024, 2024))           # no episodes: kept
        self.assertIsNone(match._shift_seasons(self.c, "tt9335498", [4], 2024, 2024, eps=11))   # S4 has 11: kept
        # S3 and S4 both have 11: two years apart, the release names the part itself
        self.assertEqual(match._shift_seasons(self.c, "tt9335498", [3], 2023, 2023, eps=11,
                                              names=["Клинок, рассекающий демонов: Деревня кузнецов"]), [4])
        self.assertIsNone(match._shift_seasons(self.c, "tt9335498", [4], 2024, 2024, need_eps=True))
        self.assertEqual(match._all_episodes({"episodes": "1-8"}), 8)
        self.assertEqual(match._all_episodes({"episodes": "01-11"}), 11)
        self.assertIsNone(match._all_episodes({"episodes": "3-8"}))
        self.assertIsNone(match._all_episodes({"episodes": None}))

    def test_recheck_moves_the_links(self):
        p = {"imdb_id": "tt0452046", "year": 2024, "name_parsed": {"year_from": 2024, "seasons": [2]}}
        self.c.execute("INSERT INTO releases (id, source, source_id, url, name, parsed, title_id) VALUES (1, 'kinozal', "
                       "'2142264', 'u', 'Мыслить как преступник: Эволюция (2 сезон: 1-10 серии из 10)', ?, 102)",
                       (json.dumps(p),))
        self.c.execute("INSERT INTO release_titles (release_id, title_id, how) VALUES (1, 102, 'imdb_resolve')")
        st = match.recheck_series_links(self.c, log=lambda *_: None)
        self.assertEqual(st["relinked"], [{"release_id": 1, "from": [102], "to": [117]}])
        links = dict(self.c.execute("SELECT title_id, how FROM release_titles WHERE release_id = 1").fetchall())
        self.assertEqual(links[117], "season_recheck")
        self.assertTrue(links[102].startswith("wrong_link: season recheck → 117"))
        self.assertEqual(match.recheck_series_links(self.c, log=lambda *_: None)["relinked"], [])   # idempotent


class NamedSeasonTest(unittest.TestCase):
    """Review round 8 (2026-10-28): the season written in words was not read — season 2 releases sat on season 1."""

    def test_name_seasons(self):
        from films.tracker_only import name_seasons
        cases = {
            "Фоллаут / Fallout (Сезон: 2 / Серии: 1-8 из 8) (Фред Туа) [2025, Фантастика]": [2],
            "Пацаны / The Boys (4 Сезон / 1 - 8 Серия из 8) ( Филип Сгриккиа) [2024]": [4],
            "Поколение «Ви» / Gen V / 1-2 Сезоны: 1-16 серии из 16 [2025]": [1, 2],
            "Клиника (2 (11) сезон: 1-5 серии из 10) / Scrubs / 2026": [2],
            "Розы и грехи / Guller ve Gunahlar (2026) WEB-DLRip (сезон 2, серии 1-7 (33-39) из ??)": [2],
            "Альф / ALF, 2-ой сезон (1987-1988) TVRip [UKR]": [2],
            "Южный парк / South Park / Season: 16 / Серии: 1-14 (14) [2012]": [16],
            "Южный Парк / South Park / 1~7 сезоны / Все серии [1997-2003]": [1, 2, 3, 4, 5, 6, 7],
            "Кік Бутовський (1 сезон, 1-14,16-20; 2 сезон, 1-5,7-11) / Kick Buttowski (2010-12)": [1, 2],
            "Звёздный путь / Star Trek: The next generation [full 1-7 season (1987-1994) DVDRip]": [1, 2, 3, 4, 5, 6, 7],
            "Два сезона, два незнакомца / Two Seasons, Two Strangers [2025]": [],
            "Высокий сезон / 1-8 серии из 8 (Стас Иванов) [2025]": [],
        }
        for name, want in cases.items():
            self.assertEqual(name_seasons(name), want, name)

    def test_recheck_reads_the_words_and_makes_the_season(self):
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        db.migrate(c)
        c.execute("INSERT INTO titles (id, kind, season_number, series_imdb_id, series_tmdb_id, tmdb_id, year) "
                  "VALUES (1, 'series', 1, 'tt12637874', 106379, 1, 2024)")
        c.execute("INSERT INTO releases (id, source, source_id, url, name, parsed) VALUES (1, 'rustorka', '1', 'u', "
                  "'Фоллаут / Fallout (Сезон: 2 / Серии: 1-8 из 8) [2025]', ?)", (json.dumps({"year": 2025}),))
        c.execute("INSERT INTO release_titles (release_id, title_id, how) VALUES (1, 1, 'imdb_page')")
        from unittest import mock
        with mock.patch.object(match, "ensure_season", side_effect=lambda conn, tv, s: conn.execute(
                "INSERT INTO titles (kind, season_number, series_imdb_id, series_tmdb_id, tmdb_id, year) "
                "VALUES ('series', ?, 'tt12637874', ?, 2, 2025)", (s, tv)).lastrowid):
            st = match.recheck_series_links(c, log=lambda *_: None)
        self.assertEqual(st["seasons_made"], 1)
        s2 = c.execute("SELECT id FROM titles WHERE season_number = 2").fetchone()[0]
        self.assertEqual(st["relinked"], [{"release_id": 1, "from": [1], "to": [s2]}])

    def test_revival_counted_from_the_start(self):
        # «Клиника / Scrubs [S10] (2026)»: TMDB keeps the 2026 «Scrubs» as its own series; kinozal «1 (10) сезон» gives
        # the offset when both its seasons are of 2026
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        db.migrate(c)
        for tid, s, tm in ((1, 1, 1), (2, 2, 2), (10, 10, None)):
            c.execute("INSERT INTO titles (id, kind, season_number, series_imdb_id, tmdb_id, year) VALUES (?, 'series', ?, "
                      "'tt40197357', ?, 2026)", (tid, s, tm))
        rels = ((1, "Клиника / Scrubs [S10] (2026)", {"year": 2026, "name_parsed": {"seasons": [10]}}, 10),
                (2, "Клиника (1 (10) сезон: 1-9 серии из 9) / Scrubs / 2026", {"year": 2026}, 1))
        for rid, name, p, tid in rels:
            c.execute("INSERT INTO releases (id, source, source_id, url, name, parsed) VALUES (?, 'kinozal', ?, 'u', ?, ?)",
                      (rid, str(rid), name, json.dumps(p)))
            c.execute("INSERT INTO release_titles (release_id, title_id, how) VALUES (?, ?, 'imdb_page')", (rid, tid))
        st = match.recheck_series_links(c, log=lambda *_: None)
        self.assertEqual(st["relinked"], [{"release_id": 1, "from": [10], "to": [1]}])


class PartTwinTest(unittest.TestCase):
    def setUp(self):
        self.c = c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        db.migrate(c)
        # the «Монстр» anthology season on the IMDb id (no TMDB data) and the part's own one-season TMDB series
        c.execute("INSERT INTO titles (id, kind, season_number, series_imdb_id, title_original, year) VALUES "
                  "(1, 'series', 2, 'tt13207736', 'Monster', 2024)")
        c.execute("INSERT INTO titles (id, kind, season_number, series_tmdb_id, tmdb_id, title_original, year) VALUES "
                  "(2, 'series', 1, 225634, 225634, 'Monsters: The Lyle and Erik Menendez Story', 2024)")
        # a long show's season 6 and its ONA on the tracker (not a twin)
        c.execute("INSERT INTO titles (id, kind, season_number, series_imdb_id, series_tmdb_id, tmdb_id, title_original, "
                  "year) VALUES (3, 'series', 6, 'tt5626028', 65930, 65930, 'Boku no Hero Academia', 2022)")
        c.execute("INSERT INTO titles (id, kind, season_number, series_tmdb_id, tmdb_id, year) VALUES "
                  "(5, 'series', 1, 65930, 65931, 2016)")
        c.execute("INSERT INTO titles (id, kind, season_number, tracker_only, title_original, year) VALUES "
                  "(4, 'series', 1, 1, 'Boku no Hero Academia', 2022)")
        for rid, tid, name, p in (
                (1, 1, "Монстры / Monsters: The Lyle And Erik Menendez Story [S02] (2024)",
                 {"title_original": "Monsters: The Lyle And Erik Menendez Story", "name_parsed": {"seasons": [2]}}),
                (2, 4, "Моя геройская академия ONA (1-2 серии из 2) / Boku no Hero Academia / 2022",
                 {"title_original": "Boku no Hero Academia"})):
            c.execute("INSERT INTO releases (id, source, source_id, url, name, parsed) VALUES (?, 'kinozal', ?, 'u', ?, ?)",
                      (rid, str(rid), name, json.dumps(p)))
            c.execute("INSERT INTO release_titles (release_id, title_id, how) VALUES (?, ?, 'imdb_page')", (rid, tid))

    def test_merge(self):
        st = match.merge_part_twins(self.c, log=lambda *_: None)
        self.assertEqual(st["merged"], [(1, 2)])
        self.assertEqual(self.c.execute("SELECT duplicate_of FROM titles WHERE id = 1").fetchone()[0], 2)
        self.assertEqual(self.c.execute("SELECT how FROM release_titles WHERE release_id = 1 AND title_id = 2").fetchone()[0],
                         "part_twin")
        self.assertIsNone(self.c.execute("SELECT duplicate_of FROM titles WHERE id = 4").fetchone()[0])   # the ONA stays


class Round9Test(unittest.TestCase):
    """Review round 9 (2026-10-28): twins outside the season rules, phantom seasons, a page id beating a name."""

    def setUp(self):
        self.c = c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        db.migrate(c)
        self.rid = 0

    def title(self, **kw):
        cols = ", ".join(kw)
        return self.c.execute(f"INSERT INTO titles ({cols}) VALUES ({', '.join('?' * len(kw))})", list(kw.values())).lastrowid

    def rel(self, tid, name, p=None, how="imdb_page", src="rutor"):
        self.rid += 1
        self.c.execute("INSERT INTO releases (id, source, source_id, url, name, parsed) VALUES (?, ?, ?, 'u', ?, ?)",
                       (self.rid, src, str(self.rid), name, json.dumps(p or {})))
        self.c.execute("INSERT INTO release_titles (release_id, title_id, how) VALUES (?, ?, ?)", (self.rid, tid, how))
        return self.rid

    def test_title_twins(self):
        film = self.title(kind="movie", imdb_id="tt32537226", tmdb_id=1387382, title_ru="Сезон охоты",
                          title_original="Hunting Season", year=2025)
        self.rel(film, "Сезон охоты / Hunting Season (2025) BDRip", {"title_ru": "Сезон охоты", "title_original": "Hunting Season"})
        tr = self.title(kind="series", tracker_only=1, season_number=1, title_ru="Сезон охоты",
                        title_original="Hunting Season", year=2025)
        self.rel(tr, "Сезон охоты / Hunting Season (Раджа Коллинз) [2025, боевик]", how="tracker_only", src="rustorka")
        pack = self.title(kind="movie", tracker_only=1, title_ru="Сезон охоты", title_original="Hunting Season", year=2025)
        self.rel(pack, "Сезон охоты / Hunting Season (2025 - 2026) BDRip [Дилогия]", how="tracker_only", src="nnmclub")
        a = self.title(kind="movie", imdb_id="tt1", title_ru="Убийства в...", title_original="Meurtres dans les Vosges", year=2026)
        b = self.title(kind="movie", tmdb_id=2, title_ru="Убийства в...", title_original="Petits meurtres en Corrèze", year=2026)
        self.rel(a, "Убийства в... / Meurtres dans les Vosges (2026)")
        self.rel(b, "Убийства в... / Petits meurtres en Corrèze (2026)", how="tmdb_search")
        cop_s = self.title(kind="series", season_number=1, series_imdb_id="tt39307240", title_ru="Зверский коп", year=2026)
        cop_f = self.title(kind="movie", imdb_id="tt39307240", tmdb_id=1617245, title_ru="Зверский коп", year=2026)
        self.rel(cop_s, "Зверский коп / Bêtes de Flic (2026)")
        self.rel(cop_f, "Зверский коп / Betes de Flic / 2026", how="tmdb_search", src="kinozal")
        st = match.merge_title_twins(self.c, log=lambda *_: None)
        self.assertEqual(sorted((d, m) for d, m, _ in st["merged"]), sorted([(tr, film), (cop_s, cop_f)]))
        dup = lambda t: self.c.execute("SELECT duplicate_of FROM titles WHERE id = ?", (t,)).fetchone()[0]
        self.assertIsNone(dup(pack))     # a pack is not the film
        self.assertIsNone(dup(a))        # «Убийства в...»: another film of the same Russian name

    def test_phantom_season(self):
        for s, y, tm in ((1, 2023, 1), (2, 2024, 2)):
            self.title(kind="series", season_number=s, series_imdb_id="tt15591076", tmdb_id=tm, year=y)
        s3 = self.title(kind="series", season_number=3, series_imdb_id="tt15591076", year=2024)
        rid = self.rel(s3, "Шоу 90-х / That '90s Show [S01-03] (2023-2024)")
        for s in (1, 2):
            sid = self.c.execute("SELECT id FROM titles WHERE season_number = ?", (s,)).fetchone()[0]
            self.c.execute("INSERT INTO release_titles (release_id, title_id, how) VALUES (?, ?, 'imdb_page')", (rid, sid))
        self.assertEqual(match.phantom_seasons(self.c), {s3})
        self.rel(s3, "Шоу 90-х / That '90s Show [S03] (2024)")    # a release of its own: a real new season
        self.assertEqual(match.phantom_seasons(self.c), set())

    def test_page_id_beats_name(self):
        # «Шип любви»: rutor / kinozal pages link tt33243173; the TMDB row «与君诀» tt32868957 held a release found by name
        a = self.title(kind="series", season_number=1, series_imdb_id="tt33243173", title_ru="Шип любви", year=2024)
        b = self.title(kind="series", season_number=1, series_imdb_id="tt32868957", series_tmdb_id=258762, tmdb_id=258762,
                       title_original="与君诀", year=2024)
        self.rel(a, "Шип любви / Yu Jun Jue / Love & Bid Farewell [S01] (2024)", {"title_original": "Yu Jun Jue"})
        self.rel(b, "Шип любви / Yu Jun Jue (Love & Bid Farewell) / 2024", {"title_original": "Yu Jun Jue"},
                 how="imdb_resolve", src="kinozal")
        self.assertEqual(match.merge_part_twins(self.c, log=lambda *_: None)["merged"], [])

    def test_merge_revives_a_wrong_link(self):
        # review round 9: «Легенда о чёрном дереве» S2 — the release once moved off the TMDB row came back with a twin merge;
        # INSERT OR IGNORE kept the old wrong_link row, the release had no active link at all
        main = self.title(kind="series", season_number=2, series_imdb_id="tt31410134", tmdb_id=246614, year=2024)
        dup = self.title(kind="series", season_number=2, series_imdb_id="tt31409605", year=2024)
        rid = self.rel(dup, "Легенда о чёрном дереве (2 сезон) / Kara Agac Destani / 2024")
        self.c.execute("INSERT INTO release_titles (release_id, title_id, how) VALUES (?, ?, 'wrong_link: season recheck → 2')",
                       (rid, main))
        match._merge_into(self.c, dup, main, "part_twin")
        self.assertEqual(self.c.execute("SELECT how FROM release_titles WHERE release_id = ? AND title_id = ?",
                                        (rid, main)).fetchone()[0], "part_twin")
        from films import visible_check
        self.assertEqual(visible_check.lost_links(self.c), [])
        self.c.execute("UPDATE release_titles SET how = 'wrong_link: x → 9' WHERE release_id = ?", (rid,))
        self.assertEqual(len(visible_check.lost_links(self.c)), 1)

    def test_movie_link_by_page_id(self):
        # review round 10: «Взаперти / Shut In (2022)» linked by the Russian name to a Russian 2023 film; the page says
        # tt10131024. Kept: a series record, another length («Одиссея» 86 min ≠ Nolan's 173), the Russian name's title.
        ins = "INSERT INTO imdb_basics (tconst, title_type, primary_title, original_title, start_year, runtime_min) VALUES (?, ?, ?, ?, ?, ?)"
        self.c.executemany(ins, [("tt10131024", "movie", "Shut In", "Shut In", 2022, 89),
                                 ("tt6741278", "tvSeries", "Invincible", "Invincible", 2021, None),
                                 ("tt41605854", "movie", "The Odyssey", "The Odyssey", 2026, 86)])
        ru = self.title(kind="movie", imdb_id="tt29107332", year=2023, title_ru="Взаперти", title_original="Взаперти")
        us = self.title(kind="movie", imdb_id="tt10131024", year=2022, title_ru="Взаперти", title_original="Shut In")
        inv = self.title(kind="movie", imdb_id="tt14849458", year=2021, title_ru="Неуязвимый", title_original="Invincible")
        nolan = self.title(kind="movie", imdb_id="tt33764258", year=2026, title_ru="Одиссея", title_original="The Odyssey")
        a = self.rel(ru, "Взаперти / Shut In (2022) BDRip", {"imdb_id": "tt10131024", "year": 2022, "title_ru": "Взаперти",
                                                            "title_original": "Shut In", "runtime_min": 89}, how="exact_name_year")
        self.rel(inv, "Неуязвимый / Invincible (2021)", {"imdb_id": "tt6741278", "year": 2021, "title_original": "Invincible"},
                 how="imdb_resolve", src="nnmclub")
        self.rel(nolan, "Одиссея / The Odyssey (2026)", {"imdb_id": "tt41605854", "year": 2026, "title_original": "The Odyssey",
                                                        "runtime_min": 173}, how="imdb_resolve", src="kinozal")
        st = match.recheck_movie_links(self.c, log=lambda *_: None)
        self.assertEqual(st["relinked"], [{"release_id": a, "from": ru, "to": us}])
        self.assertEqual(self.c.execute("SELECT how FROM release_titles WHERE release_id = ? AND title_id = ?",
                                        (a, us)).fetchone()[0], "page_recheck")

    def test_tracker_twin_a_year_off(self):
        # rustorka «Грязные деньги / In the Grey (Гай Ричи) [2025 …]» — the film 270 is 2026; the director decides
        film = self.title(kind="movie", imdb_id="tt27681354", tmdb_id=1, year=2026, title_ru="Грязные деньги",
                          title_original="In the Grey")
        self.rel(film, "Грязные деньги / In the Grey (2026)", {"title_original": "In the Grey"})
        tr = self.title(kind="movie", tracker_only=1, year=2025, title_ru="Грязные деньги", title_original="In the Grey")
        self.rel(tr, "Грязные деньги / In the Grey (Гай Ричи) [2025, боевик, BDRemux 2160p]", how="tracker_only", src="rustorka")
        self.assertEqual(match.merge_title_twins(self.c, dry=True, log=lambda *_: None)["merged"], [])
        pid = self.c.execute("INSERT INTO people (name, name_ru) VALUES ('Guy Ritchie', 'Гай Ричи')").lastrowid
        self.c.execute("INSERT INTO credits (title_id, person_id, role) VALUES (?, ?, 'director')", (film, pid))
        self.assertEqual(match.merge_title_twins(self.c, dry=True, log=lambda *_: None)["merged"], [(tr, film, "tracker_twin")])

    def test_release_data_says_another_film(self):
        # review round 11: rutor «Корни (2024)» — Kazakhstan, 138 min — on the Irish «Frewaka» (103 min); rustorka «Лакомый
        # кусок / The Rip (Джо Карнахан) [2025]» on an Italian 97-min «RIP» while Carnahan's film is in the DB
        irish = self.title(kind="movie", imdb_id="tt1", tmdb_id=1, year=2024, title_ru="Корни", title_original="Fréamhacha",
                           runtime_min=103, countries='["Ирландия", "Великобритания"]')
        kz = self.rel(irish, "Корни (2024) WEB-DL 2160p", {"year": 2024, "title_ru": "Корни", "runtime_min": 138,
                                                          "countries": ["Казахстан"], "directors": ["Илья Таучелов"]},
                      how="exact_name_year")
        it = self.title(kind="movie", imdb_id="tt2", tmdb_id=2, year=2025, title_ru="Покойся с миром", title_original="RIP",
                        runtime_min=97, countries='["Италия"]')
        rip = self.title(kind="movie", imdb_id="tt32642706", tmdb_id=3, year=2026, title_ru="Лакомый кусок",
                         title_original="The Rip", runtime_min=113, countries='["США"]')
        pid = self.c.execute("INSERT INTO people (name, name_ru) VALUES ('Joe Carnahan', 'Джо Карнахан')").lastrowid
        self.c.execute("INSERT INTO credits (title_id, person_id, role) VALUES (?, ?, 'director')", (rip, pid))
        r2 = self.rel(it, "Лакомый кусок / The Rip (Джо Карнахан) [2025, боевик]",
                      {"year": 2025, "title_ru": "Лакомый кусок", "title_original": "The Rip", "runtime_min": 115,
                       "countries": ["США"], "directors": ["Джо Карнахан"]}, how="imdb_resolve", src="rustorka")
        # a Director's cut of the same country stays
        ext = self.rel(irish, "Корни (2024) Extended", {"year": 2024, "title_ru": "Корни", "runtime_min": 140,
                                                       "countries": ["Ирландия."]}, how="exact_name_year")
        st = match.recheck_release_data(self.c, log=lambda *_: None)
        moved = {r["release_id"]: r["to"] for r in st["relinked"]}
        self.assertEqual(set(moved), {kz, r2})
        self.assertEqual(moved[r2], rip)
        self.assertEqual(self.c.execute("SELECT tracker_only FROM titles WHERE id = ?", (moved[kz],)).fetchone()[0], 1)
        self.assertNotIn(ext, moved)
        # … and the twin rule does not bring it back by name and year
        self.assertEqual(match.merge_title_twins(self.c, dry=True, log=lambda *_: None)["merged"], [])

    def test_series_pack_on_a_film_row(self):
        # «Наруто + Наруто: Ураганные хроники [01-135, 01-479] (2002)» — a «film» on the series' IMDb id
        self.c.execute("INSERT INTO imdb_basics (tconst, title_type, primary_title, original_title, start_year) "
                       "VALUES ('tt0409591', 'tvSeries', 'Naruto', 'Naruto', 2002)")
        s1 = self.title(kind="series", season_number=1, series_imdb_id="tt0409591", tmdb_id=46260, year=2002)
        film = self.title(kind="movie", imdb_id="tt0409591", year=2002, title_ru="Наруто + Наруто: Ураганные хроники")
        self.rel(film, "Наруто + Наруто: Ураганные хроники / Naruto + Naruto Shippuuden [01-135, 01-479] (2002)")
        self.assertIn((film, s1, "series_pack_twin"), match.merge_title_twins(self.c, dry=True, log=lambda *_: None)["merged"])

    def test_carried_link_off_the_specials(self):
        # «Атака титанов … [1x1-25 из 25] (2013)» came onto the OVA row (season 0) with a duplicate's links
        s0 = self.title(kind="series", season_number=0, series_imdb_id="tt2560140", tmdb_id=1, year=2013)
        s1 = self.title(kind="series", season_number=1, series_imdb_id="tt2560140", tmdb_id=2, year=2013)
        self.rel(s0, "Атака титанов / Attack on Titan (2013) WEB-DLRip [AV1/2160p] (сезон 1 из 4, 1-25 серии из 25)",
                 {"year": 2013, "name_parsed": {"seasons": [1]}}, how="exact_name_year via-duplicate-5529", src="nnmclub")
        st = match.recheck_series_links(self.c, log=lambda *_: None)
        self.assertEqual(st["relinked"], [{"release_id": 1, "from": [s0], "to": [s1]}])

    def test_round12_release_data(self):
        # «Люси / Lucy (2003)»: the page names the title itself, the tracker writes the director in Russian only —
        # «Гленн Джордан» = Glenn Jordan; MediaInfo 94 vs 128 is no other film
        lucy = self.title(kind="movie", imdb_id="tt0361856", tmdb_id=1, year=2003, title_ru="Люси", title_original="Lucy",
                          runtime_min=128)
        pid = self.c.execute("INSERT INTO people (name) VALUES ('Glenn Jordan')").lastrowid
        self.c.execute("INSERT INTO credits (title_id, person_id, role) VALUES (?, ?, 'director')", (lucy, pid))
        self.rel(lucy, "Люси / Lucy (2003) WEB-DLRip", {"year": 2003, "title_ru": "Люси", "imdb_id": "tt0361856",
                                                       "runtime_min": 94, "countries": ["США"],
                                                       "directors": ["Гленн Джордан"]}, how="exact_name_year", src="nnmclub")
        self.assertEqual(match.recheck_release_data(self.c, log=lambda *_: None)["relinked"], [])
        # the year-mismatch length rule needs the director too: «Кевин Фэйр» = Kevin Fair, «Чо Ён-джун» ≠ the 2006 film's
        hint = self.title(kind="movie", imdb_id="tt12739938", year=2020, countries='["Канада"]')
        kf = self.c.execute("INSERT INTO people (name) VALUES ('Kevin Fair')").lastrowid
        self.c.execute("INSERT INTO credits (title_id, person_id, role) VALUES (?, ?, 'director')", (hint, kf))
        sun = self.title(kind="movie", imdb_id="tt0844347", year=2006, countries='["Япония"]')
        nm = self.c.execute("INSERT INTO people (name) VALUES ('Norihiro Koizumi')").lastrowid
        self.c.execute("INSERT INTO credits (title_id, person_id, role) VALUES (?, ?, 'director')", (sun, nm))
        self.assertTrue(match._release_data_agrees(self.c, {"countries": ["Канада"], "directors": ["Кевин Фэйр"]}, hint))
        self.assertFalse(match._release_data_agrees(self.c, {"countries": ["Южная Корея"], "directors": ["Чо Ён-джун"]}, sun))

    def test_round12_page_by_russian_name(self):
        # «Женщина в чёрном. Невеста дьявола / Pengantin Iblis» 90 min: the page's «Pengantin Setan» 91 (in the DB under
        # that Russian name), not «Pengantin Iblis» 105 — no Latin name in common
        self.c.execute("INSERT INTO imdb_basics (tconst, title_type, primary_title, original_title, start_year, runtime_min) "
                       "VALUES ('tt33051946', 'movie', 'Pengantin Setan', 'Pengantin Setan', 2025, 91)")
        iblis = self.title(kind="movie", imdb_id="tt30877614", year=2025, title_ru="Невеста дьявола",
                           title_original="Pengantin Iblis", runtime_min=105)
        setan = self.title(kind="movie", imdb_id="tt33051946", year=2025, title_ru="Женщина в чёрном. Невеста дьявола",
                           title_original="Pengantin Setan", runtime_min=91)
        rid = self.rel(iblis, "Женщина в чёрном. Невеста дьявола / Pengantin Iblis (The Demon's Bride) / 2025 / ДБ",
                       {"imdb_id": "tt33051946", "year": 2025, "title_ru": "Женщина в чёрном. Невеста дьявола",
                        "title_original": "Pengantin Iblis", "runtime_min": 90}, how="tmdb_search", src="kinozal")
        st = match.recheck_movie_links(self.c, log=lambda *_: None)
        self.assertEqual(st["relinked"], [{"release_id": rid, "from": iblis, "to": setan}])

    def test_round13_director_names(self):
        # cross-script names: «yes» by both words, «no» only for plainly other people, else undecided (None)
        P = lambda n: [{"name": n}]   # noqa: E731
        for said, name in (("Мэтт Джонсон", "Matt Johnson"), ("Марсель Вальц", "Marcel Walz"), ("Келси Иган", "Kelsey Egan"),
                           ("Рудольф Гавлик", "Rudolf Havlík"), ("Павел Яндоурек", "Pavel Jandourek"),
                           ("Джордж Жакс", "George Jaques"), ("Кевин Фэйр", "Kevin Fair")):
            self.assertIs(match._director_same([said], P(name)), True, said)
        for said, name in (("Дебора Брок", "Amy Holden Jones"), ("Нэйтан Сильвер", "Sylvester Stallone")):
            self.assertIs(match._director_same([said], P(name)), False, said)
        for said, name in (("Ким Чжи-ун", "Kim Jee-woon"), ("Чо Ён-джун", "Norihiro Koizumi")):
            self.assertIsNone(match._director_same([said], P(name)), said)
        # round 14: W inside a word is a vowel — «Мэтьюз» / Matthews now agree
        self.assertIs(match._director_same(["Морган Мэтьюз"], P("Morgan Matthews")), True)
        # review round 14: f = v, W a vowel, diacritics; another script is undecided
        for said, name in (("Стивен Коньетти", "Stephen Cognetti"), ("Орсон Уэллс", "Orson Wells"),
                           ("Лукаш Космицкий", "Łukasz Kośmicki"), ("Дэвид Боузер", "David Bowser")):
            self.assertIsNot(match._director_same([said], P(name)), False, said)
        self.assertIs(match._director_same(["Стивен Коньетти"], P("Stephen Cognetti")), True)
        self.assertIsNot(match._director_same(["Пол Томас Андерсон"], P("Paul W. S. Anderson")), True)
        self.assertIs(match._director_same(["Пол Томас Андерсон"], P("Paul Thomas Anderson")), True)
        self.assertIsNone(match._director_same(["Динг Лианг"], [{"name": "丁亮"}, {"name": "Wang Wei Ming"}]))

    def test_round13_episodes_on_a_film(self):
        # nnmclub «Неуязвимый / Invincible (2021) … (сезон 1, серии 1-8 из 8)» by name on the 2021 film: checked again
        film = self.title(kind="movie", imdb_id="tt14849458", year=2021, title_ru="Неуязвимый", title_original="Invincible")
        self.rel(film, "Неуязвимый / Invincible (2021) (сезон 1, серии 1-8 из 8)",
                 {"year": 2021, "name_parsed": {"seasons": [1], "episodes": [1, 8]}}, how="tmdb_search", src="nnmclub")
        self.assertEqual(match.recheck_episode_films(self.c, dry=True, log=lambda *_: None)["checked"], 1)

    def test_round12_named_pack(self):
        # «Клерки (Дилогия) / Clerks (Dilogy) / 1994-2006» linked by name to «Клерки» — a pack in rank, not its best release
        from films import rank
        pack = {"link_how": "imdb_resolve", "name": "Клерки (Дилогия) / Clerks (Dilogy) / 1994-2006 / ДБ",
                "parsed": json.dumps({"year": 1994, "name_parsed": {"name": "Клерки (Дилогия) / Clerks (Dilogy)",
                                                                    "year_from": 1994, "year_to": 2006}})}
        # review round 13: «Collection» in the film's own name, no span of years — not a pack
        coll = {"link_how": "imdb_page", "name": "Коллекционер 2 / The Collection (2012) BDRip",
                "parsed": json.dumps({"year": 2012, "name_parsed": {"name": "Коллекционер 2 / The Collection",
                                                                    "year_from": 2012}})}
        self.assertFalse(rank._pack_release(coll, "movie"))
        own = {"link_how": "imdb_page", "name": "Клерки / Clerks / 1994 / ПМ",
               "parsed": json.dumps({"year": 1994, "name_parsed": {"name": "Клерки / Clerks"}})}
        self.assertTrue(rank._pack_release(pack, "movie"))
        self.assertFalse(rank._pack_release(own, "movie"))
        self.assertFalse(rank._pack_release(pack, "series"))

    def test_round12_season_name_twin(self):
        # rutracker «Cyberpunk: Edgerunners 2 [ONA] [10 из 10] [2026]» as a tracker-only film = the season S2 2026
        s2 = self.title(kind="series", season_number=2, series_imdb_id="tt12590266", tmdb_id=1, year=2026,
                        title_ru="Киберпанк: Бегущие по краю", title_original="Cyberpunk: Edgerunners")
        self.rel(s2, "Киберпанк: Бегущие по краю / Cyberpunk: Edgerunners [S02] (2026)")
        tr = self.title(kind="movie", tracker_only=1, year=2026, title_ru="Киберпанк: Бегущие по краю 2",
                        title_original="Cyberpunk: Edgerunners 2")
        self.rel(tr, "Киберпанк: Бегущие по краю 2 / Cyberpunk: Edgerunners 2 [ONA] [10 из 10] [2026, WEB-DL 1080p]",
                 how="tracker_only", src="rutracker")
        self.assertIn((tr, s2, "season_name_twin"), match.merge_title_twins(self.c, dry=True, log=lambda *_: None)["merged"])


if __name__ == "__main__":
    unittest.main()
