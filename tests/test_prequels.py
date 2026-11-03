"""R180 (owner, 2026-10-25): unwatched earlier parts of the sequels shown on the sheet — not hidden by their year
(films.prequels.year_exempt, used by rank.compute) and kept next to the sequel (films.sheet._group_keys / _grouped).

Run:  python -X utf8 -m unittest tests/test_prequels.py
"""
import sqlite3
import unittest
from unittest import mock

from films import prequels, sheet


def title(id, year, cid=None, date=None, kind="movie", imdb=None, runtime=100):
    return {"id": id, "kind": kind, "year": year, "release_date": date or f"{year}-06-01", "collection_id": cid,
            "imdb_id": imdb or f"tt{id:07d}", "series_imdb_id": None, "series_tmdb_id": None, "series_key": None,
            "season_number": None, "runtime_min": runtime}


def row(t, hidden=(), vi=0, **kw):
    return {"title": t, "variant_index": vi, "hidden_reasons": list(hidden), **kw}


TODAY = "2026-10-25"


class YearExemptTest(unittest.TestCase):
    def exempt(self, rows, ignored=(), from_year=None):
        return prequels.year_exempt(rows, set(ignored), from_year or {}, TODAY)

    def test_earlier_part_of_shown_sequel(self):
        rows = [row(title(1, 2025, cid=10)), row(title(2, 1999, cid=10), ["год 1999"]),
                row(title(3, 2010, cid=10), ["год 2010"])]
        self.assertEqual(self.exempt(rows), {2, 3})

    def test_other_reasons_keep_it_hidden(self):
        rows = [row(title(1, 2025, cid=10)), row(title(2, 1999, cid=10), ["год 1999", "нет озвучки"]),
                row(title(3, 2001, cid=10), ["видел", "год 2001"]), row(title(4, 2002, cid=10), ["нет озвучки"])]
        ex = self.exempt(rows)
        self.assertEqual(ex, {2, 3})   # only the year reason is lifted (rank keeps «нет озвучки» / «видел»)
        # what rank.compute does with the set
        for r in rows:
            if r["title"]["id"] in ex:
                r["hidden_reasons"][:] = [h for h in r["hidden_reasons"] if not h.startswith("год ")]
        self.assertEqual([r["hidden_reasons"] for r in rows[1:]], [["нет озвучки"], ["видел"], ["нет озвучки"]])

    def test_sequel_must_be_shown(self):
        rows = [row(title(1, 2025, cid=10), ["IMDb 4.1"]), row(title(2, 1999, cid=10), ["год 1999"])]
        self.assertEqual(self.exempt(rows), set())
        rows = [row(title(1, 2025, cid=10), quality_watch=True), row(title(2, 1999, cid=10), ["год 1999"])]
        self.assertEqual(self.exempt(rows), set())   # «Обновить качество» is not «Фильмы» / «Мультфильмы»

    def test_only_earlier_parts_of_the_same_collection(self):
        rows = [row(title(1, 2025, cid=10, date="2025-03-01")), row(title(2, 2025, cid=10, date="2025-09-01"), ["год 2025"]),
                row(title(3, 1999, cid=11), ["год 1999"]), row(title(4, 1999), ["год 1999"]),
                row(title(5, 2027, cid=10, date="2027-01-01"), ["год 2027"])]
        self.assertEqual(self.exempt(rows), set())   # a later part, another collection, no collection, not out yet

    def test_owner_franchise_rules(self):
        rows = [row(title(1, 2025, cid=10)), row(title(2, 1968, cid=10), ["год 1968"]),
                row(title(3, 2011, cid=10), ["год 2011"])]
        self.assertEqual(self.exempt(rows, from_year={10: 2011}), {3})   # «с 2011 года, классику не собирать»
        self.assertEqual(self.exempt(rows, ignored={10}), set())          # franchise set to «none»

    def test_shorts_and_series_are_not_story_parts(self):
        rows = [row(title(1, 2025, cid=10)), row(title(2, 2015, cid=10, runtime=22), ["год 2015"]),
                row(title(3, 2016, cid=10, kind="series"), ["год 2016"])]
        self.assertEqual(self.exempt(rows), set())

    def test_variant_rows_ignored(self):
        rows = [row(title(1, 2025, cid=10)), row(title(1, 2025, cid=10), vi=1),
                row(title(2, 1999, cid=10), ["год 1999"]), row(title(2, 1999, cid=10), ["год 1999"], vi=1)]
        self.assertEqual(self.exempt(rows), {2})


class GroupingTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("CREATE TABLE franchise_members (franchise_key TEXT, imdb_id TEXT, confidence TEXT)")
        # the sequel (id 1) is a member of the followed franchise «saga», its earlier parts (2, 3) are not
        self.conn.executemany("INSERT INTO franchise_members VALUES (?, ?, 'confirmed')",
                              [("saga", "tt0000001"), ("saga", "tt0000009"), ("big", "tt0000001"), ("big", "tt0000008"),
                               ("big", "tt0000007")])
        cfg = [{"key": "saga", "scope": "all"}, {"key": "big", "scope": "all"}]
        self.p = mock.patch.object(sheet.rank, "franchise_cfg", return_value=cfg)
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_collection_joins_the_franchise_group(self):
        rows = [row(title(5, 2025)), row(title(1, 2025, cid=10)), row(title(6, 2024)),
                row(title(3, 2010, cid=10)), row(title(2, 1999, cid=10)), row(title(9, 2019))]
        g = sheet._group_keys(self.conn, rows)
        self.assertEqual({g[1], g[2], g[3], g[9]}, {"fr:saga"})   # the smaller franchise of the sequel, whole collection
        order = [r["title"]["id"] for r in sheet._grouped(rows, g)]
        # the group sits at its top row's place (after 5), oldest first: earlier parts right before the sequel
        self.assertEqual(order, [5, 2, 3, 9, 1, 6])

    def test_collection_without_franchise(self):
        rows = [row(title(5, 2025)), row(title(21, 2025, cid=20)), row(title(22, 2001, cid=20)), row(title(6, 2024))]
        g = sheet._group_keys(self.conn, rows)
        self.assertEqual(g[21], g[22])
        self.assertEqual([r["title"]["id"] for r in sheet._grouped(rows, g)], [5, 22, 21, 6])

    def test_same_year_parts_by_release_date(self):
        # «Кризис на бесконечных землях»: parts 1–3 in one year, the sequel ranks higher and has a smaller id
        rows = [row(title(31, 2024, cid=30, date="2024-11-12")), row(title(33, 2024, cid=30, date="2024-01-09")),
                row(title(32, 2024, cid=30, date="2024-04-23"))]
        self.assertEqual([r["title"]["id"] for r in sheet._grouped(rows, sheet._group_keys(self.conn, rows))], [33, 32, 31])

    def test_variant_rows_stay_after_their_title(self):
        rows = [row(title(1, 2025, cid=10)), row(title(1, 2025, cid=10), vi=1), row(title(2, 1999, cid=10)),
                row(title(2, 1999, cid=10), vi=1)]
        out = [(r["title"]["id"], r["variant_index"]) for r in sheet._grouped(rows, sheet._group_keys(self.conn, rows))]
        self.assertEqual(out, [(2, 0), (2, 1), (1, 0), (1, 1)])


class RutorNameMatchTest(unittest.TestCase):
    """films.encountered._match: the rutor name of an earlier part may add «Part 1» to the TMDB name."""

    def test_close_names(self):
        from films.encountered import _match, _norm
        keys = {_norm("Fear Street: 1994"), _norm("Чудаки 2")}
        self.assertTrue(_match("Улица страха. Часть 1: 1994 / Fear Street Part 1: 1994 (2021) WEB-DL", keys, {2021}))
        self.assertFalse(_match("Клетка для чудаков 2 / La Cage Aux Folles 2 (1980) DVDRip", keys, {1980}))
        self.assertFalse(_match("Fear Street Part 1: 1994 (2021)", keys, {2006}))           # the year still decides
        self.assertFalse(_match("Fear Street: The Long Way Back Home 1994 (2021)", keys, {2021}))   # > 2 extra words


if __name__ == "__main__":
    unittest.main()
