"""plan_guard.done_mark_problem: finished work ends with the table link and 🟢, unfinished never shows 🟢 (owner,
2026-11-02: «когда всё заканчиваешь давай ссылку на таблицу всегда … и присылай зелёную точку как знак того что всё
закончено»)."""
import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("plan_guard", Path(__file__).resolve().parents[1] / ".claude" / "hooks" / "plan_guard.py")
pg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pg)

URL = "https://docs.google.com/spreadsheets/d/abc/edit"


class DoneMarkTest(unittest.TestCase):
    def test_finished_needs_link_and_dot(self):
        self.assertTrue(pg.done_mark_problem("Готово.", True, URL))
        self.assertTrue(pg.done_mark_problem(f"Готово. [Таблица]({URL})", True, URL))       # no dot
        self.assertTrue(pg.done_mark_problem("🟢 Готово.", True, URL))                        # no link
        self.assertEqual(pg.done_mark_problem(f"Готово. [Таблица]({URL}) 🟢", True, URL), "")

    def test_unfinished_has_no_dot(self):
        self.assertTrue(pg.done_mark_problem("Пуш идёт 🟢", False, URL))
        self.assertEqual(pg.done_mark_problem("Пуш идёт, жду.", False, URL), "")

    def test_no_sheet_configured(self):
        self.assertEqual(pg.done_mark_problem("Готово.", True, ""), "")

    def test_sheet_url_from_private(self):
        self.assertTrue(pg.sheet_url() == "" or pg.sheet_url().startswith("https://docs.google.com/spreadsheets/d/"))


if __name__ == "__main__":
    unittest.main()
