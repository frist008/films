"""Tests for films/sources/tmdb.py.

Offline tests cover the pure helpers (name matching, trailer ranking, season-trailer assignment, cross-script
director matching).  TestNetwork uses the real API through the tmdb_raw cache (data/films.db); it is
skipped when TMDB_API_KEY is not configured.  Expected values were checked by hand on themoviedb.org.
Run:  python -X utf8 -m unittest tests.test_tmdb
"""
import unittest

from films.sources import tmdb


def vid(name, type_="Trailer", lang="ru", official=False, published="2025-01-01T00:00:00.000Z", key=None):
    return {"site": "YouTube", "key": key or name, "name": name, "type": type_, "iso_639_1": lang,
            "iso_3166_1": "RU", "official": official, "published_at": published, "size": 1080}


class TestHelpers(unittest.TestCase):
    def test_is_latin(self):
        self.assertTrue(tmdb.is_latin("Amélie: Ça va? 2"))
        self.assertFalse(tmdb.is_latin("イクサガミ"))
        self.assertFalse(tmdb.is_latin("Кадет"))

    def test_name_fit(self):
        self.assertEqual(tmdb.name_fit("Dune: Part Two", ["Dune: Part Two"])[:2], (1.0, "exact"))
        self.assertEqual(tmdb.name_fit("The Gentlemen", ["Gentlemen"])[:2], (0.9, "no_article"))
        self.assertEqual(tmdb.name_fit("Дюна", ["Дюна: Часть вторая"])[:2], (0.75, "before_subtitle"))
        self.assertEqual(tmdb.name_fit("Дюна", ["Нюрнберг"])[0], 0.0)

    def test_mentions_season(self):
        self.assertTrue(tmdb._mentions_season("Король Талсы (2 сезон) — Русский трейлер (2024)", 2))
        self.assertFalse(tmdb._mentions_season("Король Талсы (2 сезон) — Русский трейлер (2024)", 3))
        self.assertTrue(tmdb._mentions_season("БРИДЖЕРТОНЫ 1-сезон   Русский трейлер 2", 1))
        self.assertTrue(tmdb._mentions_season("ДЖЕНТЛЬМЕНЫ Сезон 1 - Русский трейлер 2", 1))
        self.assertTrue(tmdb._mentions_season("The Bear Season 3 | Official Trailer", 3))
        self.assertIsNone(tmdb._mentions_season("Основание — Русский трейлер (2021)", 1))

    def test_person_match_cross_script(self):
        self.assertTrue(tmdb._person_match(["Джеймс Мэдиган"], [{"name": "James Madigan"}]))
        self.assertTrue(tmdb._person_match(["Джереми Раш"], [{"name": "Jeremy Rush"}]))
        self.assertTrue(tmdb._person_match(["Амадеус Коцан"], [{"name": "Amadeusz Kocan"}]))
        self.assertTrue(tmdb._person_match(["Кристиан Лат"], [{"name": "Christian Paolo Lat"}]))
        self.assertTrue(tmdb._person_match(["Дени Вильнёв"], [{"name": "Denis Villeneuve", "name_ru": "Дени Вильнёв"}]))
        self.assertFalse(tmdb._person_match(["Бен Форчун"], [{"name": "Ben Mole"}]))
        self.assertIsNone(tmdb._person_match(["Данте Лам"], [{"name": "林超賢"}]))  # not comparable

    def test_pname(self):
        self.assertEqual(tmdb._pname({"name": "Дени Вильнёв", "original_name": "Denis Villeneuve"}),
                         {"name": "Denis Villeneuve", "name_ru": "Дени Вильнёв"})
        self.assertEqual(tmdb._pname({"name": "David Nutter", "original_name": "David Nutter"}),
                         {"name": "David Nutter", "name_ru": None})

    def test_pick_trailers_order(self):
        vids = {"results": [
            vid("Тизер", "Teaser", official=True, published="2025-05-01T00:00:00.000Z"),
            vid("Трейлер [Субтитры]", official=True, published="2025-04-01T00:00:00.000Z"),
            vid("Трейлер (дубляж)", published="2025-02-01T00:00:00.000Z"),
            vid("Финальный трейлер (дубляж)", published="2025-03-01T00:00:00.000Z"),
            vid("Отрывок из фильма", published="2025-06-01T00:00:00.000Z"),
            vid("Official Trailer", lang="en", official=True),
            vid("Офіційний трейлер", lang="uk", official=True),
            vid("Behind the scenes", "Featurette"),
        ]}
        t = tmdb.pick_trailers(vids)
        self.assertEqual(t["trailer_ru"]["name"], "Финальный трейлер (дубляж)")   # Trailer, not subbed, newest
        self.assertEqual([v["name"] for v in t["trailers_ru"]],
                         ["Финальный трейлер (дубляж)", "Трейлер (дубляж)", "Трейлер [Субтитры]", "Тизер",
                          "Отрывок из фильма"])
        self.assertTrue(t["trailers_ru"][4]["not_trailer"])
        only_clip = tmdb.pick_trailers({"results": [vid("Игра престолов 1 сезон 1 серия короткая версия")]})
        self.assertIsNone(only_clip["trailer_ru"])
        self.assertTrue(t["trailers_ru"][2]["subtitled"])
        self.assertEqual(len(t["trailers_uk"]), 1)
        self.assertEqual(t["trailer_ru_url"], "https://www.youtube.com/watch?v=Финальный трейлер (дубляж)")
        self.assertEqual([v["name"] for v in t["videos_ru_other"]], ["Behind the scenes"])

    def test_season_trailer_assignment(self):
        series = {"results": [
            vid("Сериал — Русский трейлер (1 сезон)", published="2023-01-07T00:00:00.000Z"),
            vid("Сериал — Русский трейлер", published="2024-06-20T00:00:00.000Z"),
            vid("Сериал — старый трейлер", published="2022-12-01T00:00:00.000Z"),
        ]}
        # season 2 premiered 2024-07-17 (season 1: 2023-02-01, season 3: 2026-07-28)
        t = tmdb._season_trailer({"results": []}, series, 2, "2024-07-17", "2023-02-01", "2026-07-28")
        self.assertEqual(t["trailer_ru"]["name"], "Сериал — Русский трейлер")
        self.assertEqual(t["trailer_ru_source"], "series_videos:published_near_season_premiere")
        t = tmdb._season_trailer({"results": []}, series, 1, "2023-02-01", None, "2024-07-17")
        self.assertEqual(t["trailer_ru"]["name"], "Сериал — Русский трейлер (1 сезон)")
        self.assertEqual(t["trailer_ru_source"], "series_videos:name_mentions_season")
        t = tmdb._season_trailer({"results": []}, series, 3, "2026-07-28", "2024-07-17", None)
        self.assertIsNone(t["trailer_ru"])
        self.assertEqual(len(t["series_trailers_ru_unassigned"]), 3)
        own = {"results": [vid("Сезон 3 трейлер", published="2026-07-01T00:00:00.000Z")]}
        t = tmdb._season_trailer(own, series, 3, "2026-07-28", "2024-07-17", None)
        self.assertEqual(t["trailer_ru_source"], "season_videos")


@unittest.skipUnless(tmdb._key_cache or __import__("os").environ.get("TMDB_API_KEY")
                     or (tmdb.db.ROOT / ".env").exists(), "no TMDB_API_KEY")
class TestNetwork(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = tmdb.db.connect()

    def test_find(self):
        f = tmdb.find_by_imdb(self.conn, "tt15239678")
        self.assertEqual(f["movie"], 693134)
        self.assertIsNone(f["tv"])
        f = tmdb.find_by_imdb(self.conn, "tt0944947")
        self.assertEqual(f["tv"], 1399)

    def test_movie(self):
        m = tmdb.movie(self.conn, 693134)
        self.assertEqual(m["title_ru"], "Дюна: Часть вторая")
        self.assertEqual(m["title_uk"], "Дюна: Частина друга")
        self.assertEqual(m["imdb_id"], "tt15239678")
        self.assertEqual(m["year"], 2024)
        self.assertEqual(m["collection"]["id"], 726871)
        self.assertEqual(m["main_genre"], "фантастика")
        self.assertIn("США", m["countries"])
        self.assertEqual(m["budget_usd"], 190000000)
        self.assertEqual([d["name"] for d in m["directors"]], ["Denis Villeneuve"])
        self.assertIn("Novel", sum((w["jobs"] for w in m["writers"]), []))
        self.assertEqual(m["cast"][0]["name"], "Timothée Chalamet")
        self.assertTrue(m["trailer_ru_url"].startswith("https://www.youtube.com/watch?v="))
        row = tmdb.movie_row(m)
        self.assertEqual(row["tmdb_type"], "movie")

    def test_non_latin_original(self):
        m = tmdb.movie(self.conn, 1280330)          # 蛟龙行动 (Operation Leviathan / «Операция «Красное море» 2»)
        self.assertFalse(tmdb.is_latin(m["title_original"]))
        self.assertTrue(m["title_latin"] and tmdb.is_latin(m["title_latin"]))

    def test_season(self):
        s = tmdb.tv(self.conn, 1399)
        self.assertEqual(s["imdb_id"], "tt0944947")
        self.assertEqual(s["series_status"], "ended")
        se = tmdb.season(self.conn, 1399, 2, series=s)
        self.assertEqual((se["year"], se["episode_count"]), (2012, 10))
        self.assertTrue(se["cast"])
        row = tmdb.season_row(s, se)
        self.assertEqual((row["season_number"], row["series_key"]), (2, "imdb:tt0944947"))
        self.assertIsNone(tmdb.season(self.conn, 1399, 42))

    def test_collection(self):
        c = tmdb.collection(self.conn, 726871)
        self.assertEqual([p["tmdb_id"] for p in c["parts"]][:2], [438631, 693134])

    def test_search(self):
        r = tmdb.search(self.conn, ["Дюна: Часть вторая"], 2024, "movie")
        self.assertEqual((r["status"], r["best"]["tmdb_id"]), ("matched", 693134))
        r = tmdb.search(self.conn, ["Kaimynai", "Соседи"], 2024, "movie")       # two «Соседи» in 2024
        self.assertEqual((r["status"], r["best"]["tmdb_id"]), ("matched", 1352757))
        r = tmdb.search(self.conn, ["Соседи"], 2024, "movie")                   # without the original
        self.assertEqual(r["status"], "ambiguous")
        r = tmdb.search(self.conn, ["Гра престолів"], 2012, "tv")                # Ukrainian title, season year
        self.assertEqual((r["status"], r["best"]["tmdb_id"]), ("matched", 1399))


if __name__ == "__main__":
    unittest.main()
