"""Tests for the multi-film pack logic of films/match.py (offline: pure helpers, an in-memory release page).

A pack release («Омен: Коллекция / The Omen: Collection (1976-2024)») is linked to every film it holds — the page's own
list of IMDb ids, else a TMDB collection that fits the year span and the promised count.
Run:  python -X utf8 -m unittest tests.test_match_packs
"""
import sqlite3
import unittest
import zlib
from unittest import mock

from films import match


def part(year, tmdb_id=None):
    return {"year": year, "tmdb_id": tmdb_id or year}


class TestIsPack(unittest.TestCase):
    def test_pack_words(self):
        self.assertTrue(match._is_pack("Омен: Коллекция / The Omen: Collection (1976-2024) BDRip-HEVC 1080p", 1976, 2024))
        self.assertTrue(match._is_pack("Блэйд: Трилогия / Blade: Trilogy (1998-2004) BDRip 1080p", 1998, 2004))
        self.assertTrue(match._is_pack("Монстры на каникулах: Квадрология / Hotel Transylvania: Quadrilogy (2012 - 2022)",
                                       2012, 2022))
        self.assertTrue(match._is_pack("Горец: Пенталогия / Highlander: Pentalogy (1986-2007) HDRip-AVC", 1986, 2007))

    def test_edition_tag_after_year_is_not_a_pack(self):
        self.assertFalse(match._is_pack("Старикам тут не место / No Country for Old Men (2007) BDRemux 1080p | D, P, P2, A"
                                        " | Criterion Collection", 2007, 2007))

    def test_season_pack_is_a_series(self):
        name = "Аватар: Легенда об Аанге. Трилогия / Avatar: The Last Airbender. Trilogy [S01-03 из 03] (2005-2008) DVDRip"
        self.assertFalse(match._is_pack(name, 2005, 2008, [1, 2, 3]))

    def test_saga_needs_a_span(self):
        self.assertFalse(match._is_pack("Horizon: An American Saga - Chapter 1 (2024) WEB-DL", 2024, 2024))
        self.assertTrue(match._is_pack("Сумерки. Сага / The Twilight Saga (2008-2012) BDRip", 2008, 2012))


class TestSeasons(unittest.TestCase):
    def test_unread_season_blocks(self):
        self.assertEqual(match._seasons({"seasons": [], "season_raw": "S01-03 из 03"}), [1, 2, 3])
        self.assertEqual(match._seasons({"seasons": [], "season_raw": "S01, 03 из 03"}), [1, 3])
        self.assertEqual(match._seasons({"seasons": [], "season_raw": "S01-06, 10"}), [1, 2, 3, 4, 5, 6, 10])
        self.assertEqual(match._seasons({"seasons": [], "season_raw": "S14x01-10 из 18"}), [14])
        self.assertEqual(match._seasons({"seasons": [], "season_raw": "s02x01-19 из ?"}), [2])
        self.assertEqual(match._seasons({"seasons": [], "season_raw": "01-12 из 12"}), [])
        self.assertEqual(match._seasons({"seasons": [4], "season_raw": "S04"}), [4])


class TestPackNames(unittest.TestCase):
    def test_expected_count(self):
        self.assertEqual(match._pack_expected("Дюна: Дилогия / Dune: Dilogy (2021-2024) HDRip-AVC"), 2)
        self.assertEqual(match._pack_expected("Голодные игры: Квадрология / The Hunger Games: Quadrilogy (2012-2015)"), 4)
        self.assertEqual(match._pack_expected("Семейка Аддамс: Коллекция / The Addams Family 2 Movie Collection (1991-1993)"), 2)
        self.assertIsNone(match._pack_expected("Чужой: Коллекция / Alien: Collection (1979-2024) BDRip"))

    def test_base_names(self):
        p = {"title_original": "The Omen: Collection", "title_ru": "Омен: Коллекция", "name_parsed": {}}
        self.assertEqual(match._pack_base_names(p), ["The Omen", "Омен"])
        p = {"title_original": "Hotel Transylvania: Quadrilogy", "title_ru": "Монстры на каникулах: Квадрология",
             "name_parsed": {}}
        self.assertEqual(match._pack_base_names(p), ["Hotel Transylvania", "Монстры на каникулах"])
        p = {"title_original": "Sherlock Holmes: The Complete Collection", "title_ru": "Шерлок Холмс: Полная коллекция",
             "name_parsed": {}}
        self.assertEqual(match._pack_base_names(p), ["Sherlock Holmes", "Шерлок Холмс"])


class TestFitParts(unittest.TestCase):
    def test_span_and_count(self):
        dune = [part(2021), part(2024), part(2026)]                       # «Дюна: Часть третья» is not in the 2021-2024 pack
        self.assertEqual([x["year"] for x in match._fit_parts(dune, 2021, 2024, 2)], [2021, 2024])
        self.assertIsNone(match._fit_parts(dune, 2021, 2024, 3))           # promised three, the span holds two

    def test_span_ends_must_be_there(self):
        wick = [part(2014), part(2017), part(2019), part(2023)]          # «Балерина» 2025 is missing
        self.assertIsNone(match._fit_parts(wick, 2014, 2025, None))
        self.assertIsNotNone(match._fit_parts([part(1980), part(2024)], 1979, 2024, None))   # ±1 at the ends
        self.assertIsNone(match._fit_parts([part(1979), part(2025)], 1979, 2024, None))      # outside the span…
        self.assertIsNotNone(match._fit_parts([part(1979), part(2025)], 1979, 2024, None, slack=1))   # …unless widened

    def test_exact_tier(self):
        red = [part(2010), part(2013)]
        other_red = [part(2010), part(2012)]
        self.assertIsNotNone(match._fit_parts(red, 2010, 2013, 2, tol=0))
        self.assertIsNone(match._fit_parts(other_red, 2010, 2013, 2, tol=0))
        self.assertIsNotNone(match._fit_parts(other_red, 2010, 2013, 2, tol=1))

    def test_one_film_is_no_pack(self):
        self.assertIsNone(match._fit_parts([part(2011)], 2011, 2011, None))
        self.assertIsNone(match._fit_parts([part(2011), part(2011)], None, None, None))


PAGE = """<html><h1>Блэйд: Трилогия / Blade: Trilogy (1998-2004) BDRip 1080p</h1>
<table id="details"><tr><td class="header">Описание</td><td>
<a href="https://www.imdb.com/title/tt0120611/">Блэйд</a> <a href="http://imdb.com/title/tt0187738">Блэйд 2</a>
IMDb: tt0359013 и сериал <a href="https://www.imdb.com/ru/title/tt0823333/">Blade: The Series</a>
</td></tr></table><div id="related">tt9999999</div></html>"""


class TestPackParts(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("CREATE TABLE release_raw (release_id INTEGER PRIMARY KEY, fetched_at TEXT, body BLOB)")
        self.conn.execute("INSERT INTO release_raw VALUES (1, '', ?)", (zlib.compress(PAGE.encode()),))
        match._REDIRECTS = {}

    def test_page_ids_then_films_of_the_span(self):
        split = {"h1": None, "btih": None, "meta": {}, "desc_html": PAGE.split('id="related"')[0]}
        with mock.patch("films.parse.rutor._split_details", return_value=split):
            self.assertEqual(match.page_imdb_ids(self.conn, 1), ["tt0120611", "tt0187738", "tt0823333", "tt0359013"])
            finds = {"tt0120611": {"movie": 36647, "movie_year": 1998, "movie_title": "Blade"},
                     "tt0187738": {"movie": 36586, "movie_year": 2002, "movie_title": "Blade II"},
                     "tt0359013": {"movie": 36648, "movie_year": 2004, "movie_title": "Blade: Trinity"},
                     "tt0823333": {"tv": 4619, "tv_title": "Blade: The Series"}}
            with mock.patch.object(match.tmdb, "find_by_imdb", side_effect=lambda conn, tt: finds[tt]):
                p = {"imdb_id": "tt0120611", "name_parsed": {"name": "Блэйд: Трилогия / Blade: Trilogy (1998-2004) BDRip"}}
                parts, info = match._pack_parts(self.conn, 1, p, 1998, 2004)
        self.assertEqual(info["via"], "page_imdb_ids")
        self.assertEqual([x["imdb_id"] for x in parts], ["tt0120611", "tt0187738", "tt0359013"])
        self.assertEqual(info["page_skipped"][0]["tv"], 4619)

    def test_collection_when_the_page_has_no_list(self):
        self.conn.execute("DELETE FROM release_raw")   # in-memory test table only
        cols = {404609: [part(2014, 1), part(2017, 2), part(2019, 3), part(2023, 4)],
                163902: [part(2010, 39514), part(2013, 146216)], 181443: [part(2010, 54340), part(2012, 158358)]}
        with mock.patch.object(match, "_collection_parts", side_effect=lambda conn, cid: cols[cid]), \
                mock.patch.object(match, "_search_collections", return_value=[181443, 163902]):
            p = {"imdb_id": None, "title_original": "Red: Dilogy", "name_parsed": {"name": "РЭД: Дилогия / Red: Dilogy (2010-2013)"}}
            parts, info = match._pack_parts(self.conn, 1, p, 2010, 2013)
            self.assertEqual((info["via"], info["collection"]), ("tmdb_collection_search", 163902))
            self.assertEqual([x["tmdb_id"] for x in parts], [39514, 146216])
            with mock.patch.object(match, "_search_collections", return_value=[404609]):
                p = {"imdb_id": None, "title_original": "John Wick: Collection",
                     "name_parsed": {"name": "Джон Уик: Коллекция / John Wick: Collection (2014-2025)"}}
                parts, info = match._pack_parts(self.conn, 1, p, 2014, 2025)
            self.assertIsNone(parts)                       # the 2025 film is not in the collection — left, not guessed
            self.assertEqual(info["reason"], "no TMDB collection fits the year span / count")


class TestLastResortPicks(unittest.TestCase):
    """The fallback rules for releases the ordinary IMDb / TMDB picks leave undecided."""

    def test_agree(self):
        res = {"candidates": [{"tconst": "tt43672139", "score": 0.7516, "start_year": 2026}]}
        tr = {"candidates": [{"tmdb_id": 1617484, "name_fit": 1.0, "score": 0.75, "year": 2026, "year_diff": 1,
                              "imdb_id": "tt43672139"}]}
        self.assertEqual(match._agree_pick(res, tr, "movie", 2025, 2025, []), "tt43672139")
        tr["candidates"][0]["imdb_id"] = "tt1"                       # the sources disagree
        self.assertIsNone(match._agree_pick(res, tr, "movie", 2025, 2025, []))

    def test_agree_later_season_under_show_year(self):
        res = {"candidates": [{"tconst": "tt0398417", "score": 1.0, "start_year": 2004}]}
        tr = {"candidates": [{"tmdb_id": 2022, "name_fit": 1.0, "score": 1.0, "year": 2004, "imdb_id": "tt0398417"},
                             {"tmdb_id": 2098, "name_fit": 1.0, "score": 0.0, "year": 1992, "imdb_id": "tt0103359"}]}
        self.assertEqual(match._agree_pick(res, tr, "series", 2004, 2004, [5]), "tt0398417")

    def test_dub_year(self):
        c = {"tmdb_id": 283886, "name_fit": 1.0, "year": 2023, "verified": True, "hint_notes": ["country matches"],
             "titles_matched": ["The Roofus", "Руфусы. Хранители добрых дел"]}
        names = ["The Roofus", "Руфусы. Хранители добрых дел"]
        self.assertEqual(match._dub_year_pick({"candidates": [c]}, names, "series", 2025, [1]), 283886)
        self.assertIsNone(match._dub_year_pick({"candidates": [c]}, names, "series", 2025, [2]))      # season 1 only
        partial = dict(c, titles_matched=["The Roofus"])            # the Russian name is not TMDB's: another show
        self.assertIsNone(match._dub_year_pick({"candidates": [partial]}, names, "series", 2025, [1]))
        self.assertIsNone(match._dub_year_pick({"candidates": [c, dict(c, tmdb_id=1)]}, names, "series", 2025, [1]))

    def test_director(self):
        mk = lambda i, notes, v=True: {"tmdb_id": i, "name_fit": 1.0, "year_diff": 0, "verified": v, "hint_notes": notes}
        tr = {"candidates": [mk(30840, ["director matches"]), mk(417160, ["director differs"])]}
        self.assertEqual(match._director_pick(tr, "movie", 1991), 30840)
        tr["candidates"].append(mk(5, [], v=False))                 # an exact namesake not checked in full
        self.assertIsNone(match._director_pick(tr, "movie", 1991))


if __name__ == "__main__":
    unittest.main()
