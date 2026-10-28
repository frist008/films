"""Tests for nnmclub.to (source priority 3): films/parse/nnmclub.py and films/sources/nnmclub.py.

The fixtures in tests/fixtures/nnmclub/ are trimmed guest pages saved on 2026-10-26 (forum 954 listing, a tracker.php
search for «Дюна», topic 1871886), stored as UTF-8 (the site serves windows-1251). Run:
python -X utf8 -m unittest tests.test_nnmclub
"""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from films import db
from films.parse import nnmclub as P
from films.sources import nnmclub as N

FIX = Path(__file__).resolve().parent / "fixtures" / "nnmclub"


def fx(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def mem_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.migrate(conn)
    N.ensure_sources(conn)
    return conn


class TestTitle(unittest.TestCase):
    def test_film_dv_profile(self):
        n, w = P.rutor_style_name("Напоминание о нём / Reminders of Him (2026) WEB-DL [H.265/2160p] [4K, HDR10, DV 8.1, 10-bit]")
        self.assertEqual(n, "Напоминание о нём / Reminders of Him (2026) WEB-DL 2160p | H.265, 4K, HDR10, DV 8.1")
        self.assertEqual(w, [])

    def test_series_studios_and_updating(self):
        n, _ = P.rutor_style_name("Медведь / The Bear (2026) WEB-DL [H.265/2160p] [4K, SDR, 10-bit] (сезон 5, серии 1 из 9) "
                                  "TVShows, HDrezka, LostFilm (обновляемая)")
        self.assertEqual(n, "Медведь / The Bear [5x1 из 9] (2026) WEB-DL 2160p | H.265, 4K, SDR | TVShows, HDrezka, LostFilm")

    def test_fan_ai_work_is_not_an_official_remaster(self):
        for tag in ("[handmade remastered AI]", "[handmade Upscale AI]", "[handmade AI]"):
            n, _ = P.rutor_style_name(f"Шрек / Shrek (2001) BDRip [H.264/2160p] [4K, SDR, 8-bit] {tag}")
            self.assertIn("AI Upscale", n)
            self.assertNotIn("remaster", n.lower())
        n, _ = P.rutor_style_name("Хитрый койот / Coyote vs. Acme (2026) DCPRip [H.265/2160p] [4K, SDR, 10-bit] [handmade]")
        self.assertNotIn("AI", n)

    def test_episode_counts_and_season_lists(self):
        n, _ = P.rutor_style_name("Трон / Throne of Seal (2022-2026) UHDTVRip [H.265/2160p] (сезон 1-8, 208 серий) Deep")
        self.assertIn("[1-8x1-208 из 208]", n)
        n, _ = P.rutor_style_name("The Simpsons / Симпсоны (1989-2005) DVDRip (Сезоны 1-17, 1-378 серий из 378) [UKR]")
        self.assertEqual(n, "The Simpsons / Симпсоны [1-17x1-378 из 378] (1989-2005) DVDRip | UKR")
        n, _ = P.rutor_style_name("Царь Горы / King of the Hill (1997-2002, 2009) WEBRip [H.265/1080p] [10-bit] "
                                  "(Сезон 1-6, 12, Серий 1-148 из 148) Дмитрий Пучков")
        self.assertIn("[S1-6] [S12] [1-148 из 148] (1997-2009) WEBRip 1080p", n)

    def test_voice_and_language_tags(self):
        n, _ = P.rutor_style_name("Скотный двор / Animal Farm (2025) BDRip [H.264/720p] [MVO]")
        self.assertTrue(n.endswith("| H.264 | MVO"))
        n, _ = P.rutor_style_name("Фоллаут / Fallout (2024) WEB-DL [H.264/1080p] (сезон 1, серии 1-8 из 8) [UKR, EN / UKR, EN Sub]")
        self.assertTrue(n.endswith("| UKR"))
        n, _ = P.rutor_style_name("Блуи / Bluey (2020) WEBRip [H.264/1080p] (сезон 2, серии 1-52) [EN / RU, EN, Multi Sub]")
        self.assertIn("| Sub", n)
        n, _ = P.rutor_style_name("ПиськаДу / Peepoodo (2017-2021) WEBRip [H.264/720p-1080p] (Сезон 1-2, серии 1-26 из 26)")
        self.assertIn("WEBRip 720p", n)

    def test_title_only_dv5(self):
        d = P.parse_title_only("Противостояние святого / Xian Ni (2023-2026) UHDTVRip [H.265/2160p] [4K, DV 5, 10-bit]", "animation")
        self.assertTrue(d["dv5_only"])

    def test_dates_sizes(self):
        self.assertEqual(P.parse_date("26 Окт 2026 03:12:47"), "2026-10-26T03:12:47")
        self.assertEqual(P.parse_date("19 Май 2026 07:31:21"), "2026-05-19T07:31:21")
        self.assertEqual(P.parse_date("20-09-2026 06:21"), "2026-09-20T06:21:00")
        self.assertEqual(P.parse_size("21.2&nbsp;GB"), int(21.2 * 1024 ** 3))


class TestPages(unittest.TestCase):
    def test_listing_skips_announcements(self):
        r = P.parse_listing(fx("listing_954.html"))
        self.assertEqual((r["page"], r["pages"]), (1, 79))
        self.assertEqual([x["t"] for x in r["rows"]], ["1896381", "1896743", "1894895"])
        self.assertEqual(r["rows"][0]["last_post"], "2026-10-26T03:12:47")
        self.assertEqual((r["rows"][0]["seeds"], r["rows"][0]["leech"]), (111, 13))
        self.assertTrue(all(x["torrent"] for x in r["rows"]))

    def test_search(self):
        r = P.parse_search(fx("search_dune.html"))
        self.assertEqual((r["total"], len(r["rows"])), (64, 5))
        row = r["rows"][0]
        self.assertEqual((row["t"], row["fid"], row["size"], row["seeds"]), ("1861062", 1296, 30915193032, 15))
        self.assertEqual(P.category_for_forum(row["fid"]), "foreign_films")
        self.assertEqual(row["added"], "2026-03-23T21:16:13")

    def test_topic_and_compact_roundtrip(self):
        page = fx("topic_1871886.html")
        d = P.parse_topic(page)
        self.assertEqual((d["resolution"], d["best_voice"], d["dv_profile"]), (2160, "dub", 8))
        self.assertEqual(d["voice_by_lang"], {"ru": ["dub"], "uk": ["dub"]})
        self.assertEqual(d["forced_subs"], ["ru", "uk"])
        self.assertEqual((d["imdb_id"], d["btih"]), ("tt33714084", "fc5b53f9e5a566ed79efb902c981c600abb2568f"))
        self.assertEqual((d["category"], d["added_at"]), ("foreign_films", "2026-05-19T07:31:21"))
        self.assertIn("mediainfo hidden from guests (description lines only)", d["warnings"])
        t = P.split_topic(page)
        d2 = P.parse_topic(P.compact_topic_html(dict(t, fetched_at="2026-10-26T05:00:00")))
        for k in ("resolution", "best_voice", "voice_by_lang", "imdb_id", "btih", "category", "size_bytes", "added_at"):
            self.assertEqual(d2[k], d[k], k)

    def test_page_problem(self):
        self.assertIsNone(P.page_problem('<form action="login.php"></form><a href="viewforum.php?f=1">x</a>'))
        self.assertEqual(P.page_problem("<title>Just a moment...</title><div id=cf-chl></div>"), "cloudflare")
        self.assertEqual(P.page_problem('<div class="g-recaptcha"></div>'), "captcha")
        self.assertEqual(P.page_problem('<form action="https://nnmclub.to/forum/login.php" method="post"></form>'), "login")


def _listing(rows):
    """A listing page: rows = [(topic id, title, last post 'DD Mon YYYY HH:MM:SS')]."""
    tr = "".join(
        f'<tr><td><span class="tDL">DL: </span><a href="viewtopic.php?t={t}"class="topictitle">{title}</a></td>'
        f'<td><span class="seedmed"><b>5</b></span><a href="download.php?id=1">1.0&nbsp;GB</a></td>'
        f'<td><span class="postdetails">{lp}<br>x</span></td></tr>' for t, title, lp in rows)
    return f'Страница <b>1</b> из <b>1</b><table><tr><td class="topicSep">Топики</td></tr>{tr}</table>'


class FakeHttp:
    def __init__(self, pages):
        self.pages, self.requests, self.errors, self.urls = pages, 0, 0, []

    def get(self, path, params=None):
        self.requests += 1
        self.urls.append(path)
        return self.pages.get(path)


class TestWalk(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.p = [mock.patch.object(N, "STATE", self.tmp / "state.json"), mock.patch.object(N, "ROWS", self.tmp / "rows.json")]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()

    def test_first_walk_floor_then_incremental_sync_point(self):
        page = _listing([("10", "A / A (2026) WEB-DL [H.264/1080p]", "26 Окт 2026 03:00:00"),
                         ("9", "B / B (2023) WEB-DL [H.264/1080p]", "10 Дек 2023 10:00:00")])
        http = FakeHttp({"viewforum.php?f=954": page})
        state, rows = {}, {}
        st = N.walk_forum(http, 954, state, rows, "2024-01-01T00:00:00", log=lambda m: None)
        self.assertEqual((st["mode"], st["stopped"]), ("first", "last page"))
        self.assertEqual(state["forums"]["954"]["last_success"]["newest"], "2026-10-26T03:00:00")
        # next run: incremental, stops at the sync point minus 2 days (the page is older than 2026-10-24T03:00)
        page2 = _listing([("11", "C / C (2026) WEB-DL [H.264/1080p]", "20 Окт 2026 03:00:00")])
        http2 = FakeHttp({"viewforum.php?f=954": page2})
        st2 = N.walk_forum(http2, 954, state, rows, "2024-01-01T00:00:00", log=lambda m: None)
        self.assertEqual((st2["mode"], st2["stopped"], st2["stop_at"]),
                         ("incremental", "sync point reached", "2026-10-24T03:00:00"))
        self.assertIn("11", rows)
        # a walk cut by the deadline does not move the sync point
        before = dict(state["forums"]["954"]["last_success"])
        st3 = N.walk_forum(http2, 954, state, rows, "2024-01-01T00:00:00", log=lambda m: None, deadline=1.0)
        self.assertEqual(st3["stopped"], "deadline")
        self.assertEqual(state["forums"]["954"]["last_success"], before)


class TestStore(unittest.TestCase):
    def test_store_title_then_page_idempotent_and_dv5(self):
        conn = mem_db()
        st, excl = N.new_stats(), set()
        title = "Напоминание о нём / Reminders of Him (2026) WEB-DL [H.265/2160p] [4K, HDR10, DV 8.1, 10-bit]"
        rid = N.store_topic(conn, "nnmclub", "1871886", title, "foreign_films", P.topic_url("1871886"), st, excl,
                            meta={"seeders": 5}, title_parse=P.parse_title_only)
        self.assertEqual((st["new"], st["name_only"]), (1, 1))
        t = P.split_topic(fx("topic_1871886.html"))
        raw = P.compact_topic_html(dict(t, fetched_at="x"))
        d = P.parse_topic(raw)
        rid2 = N.store_topic(conn, "nnmclub", "1871886", title, "foreign_films", P.topic_url("1871886"), st, excl,
                             raw=raw, d=d, meta={"btih": d["btih"]})
        self.assertEqual(rid, rid2)
        self.assertEqual((st["updated"], st["pages_stored"], st["ru_dub"], st["uk_dub"]), (1, 1, 1, 1))
        row = conn.execute("SELECT source, resolution, best_voice, btih, seeders, url FROM releases WHERE id=?", (rid,)).fetchone()
        self.assertEqual(tuple(row), ("nnmclub", 2160, "dub", "fc5b53f9e5a566ed79efb902c981c600abb2568f", 5,
                                      "https://nnmclub.to/forum/viewtopic.php?t=1871886"))
        self.assertEqual(N.raw_page(conn, rid), raw)
        # a title-only re-store keeps the page parse
        N.store_topic(conn, "nnmclub", "1871886", title, "foreign_films", P.topic_url("1871886"), st, excl,
                      title_parse=P.parse_title_only)
        self.assertEqual(st["meta_refreshed"], 1)
        # DV Profile 5 only: never inserted
        r5 = N.store_topic(conn, "nnmclub", "5", "X / X (2026) UHDTVRip [H.265/2160p] [4K, DV 5, 10-bit]", "foreign_films",
                           P.topic_url("5"), st, excl, title_parse=P.parse_title_only)
        self.assertIsNone(r5)
        self.assertIsNone(conn.execute("SELECT 1 FROM releases WHERE source='nnmclub' AND source_id='5'").fetchone())
        self.assertEqual(conn.execute("SELECT reason FROM excluded_releases WHERE source='nnmclub' AND source_id='5'").fetchone()[0],
                         "dv5_only")

    def test_search_selection(self):
        q = {"q": "Shrek", "lo": 2001, "hi": 2001, "need": ["4k"]}
        ok = {"fid": 1339, "title": "Шрек / Shrek (2001) UHD BDRip [AV1/2160p] [4K, HDR10, 10-bit]"}
        self.assertTrue(N.select_row(ok, q))
        self.assertFalse(N.select_row(dict(ok, title="Шрек / Shrek (2001) BDRip [H.264/1080p]"), q))
        self.assertFalse(N.select_row(dict(ok, fid=413), q))                     # a game forum
        self.assertFalse(N.select_row(dict(ok, title="Шрек 2 / Shrek 2 (2004) UHD BDRip [AV1/2160p]"), q))
        self.assertTrue(N.select_row(dict(ok, title="Шрек / Shrek (2001) BDRip [H.264/1080p]"), dict(q, need=["dub"])))


if __name__ == "__main__":
    unittest.main()
