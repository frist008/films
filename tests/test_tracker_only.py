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
