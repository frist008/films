"""films.plex_viewstate (баг 2026-11-23: владелец снял отметки «просмотрено» в Plex, а база считала их просмотренными —
«ты уверен что в плекс они отмечены просмотренными ты проверил?»). Plex подделан, база в памяти — только чтение Plex.
Run:  python -X utf8 -m unittest tests.test_plex_viewstate
"""
import sqlite3
import unittest

import requests

from films import db, plex_viewstate as V

MATCHES = "/library/metadata/matches"


class FakePlex:
    """Discover: фильм m1 (0), фильм m2 (просмотрен), сериал show1 — сезон 1 без просмотров, сезон 2 частично.
    Библиотека: ключ lib-gone — 404; guid-поиск фильма m1 — файл с viewCount 0."""

    def __init__(self):
        self.asked = []
        self.s = None

    def get(self, url, params=None, **kw):
        self.asked.append(url)
        params = params or {}
        if url.endswith(MATCHES):
            ids = {"imdb://tt0000001": "m1", "imdb://tt0000002": "m2", "imdb://tt0000010": "show1",
                   "imdb://tt0000003": "m3", "imdb://tt0000005": "m1"}
            pid = ids.get(params.get("guid"))
            return {"MediaContainer": {"Metadata": [{"ratingKey": pid}] if pid else []}}
        if url.endswith("/userState"):
            vc = {"m1": 0, "m2": 1, "m3": 0}[url.split("/")[-2]]
            return {"MediaContainer": {"UserState": [{"viewCount": vc}]}}
        if url.endswith("/show1/children"):
            return {"MediaContainer": {"totalSize": 2, "Metadata": [
                {"ratingKey": "s1", "index": 1, "viewedLeafCount": 0, "leafCount": 2},
                {"ratingKey": "s2", "index": 2, "viewedLeafCount": 1, "leafCount": 2}]}}
        if url.endswith("/s1/children"):
            return {"MediaContainer": {"totalSize": 2, "Metadata": [
                {"ratingKey": "e1", "index": 1, "viewCount": 0}, {"ratingKey": "e2", "index": 2}]}}
        if "/library/metadata/lib-gone" in url:
            r = requests.Response()
            r.status_code = 404
            raise requests.HTTPError("404", response=r)
        if url.endswith("/library/all"):
            if params.get("guid") == "plex://movie/m1":
                return {"MediaContainer": {"Metadata": [{"ratingKey": "900", "viewCount": 0}]}}
            return {"MediaContainer": {}}
        raise AssertionError("unexpected Plex request " + url)


class ViewState(unittest.TestCase):
    def setUp(self):
        self.c = sqlite3.connect(":memory:")
        self.c.row_factory = sqlite3.Row
        db.migrate(self.c)
        ins = "INSERT INTO titles (id, kind, title_original, year, imdb_id, series_imdb_id, season_number) VALUES (?,?,?,?,?,?,?)"
        self.c.execute(ins, (1, "movie", "Removed", 2008, "tt0000001", None, None))      # владелец снял
        self.c.execute(ins, (2, "movie", "Watched", 2020, "tt0000002", None, None))      # просмотрен
        self.c.execute(ins, (3, "series", "Show", 2015, None, "tt0000010", 1))          # сезон: снял
        self.c.execute(ins, (4, "series", "Show", 2016, None, "tt0000010", 2))          # сезон: частично
        self.c.execute(ins, (5, "movie", "Partial row", 2021, "tt0000005", None, None))  # только *_partial
        self.c.execute(ins, (6, "movie", "Library only", 2024, "tt0000003", None, None))  # аккаунт не знал
        w = "INSERT INTO watched (title_id, source, watched_at) VALUES (?, ?, '2026-10-23')"
        for tid, src in ((1, "plex"), (1, "manual"), (2, "plex"), (3, "plex"), (3, "manual"), (4, "plex"),
                         (5, "plex_partial"), (6, "plex"), (6, "jellyfin")):
            self.c.execute(w, (tid, src))
        self.c.execute("INSERT INTO plex_items (rating_key, library, title_id, present) VALUES ('lib-gone', 'L', 1, 1)")
        self.c.commit()
        self.known = {"m1", "e2", "m2"}   # история plex.tv: фильм m1, серия e2 сезона 1

    def sources(self, tid):
        return sorted(r[0] for r in self.c.execute("SELECT source FROM watched WHERE title_id = ?", (tid,)))

    def run_check(self, **kw):
        return V.check(self.c, px=FakePlex(), log=lambda m: None, known=self.known, **kw)

    def test_removed_mark_is_reverted(self):
        rep = self.run_check()
        self.assertEqual(sorted(r["id"] for r in rep["reverted"]), [1, 3])
        self.assertEqual(self.sources(1), ["manual_reverted_partial", "plex_reverted_partial"])
        self.assertEqual(self.sources(3), ["manual_reverted_partial", "plex_reverted_partial"])
        self.assertEqual(rep["not_seen_suggest"], ["tt0000001", "tt0000010:S1"])   # сезон — «tt…:S1»

    def test_owner_confirmed_seen_is_kept(self):
        # владелец ответил «Да, смотрел» («Что, если...?» S2, 2026-11-23), хотя в Plex 0 — отметка остаётся
        from unittest import mock
        with mock.patch.object(V, "seen_confirmed", return_value={"tt0000010:S1"}):
            rep = self.run_check()
        self.assertEqual([r["id"] for r in rep["reverted"]], [1])
        self.assertEqual([r["id"] for r in rep["owner_confirmed"]], [3])
        self.assertEqual(self.sources(3), ["manual", "plex"])

    def test_watched_and_partial_season_untouched(self):
        rep = self.run_check()
        self.assertEqual(self.sources(2), ["plex"])
        self.assertEqual(self.sources(4), ["plex"])     # одна серия из двух — частично, не трогаем
        self.assertEqual(rep["watched"], 2)

    def test_partial_rows_not_checked(self):
        rep = self.run_check()
        self.assertNotIn(5, [r["id"] for k in ("reverted", "no_account_trace", "unknown") for r in rep[k]])
        self.assertEqual(self.sources(5), ["plex_partial"])
        self.assertEqual(rep["total"], 5)

    def test_no_account_trace_is_report_only(self):
        rep = self.run_check()
        self.assertEqual([r["id"] for r in rep["no_account_trace"]], [6])
        self.assertEqual(self.sources(6), ["jellyfin", "plex"])

    def test_dry_run_changes_nothing(self):
        rep = self.run_check(dry_run=True)
        self.assertEqual(sorted(r["id"] for r in rep["reverted"]), [1, 3])
        self.assertEqual(self.sources(1), ["manual", "plex"])
        self.assertEqual(self.sources(3), ["manual", "plex"])

    def test_ids_checks_any_title_without_touching_partial_rows(self):
        rep = self.run_check(ids=[5])
        self.assertEqual([r["id"] for r in rep["reverted"]], [5])   # в Plex 0 — сказать владельцу
        self.assertEqual(self.sources(5), ["plex_partial"])         # строка *_partial не тронута

    def test_only_reads_plex(self):
        px = FakePlex()
        V.check(self.c, px=px, log=lambda m: None, known=self.known, dry_run=True)
        self.assertFalse([u for u in px.asked if "/actions/" in u or "/:/" in u])


if __name__ == "__main__":
    unittest.main()
