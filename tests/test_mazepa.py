"""Tests for the Ukrainian trackers' parser (films/parse/ua_trackers.py) on mazepa.to pages and for the mazepa crawler /
ingest (films/sources/mazepa.py, films/sources/ua_trackers.py; a fresh, migrated in-memory DB, no network).

tests/fixtures/mazepa/ are real pages saved by HTTP on 2026-10-26 and trimmed (plot text, scripts, ads cut; the announce
key replaced). Run:  python -X utf8 -m unittest tests.test_mazepa
"""
import json
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from films import db
from films.parse import ua_trackers as u
from films.sources import mazepa as mz
from films.sources import ua_trackers as ua

FIX = Path(__file__).resolve().parent / "fixtures" / "mazepa"
TODAY = date(2026, 10, 26)


def fx(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


class TestName(unittest.TestCase):
    def test_film_quality_codec_audio(self):
        self.assertEqual(u.rutor_style_name("Давай сестро 2: Знову за старе / Sister Act 2: Back in the Habit (1993) BDRip "
                                            "1080p H.265 2xUkr/Eng"),
                         ("Давай сестро 2: Знову за старе / Sister Act 2: Back in the Habit (1993) BDRip 1080p | H.265 | UKR", []))

    def test_seasons_and_episodes(self):
        n = lambda s: u.rutor_style_name(s)[0]  # noqa: E731
        self.assertEqual(n("Цілком таємно / The X-Files (Сезони 1-7) (1993-1998) BDRip 1080p H.265 Ukr/Eng"),
                         "Цілком таємно / The X-Files [S1-7] (1993-1998) BDRip 1080p | H.265 | UKR")
        self.assertEqual(n("Король Талси / Tulsa King (Сезон 4, серія 1) (2026) WEB-DL 1080p 2xUkr/Eng"),
                         "Король Талси / Tulsa King [4x1] (2026) WEB-DL 1080p | UKR")
        self.assertEqual(n("Пінгвін (Сезон 1, серії 1-8 з 8) / The Penguin (Season 1, episodes 1-8 of 8) (2024) WEB-DL 1080p "
                           "Ukr/Eng | Sub Eng"), "Пінгвін / The Penguin [1x1-8 из 8] (2024) WEB-DL 1080p | UKR")

    def test_hdr_dv_tags(self):
        n = lambda s: u.rutor_style_name(s)[0]  # noqa: E731
        self.assertEqual(n("Шуґар / Sugar (Сезон 2) (2026) WEB-DL 2160p HDR DV P8 2xUkr/Eng"),
                         "Шуґар / Sugar [S2] (2026) WEB-DL 2160p | HDR | DV P8 | UKR")
        self.assertEqual(n("Тенцинґ / Tenzing (2026) WEB-DL 2160p Dolby Vision TV P5 Ukr/Eng"),
                         "Тенцинґ / Tenzing (2026) WEB-DL 2160p | DV P5 | UKR")
        self.assertTrue(u.parse_title_only("Тенцинґ / Tenzing (2026) WEB-DL 2160p Dolby Vision TV P5 Ukr/Eng")["dv5_only"])
        self.assertFalse(u.parse_title_only("Шуґар / Sugar (Сезон 2) (2026) WEB-DL 2160p HDR DV P8 2xUkr/Eng")["dv5_only"])

    def test_no_ukr_in_title_is_assumed_with_warning(self):
        n, w = u.rutor_style_name("Перший шлюб Джорджі та Менді / Georgie & Mandy's First Marriage (Сезон 3, серії 1-3) "
                                  "(2026) WEBRip 1080p")
        self.assertEqual(n, "Перший шлюб Джорджі та Менді / Georgie & Mandy's First Marriage [3x1-3] (2026) WEBRip 1080p | UKR")
        self.assertEqual(len(w), 1)
        np = u.parse_title_only("Перший шлюб Джорджі та Менді / Georgie & Mandy's First Marriage (Сезон 3, серії 1-3) "
                                "(2026) WEBRip 1080p")["name_parsed"]
        self.assertEqual((np["title_original"], np["seasons"], np["episodes"], np["has_ukr"]),
                         ("Georgie & Mandy's First Marriage", [3], "1-3", True))

    def test_title_years(self):
        self.assertEqual(u.title_years("X / Y (Сезони 23-24) (2024-2025) WEBRip 1080p"), (2024, 2025))
        self.assertIsNone(u.title_years("без року"))


class TestVocabulary(unittest.TestCase):
    def test_labels_kinds_and_ukrainian_voice_line(self):
        t = u.ua_to_ru("Озвучення: багатоголосе закадрове (1) 2+2, двоголосе закадрове (2) Новий канал\n"
                       "Аудіо 1: AC3 2.0ch 192 Kbps - українська\nСубтитри: українські (форсовані), англійські\n"
                       "Тривалість: серія 00:26:38 (загалом 10)")
        self.assertIn("Перевод: украинский многоголосый закадровый 2+2, украинский двухголосый закадровый Новий канал", t)
        self.assertIn("Аудио 1: AC3 2.0ch 192 Kbps - украинский", t)
        self.assertIn("Субтитры: украинский (форсированные), английский", t)
        self.assertIn("Продолжительность: серия 00:26:38 (всего 10)", t)
        # one voice on two tracks: once per track; Ukrainian units
        self.assertEqual(u.ua_to_ru("Озвучення: дубльоване (1,2) Le Doyen"),
                         "Перевод: украинский дублированный Le Doyen, украинский дублированный Le Doyen")
        self.assertEqual(u.ua_to_ru("Тривалість: 55 хв. серія"), "Продолжительность: 55 мин серия")
        # «kind (1) A, (2) B»: track 2 has the same kind
        self.assertEqual(u.ua_to_ru("Озвучення: багатоголосе закадрове (1) Цікава Ідея, (2) DniproFilm"),
                         "Перевод: украинский многоголосый закадровый Цікава Ідея, украинский многоголосый закадровый DniproFilm")
        self.assertEqual(u.ua_to_ru("Аудіо 1: E-AC3 5.1, 384 Кбіт/сек - українська"),
                         "Аудио 1: E-AC3 5.1, 384 кбит/сек - украинский")
        # the first audio line without a language is the Ukrainian track; later ones stay unknown
        self.assertEqual(u.ua_to_ru("Аудіо: AAC 2.0ch 160 Kbps\nАудіо 2: AC3 5.1"),
                         "Аудио: AAC 2.0ch 160 Kbps - украинский\nАудио 2: AC3 5.1")

    def test_single_unlabelled_track_gets_the_voice_line(self):
        html_ = ('<div class="post_body">Тривалість: 01:16:12<br />Озвучення: багатоголосе закадрове<br />'
                 'Аудіо: AAC 2.0ch 160 Kbps</div>')
        d = u.parse_release_page(u.shape_html("Мій суджений – перевертень (2026) WEB-DL 1080p", html_))
        self.assertEqual((d["best_voice"], d["voice_by_lang"]), ("mvo", {"uk": ["mvo"]}))

    def test_voice_track_without_language_is_ukrainian_not_russian(self):
        html_ = ('<div class="post_body">X / Y (2026)<br />Аудіо 1: AC3 5.1 - українська, дубльований<br />'
                 'Аудіо 2: AAC 2.0 багатоголосий закадровий<br />Аудіо 3: AC3 5.1 - англійська</div>')
        d = u.parse_topic("mazepa", u.compact_topic_html("mazepa", {"topic_id": 1, "title": "X / Y (2026) WEB-DL 1080p",
                                                                     "forum_id": "12", "post_html": html_}), TODAY)
        self.assertEqual([a["lang"] for a in d["audio"]], ["uk", "uk", "en"])
        self.assertEqual(d["voice_by_lang"], {"uk": ["dub", "mvo"]})

    def test_voice_line_with_language_is_kept(self):
        self.assertEqual(u.ua_to_ru("Озвучення: Російське дубльоване"), "Перевод: русский дублированный")


class TestDatesSizes(unittest.TestCase):
    def test_dates(self):
        self.assertEqual(u.parse_ua_date("25 Жов 2025, 21:45", TODAY), "2025-10-25T21:45:00")
        self.assertEqual(u.parse_ua_date("Вчора 07:14", TODAY), "2026-10-25T07:14:00")
        self.assertEqual(u.parse_ua_date("Сьогодні 01:20", TODAY), "2026-10-26T01:20:00")
        self.assertEqual(u.parse_ua_date("25-Жов-25 21:45", TODAY), "2025-10-25T21:45:00")
        self.assertEqual(u.parse_ua_date("2024-05-20 18:02", TODAY), "2024-05-20T18:02:00")
        self.assertIsNone(u.parse_ua_date("колись", TODAY))

    def test_sizes(self):
        self.assertEqual(u.parse_size("18.73&nbsp;GB"), round(18.73 * 1024 ** 3))
        self.assertEqual(u.parse_size("823 MB"), 823 * 1024 ** 2)
        self.assertEqual(u.parse_size("1,5 ГБ"), round(1.5 * 1024 ** 3))
        self.assertIsNone(u.parse_size(""))


class TestPages(unittest.TestCase):
    def test_index_and_listing(self):
        forums = {f["f"]: f for f in u.parse_mazepa_index(fx("index.html"))}
        self.assertEqual(forums[12]["slug"], "forum-filmy-hd-f=12")
        self.assertEqual((forums[12]["category"], forums[44]["category"], forums[43]["category"]),
                         ("foreign_films", "foreign_series", "animation"))
        self.assertIsNone(forums[174]["category"])     # subtitled films: no voice
        self.assertIsNone(forums[16]["category"])      # anime (R44)
        self.assertTrue(set(u.MAZEPA_FORUMS) <= set(forums))
        p = u.parse_mazepa_listing(fx("forum_152.html"))
        self.assertEqual((p["page"], p["pages"], len(p["rows"])), (1, 6, 6))
        r = p["rows"][1]
        self.assertEqual(r["t"], "104092")
        self.assertTrue(r["href"].startswith("topic-") and r["href"].endswith("-t=104092.html"))
        self.assertEqual(r["title"], "Шуґар / Sugar (Сезон 2) (2026) WEB-DL 2160p HDR DV P8 2xUkr/Eng")

    def test_film_mvo_studio(self):
        d = u.parse_topic("mazepa", fx("topic_104085.html"), TODAY)
        self.assertEqual((d["category"], d["imdb_id"], d["resolution"], d["source_type"], d["video_codec"], d["video_kbps"]),
                         ("foreign_films", "tt26625693", 1080, "BDRemux", "AVC", 19700))
        self.assertEqual((d["best_voice"], d["voice_by_lang"]), ("mvo", {"uk": ["mvo"]}))
        self.assertEqual((d["audio"][0]["lang"], d["audio"][0]["studio"]), ("uk", "В одне рило"))
        self.assertEqual(d["audio"][1]["lang"], "en")
        self.assertEqual((d["added_at"], d["size_bytes"], d["seeders"], d["leechers"], d["btih"]),
                         ("2026-10-25T06:40:00", u.parse_size("18.73 GB"), 1, 3, "70f566ee63abda16b806ff454fb269318b5cbc77"))
        self.assertEqual(d["tracker"], {"source": "mazepa", "topic_id": "104085", "forum": "Фільми HD", "forum_id": "12"})

    def test_two_ukrainian_voices(self):
        d = u.parse_topic("mazepa", fx("topic_95804.html"), TODAY)
        self.assertEqual(d["voice_by_lang"], {"uk": ["mvo", "dvo"]})
        self.assertEqual([a.get("studio") for a in d["audio"][:2]][1], "Новий канал")

    def test_series_dub_forced_subs(self):
        d = u.parse_topic("mazepa", fx("topic_99234.html"), TODAY)
        self.assertEqual((d["category"], d["season"], d["resolution"], d["best_voice"]), ("foreign_series", "2", 2160, "dub"))
        self.assertEqual((d["audio"][0]["lang"], d["audio"][0]["kind"], d["audio"][0]["studio"]),
                         ("uk", "dub", "Так Треба Продакшн"))
        self.assertEqual((d["forced_subs"], d["runtime_episode_min"], d["added_at"]), (["uk"], 27, "2025-10-25T21:45:00"))
        self.assertIsNone(d["btih"])      # the page's magnet has 38 hex digits: not a btih

    def test_dv_hdr10plus_name(self):
        d = u.parse_topic("mazepa", fx("topic_103911.html"), TODAY)
        self.assertEqual((d["hdr"], d["dv5_only"], d["best_voice"]), (["DV", "HDR10+", "HDR10"], False, "mvo"))
        self.assertEqual(d["audio"][0]["studio"], "DniproFilm")

    def test_compact_roundtrip_and_no_announce_key(self):
        page = fx("topic_104085.html")
        sp = u.split_mazepa_topic(page)
        c = u.compact_topic_html("mazepa", dict(sp, fetched_at="2026-10-26T01:30:00"))
        self.assertNotIn("PASSKEY", c)
        a, b = u.parse_topic("mazepa", page, TODAY), u.parse_topic("mazepa", c, TODAY)
        for k in ("name", "audio", "best_voice", "added_at", "size_bytes", "btih", "imdb_id", "category", "tracker"):
            self.assertEqual(a[k], b[k], k)


class FakeHttp:
    """Serves forum pages from a dict: path -> html."""
    def __init__(self, pages):
        self.pages, self.requests, self.errors, self.calls = pages, 0, 0, []

    def get(self, path):
        self.calls.append(path)
        self.requests += 1
        return self.pages.get(path)


def listing(rows, page=1, pages=1):
    trs = "".join(f'<tr id="tr-{t}"><td><a id="tt-{t}" href="topic-x-t={t}.html" class="torTopic"><b>{title}</b></a></td></tr>'
                  for t, title in rows)
    return f'<table id="forum_table">{trs}</table><div class="pagination">Сторінка <b>{page}</b> з <b>{pages}</b></div>'


class TestCrawl(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.patches = [mock.patch.object(mz, "ROWS", d / "rows.json"), mock.patch.object(mz, "STATE", d / "state.json")]
        for p in self.patches:
            p.start()
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.migrate(self.conn)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_first_walk_all_pages_then_stop_at_nothing_new(self):
        a = [("1", "A / A (2024) WEB-DL 1080p Ukr/Eng"), ("2", "B / B (2025) WEB-DL 1080p Ukr/Eng")]
        b = [("3", "C / C (2010) BDRip 1080p Ukr/Eng")]
        http = FakeHttp({"forum-x-f=12.html": listing(a, 1, 2), "forum-x-f=12&start=50.html": listing(b, 2, 2)})
        state, rows = {}, {}
        st = mz.walk_forum(http, 12, "forum-x-f=12", state, rows, log=lambda *_: None)
        self.assertEqual((st["pages"], st["new"], st["stopped"]), (2, 3, "last page"))
        self.assertTrue(state["forums"]["12"]["complete"])
        # next run: page 1 has a new topic and a renamed one -> read; then page 2 unchanged -> stop
        a2 = [("4", "D / D (2026) WEB-DL 2160p Ukr/Eng"), ("1", "A / A (2024) WEB-DL 2160p Ukr/Eng")]
        http.pages["forum-x-f=12.html"] = listing(a2 + a[1:], 1, 2)
        st = mz.walk_forum(http, 12, "forum-x-f=12", state, rows, log=lambda *_: None)
        self.assertEqual((st["pages"], st["new"], st["changed"], st["stopped"], st["boundary_topic"]),
                         (2, 1, 1, "sync point reached", 3))
        self.assertEqual(rows["1"]["title"], "A / A (2024) WEB-DL 2160p Ukr/Eng")
        self.assertEqual(json.loads(mz.ROWS.read_text(encoding="utf-8"))["4"]["f"], 12)
        self.assertEqual([p["max_topic"] for p in state["forums"]["12"]["syncs"]], [3, 4])
        # nothing new at all: one page, stop
        st = mz.walk_forum(http, 12, "forum-x-f=12", state, rows, log=lambda *_: None)
        # both syncs are younger than the 2-day overlap: back to the first one (topic 3) — 2 pages
        self.assertEqual((st["pages"], st["stopped"], st["boundary_topic"]), (2, "sync point reached", 3))

    def test_overlap_two_days_and_unsuccessful_walk_is_not_a_sync_point(self):
        fs = {"complete": True, "syncs": [{"at": "2026-10-20T10:00:00", "max_topic": 2},
                                          {"at": "2026-10-25T10:00:00", "max_topic": 9}]}
        state = {"forums": {"12": dict(fs)}}
        rows = {str(t): {"f": 12, "title": f"T{t} (2025)"} for t in range(1, 10)}
        page1 = listing([(str(t), f"T{t} (2025)") for t in (9, 8, 7, 6)], 1, 4)
        page2 = listing([(str(t), f"T{t} (2025)") for t in (5, 4, 3)], 2, 4)
        page3 = listing([(str(t), f"T{t} (2025)") for t in (2, 1)], 3, 4)
        http = FakeHttp({"forum-x-f=12.html": page1, "forum-x-f=12&start=50.html": page2,
                         "forum-x-f=12&start=100.html": page3})
        st = mz.walk_forum(http, 12, "forum-x-f=12", state, rows, log=lambda *_: None)
        # boundary = newest topic of the sync ≥ 2 days before the last one (2026-10-20: topic 2): pages 1-3 re-read, page 4 not
        self.assertEqual((st["boundary_topic"], st["pages"], st["stopped"]), (2, 3, "sync point reached"))
        # a walk cut by a missing page records no sync point
        state = {"forums": {"12": dict(fs)}}
        http = FakeHttp({"forum-x-f=12.html": listing([("10", "New (2026)")], 1, 4)})
        st = mz.walk_forum(http, 12, "forum-x-f=12", state, rows, log=lambda *_: None)
        self.assertEqual(st["stopped"], "page missing")
        self.assertEqual(len(state["forums"]["12"]["syncs"]), 2)

    def test_wanted_year_and_old_titles(self):
        rows = {"1": {"f": 12, "title": "Нове / New (2025) WEB-DL 1080p"},
                "2": {"f": 12, "title": "Старе / Old Film (2010) BDRip 1080p"},
                "3": {"f": 12, "title": "Інше / Other (2010) BDRip 1080p"},
                "4": {"f": 174, "title": "Субтитри / Subbed (2025) WEB-DL 1080p"},
                "5": {"f": 44, "title": "Серіал / Show (Сезон 2) (2011) WEB-DL 1080p"}}
        want = mz.wanted(rows, 2024, {"old film": {2011}, "show": {2011}})
        self.assertEqual(want, {"1": "year", "2": "old_title", "5": "old_title"})

    def test_old_title_index_skips_watched(self):
        c = self.conn
        c.execute("INSERT INTO titles (id, kind, title_original, title_uk, year) VALUES (1, 'movie', 'Old Film', 'Старий', 2010)")
        c.execute("INSERT INTO titles (id, kind, title_original, year) VALUES (2, 'movie', 'Seen Film', 2011)")
        for rid, tid in ((1, 1), (2, 2)):
            c.execute("INSERT INTO releases (id, source, source_id, url, name) VALUES (?, 'rutor', ?, 'u', 'n')", (rid, str(rid)))
            c.execute("INSERT INTO release_titles (release_id, title_id) VALUES (?, ?)", (rid, tid))
        c.execute("INSERT INTO watched (title_id, source) VALUES (2, 'plex')")
        idx = mz.old_title_index(c, 2024)
        self.assertEqual(idx, {"old film": {2010}, "старий": {2010}})
        # review round 12: an old tracker-only row the trackers alone made wants no old pages; one the owner has does
        c.execute("INSERT INTO titles (id, kind, title_original, year, tracker_only) VALUES (3, 'movie', 'Breaking In', 2018, 1)")
        c.execute("INSERT INTO releases (id, source, source_id, url, name) VALUES (3, 'rutor', '3', 'u', 'n')")
        c.execute("INSERT INTO release_titles (release_id, title_id) VALUES (3, 3)")
        self.assertNotIn("breaking in", mz.old_title_index(c, 2024))
        c.execute("INSERT INTO qbt_torrents (infohash, name, title_id, present, synced_at) VALUES ('h', 'Breaking In', 3, 1, 'x')")
        self.assertIn("breaking in", mz.old_title_index(c, 2024))


class TestIngest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.migrate(self.conn)

    def rel(self, sid):
        return self.conn.execute("SELECT * FROM releases WHERE source='mazepa' AND source_id=?", (sid,)).fetchone()

    def test_name_then_page_then_dv5(self):
        st, excl = ua.new_stats(), set()
        title = "Ніхто цього не хоче / Nobody Wants This (Сезон 2) (2025) WEB-DL 2160p SDR Ukr/Eng"
        rid = ua.store_topic(self.conn, "mazepa", "99234", title, "foreign_series", None, st, excl)
        r = self.rel("99234")
        self.assertEqual((r["id"], r["url"], r["category"], r["resolution"], r["best_voice"]),
                         (rid, "https://mazepa.to/viewtopic.php?t=99234", "foreign_series", 2160, None))
        topic = u.split_mazepa_topic(fx("topic_99234.html"))
        topic["fetched_at"] = "2026-10-26T01:30:00"
        ua.store_topic(self.conn, "mazepa", "99234", topic["title"], "foreign_series", topic, st, excl, TODAY)
        r = self.rel("99234")
        self.assertEqual((r["best_voice"], r["season"], r["size_bytes"], r["added_at"]),
                         ("dub", "2", u.parse_size("23.45 GB"), "2025-10-25T21:45:00"))
        self.assertTrue(r["parsed_version"].startswith("ua-page:"))
        self.assertEqual((st["new"], st["updated"], st["name_only"], st["pages_stored"], st["ukr_dub"]), (1, 1, 1, 1, 1))
        self.assertIn("Так Треба Продакшн", ua.raw_page(self.conn, rid))
        # a name-only DV5 topic is never inserted
        ua.store_topic(self.conn, "mazepa", "104037", "Тенцинґ / Tenzing (2026) WEB-DL 2160p Dolby Vision TV P5 Ukr/Eng",
                       "foreign_films", None, st, excl)
        self.assertIsNone(self.rel("104037"))
        self.assertEqual(self.conn.execute("SELECT reason FROM excluded_releases WHERE source='mazepa' AND source_id='104037'")
                         .fetchone()[0], "dv5_only")
        self.assertIsNone(ua.store_topic(self.conn, "mazepa", "104037", "x", "foreign_films", None, st, excl))
        self.assertEqual(st["excluded_skipped"], 1)
        s = ua.status(self.conn, "mazepa")
        self.assertEqual((s["releases"], s["with_page"], s["ukr_dub"], s["excluded"]), (1, 1, 1, 1))
        self.assertEqual(ua.reparse(self.conn, "mazepa", all_=True)["parsed"], 1)

    def test_source_check_allows_new_trackers_only(self):
        self.conn.execute("INSERT INTO releases (source, source_id, url, name) VALUES ('toloka', '1', 'u', 'n')")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO releases (source, source_id, url, name) VALUES ('other', '1', 'u', 'n')")


if __name__ == "__main__":
    unittest.main()
