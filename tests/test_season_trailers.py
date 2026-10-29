"""films.season_trailers: a season ≥ 2 must not carry another season's trailer (PLAN V4, owner 2026-10-29: «проверить
трейлеры сезонов сериалов; Проверяй»; the whole-series trailer stays — «Оставить у всех сезонов»)."""
import unittest
from unittest import mock

from films import season_trailers as st
from films.sources import trailer_lang as tl
from films.sources import youtube as yt

TEHRAN = {1: "2020-06-22", 2: "2022-05-06", 3: "2025-01-10"}
TNG = {1: "1987-09-28", 2: "1988-11-21", 3: "1989-09-25"}


class VerdictTest(unittest.TestCase):
    def test_named(self):
        self.assertEqual(st.verdict(3, "STARGATE SG1 season 1 Trailer #1", None, {})[:2], (False, "title names season [1]"))
        self.assertTrue(st.verdict(2, "Тегеран — 2 сезон, русский трейлер", None, {})[0])

    def test_series_word_and_pairs(self):   # British «Series 6», Dutch «seizoen», «Season 1&2»
        self.assertEqual(yt.season_mentions("NEW Shaun The Sheep Series 6 (Official Trailer)"), {6})
        self.assertEqual(yt.season_mentions("McDonald & Dodds (seizoen 3) - TRAILER"), {3})
        self.assertEqual(yt.season_mentions("Iron Man: The Animated Series (1994) - Intro Season 1&2"), {1, 2})
        self.assertEqual(yt.season_mentions("X-Men: The Animated Series | Trailer"), set())

    def test_dates(self):
        self.assertFalse(st.verdict(2, "Tehran — Official Trailer | Apple TV", "2020-06-01T10:00:00Z", TEHRAN)[0])
        self.assertTrue(st.verdict(2, "Tehran — Official Trailer | Apple TV", "2022-04-20", TEHRAN)[0])
        self.assertFalse(st.verdict(3, "Tehran — Official Trailer", "2022-04-20", TEHRAN)[0])   # season 2's window

    def test_whole_series_stays(self):
        self.assertTrue(st.verdict(2, "Star Trek: The Next Generation Blu-ray Trailer", "2012-01-31", TNG)[0])
        self.assertTrue(st.verdict(5, "32 Seasons | The Simpsons | Disney+", "2021-09-10",
                                   {1: "1989-12-17", 32: "2020-09-27", 33: "2021-09-26"})[0])

    def test_unknown(self):
        self.assertIsNone(st.verdict(2, None, None, TEHRAN)[0])
        self.assertIsNone(st.verdict(7, "X-Men trailer", None, {1: "1992-10-31"})[0])


def vid(key, name, published, not_trailer=False):
    return {"url": f"https://www.youtube.com/watch?v={key}", "name": name, "published_at": published,
            "not_trailer": not_trailer}


class SeasonTmdbTest(unittest.TestCase):
    SERIES = {"seasons": [{"season_number": n, "air_date": a} for n, a in TEHRAN.items()],
              "trailers_uk": [], "trailer_en_url": "https://www.youtube.com/watch?v=S1",
              "trailers_en": [vid("S1", "Tehran — Official Trailer", "2020-06-01"),
                              vid("S2", "Tehran — Season 2 Official Trailer", "2022-04-20")]}

    def run_for(self, n, season_videos=None):
        with mock.patch("films.sources.tmdb.tv", return_value=self.SERIES), \
                mock.patch("films.sources.tmdb.season_raw", return_value={"videos": season_videos}):
            return tl._season_tmdb(None, 1, n)

    def test_season_2_gets_its_own(self):   # was: the season 1 trailer on every season
        self.assertEqual(self.run_for(2), (None, "https://www.youtube.com/watch?v=S2"))

    def test_season_3_none(self):
        self.assertEqual(self.run_for(3), (None, None))

    def test_season_1(self):
        self.assertEqual(self.run_for(1), (None, "https://www.youtube.com/watch?v=S1"))

    def test_season_videos_first(self):
        own = {"results": [{"key": "OWN", "site": "YouTube", "type": "Trailer", "iso_639_1": "en", "name": "Trailer",
                            "published_at": "2025-01-01"}]}
        self.assertEqual(self.run_for(3, own), (None, "https://www.youtube.com/watch?v=OWN"))


if __name__ == "__main__":
    unittest.main()
