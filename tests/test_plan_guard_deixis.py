"""plan_guard.deixis_hint: «тут» + a link in the same message → a hint that «тут» is that link (owner, 2026-10-26:
«ты вопрос не читал что ли?» — «как тут зарегистрироватся … https://rustorka.com/forum/index.php» was answered about
toloka/mazepa)."""
import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("plan_guard", Path(__file__).resolve().parents[1] / ".claude" / "hooks" / "plan_guard.py")
pg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pg)


class DeixisTest(unittest.TestCase):
    def test_real_message(self):
        h = pg.deixis_hint("как тут зарегистрироватся. Потенциально ещё один источник https://rustorka.com/forum/index.php")
        self.assertIn("https://rustorka.com/forum/index.php", h)
        self.assertIn("тут", h)

    def test_no_link_no_hint(self):
        self.assertEqual(pg.deixis_hint("я ж тут зареган, в чём вопрос?"), "")

    def test_link_without_deixis(self):
        self.assertEqual(pg.deixis_hint("https://toloka.to/ ещё источник"), "")

    def test_word_inside_other_word(self):
        self.assertEqual(pg.deixis_hint("тамбур https://x.org"), "")


if __name__ == "__main__":
    unittest.main()
