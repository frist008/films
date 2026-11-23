"""R340: rutor release links in the sheet go through the GitHub Pages redirect page that tries the mirrors."""
import json
import re
import unittest
from unittest import mock

from films import rutor_redirect as rr
from films import sheet

PAGE = "https://owner.github.io/films/rutor.html"


class PageTest(unittest.TestCase):
    def test_render_has_every_mirror(self):
        ms = rr.mirrors()
        self.assertIn("http://new-rutor.org", ms)
        self.assertNotIn("http://rutor.org", ms)   # a paid site of another owner, not a mirror
        html = rr.render()
        m = re.search(r"var MIRRORS = (\[.*?\]);", html)
        self.assertEqual(json.loads(m.group(1)), ms)   # same list, same order
        self.assertNotIn("__MIRRORS__", html)
        for probe in ("favicon.ico", "location.replace", "TIMEOUT = 3500", "location.hash"):
            self.assertIn(probe, html)
        self.assertNotIn("fetch(", html)   # mixed content: http mirrors from an https page — images only

    def test_page_in_docs_is_current(self):
        """docs/rutor.html = what `films.rutor_redirect build` writes from [rutor] mirrors (lists never diverge)."""
        self.assertEqual(rr.PAGE.read_text(encoding="utf-8"), rr.render())

    def test_custom_mirrors(self):
        html = rr.render(["http://a.example", "http://b.example"])
        self.assertIn('["http://a.example", "http://b.example"]', html)


class LinkTest(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(rr, "page_url", return_value=PAGE)
        p.start()
        self.addCleanup(p.stop)

    def test_rutor_goes_through_page(self):
        rel = {"source": "rutor", "source_id": "1114162", "url": "http://rutor.info/torrent/1114162"}
        self.assertEqual(rr.release_link(rel), PAGE + "#1114162")
        self.assertEqual(rr.release_link({"source": "rutor", "source_id": "77", "url": None}), PAGE + "#77")

    def test_other_trackers_direct(self):
        for src, url in (("rutracker", "https://rutracker.org/forum/viewtopic.php?t=1"),
                         ("kinozal", "https://kinozal.guru/details.php?id=2"), ("nnmclub", "https://nnmclub.to/forum/viewtopic.php?t=3"),
                         ("toloka", "https://toloka.to/t4"), ("mazepa", "https://mazepa.to/viewtopic.php?t=5"),
                         ("rustorka", "https://rustorka.com/forum/viewtopic.php?t=6")):
            self.assertEqual(rr.release_link({"source": src, "source_id": "1", "url": url}), url)
        self.assertIsNone(rr.release_link(None))

    def test_no_page_falls_back(self):
        with mock.patch.object(rr, "page_url", return_value=None):
            rel = {"source": "rutor", "source_id": "5", "url": "http://rutor.info/torrent/5"}
            self.assertEqual(rr.release_link(rel), "http://rutor.info/torrent/5")

    def test_sheet_title_cell(self):
        """The title cell of a media tab links the release: rutor → the page, kinozal → kinozal."""
        t = {"id": 1, "title_ru": "Фильм", "title_original": "Film", "title_latin": None, "year": 2025, "kind": "movie",
             "is_animation": 0, "season_number": None, "imdb_id": "tt1", "series_imdb_id": None}
        base = {"title": t, "responsible": None, "variant_index": 0, "variant": "Лучшее", "quality": "1080p",
                "have": None, "owned": False, "in_wishlist": False, "upgrade": [], "factors": {}}
        with mock.patch.object(sheet, "franchise_label", return_value=None), \
                mock.patch.object(sheet, "game_line", return_value=""), \
                mock.patch.object(sheet, "_voice_text", return_value=""):
            rutor = sheet._main_row({**base, "release": {"source": "rutor", "source_id": "42",
                                                          "url": "http://rutor.info/torrent/42"}}, {}, {})
            kz = sheet._main_row({**base, "release": {"source": "kinozal", "source_id": "9",
                                                       "url": "https://kinozal.guru/details.php?id=9"}}, {}, {})
        link = lambda c: c["textFormatRuns"][0]["format"]["link"]["uri"]
        self.assertEqual(link(rutor[0]), PAGE + "#42")
        self.assertEqual(link(kz[0]), "https://kinozal.guru/details.php?id=9")


class OwnerTest(unittest.TestCase):
    def test_owner_from_remote(self):
        rr.github_owner.cache_clear()
        self.addCleanup(rr.github_owner.cache_clear)
        for url, owner in (("git@github.com:SomeOne/films.git", "SomeOne"), ("https://github.com/some-one/films", "some-one")):
            text = f'[core]\n\tbare = false\n[remote "origin"]\n\turl = {url}\n\tfetch = +refs/heads/*\n'
            with mock.patch("pathlib.Path.read_text", return_value=text):
                rr.github_owner.cache_clear()
                self.assertEqual(rr.github_owner(), owner)
        rr.github_owner.cache_clear()


if __name__ == "__main__":
    unittest.main()
