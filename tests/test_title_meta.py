"""Tests for the metadata survey fields (owner, 2026-10-25): TMDB extras / credits / person names, IMDb distributor /
colour / film format / locations, store dates, duplicate copies.  Offline: fixtures in tests/fixtures/tmdb and
tests/fixtures/imdb are trimmed real responses (Dune: Part Two, Game of Thrones, Denis Villeneuve).
Run:  python -X utf8 -m unittest tests.test_title_meta
"""
import json
import unittest
from pathlib import Path

from films import duplicates, title_meta
from films.sources import imdb_specs, tmdb

FIX = Path(__file__).resolve().parent / "fixtures"


def load(*parts):
    return json.loads((FIX.joinpath(*parts)).read_text(encoding="utf-8"))


class TestTmdbExtras(unittest.TestCase):
    def test_movie_extras(self):
        ex = tmdb.title_extras(load("tmdb", "movie_693134_dune2.json"), "movie")
        self.assertIn("sequel", ex["keywords"])
        self.assertEqual(ex["original_language"], "en")
        self.assertEqual(ex["spoken_languages"], ["en"])
        self.assertEqual(ex["wikidata_id"], "Q109228991")
        self.assertIsNone(ex["is_miniseries"])
        # the ru-RU body has an empty homepage; the English translation carries it
        self.assertTrue(ex["homepage"] and ex["homepage"].startswith("http"))

    def test_tv_extras(self):
        ex = tmdb.title_extras(load("tmdb", "tv_1399_got.json"), "tv", {"external_ids": {"wikidata_id": "Q1"}})
        self.assertEqual(ex["is_miniseries"], 0)
        self.assertEqual(ex["wikidata_id"], "Q1")              # the season's own article first
        self.assertEqual(ex["series_wikidata_id"], "Q23572")
        self.assertEqual(ex["homepage"], "https://www.hbo.com/game-of-thrones")
        mini = dict(load("tmdb", "tv_1399_got.json"), type="Miniseries")
        self.assertEqual(tmdb.title_extras(mini, "tv")["is_miniseries"], 1)

    def test_movie_credits_survey_roles(self):
        cr = tmdb._movie_credits(load("tmdb", "movie_693134_dune2.json")["credits"])
        self.assertEqual([p["name"] for p in cr["dops"]], ["Greig Fraser"])
        self.assertEqual([p["name"] for p in cr["composers"]], ["Hans Zimmer"])
        self.assertEqual(cr["editors"], [])                    # the trimmed fixture has no Editing department
        # editor: «только хранить» (owner, 2026-10-25) — job «Editor» only
        ed = tmdb._movie_credits({"crew": [
            {"id": 7, "name": "Joe Walker", "original_name": "Joe Walker", "department": "Editing", "job": "Editor"},
            {"id": 8, "name": "X", "original_name": "X", "department": "Editing", "job": "First Assistant Editor"}]})
        self.assertEqual([p["name"] for p in ed["editors"]], ["Joe Walker"])
        producers = {p["name"] for p in cr["producers"]}
        self.assertIn("Denis Villeneuve", producers)
        self.assertNotIn("Thomas Tull", producers)             # executive producers are not shown for films
        self.assertTrue(all("Producer" in p["jobs"] for p in cr["producers"]))

    def test_aggregate_credits_tv_producers(self):
        agg = {"cast": [], "crew": [
            {"id": 1, "name": "A", "original_name": "A", "department": "Production",
             "jobs": [{"job": "Executive Producer", "episode_count": 8}]},
            {"id": 2, "name": "B", "original_name": "B", "department": "Production",
             "jobs": [{"job": "Producer", "episode_count": 8}]},
            {"id": 3, "name": "C", "original_name": "C", "department": "Camera",
             "jobs": [{"job": "Director of Photography", "episode_count": 3}]},
            {"id": 4, "name": "D", "original_name": "D", "department": "Editing",
             "jobs": [{"job": "Editor", "episode_count": 2}]},
            {"id": 5, "name": "E", "original_name": "E", "department": "Editing",
             "jobs": [{"job": "Editor", "episode_count": 6}, {"job": "Assistant Editor", "episode_count": 1}]}]}
        cr = tmdb._aggregate_credits(agg)
        self.assertEqual([p["tmdb_id"] for p in cr["producers"]], [1])
        self.assertEqual([p["tmdb_id"] for p in cr["dops"]], [3])
        self.assertEqual([p["tmdb_id"] for p in cr["editors"]], [5, 4])     # by episodes, «Editor» job only

    def test_person_names(self):
        b = load("tmdb", "person_137427_villeneuve.json")
        self.assertEqual(tmdb.person_name_uk(b), ("Дені Вільнев", "translation:uk-UA"))
        no_tr = dict(b, translations={"translations": []}, also_known_as=["Дени Вильнев", "Дені Вільньов"])
        self.assertEqual(tmdb.person_name_uk(no_tr), ("Дені Вільньов", "also_known_as"))
        self.assertEqual(tmdb.person_name_uk(dict(no_tr, also_known_as=["Дени Вильнёв"])), (None, None))


class TestImdbSurvey(unittest.TestCase):
    def test_dune2(self):
        s = imdb_specs.summarize(load("imdb", "tt15239678_dune2.json"))
        self.assertEqual(s["color"], "Color | Black and White (Infrared)")
        # three worldwide storefronts (Amazon, Apple TV+, HBO Max) are not one rights holder -> the US theatrical one
        self.assertEqual((s["distributor"], s["distributor_country"], s["distributor_rule"]), ("Warner Bros.", "US", "us"))
        self.assertTrue(s["cameras"])
        self.assertTrue(s["filming_locations"])
        self.assertIn("negative", s["film_format"] or {})

    def test_pick_distributor_rules(self):
        c = lambda name, country, *attrs: {"name": name, "country": country, "countries": [country], "attrs": list(attrs)}  # noqa: E731
        self.assertEqual(imdb_specs.pick_distributor([c("WB", "US", "theatrical"), c("B&H", "UA", "theatrical")])[:2],
                         ("B&H", "UA"))
        self.assertEqual(imdb_specs.pick_distributor([c("Netflix", "XWW", "VOD", "video"), c("Netflix", "US", "VOD")])[:2],
                         ("Netflix", "XWW"))
        self.assertEqual(imdb_specs.pick_distributor([c("SDS", "US", "Blu-ray"), c("Max", "US", "VOD"),
                                                      c("WB", "US", "theatrical")])[:2], ("WB", "US"))
        self.assertEqual(imdb_specs.pick_distributor([c("Home", "UA", "DVD"), c("Gaumont", "FR", "theatrical")])[:2],
                         ("Gaumont", "FR"))
        self.assertEqual(imdb_specs.pick_distributor([]), (None, None, "none"))

    def test_color_bw(self):
        specs = {"colorations": {"total": 1, "items": [{"text": "Black and White", "attributes": []}]}}
        self.assertEqual(imdb_specs.color_text(specs), "Black and White")
        self.assertIsNone(imdb_specs.color_text({}))


class TestStoreDates(unittest.TestCase):
    def test_ru_date(self):
        self.assertEqual(title_meta.ru_date("23 мар.\u202f2021\u202fг."), "2021-03-23")
        self.assertEqual(title_meta.ru_date("3 мая 2023 г."), "2023-05-03")
        self.assertIsNone(title_meta.ru_date("yes"))

    def test_store_dates(self):
        d = title_meta.store_dates({"acquired": "23 мар. 2021 г."}, "youtube", None, "2026-10-23 07:17:01")
        self.assertEqual((d["purchased_at"], d["purchased_at_source"]), ("2021-03-23", "youtube_acquired"))
        d = title_meta.store_dates({"play_orders": [{"date": "3 мар. 2023 г."}, {"date": "1 янв. 2022 г."}]},
                                   "google_play", None, None)
        self.assertEqual(d["purchased_at"], "2022-01-01")
        d = title_meta.store_dates({"added": "2026-09-26"}, "google_tv", 1, "2026-10-23 07:17:01")
        self.assertEqual((d["wishlist_added_at"], d["wishlist_added_source"]), ("2026-09-26", "google_list"))
        d = title_meta.store_dates({"how": "added by gtv_watchlist_guard 2026-10-24"}, "google_tv", 1, "2026-10-24 18:56:26")
        self.assertEqual(d["wishlist_added_source"], "gtv_guard")
        d = title_meta.store_dates({}, "google_tv", 1, "2026-10-23 07:17:01")
        self.assertEqual((d["wishlist_added_at"], d["wishlist_added_source"]), ("2026-10-23", "first_seen"))
        self.assertIsNone(title_meta.store_dates({}, "google_tv", 0, "2026-10-23")["wishlist_added_at"])


class TestDuplicates(unittest.TestCase):
    def test_copy_key_multipart(self):
        a = duplicates.copy_key(r"E:\Films\Movie (1999)\Movie.1999.CD1.avi")
        b = duplicates.copy_key(r"E:\Films\Movie (1999)\Movie.1999.CD2.avi")
        self.assertEqual(a, b)
        self.assertEqual(duplicates.copy_key(r"E:\Films\A.part1.mkv"), duplicates.copy_key("e:/films/A.part2.mkv"))
        self.assertNotEqual(duplicates.copy_key(r"E:\Films\Sasha\X.mkv"), duplicates.copy_key(r"E:\Films\Stas\X.mkv"))
        self.assertEqual(duplicates.copy_key(r"E:\Films\Counterpart1.mkv"), r"e:\films\counterpart1.mkv")

    def test_edition(self):
        self.assertEqual(duplicates.edition_of(r"E:\Films\Crank.2006.Theatrical.Cut.mkv"), "Theatrical")
        self.assertEqual(duplicates.edition_of(r"E:\x\Film.Directors.Cut.mkv"), "Director's Cut")
        self.assertEqual(duplicates.edition_of("x.mkv", "Extended Edition"), "Extended Edition")
        self.assertIsNone(duplicates.edition_of(r"E:\x\Film.2020.mkv"))


if __name__ == "__main__":
    unittest.main()
