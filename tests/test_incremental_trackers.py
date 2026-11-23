"""R212 / R322 (owner, 2026-11-13): «при обновлении данных мы ищем только всё новое для новых дат и новых фильмов и не
трогаем ничего старого на что материал уже найден … Если даже новое качество для фильма вышло на трекере … это просто
новое качество для обновления». Per-title tracker searches run ONCE per title; later releases of older titles arrive
through the rutor category listing (rutor.tracked_index). Offline: in-memory DB, temp state files, HTTP mocked.

  A1 films.sources.rutor   — the listing keeps rows of tracked older titles;
  A2 films.encountered / films.prequels — a unit is searched once (cursor without expiry), --again on request;
  A3 films.franchise_releases — a work is searched once (franchise_releases_searched.json);
  A4 films.quality_watch   — a film is searched once (quality_watch_searched.json);
  A5 films.sources.nnmclub — a search query runs once (state «search_done» kept for good, no daily reset).

Run: python -X utf8 -m unittest tests.test_incremental_trackers
"""
import json
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from films import db, encountered, franchise_releases, prequels, quality_watch
from films.sources import nnmclub as N
from films.sources import rutor


def mem_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.migrate(conn)
    return conn


def add_title(conn, kind, orig, year, ru=None, imdb=None, release=False):
    tid = conn.execute("INSERT INTO titles (kind, title_original, title_ru, year, imdb_id) VALUES (?, ?, ?, ?, ?)",
                       (kind, orig, ru, year, imdb)).lastrowid
    if release:
        rid = conn.execute("INSERT INTO releases (source, source_id, url, name) VALUES ('rutor', ?, 'u', ?)",
                           (f"x{tid}", f"{orig} ({year})")).lastrowid
        conn.execute("INSERT INTO release_titles (release_id, title_id) VALUES (?, ?)", (rid, tid))
    return tid


# ------------------------------------------------------------------------------------------------------------- A1

class TestRutorListingKeepsTrackedTitles(unittest.TestCase):
    def setUp(self):
        self.conn = mem_db()
        c = self.conn
        self.shrek = add_title(c, "movie", "Shrek", 2001, "Шрек", release=True)            # has releases
        add_title(c, "movie", "Old Unknown", 1999)                                          # nothing: not tracked
        self.heat = add_title(c, "movie", "Heat", 1995, "Схватка")                          # downloaded (Plex)
        self.seen = add_title(c, "movie", "Seen Film", 2005, release=True)                  # watched
        c.execute("INSERT INTO watched (title_id, source) VALUES (?, 'plex')", (self.seen,))
        add_title(c, "movie", "Border Film", 2024, release=True)                            # year_from itself
        c.commit()
        self.p = [mock.patch.object(encountered, "_sources", return_value={self.heat: {"plex"}, self.seen: {"lists"}}),
                  mock.patch.object(franchise_releases, "todo", return_value=[
                      {"title": "Blade Runner", "title_ru": "Бегущий по лезвию", "year": 1982, "imdb": "tt0083658"}])]
        for p in self.p:
            p.start()

    def tearDown(self):
        for p in self.p:
            p.stop()

    def test_index_and_keep_row(self):
        idx = rutor.tracked_index(self.conn, 2024)
        years = [2024, 2025, 2026, 2027]
        keep = lambda n: rutor.keep_row(n, years, idx)  # noqa: E731
        self.assertTrue(keep("Шрек / Shrek (2001) BDRip 1080p | D"))                        # title with releases
        self.assertTrue(keep("Схватка / Heat (1995) BDRemux 2160p | HDR"))                  # encountered (Plex)
        self.assertTrue(keep("Бегущий по лезвию / Blade Runner (1982) UHD BDRemux"))        # franchise work, no release
        self.assertTrue(keep("Border Film (2023) WEB-DL 1080p"))                            # a 2024 title named (2023)
        self.assertTrue(keep("Кто угодно / Anyone (2025) WEB-DL"))                          # collected years
        self.assertFalse(keep("Old Unknown (1999) DVDRip"))                                 # not tracked
        self.assertFalse(keep("Seen Film (2005) BDRip"))                                    # watched, list-only
        self.assertFalse(keep("Шрек / Shrek (1980) VHSRip"))                                # other year
        self.assertFalse(rutor.keep_row("Шрек / Shrek (2001) BDRip", years, None))           # no index: years only

    def test_walk_keeps_new_release_of_tracked_old_title(self):
        idx = rutor.tracked_index(self.conn, 2024)
        rows = [{"source_id": "900", "name": "Шрек / Shrek (2001) UHD BDRemux 2160p | HDR10, Dolby Vision"},
                {"source_id": "901", "name": "Old Unknown (1999) DVDRip"},
                {"source_id": "902", "name": "Новинка / New One (2026) WEB-DL 1080p"}]
        for r in rows:
            r.update(added_at=date.today().isoformat(), btih=None, size_bytes=1, seeders=1, leechers=0)
        pages = {0: rows, 1: []}
        with mock.patch.object(rutor, "get", side_effect=lambda url: url), \
                mock.patch.object(rutor, "parse_listing", side_effect=lambda url: pages[int(url.split("/")[-4])]):
            st = rutor.walk_category(self.conn, 1, "foreign_films", [2024, 2025, 2026, 2027], tracked=idx)
        have = {r[0] for r in self.conn.execute("SELECT source_id FROM releases WHERE source = 'rutor'")}
        self.assertIn("900", have)       # the new quality of an old film arrives through the listing
        self.assertIn("902", have)
        self.assertNotIn("901", have)
        self.assertEqual((st["kept"], st["kept_tracked"], st["new"]), (2, 1, 2))


# ------------------------------------------------------------------------------------------------------------- A2

def unit(key, ids, name):
    return {"key": key, "kind": "movie", "title_ids": ids, "years": {2001}, "sources": {"plex"}, "names": [name],
            "imdb": set(), "cats": [(1, "foreign_films")]}


class TestEncounteredOnce(unittest.TestCase):
    def setUp(self):
        self.conn = mem_db()
        self.tmp = tempfile.TemporaryDirectory()
        self.cursor = Path(self.tmp.name) / "encountered_cursor.json"
        self.cursor.write_text(json.dumps({"m:old": "2026-11-01"}), encoding="utf-8")   # legacy entry: a bare date
        self.groups = [unit("m:old", [1], "Old"), unit("m:new", [2], "New")]
        self.p = [mock.patch.object(encountered, "CURSOR", self.cursor),
                  mock.patch.object(encountered, "units", side_effect=lambda *a, **k: (self.groups, {}))]
        for p in self.p:
            p.start()

    def tearDown(self):
        for p in self.p:
            p.stop()
        self.tmp.cleanup()

    def run_collect(self, **kw):
        with mock.patch.object(encountered, "search_unit", return_value=({}, 3)) as su:
            st = encountered.collect(self.conn, do_match=False, log=lambda *_: None, **kw)
        return [c.args[0]["key"] for c in su.call_args_list], st

    def test_old_unit_not_searched_again_new_one_is(self):
        searched, st = self.run_collect()
        self.assertEqual(searched, ["m:new"])
        self.assertEqual(st["already_searched"], 1)
        cur = json.loads(self.cursor.read_text(encoding="utf-8"))
        self.assertEqual(cur["m:new"]["ids"], [2])
        self.assertEqual(cur["m:old"], {"at": "2026-11-01", "ids": [1]})    # legacy entry gets its ids
        self.assertEqual(self.run_collect()[0], [])                          # next run: nothing to search

    def test_unit_with_a_new_title_is_searched(self):
        self.run_collect()
        self.groups[0] = unit("m:old", [1, 7], "Old")                         # a newly encountered season / part
        self.assertEqual(self.run_collect()[0], ["m:old"])

    def test_again_and_days_are_explicit(self):
        self.assertEqual(sorted(self.run_collect(force=True)[0]), ["m:new", "m:old"])
        self.assertEqual(self.run_collect()[0], [])
        old = {"at": (date.today() - timedelta(days=30)).isoformat(), "ids": [1]}
        self.assertTrue(encountered.due(self.groups[0], {"m:old": old}, days=14))
        self.assertFalse(encountered.due(self.groups[0], {"m:old": old}))

    def test_failed_unit_is_not_recorded(self):
        with mock.patch.object(encountered, "search_unit", return_value=({}, 0)):
            encountered.collect(self.conn, do_match=False, log=lambda *_: None)
        self.assertNotIn("m:new", json.loads(self.cursor.read_text(encoding="utf-8")))


class TestPrequelsOnce(unittest.TestCase):
    def collect_force(self, *flags):
        with mock.patch.object(prequels.db, "connect", return_value=mem_db()), \
                mock.patch.object(prequels, "ensure", return_value={}), \
                mock.patch.object(encountered, "collect", return_value={}) as col, \
                mock.patch("builtins.print"):
            prequels.main(["prequels", "collect", *flags])
        return col.call_args.kwargs["force"]

    def test_parts_searched_once_unless_asked(self):
        self.assertFalse(self.collect_force())
        self.assertTrue(self.collect_force("--again"))


# ------------------------------------------------------------------------------------------------------------- A3

class TestFranchiseReleasesOnce(unittest.TestCase):
    def setUp(self):
        self.conn = mem_db()
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "franchise_releases_searched.json"
        self.state.write_text(json.dumps({"tt_old": "2026-11-13"}), encoding="utf-8")
        works = [{"franchise": "f", "imdb": imdb, "kind": "film", "title": name, "title_ru": None, "year": 1990,
                  "tmdb": None, "tmdb_type": None, "cats": [(1, "foreign_films")]}
                 for imdb, name in (("tt_old", "Old Work"), ("tt_new", "New Work"))]
        self.p = [mock.patch.object(franchise_releases, "SEARCHED", self.state),
                  mock.patch.object(franchise_releases, "todo", return_value=works)]
        for p in self.p:
            p.start()

    def tearDown(self):
        for p in self.p:
            p.stop()
        self.tmp.cleanup()

    def queries(self, **kw):
        with mock.patch.object(franchise_releases.rutor, "search_all", return_value=(0, [])) as sa:
            st = franchise_releases.collect(self.conn, log=lambda *_: None, **kw)
        return [c.args[1] for c in sa.call_args_list], st

    def test_old_work_not_searched_again_new_one_is(self):
        q, st = self.queries()
        self.assertEqual(q, ["New Work 1990"])
        self.assertEqual((st["works"], st["already_searched"], st["searched"]), (2, 1, 1))
        self.assertIn("tt_new", json.loads(self.state.read_text(encoding="utf-8")))
        self.assertEqual(self.queries()[0], [])
        self.assertEqual(sorted(self.queries(again=True)[0]), ["New Work 1990", "Old Work 1990"])

    def test_failed_search_is_not_recorded(self):
        with mock.patch.object(franchise_releases.rutor, "search_all", side_effect=RuntimeError("down")):
            franchise_releases.collect(self.conn, log=lambda *_: None)
        self.assertNotIn("tt_new", json.loads(self.state.read_text(encoding="utf-8")))


# ------------------------------------------------------------------------------------------------------------- A4

class TestQualityWatchOnce(unittest.TestCase):
    def setUp(self):
        self.conn = mem_db()
        add_title(self.conn, "movie", "Old Film", 2010, "Старый фильм", imdb="tt1")
        add_title(self.conn, "movie", "New Film", 2012, "Новый фильм", imdb="tt2")
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "quality_watch_searched.json"
        self.state.write_text(json.dumps({"tt1": "2026-11-13"}), encoding="utf-8")
        self.p = [mock.patch.object(quality_watch, "SEARCHED", self.state),
                  mock.patch.object(quality_watch.rank, "settings", return_value={"quality_watch": {"imdb": ["tt1", "tt2"]}})]
        for p in self.p:
            p.start()

    def tearDown(self):
        for p in self.p:
            p.stop()
        self.tmp.cleanup()

    def queries(self, **kw):
        with mock.patch.object(quality_watch.rutor, "search_all", return_value=(0, [])) as sa:
            st = quality_watch.collect(self.conn, log=lambda *_: None, **kw)
        return {c.args[1] for c in sa.call_args_list}, st

    def test_old_film_not_searched_again_new_one_is(self):
        q, st = self.queries()
        self.assertEqual(q, {"Новый фильм 2012", "New Film 2012"})
        self.assertEqual((st["films"], st["already_searched"], st["searched"]), (2, 1, 1))
        self.assertEqual(self.queries()[0], set())
        self.assertIn("Старый фильм 2010", self.queries(again=True)[0])


# ------------------------------------------------------------------------------------------------------------- A5

class TestNnmclubSearchOnce(unittest.TestCase):
    def setUp(self):
        self.conn = mem_db()
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.state = d / "state.json"
        # a key done on an EARLIER day: before R322 it was dropped by the daily reset
        self.state.write_text(json.dumps({"search_done": {"day": "2026-10-26", "keys": ["Old|1990|1990"]}}), encoding="utf-8")
        qs = [{"q": "Old", "lo": 1990, "hi": 1990, "why": ["r1"]}, {"q": "New", "lo": 1995, "hi": 1995, "why": ["r1"]}]
        self.p = [mock.patch.object(N, "STATE", self.state), mock.patch.object(N, "ROWS", d / "rows.json"),
                  mock.patch.object(N, "Http", return_value=SimpleNamespace(requests=0, errors=0)),
                  mock.patch.object(N, "forums", return_value=[]),
                  mock.patch.object(N, "search_queries", return_value=qs)]
        for p in self.p:
            p.start()

    def tearDown(self):
        for p in self.p:
            p.stop()
        self.tmp.cleanup()

    def searched(self, **kw):
        res = lambda http, q, fids: {"q": q, "total": 0, "capped": False, "rows": [], "pages": 1}  # noqa: E731
        with mock.patch.object(N, "_search_all", side_effect=res) as sa:
            N.crawl(self.conn, only_forums=[-1], pages=False, log=lambda *_: None, **kw)
        return [c.args[1] for c in sa.call_args_list]

    def test_old_query_not_run_again_new_one_is(self):
        self.assertEqual(self.searched(), ["New"])
        keys = json.loads(self.state.read_text(encoding="utf-8"))["search_done"]["keys"]
        self.assertEqual(sorted(keys), ["New|1995|1995", "Old|1990|1990"])
        self.assertEqual(self.searched(), [])                       # the next run (any day): nothing
        self.assertEqual(self.searched(search_again=True), ["Old", "New"])


if __name__ == "__main__":
    unittest.main()
