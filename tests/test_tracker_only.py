"""Tracker-only titles (films/tracker_only.py) and the poster chain (films/posters.py) — owner decisions 2026-10-25:
«Добавлять в таблицу просто без данных, раз не удаётся найти»; «TMDB — когда на IMDb и RT нет; а когда и там нет — с
источников торрента». Offline: an in-memory DB with the real schema, the matcher and image checks mocked.
Run:  python -X utf8 -m unittest tests.test_tracker_only
"""
import json
import sqlite3
import unittest
import zlib
from unittest import mock

from films import db, posters, sheet, tracker_only as T


def mem_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.migrate(conn)
    return conn


def parsed(name, ru, orig=None, year=2025, year_to=None, seasons=None, **kw):
    np = {"name": name, "title_ru": ru, "title_original": orig, "titles_other": [orig] if orig else [], "year": year,
          "year_from": year, "year_to": year_to or year, "seasons": seasons or [], "season_raw": None}
    d = {"name_parsed": np, "title_ru": ru, "title_original": orig or ru, "year": year, "imdb_id": None}
    d.update(kw)
    return d


def add_release(conn, rid, name, category, p, problem="no_imdb_not_found", source_id=None, btih=None):
    conn.execute("INSERT INTO releases (id, source, source_id, url, name, category, parsed, btih) VALUES (?, 'rutor', ?, ?, ?, ?, ?, ?)",
                 (rid, source_id or str(rid), f"http://rutor.info/torrent/{rid}", name, category, json.dumps(p, ensure_ascii=False), btih))
    conn.execute("INSERT INTO match_issues (release_id, problem, candidates) VALUES (?, ?, NULL)", (rid, problem))
    conn.commit()


PAGE = ('<html><h1>x</h1><div id="details"><table><tr><td>'
        '<img src="https://i6.imageban.ru/out/2026/09/17/poster.jpg" />'
        '<img src="http://www.kinopoisk.ru/rating/12714598.gif" />'
        '<img src="https://i128.fastpic.org/thumb/2026/0917/c0/shot.jpeg" />'
        '</td></tr><td class="header">Залит</td><td>2026</td></table></div></html>')


class TestPageImages(unittest.TestCase):
    def test_first_real_image(self):
        html = ('<img src="http://s.rutor.info/imdb/pic/27486290.gif" /><img src="http://www.kinopoisk.ru/rating/1.gif" />'
                '<img src="https://i4.imageban.ru/out/2026/poster.png" style="float:right" /><img src="https://x/shot.jpg">')
        self.assertEqual(posters.page_images(html)[0], "https://i4.imageban.ru/out/2026/poster.png")

    def test_spoiler_screenshots_skipped(self):
        html = ('<img src="https://imageban.ru/out/poster.jpg" /><div class="hidewrap"><div class="hidehead">Скриншоты</div>'
                '<textarea class="hidearea"><a href="https://fastpic.org/full/s.png"><img src="https://fastpic.org/thumb/s.jpeg">'
                '</a></textarea></div><img src="https://fastpic.org/big/second.jpg">')
        self.assertEqual(posters.page_images(html), ["https://imageban.ru/out/poster.jpg", "https://fastpic.org/big/second.jpg"])

    def test_kinopoisk_never(self):
        self.assertEqual(posters.page_images('<img src="https://st.kinopoisk.ru/images/film_big/1.jpg">'), [])

    def test_link_to_big_image_and_rutracker(self):
        html = '<a href="https://fastpic.org/big/p.jpg"><img src="https://fastpic.org/thumb/p.jpeg"></a>'
        self.assertEqual(posters.page_images(html), ["https://fastpic.org/big/p.jpg"])
        self.assertEqual(posters.page_images('<var class="postImg postImgAligned" title="https://i.ibb.co/a/b.jpg">&#10;</var>'),
                         ["https://i.ibb.co/a/b.jpg"])


class TestPosterChain(unittest.TestCase):
    def setUp(self):
        self.conn = mem_db()
        c = self.conn
        for tid in (1, 2, 3):
            c.execute("INSERT INTO titles (id, kind, title_ru, year) VALUES (?, 'movie', ?, 2025)", (tid, f"t{tid}"))
            c.execute("INSERT INTO releases (id, source, source_id, url, name) VALUES (?, 'rutor', ?, 'u', 'n')", (tid, str(tid)))
            c.execute("INSERT INTO release_titles (release_id, title_id, how) VALUES (?, ?, 'imdb_page')", (tid, tid))
            c.execute("INSERT INTO release_raw (release_id, body) VALUES (?, ?)", (tid, zlib.compress(PAGE.encode())))
        c.execute("INSERT INTO rt_pages (url, poster_url, fetched_at, title_id) VALUES ('https://rt/m/x', 'https://rt/p.jpg', 'now', 1)")
        c.execute("UPDATE titles SET poster_url = 'https://old/dead.jpg', poster_source = 'tracker', poster_release_id = 3 WHERE id = 3")
        c.commit()

    def test_rt_then_tracker(self):
        st = posters.fill(self.conn, alive=lambda u: "dead" not in u, fetch_imdb=False, log=lambda *_: None)
        got = {r["id"]: (r["poster_source"], r["poster_url"], r["poster_release_id"]) for r in
               self.conn.execute("SELECT id, poster_source, poster_url, poster_release_id FROM titles")}
        self.assertEqual(got[1], ("rt", "https://rt/p.jpg", None))
        self.assertEqual(got[2], ("tracker", "https://i6.imageban.ru/out/2026/09/17/poster.jpg", 2))
        # a dead tracker image is re-taken from the cached page
        self.assertEqual(got[3][0], "tracker")
        self.assertEqual(got[3][1], "https://i6.imageban.ru/out/2026/09/17/poster.jpg")
        self.assertEqual(st["tracker_retaken"], 1)

    def test_dead_everywhere_clears_and_reports(self):
        with mock.patch("films.sources.rutor.get", side_effect=RuntimeError("offline")):
            st = posters.fill(self.conn, ids=[3], alive=lambda u: False, fetch_imdb=False, log=lambda *_: None)
        self.assertEqual(st["tracker_cleared"], 1)
        self.assertIsNone(self.conn.execute("SELECT poster_url FROM titles WHERE id = 3").fetchone()[0])
        self.assertEqual(st["none_list"][0]["title_id"], 3)

    def test_wrong_link_and_duplicates_skipped(self):
        self.conn.execute("UPDATE release_titles SET how = 'wrong_link: x' WHERE title_id = 2")
        self.conn.execute("UPDATE titles SET duplicate_of = 1 WHERE id = 1")
        ids = [r["id"] for r in posters.targets(self.conn)]
        self.assertEqual(ids, [3])


class TestScope(unittest.TestCase):
    FRS = {"sword_art_online": ["мастера меча онлайн", "sword art online"], "monster_urasawa": ["монстр", "наоки урасава"],
           "bleach": ["блич"]}

    def test_franchise_by_name(self):
        self.assertEqual(T.franchise_of(["Мастера меча онлайн: Алисизация - Война Андерворлда"], self.FRS), "sword_art_online")
        self.assertEqual(T.franchise_of(["Sword Art Online"], self.FRS), "sword_art_online")
        self.assertIsNone(T.franchise_of(["Монстр за соседней партой ОВА"], self.FRS))
        self.assertEqual(T.franchise_of(["Монстр"], self.FRS), "monster_urasawa")
        self.assertEqual(T.franchise_of(["Блич: Тысячелетняя кровавая война"], self.FRS), "bleach")

    def test_ru_date(self):
        self.assertEqual(T.ru_date("19 августа 2026 года"), "2026-08-19")
        self.assertIsNone(T.ru_date("2026"))

    def test_fields_season_pack(self):
        rel = {"category": "animation", "name": "Шум и гам / Mongo [S01-02] (2024-2025)"}
        f = T.fields(rel, parsed(rel["name"], "Шум и гам", "Mongo", 2024, 2025, seasons=[1, 2], genres=["мультсериал"],
                                 countries=["Китай"], runtime_episode_min=11))
        self.assertEqual(f["kind"], "series")
        self.assertEqual(f["seasons"], [(1, 2024), (2, 2025)])
        self.assertEqual(f["is_animation"], 1)
        self.assertEqual(f["runtime_min"], 11)
        self.assertEqual(json.loads(f["countries"]), ["Китай"])
        self.assertEqual(T.series_key(f), "tracker:шум и гам")

    def test_eligible(self):
        conn = mem_db()
        frs, yf = self.FRS, 2024
        rel = {"id": 1, "source": "rutor", "source_id": "1", "name": "X (2025)", "category": "foreign_films", "btih": "ab"}
        ok = lambda p, problem="no_imdb_not_found", r=rel: T.eligible(conn, r, p, problem, frs, yf)
        self.assertTrue(ok(parsed("X (2025)", "X"))[0])
        self.assertFalse(ok(parsed("X (2025)", "X"), "collection_pack")[0])
        self.assertFalse(ok(parsed("Блэйд: Трилогия (1998-2004)", "Блэйд: Трилогия", year=1998, year_to=2004))[0])
        self.assertEqual(ok(parsed("X (2025)", "X", dv5_only=True))[1], "dv5_only")
        old = parsed("Y (2019)", "Y", year=2019)
        self.assertEqual(ok(old)[1], "before 2024, not encountered")
        conn.execute("INSERT INTO qbt_torrents (infohash, release_id) VALUES ('ab', NULL)")
        self.assertTrue(ok(old)[0])   # R162: in the owner's qBittorrent
        anime = dict(rel, category="anime")
        self.assertFalse(ok(parsed("Боец Баки (2018)", "Боец Баки", year=2018), r=anime)[0])
        r = ok(parsed("Мастера меча онлайн: Алисизация (2019)", "Мастера меча онлайн: Алисизация", year=2019), r=anime)
        self.assertEqual((r[0], r[2]), (True, "sword_art_online"))
        conn.execute("INSERT INTO excluded_releases (source, source_id, reason) VALUES ('rutor', '1', 'kpk')")
        self.assertEqual(ok(parsed("X (2025)", "X"))[1], "excluded kpk")


class TestCreateAndReidentify(unittest.TestCase):
    def setUp(self):
        self.conn = mem_db()
        add_release(self.conn, 10, "Пожалей / Аячы (2024) WEB-DL", "foreign_films",
                    parsed("Пожалей / Аячы (2024) WEB-DL", "Пожалей", "Аячы", 2024, countries=["Казахстан"], genres=["драма"],
                           runtime_min=95, premiere_world="7 мая 2024"))
        add_release(self.conn, 11, "Пожалей / Аячы (2024) WEBRip", "foreign_films",
                    parsed("Пожалей / Аячы (2024) WEBRip", "Пожалей", "Аячы", 2024), problem="no_imdb_ambiguous")
        add_release(self.conn, 12, "Эскорт [S01-02] (2021-2025)", "foreign_series",
                    parsed("Эскорт [S01-02] (2021-2025)", "Эскорт", None, 2021, 2025, seasons=[1, 2]))
        add_release(self.conn, 13, "Эскорт [S02] (2025)", "foreign_series", parsed("Эскорт [S02] (2025)", "Эскорт", None, 2025, seasons=[2]))
        self.st = T.create(self.conn, log=lambda *_: None)

    def test_rows(self):
        c = self.conn
        self.assertEqual(self.st["linked_releases"], 4)
        films = c.execute("SELECT * FROM titles WHERE kind = 'movie'").fetchall()
        self.assertEqual(len(films), 1)   # both releases of «Пожалей» share the row
        f = films[0]
        self.assertEqual((f["tracker_only"], f["title_ru"], f["title_original"], f["year"], f["runtime_min"], f["release_date"]),
                         (1, "Пожалей", "Аячы", 2024, 95, "2024-05-07"))
        self.assertIsNone(f["imdb_id"])
        self.assertIsNone(f["tmdb_id"])
        meta = json.loads(f["tracker_meta"])
        self.assertEqual(meta["release_ids"], [10, 11])
        seasons = {r["season_number"]: r for r in c.execute("SELECT * FROM titles WHERE kind = 'series'")}
        self.assertEqual(sorted(seasons), [1, 2])
        self.assertEqual((seasons[1]["year"], seasons[2]["year"]), (2021, 2025))
        self.assertEqual({r[0] for r in c.execute("SELECT release_id FROM release_titles WHERE title_id = ?", (seasons[2]["id"],))},
                         {12, 13})
        self.assertEqual({r[0] for r in c.execute("SELECT DISTINCT how FROM release_titles")}, {"tracker_only"})
        self.assertEqual(c.execute("SELECT count(*) FROM match_issues WHERE resolved_at IS NULL").fetchone()[0], 0)
        # nothing left to make on the next run
        self.assertEqual(T.create(c, log=lambda *_: None)["candidates"], 0)

    def test_reidentified_film_moves_links(self):
        c = self.conn
        old = c.execute("SELECT id FROM titles WHERE kind = 'movie'").fetchone()[0]
        c.execute("INSERT INTO user_marks (title_id, mark, note) VALUES (?, 'want', 'посмотреть')", (old,))
        real = c.execute("INSERT INTO titles (kind, imdb_id, title_ru, year) VALUES ('movie', 'tt1', 'Пожалей', 2024)").lastrowid
        c.commit()

        def fake_match(conn, rid, category, p, stats, issue):
            if p["title_ru"] != "Пожалей":
                issue("no_imdb_not_found", None)
                return None
            conn.execute("INSERT OR IGNORE INTO release_titles (release_id, title_id, how) VALUES (?, ?, 'tmdb_search')", (rid, real))
            conn.commit()
            return [real]

        with mock.patch("films.match.match_one", side_effect=fake_match):
            st = T.reidentify(c, log=lambda *_: None, min_age_hours=0)
        self.assertEqual(st["identified"], 2)
        self.assertEqual(st["rows_merged"], 1)
        self.assertEqual(c.execute("SELECT duplicate_of FROM titles WHERE id = ?", (old,)).fetchone()[0], real)
        hows = {r[0] for r in c.execute("SELECT how FROM release_titles WHERE title_id = ?", (old,))}
        self.assertTrue(all(h.startswith("wrong_link: tracker_only") for h in hows))   # kept, never used
        self.assertEqual(c.execute("SELECT count(*) FROM release_titles WHERE title_id = ?", (real,)).fetchone()[0], 2)
        self.assertEqual(c.execute("SELECT mark FROM user_marks WHERE title_id = ?", (real,)).fetchone()[0], "want")
        # the series rows were tried too and stay tracker-only
        self.assertEqual(c.execute("SELECT count(*) FROM titles WHERE kind = 'series' AND duplicate_of IS NULL "
                                   "AND identify_checked_at IS NOT NULL").fetchone()[0], 2)
        # a second pass right away skips the rows just tried
        with mock.patch("films.match.match_one", side_effect=fake_match):
            self.assertEqual(T.reidentify(c, log=lambda *_: None)["skipped_recent"], 2)

    def test_known_season_of_tmdb_no_title(self):
        c = self.conn
        known = c.execute("INSERT INTO titles (kind, season_number, series_key, series_tmdb_id, year) "
                          "VALUES ('series', 8, 'tmdb:57243', 57243, 2014)").lastrowid
        self.assertEqual(T.known_seasons(c, "tmdb_no_title", {"tmdb_id": 57243, "kind": "series", "seasons": [7, 8]}), {8: known})
        self.assertEqual(T.known_seasons(c, "season_year_mismatch", {"tmdb_id": 57243, "kind": "series", "seasons": [8]}), {})


class TestSeasonsByName(unittest.TestCase):
    """Review 2026-10-28: tracker-only twins of seasons the DB holds — «Пацаны» S4 (rustorka «Сезон 4») made as season 1,
    «Моя геройская академия (ТВ-7)» as a film, «Блич … (ТВ-2, часть 1)» must not become «Блич» season 2."""
    def setUp(self):
        c = self.c = mem_db()
        for s, y in ((1, 2019), (4, 2024)):
            c.execute("INSERT INTO titles (kind, season_number, series_imdb_id, title_ru, title_original, year) "
                      "VALUES ('series', ?, 'tt1190634', 'Пацаны', 'The Boys', ?)", (s, y))
        for s, y in ((1, 2016), (7, 2024), (8, 2025)):
            c.execute("INSERT INTO titles (kind, season_number, series_imdb_id, title_ru, title_original, year) "
                      "VALUES ('series', ?, 'tt5626028', 'Моя геройская академия', 'Boku no Hero Academia', ?)", (s, y))
        for s, y in ((1, 2022), (2, None)):
            c.execute("INSERT INTO titles (kind, season_number, series_imdb_id, title_ru, title_original, year) "
                      "VALUES ('series', ?, 'tt14986406', 'Блич: Тысячелетняя кровавая война', 'Bleach: Sennen Kessen-hen', ?)",
                      (s, y))
        self.id = lambda imdb, s: c.execute("SELECT id FROM titles WHERE series_imdb_id = ? AND season_number = ?",
                                            (imdb, s)).fetchone()[0]

    def test_words_and_tv_n(self):
        self.assertEqual(T.name_seasons("Пацаны / The Boys (Сезон 4 /Серии 1-8 из 8) [2024]"), [4])
        self.assertEqual(T.name_seasons("Фоллаут / Fallout / 2 сезон: 1-8 серии из 8 [2024]"), [2])
        self.assertEqual(T.name_seasons("Правдива терапія / Shrinking (Сезон 1-3) (2024)"), [1, 2, 3])
        self.assertEqual(T.name_seasons("Моя геройская академия (ТВ-7) / Boku no Hero Academia 7 [TV]"), [7])
        self.assertTrue(T.is_special("Медведь (Спецвыпуск: Гэри) / The Bear (Gary) / 2026"))
        self.assertFalse(T.is_special("Моя геройская академия (ТВ-7) [TV+Special] [21+4 из 21+4]"))

    def test_tmdb_only_series_and_name_in_brackets(self):
        # review 2026-10-28: «双世异闻录 / Princess Across Time» has no IMDb id; kinozal names it «Shuang Shi Yi Wen Lu
        # (Princess Across Time)» — the tracker-only twin was shown next to it
        tid = self.c.execute("INSERT INTO titles (kind, season_number, series_tmdb_id, tmdb_id, title_original, title_latin, "
                             "year) VALUES ('series', 1, 262241, 262241, '双世异闻录', 'Princess Across Time', 2024)").lastrowid
        name = "Принцесса сквозь время (1 сезон: 1-27 серии из 27) / Shuang Shi Yi Wen Lu (Princess Across Time) / 2024"
        p = parsed(name, "Принцесса сквозь время", "Shuang Shi Yi Wen Lu (Princess Across Time)", 2024)
        self.assertEqual(T.seasons_by_name(self.c, {"name": name}, p), {1: tid})

    def test_link_to_known_season(self):
        name = "Пацаны / The Boys (Сезон 4 /Серии 1-8 из 8) [2024, WEB-DL (2160p)] DUB [Red Head Sound]"
        p = parsed(name, "Пацаны", "The Boys", 2024)
        self.assertEqual(T.seasons_by_name(self.c, {"name": name}, p), {4: self.id("tt1190634", 4)})
        name = "Моя геройская академия (ТВ-7) / Boku no Hero Academia 7 / My Hero Academia 7th Season [TV] [2024]"
        p = parsed(name, "Моя геройская академия", "Boku no Hero Academia 7", 2024)
        self.assertEqual(T.seasons_by_name(self.c, {"name": name}, p), {7: self.id("tt5626028", 7)})
        self.assertEqual(T.fields({"category": "anime", "name": name}, p)["kind"], "series")

    def test_round5_names(self):
        # review round 5, 2026-10-28: Turkish letters, the kinozal season tail in the Russian name, «… 2 [TV]»
        self.assertEqual(T.norm("Kızılcık Şerbeti"), T.norm("Kizilcik Serbeti"))
        self.assertEqual(T.norm("Мій й Її"), "мій й її")
        self.assertEqual(T.clean_name("Кизиловый щербет (3 сезон: 1-37 серии из 37 )"), "Кизиловый щербет")
        self.assertEqual(T.name_seasons("Моя геройская академия 2 / Boku no Hero Academia 2 [TV] [25 из 25] [2017]"), [2])
        self.assertEqual(T.name_seasons("Мстители 2 / Avengers 2 (2015) BDRip"), [])

    def test_absolute_episodes_to_season_1(self):
        c = self.c
        for s, y in ((1, 1999), (21, 2019)):
            c.execute("INSERT INTO titles (kind, season_number, series_imdb_id, title_ru, title_original, title_latin, year) "
                      "VALUES ('series', ?, 'tt0388629', 'Ван-Пис', 'ワンピース', 'One Piece', ?)", (s, y))
        name = "Ван-Пис (892-1051 серии) / One Piece / 2019-2023 / ЛМ (AniMaunt) / WEBRip"
        self.assertEqual(T.seasons_by_name(c, {"name": name}, parsed(name, "Ван-Пис", "One Piece", 2019)),
                         {1: self.id("tt0388629", 1)})
        name = "Пацаны / The Boys (1-8 серии) [2026]"   # a plain «1-8 серии» of a series with several seasons: no guess
        self.assertEqual(T.seasons_by_name(c, {"name": name}, parsed(name, "Пацаны", "The Boys", 2026)), {})

    def test_reseason_and_duplicate_links(self):
        c = mem_db()
        n3 = "Кизиловый щербет / Kizilcik Serbeti (2024-2025) WEB-DLRip (сезон 3, серии 1-37 (67-103) из 37)"
        n4 = "Кизиловый щербет / Kizilcik Serbeti (2025-2026) WEB-DLRip (сезон 4, серии 1-35 (104-138) из 35)"
        old = c.execute("INSERT INTO titles (kind, season_number, series_key, title_ru, tracker_only, tracker_meta) "
                        "VALUES ('series', 1, 'tracker:кизиловый щербет', 'Кизиловый щербет', 1, '{}')").lastrowid
        for rid, n, y in ((1, n3, 2024), (2, n4, 2025)):
            c.execute("INSERT INTO releases (id, source, source_id, url, name, category, parsed) VALUES (?, 'nnmclub', ?, 'u', ?, "
                      "'foreign_series', ?)", (rid, str(rid), n, json.dumps(parsed(n, "Кизиловый щербет", "Kizilcik Serbeti", y))))
            c.execute("INSERT INTO release_titles (release_id, title_id, how) VALUES (?, ?, 'tracker_only')", (rid, old))
        st = T.reseason(c, log=lambda *_: None)
        self.assertEqual((st["moved"], st["rows_merged"]), (2, 1))
        got = {r[0] for r in c.execute("SELECT t.season_number FROM release_titles x JOIN titles t ON t.id = x.title_id "
                                       "WHERE x.how = 'tracker_only'")}
        self.assertEqual(got, {3, 4})
        self.assertIsNotNone(c.execute("SELECT duplicate_of FROM titles WHERE id = ?", (old,)).fetchone()[0])
        # an active link left on a duplicate row goes to its title
        real = c.execute("INSERT INTO titles (kind, title_ru) VALUES ('movie', 'Рита')").lastrowid
        dup = c.execute("INSERT INTO titles (kind, title_ru, duplicate_of) VALUES ('movie', 'Рита', ?)", (real,)).lastrowid
        c.execute("INSERT INTO releases (id, source, source_id, url, name) VALUES (9, 'rutor', '9', 'u', 'Рита (2024)')")
        c.execute("INSERT INTO release_titles (release_id, title_id, how) VALUES (9, ?, 'exact_name_year')", (dup,))
        self.assertEqual(T.move_duplicate_links(c, log=lambda *_: None), 1)
        self.assertEqual(c.execute("SELECT how FROM release_titles WHERE release_id = 9 AND title_id = ?", (real,)).fetchone()[0],
                         f"exact_name_year via-duplicate-{dup}")

    def test_season_years_must_fit(self):
        name = "Блич: Тысячелетняя кровавая война (ТВ-2, часть 1) / Bleach: Sennen Kessen-hen [TV] [2022]"
        p = parsed(name, "Блич: Тысячелетняя кровавая война", "Bleach: Sennen Kessen-hen", 2022)
        self.assertEqual(T.seasons_by_name(self.c, {"name": name}, p), {})


class TestOutOfScope(unittest.TestCase):
    """Releases the base does not collect (anime outside the owner's anime franchises, before 2024 and not in
    qBittorrent, КПК, old packs of no franchise) — their open issues are closed with the rule as the note, the matcher
    skips them, and they come back when the rule stops holding (triage 2026-10-25)."""
    FRS = {"bleach": ["блич"]}

    def setUp(self):
        self.conn = c = mem_db()
        add_release(c, 20, "Боец Баки / Baki [S01] (2018)", "anime", parsed("Боец Баки / Baki [S01] (2018)", "Боец Баки", "Baki", 2018, seasons=[1]),
                    problem="no_imdb_ambiguous")
        add_release(c, 21, "Полиция жизни / The Life Police (2019)", "foreign_films", parsed("x", "Полиция жизни", "The Life Police", 2019),
                    btih="cd")
        add_release(c, 22, "Эскорт [S02] (2025) | КПК", "foreign_series", parsed("x", "Эскорт", None, 2025, seasons=[2]))
        c.execute("INSERT INTO excluded_releases (source, source_id, reason) VALUES ('rutor', '22', 'kpk')")
        add_release(c, 23, "Судья Дредд: Дилогия / Judge Dredd: Dilogy (1995-2012)", "foreign_films",
                    parsed("Судья Дредд: Дилогия / Judge Dredd: Dilogy (1995-2012)", "Судья Дредд: Дилогия", "Judge Dredd: Dilogy", 1995, 2012),
                    problem="collection_pack")
        add_release(c, 24, "Джон Уик: Коллекция / John Wick: Collection (2014-2025)", "foreign_films",
                    parsed("Джон Уик: Коллекция / John Wick: Collection (2014-2025)", "Джон Уик: Коллекция", "John Wick: Collection", 2014, 2025),
                    problem="collection_pack")
        add_release(c, 25, "Неизвестный фильм (2026)", "foreign_films", parsed("x", "Неизвестный фильм", None, 2026))
        c.commit()

    def run_close(self):
        with mock.patch.object(T, "anime_franchises", return_value=self.FRS):
            return T.close_out_of_scope(self.conn, log=lambda *_: None)

    def test_close_and_skip(self):
        c = self.conn
        st = self.run_close()
        self.assertEqual(st["closed"], 4)
        self.assertEqual(st["by_reason"], {"anime outside the owner's anime franchises": 1, "before 2024": 1,
                                           "excluded kpk": 1, "pack before 2024": 1})
        open_ = {r[0] for r in c.execute("SELECT release_id FROM match_issues WHERE resolved_at IS NULL")}
        self.assertEqual(open_, {24, 25})   # a 2025 pack and an in-scope film stay for the pack matcher / tracker-only
        row = c.execute("SELECT candidates FROM match_issues WHERE release_id = 21").fetchone()[0]
        note = json.loads(row)
        self.assertEqual(note["out_of_scope"], "before 2024, not encountered")
        self.assertIn("вне диапазона базы", note["resolved_note"])
        # the matcher's pending query no longer picks them up (a release whose issue was closed otherwise is retried)
        add_release(c, 26, "Другой фильм (2026)", "foreign_films", parsed("x", "Другой фильм", None, 2026))
        c.execute("UPDATE match_issues SET resolved_at = datetime('now') WHERE release_id = 26")
        c.commit()
        import tempfile
        from pathlib import Path
        from films import locks, match
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # the matcher's lock in a temp folder, not the live data/cache/locks (review round 12)
        with mock.patch.object(locks, "DIR", Path(tmp.name)), \
                mock.patch.object(match, "link_packs", return_value={}), \
                mock.patch.object(match, "merge_redirected_titles", return_value={}), \
                mock.patch.object(match, "match_one", return_value=None) as m1, \
                mock.patch.object(T, "anime_franchises", return_value=self.FRS):
            match.match_releases(c)
        self.assertEqual(sorted(call.args[1] for call in m1.call_args_list), [26])   # 24, 25 still have open issues

    def test_reopen_when_encountered(self):
        c = self.conn
        self.run_close()
        c.execute("INSERT INTO qbt_torrents (infohash, release_id) VALUES ('cd', NULL)")   # now in the owner's client
        c.commit()
        with mock.patch.object(T, "anime_franchises", return_value=self.FRS):
            self.assertEqual(T.reopen_in_scope(c), 1)
            self.assertEqual(T.reopen_in_scope(c), 0)   # once
        rows = c.execute("SELECT resolved_at, candidates FROM match_issues WHERE release_id = 21 ORDER BY id").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertIn("reopened_at", json.loads(rows[0][1]))
        self.assertIsNone(rows[1][0])   # a fresh open issue: tracker-only creation picks it up
        self.assertIsNone(rows[1][1])   # the original candidates (none)

    def test_with_note_keeps_list(self):
        self.assertEqual(json.loads(T.with_note('[{"tconst": "tt1"}]', resolved_note="x")),
                         {"candidates": [{"tconst": "tt1"}], "resolved_note": "x"})
        self.assertEqual(json.loads(T.with_note('{"title_id": 5}', resolved_note="x")), {"title_id": 5, "resolved_note": "x"})


class TestSheetNote(unittest.TestCase):
    def test_note_before_links(self):
        cell = sheet._links_cell([("Картинка", "https://p/1.jpg")], note=T.NOTE)
        self.assertTrue(cell["userEnteredValue"]["stringValue"].startswith(T.NOTE + " · Картинка"))
        link_runs = [r for r in cell["textFormatRuns"] if r["format"].get("link")]
        self.assertEqual(link_runs[0]["startIndex"], len(T.NOTE) + 3)
        self.assertEqual(sheet._links_cell([], note=T.NOTE)["userEnteredValue"]["stringValue"], T.NOTE)
        self.assertEqual(sheet._links_cell([("IMDb", "u")])["userEnteredValue"]["stringValue"], "IMDb")
        self.assertEqual(sheet.TRACKER_ONLY_NOTE, T.NOTE)


if __name__ == "__main__":
    unittest.main()
