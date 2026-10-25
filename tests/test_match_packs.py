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
        self.assertTrue(match._is_pack("Вселенная «Заклятия» / The Conjuring Universe (2013-2025) BDRip-HEVC 1080p", 2013, 2025))
        self.assertFalse(match._is_pack("Вселенная Стивена / Steven Universe: The Movie (2019) WEB-DL", 2019, 2019))


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
            # … unless the owner's franchise built on that collection has it («Балерина» 2025, triage 2026-10-25)
            ballerina = {"tmdb_id": 541671, "imdb_id": "tt7181546", "year": 2025, "title_original": "Ballerina",
                         "collection_id": 404609, "via_franchise": True}
            with mock.patch.object(match, "_search_collections", return_value=[404609]), \
                    mock.patch.object(match, "_franchise_films", return_value=[ballerina, dict(part(2014, 1), imdb_id="tt2911666")]):
                parts, info = match._pack_parts(self.conn, 1, p, 2014, 2025)
            self.assertEqual([x["tmdb_id"] for x in parts], [1, 2, 3, 4, 541671])   # the collection's film kept once
            self.assertEqual(info["collection"], 404609)


class TestMergeRedirected(unittest.TestCase):
    def test_films_and_seasons(self):
        from films import db
        c = sqlite3.connect(":memory:")
        db.migrate(c)
        c.execute("INSERT INTO titles (id, kind, imdb_id) VALUES (1, 'movie', 'tt8272064'), (2, 'movie', 'tt15679400')")
        c.execute("INSERT INTO titles (id, kind, season_number, series_imdb_id, series_key) VALUES "
                  "(3, 'series', 1, 'tt22091076', 'imdb:tt22091076'), (4, 'series', 1, 'tt26748649', 'imdb:tt26748649'), "
                  "(5, 'series', 2, 'tt26748649', 'imdb:tt26748649')")
        c.execute("INSERT INTO releases (id, source, source_id, url, name, title_id) VALUES (10, 'rutor', '10', 'u', 'n', 2), "
                  "(11, 'rutor', '11', 'u', 'n', 4)")
        c.execute("INSERT INTO release_titles (release_id, title_id, how) VALUES (10, 2, 'imdb_page'), (11, 4, 'imdb_page')")
        c.execute("INSERT INTO user_marks (title_id, mark) VALUES (4, 'want')")
        with mock.patch.object(match, "imdb_redirects", return_value={"tt15679400": "tt8272064", "tt26748649": "tt22091076"}):
            st = match.merge_redirected_titles(c, log=lambda *_: None)
            self.assertEqual(st, {"renamed": 1, "merged": 2, "marked_duplicate": 2})
            self.assertEqual(match.merge_redirected_titles(c, log=lambda *_: None),
                             {"renamed": 0, "merged": 0, "marked_duplicate": 0})   # once
        rows = {r[0]: r[1:] for r in c.execute("SELECT id, duplicate_of, series_imdb_id, series_key FROM titles")}
        self.assertEqual(rows[2][0], 1)
        self.assertEqual(rows[4][0], 3)
        self.assertEqual(rows[5], (None, "tt22091076", "imdb:tt22091076"))   # season 2: no canonical row yet → renamed
        self.assertEqual({r[0] for r in c.execute("SELECT title_id FROM release_titles")}, {1, 3})
        self.assertEqual(c.execute("SELECT mark FROM user_marks WHERE title_id = 3").fetchone()[0], "want")


class TestYearMismatch(unittest.TestCase):
    """Enrich's 'tmdb_year_mismatch' decided by data (triage 2026-10-25)."""

    def setUp(self):
        import json as _json
        from films import db
        self.c = c = sqlite3.connect(":memory:")
        db.migrate(c)
        c.execute("INSERT INTO titles (id, kind, imdb_id, title_original, title_ru, year) VALUES "
                  "(1, 'movie', 'tt10810172', 'Vertical', 'Вертикаль', 2021), "
                  "(2, 'movie', 'tt0084695', 'The Slumber Party Massacre', 'Кровавая вечеринка', 1982)")
        rel = lambda rid, ru, orig, y: (rid, str(rid), ru, _json.dumps({"title_ru": ru, "title_original": orig, "year": y,
                                        "name_parsed": {"name": f"{ru} / {orig} ({y})", "title_ru": ru, "title_original": orig, "year": y}},
                                        ensure_ascii=False))
        for r in (rel(10, "Вертикаль", "Vertical", 2024), rel(11, "Кровавая вечеринка 2", "Slumber Party Massacre II", 1987)):
            c.execute("INSERT INTO releases (id, source, source_id, url, name, category, parsed) VALUES (?, 'rutor', ?, 'u', ?, 'foreign_films', ?)", r)
        c.execute("INSERT INTO release_titles (release_id, title_id, how) VALUES (10, 1, 'imdb_page'), (11, 2, 'imdb_page')")
        for tid, ry in ((1, 2024), (2, 1987)):
            c.execute("INSERT INTO match_issues (problem, candidates) VALUES ('tmdb_year_mismatch', ?)",
                      (_json.dumps({"imdb_id": "x", "release_years": [ry], "title_id": tid}),))
        c.commit()

    def test_right_and_relinked(self):
        ii = {"tconst": "tt0093996", "title_type": "movie", "primary_title": "Slumber Party Massacre II",
              "original_title": None, "start_year": 1987, "genres": "Horror", "runtime_min": 77}
        with mock.patch.object(match, "_resolve", return_value=("imdb", "tt0093996", None)) as res, \
                mock.patch.object(match, "_basics", side_effect=lambda conn, tt: ii if tt == "tt0093996" else None):
            st = match.recheck_year_mismatch(self.c, log=lambda *_: None)
        self.assertEqual((st["link_right"], st["relinked"], st["left"]), (1, 1, 0))
        self.assertEqual(res.call_args.args[-1], frozenset({"tt0084695"}))   # the page id excluded
        new = self.c.execute("SELECT id FROM titles WHERE imdb_id = 'tt0093996'").fetchone()[0]
        hows = dict(self.c.execute("SELECT title_id, how FROM release_titles WHERE release_id = 11").fetchall())
        self.assertTrue(hows[2].startswith("wrong_link: year mismatch"))
        self.assertEqual(hows[new], "imdb_resolve year_recheck")
        self.assertEqual(self.c.execute("SELECT count(*) FROM match_issues WHERE resolved_at IS NULL").fetchone()[0], 0)
        # «Вертикаль»: the link is kept as it was
        self.assertEqual(self.c.execute("SELECT how FROM release_titles WHERE release_id = 10").fetchone()[0], "imdb_page")


class TestPageAlternative(unittest.TestCase):
    """A page IMDb id naming another work («Аватар: Пламя и Пепел / Avatar: Fire and Ash» → «Tron: Ares»)."""
    B = {"tconst": "tt6604188", "title_type": "movie", "primary_title": "Tron: Ares", "original_title": "Tron: Ares",
         "start_year": 2025}

    def disjoint(self, names, imdb_names):
        p = {"title_original": names[0], "title_ru": names[1] if len(names) > 1 else None, "name_parsed": {}}
        with mock.patch.object(match, "_imdb_names", return_value=set(imdb_names)):
            return match._page_names_disjoint(None, "tt1", self.B, p)

    def test_disjoint(self):
        self.assertTrue(self.disjoint(["Avatar: Fire and Ash", "Аватар: Пламя и Пепел"], ["tron ares", "трон арес"]))
        self.assertFalse(self.disjoint(["RJ Decker"], ["r j decker"]))                      # punctuation
        self.assertFalse(self.disjoint(["Blade Runner (Final Cut)"], ["blade runner"]))     # an edition around the name
        self.assertFalse(self.disjoint(["James Bond: The Spy Who Loved Me"], ["the spy who loved me"]))
        self.assertFalse(self.disjoint(["Аватар"], ["tron ares"]))                          # no original name: no evidence
        self.assertFalse(self.disjoint(["Avatar: Fire and Ash"], []))                       # IMDb names unknown

    def test_alternative_needs_another_work(self):
        p = {"title_original": "Avatar: Fire and Ash", "name_parsed": {}}
        with mock.patch.object(match, "_page_names_disjoint", return_value=True):
            with mock.patch.object(match, "_resolve", return_value=("imdb", "tt1757678", None)):
                self.assertTrue(match._page_alternative(None, "tt6604188", self.B, p, "foreign_films", 2025, 2025, []))
            # a TMDB entry carrying the page's own IMDb id, or none, is the same work spelled differently
            with mock.patch.object(match, "_resolve", return_value=("tmdb", 5, None)):
                for raw in ({"imdb_id": "tt6604188"}, {}):
                    with mock.patch.object(match.tmdb, "movie_raw", return_value=raw):
                        self.assertFalse(match._page_alternative(None, "tt6604188", self.B, p, "foreign_films", 2025, 2025, []))
                with mock.patch.object(match.tmdb, "movie_raw", return_value={"imdb_id": "tt1757678"}):
                    self.assertTrue(match._page_alternative(None, "tt6604188", self.B, p, "foreign_films", 2025, 2025, []))
            with mock.patch.object(match, "_resolve", return_value=(None, "no_imdb_not_found", [])):
                self.assertFalse(match._page_alternative(None, "tt6604188", self.B, p, "foreign_films", 2025, 2025, []))


class TestNamesAndKinds(unittest.TestCase):
    def test_spaced_colon_names(self):
        p = {"title_original": "Shrek 4-D : Shrek. The Ghost of Lord Farquaad",
             "name_parsed": {"title_ru": "Шрэк 4-D : Шрек. Призрак Лорда Фаркуада"}}
        names = match._title_names(p)
        self.assertEqual(names[:2], ["Shrek 4-D : Shrek. The Ghost of Lord Farquaad", "Шрэк 4-D : Шрек. Призрак Лорда Фаркуада"])
        self.assertIn("Shrek. The Ghost of Lord Farquaad", names)
        self.assertIn("Shrek 4-D", names)
        self.assertEqual(match._title_names({"title_original": "Dune: Part Two", "name_parsed": {}}), ["Dune: Part Two"])

    def test_imdb_series_pick(self):
        res = {"status": "matched", "match": {"tconst": "tt0069637", "start_year": 1973, "score": 1.0}}
        basics = {"tconst": "tt0069637", "title_type": "tvSeries"}
        p = {"name_parsed": {"season_raw": "01-22 (22)"}}
        names = ["Star Trek: The Animated Series", "Звездный путь: Анимационные серии"]
        with mock.patch.object(match.imdb, "resolve", return_value=res), \
                mock.patch.object(match, "_basics", return_value=basics):
            self.assertEqual(match._imdb_series_pick(None, names, p, "animation", "movie", 1973, []), "tt0069637")
            self.assertIsNone(match._imdb_series_pick(None, names, p, "foreign_films", "movie", 1973, []))   # cartoons only
            self.assertIsNone(match._imdb_series_pick(None, names, p, "animation", "movie", 1974, []))      # began that year
            # no episode range nor «Animated Series» in the name: a film stays a film
            self.assertIsNone(match._imdb_series_pick(None, ["Король Лев"], {"name_parsed": {}}, "animation", "movie", 1973, []))
            # the words alone are enough («Spider-Man: The Animated Series (1994)»)
            self.assertEqual(match._imdb_series_pick(None, names, {"name_parsed": {}}, "animation", "movie", 1973, []), "tt0069637")
        with mock.patch.object(match.imdb, "resolve", return_value={"status": "ambiguous", "match": None}):
            self.assertIsNone(match._imdb_series_pick(None, names, p, "animation", "movie", 1973, []))


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
