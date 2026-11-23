"""R334 (owner, 2026-11-22): the title's link to its game — «к какой игре относится какая часть», «где надо смотреть эти
аниме. Перед игровой, после игры. Во время», «если они экранизация … надо писать экранизация слово»."""
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from films import db, game_links, sheet


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    db.migrate(c)
    c.execute("INSERT INTO titles (id, kind, title_ru, title_original, year, imdb_id, tmdb_id) "
              "VALUES (1, 'movie', 'Последняя фантазия 7: Дети пришествия', 'Final Fantasy VII: Advent Children', 2005, "
              "'tt0385700', 647)")
    c.execute("INSERT INTO titles (id, kind, title_ru, title_original, year, season_number, series_imdb_id) "
              "VALUES (2, 'series', 'Ведьмак', 'The Witcher', 2019, 1, 'tt5180504')")
    c.execute("INSERT INTO titles (id, kind, title_ru, title_original, year) VALUES (3, 'movie', 'Бордерлендс', 'Borderlands', 2024)")
    return c


ROWS = [
    {"id": 1, "relation": "addition", "game": "Final Fantasy VII", "when": "после игры",
     "text": "Сиквел Final Fantasy VII: смотреть после игры (через 2 года после финала)", "note": "…",
     "sources": ["https://en.wikipedia.org/wiki/Final_Fantasy_VII:_Advent_Children",
                 "https://finalfantasy.fandom.com/wiki/Final_Fantasy_VII:_Advent_Children",
                 "https://www.reddit.com/r/FinalFantasy/comments/abc/when_to_watch_advent_children/"]},
    {"id": 2, "relation": "other", "game": "The Witcher", "when": None,
     "text": "Экранизация книг Сапковского, не игр («Последнее желание», «Меч предназначения»)", "note": "",
     "sources": ["https://en.wikipedia.org/wiki/The_Witcher_(TV_series)", "https://witcher.fandom.com/wiki/The_Witcher_(TV_series)",
                 "https://forums.cdprojektred.com/index.php?threads/netflix-series.1/"]},
    {"id": 3, "relation": "adaptation", "game": "Borderlands", "when": None,
     "text": "Экранизация по мотивам серии Borderlands: свой сюжет, не канон игр", "note": "",
     "sources": ["https://en.wikipedia.org/wiki/Borderlands_(film)", "https://borderlands.fandom.com/wiki/Borderlands_(film)",
                 "https://steamcommunity.com/app/49520/discussions/0/1/"]},
]


class MigrationTest(unittest.TestCase):
    def test_table(self):
        c = _conn()
        self.addCleanup(c.close)
        cols = [r[1] for r in c.execute("PRAGMA table_info(title_game_links)")]
        self.assertEqual(cols, ["title_id", "relation", "game", "watch_when", "text", "note", "sources", "checked_at"])
        self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], len(db.MIGRATIONS))
        with self.assertRaises(sqlite3.IntegrityError):   # relation is one of the three
            c.execute("INSERT INTO title_game_links (title_id, relation, text) VALUES (1, 'sequel', 'x')")


class WordingTest(unittest.TestCase):
    def test_good_rows(self):
        for r in ROWS:
            self.assertEqual(game_links.text_problems(r), [], r["text"])

    def test_addition_says_when(self):
        bad = {"relation": "addition", "text": "Приквел к Final Fantasy XV, история Нокта"}
        self.assertTrue(any("когда смотреть" in p for p in game_links.text_problems(bad)))
        for ok in ("Приквел к Final Fantasy XV: смотреть до игры", "Дополнение к Dragon Age II: во время игры, до DLC",
                   "Сиквел Final Fantasy V: смотреть после игры"):
            self.assertEqual(game_links.text_problems({"relation": "addition", "text": ok}), [], ok)

    def test_adaptation_has_the_word(self):
        """«Другой взгляд» — тоже экранизация; «не экранизация игр» слова не даёт."""
        for bad in ("Другой взгляд на Final Fantasy: своя история, не по игре",
                    "Другой взгляд на мир Pokémon: своя история, не экранизация игр"):
            self.assertTrue(any("Экранизация" in p for p in game_links.text_problems({"relation": "adaptation", "text": bad})), bad)
        self.assertEqual(game_links.text_problems(
            {"relation": "adaptation", "text": "Экранизация, другой взгляд на Final Fantasy: своя история, не по игре"}), [])

    def test_length_and_language(self):
        # R349: «расширенно а не только в одном месте, включая мнение игроков и зрителей»
        one = {"sources": ["https://en.wikipedia.org/wiki/X", "https://en.wikipedia.org/wiki/Y"]}
        self.assertEqual(len(game_links.source_problems(one)), 2)
        no_fans = {"sources": ["https://en.wikipedia.org/wiki/X", "https://x.fandom.com/wiki/X", "https://www.ign.com/x"]}
        self.assertEqual(len(game_links.source_problems(no_fans)), 1)
        self.assertEqual(game_links.source_problems(ROWS[0]), [])
        long = {"relation": "other", "text": "Экранизация книг " + "очень " * 15}
        self.assertTrue(any("длиннее 90" in p for p in game_links.text_problems(long)))
        self.assertIn("не по-русски", game_links.text_problems({"relation": "other", "text": "Prequel to Final Fantasy XV"}))
        self.assertIn("не по-русски", game_links.text_problems({"relation": "other", "text": "Екранізація гри, своя історія"}))
        self.assertTrue(game_links.text_problems({"relation": "sequel", "text": "Сиквел игры: смотреть после игры"}))

    def test_other_kept_as_is(self):
        """other — связь только по названию: слова «Экранизация» и «когда» не требуются."""
        self.assertEqual(game_links.text_problems(
            {"relation": "other", "text": "Не по сюжету игры: драма о сыне и отце, играющих в Final Fantasy XIV"}), [])


class LoadTest(unittest.TestCase):
    def test_load_and_replace(self):
        c = _conn()
        self.addCleanup(c.close)
        self.assertEqual(game_links.load(c, ROWS), 3)
        row = c.execute("SELECT * FROM title_game_links WHERE title_id = 1").fetchone()
        self.assertEqual((row["relation"], row["game"], row["watch_when"]), ("addition", "Final Fantasy VII", "после игры"))
        self.assertEqual(json.loads(row["sources"]), ROWS[0]["sources"])
        self.assertTrue(row["checked_at"])
        game_links.load(c, [dict(ROWS[0], text="Сиквел Final Fantasy VII: смотреть после игры")])   # INSERT OR REPLACE
        self.assertEqual(c.execute("SELECT count(*) FROM title_game_links").fetchone()[0], 3)
        self.assertEqual(c.execute("SELECT text FROM title_game_links WHERE title_id = 1").fetchone()[0],
                         "Сиквел Final Fantasy VII: смотреть после игры")

    def test_bad_row_writes_nothing(self):
        c = _conn()
        self.addCleanup(c.close)
        bad = dict(ROWS[2], text="Другой взгляд на Borderlands: свой сюжет")
        with self.assertRaises(SystemExit) as e:
            game_links.load(c, [ROWS[0], bad])
        self.assertIn("Экранизация", str(e.exception))
        self.assertEqual(c.execute("SELECT count(*) FROM title_game_links").fetchone()[0], 0)
        with self.assertRaises(SystemExit):   # unknown title, duplicate
            game_links.load(c, [dict(ROWS[0], id=99)])
        self.assertTrue(any("повторяется" in p for p in game_links.check(c, [ROWS[0], ROWS[0]])))

    def test_read_rows_from_files(self):
        with tempfile.TemporaryDirectory() as d:
            p1, p2 = Path(d) / "a.json", Path(d) / "b.json"
            p1.write_text(json.dumps(ROWS[:2], ensure_ascii=False), encoding="utf-8")
            p2.write_text(json.dumps(ROWS[2], ensure_ascii=False), encoding="utf-8")
            self.assertEqual([r["id"] for r in game_links.read_rows([str(p1), str(p2)])], [1, 2, 3])


class SheetTest(unittest.TestCase):
    def setUp(self):
        self.c = _conn()
        self.addCleanup(self.c.close)
        game_links.load(self.c, ROWS)
        sheet._GAME_LINE.clear()
        sheet._GAME_LINE.update(game_links.sheet_lines(self.c))
        self.addCleanup(sheet._GAME_LINE.clear)

    def test_sheet_lines_one_query(self):
        lines = game_links.sheet_lines(self.c)
        self.assertEqual(lines[1], ROWS[0]["text"])
        self.assertEqual(lines["tt0385700"], ROWS[0]["text"])   # a film — by its IMDb / TMDB id too («Нет на трекерах»)
        self.assertEqual(lines["tmdb:647"], ROWS[0]["text"])
        self.assertNotIn("tt5180504", lines)                      # a season: the series id is shared by all seasons
        with closing(sqlite3.connect(":memory:")) as bare:   # no migration 26 yet → nothing
            self.assertEqual(game_links.sheet_lines(bare), {})

    def test_game_line(self):
        self.assertEqual(sheet.game_line(1), "\n" + ROWS[0]["text"])
        self.assertEqual(sheet.game_line(None, "tmdb:647"), "\n" + ROWS[0]["text"])
        self.assertEqual(sheet.game_line(42), "")

    def _row(self, tid: int) -> dict:
        t = dict(self.c.execute("SELECT * FROM titles WHERE id = ?", (tid,)).fetchone())
        return {"title": t, "release": {}, "responsible": None, "have": None, "upgrade": [], "variant_index": 0,
                "variant": "", "quality": "1080p", "factors": {}, "owned": False, "in_wishlist": False}

    def test_main_row_last_line(self):
        with mock.patch.dict(sheet._FRANCHISE_OF, {"tt0385700": "Final Fantasy"}, clear=True):
            cell = sheet._main_row(self._row(1), {}, {})[0]["userEnteredValue"]["stringValue"]
        lines = cell.split("\n")
        self.assertEqual(lines[-2:], ["Франшиза: Final Fantasy", ROWS[0]["text"]])
        self.assertEqual(lines[0], "Последняя фантазия 7: Дети пришествия")
        with mock.patch.dict(sheet._FRANCHISE_OF, {}, clear=True):   # no game link → the cell is as before
            self.c.execute("INSERT INTO titles (id, kind, title_ru, title_original, year) VALUES (4, 'movie', 'Икс', 'X', 2025)")
            cell = sheet._main_row(self._row(4), {}, {})[0]["userEnteredValue"]["stringValue"]
        self.assertEqual(cell, "Икс\nX")

    def test_quality_row(self):
        r = self._row(3)
        r.update(have={"resolution": 1080, "hdr": [], "has_ru_uk": True, "size_bytes": 2**30}, upgrade=["4K"],
                 quality_watch=True, hidden_reasons=[], release={"resolution": 2160})
        with mock.patch.object(sheet.rank, "downgrade_reasons", return_value=[]):
            rows = sheet._quality_rows([r])
        self.assertTrue(rows[0][0]["userEnteredValue"]["stringValue"].endswith("\n" + ROWS[2]["text"]))


if __name__ == "__main__":
    unittest.main()
