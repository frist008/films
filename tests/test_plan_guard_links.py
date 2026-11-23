"""plan_guard.bare_sites: сайт в ответе владельцу — только кликабельной ссылкой (владелец, 2026-11-22: «твои ссылки не
кликабельные» — таблица зеркал rutor была голым текстом)."""
import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("plan_guard", Path(__file__).resolve().parents[1] / ".claude" / "hooks" / "plan_guard.py")
pg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pg)


class BareSitesTest(unittest.TestCase):
    def test_bare_domains_caught(self):
        self.assertEqual(pg.bare_sites("| **rutor.info** | не отвечает |\n| alt.rutor.org | ок |"),
                         ["alt.rutor.org", "rutor.info"])

    def test_links_code_and_files_pass(self):
        ok = ("[rutor.info](http://rutor.info/) и [зеркало](https://rutor.org/), код `films.lookup`, файл films/db.py, "
              "база data/films.db, `rutor.is` в коде, https://new-rutor.org/ сам ссылка, почта a@example.com\n"
              "```\nСторож: rutor.info\n```")
        self.assertEqual(pg.bare_sites(ok), [])


if __name__ == "__main__":
    unittest.main()
