"""collector_guard: один адрес — один сборщик (владелец, 2026-11-23: «Нельзя один и тот же сайт использовать одновременно
в 2 вкладках. Это разрешено только для зеркал. Это баг хука!»; зеркало — тоже один: «Звучит так будто ты если одно
зеркало сможешь её использовать параллельно 100 раз»)."""
import importlib.util
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("collector_guard", Path(__file__).resolve().parents[1] / ".claude" / "hooks" / "collector_guard.py")
cg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cg)

LAUNCH = "(0, eval)(await (await fetch('http://127.0.0.1:8765/queries/rustorka_code', {cache: 'no-store'})).json())"
NET = LAUNCH.replace("rustorka_code", "rustorka_net_code")
JS = "mcp__claude-in-chrome__javascript_tool"


class CollectorGuard(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.c = Path(self.d.name)
        (self.c / "ingest").mkdir()

    def tearDown(self):
        self.d.cleanup()

    def job(self, kind, run):
        (self.c / kind).mkdir(exist_ok=True)
        (self.c / kind / "queries_run.json").write_text(json.dumps({"v": 1, "run": run}), encoding="utf-8")

    def touch(self, name, age, run):
        p = self.c / "ingest" / name
        p.write_text(json.dumps({"v": 1, "kind": "rustorka", "run": run}), encoding="utf-8")
        t = time.time() - age
        os.utime(p, (t, t))

    def test_same_job_alive_denied(self):
        self.job("rustorka", "R-r2a")
        self.touch("rustorka-1.txt", 40, "R-r2a")
        self.assertIn("этого же задания", cg.decide(JS, {"text": LAUNCH}, self.c))

    def test_forgotten_old_collector_denied(self):
        # баг 2026-11-23: первый сборщик остался во вкладке вне группы Claude
        self.job("rustorka", "R-r1")
        self.touch("rustorka_status-1.txt", 30, "R")
        self.assertIn("старого задания R", cg.decide(JS, {"text": LAUNCH}, self.c))

    def test_mirror_part_allowed_but_not_twice(self):
        self.job("rustorka", "R-r2a")
        self.job("rustorka_net", "R-r2b")
        self.touch("rustorka-1.txt", 10, "R-r2a")          # .com работает
        self.assertEqual(cg.decide(JS, {"text": NET}, self.c), "")   # зеркало — можно
        self.touch("rustorka-2.txt", 5, "R-r2b")           # зеркало запущено
        self.assertTrue(cg.decide(JS, {"text": NET}, self.c))        # второй на зеркале — нельзя
        self.assertTrue(cg.decide(JS, {"text": LAUNCH}, self.c))     # второй на .com — нельзя

    def test_inside_batch_denied(self):
        self.job("rustorka", "R-r1")
        self.touch("rustorka-1.txt", 10, "R-r1")
        ti = {"actions": [{"name": "navigate", "input": {"url": "https://rustorka.com/"}},
                          {"name": "javascript_tool", "input": {"text": LAUNCH}}]}
        self.assertTrue(cg.decide("mcp__claude-in-chrome__browser_batch", ti, self.c))

    def test_quiet_bridge_allows_relaunch(self):
        self.job("rustorka", "R-r1")
        self.touch("rustorka-1.txt", 600, "R-r1")
        self.assertEqual(cg.decide(JS, {"text": LAUNCH}, self.c), "")

    def test_other_site_and_status_check_not_blocked(self):
        self.job("rustorka", "R-r1")
        self.touch("rustorka-1.txt", 10, "R-r1")
        self.assertEqual(cg.decide(JS, {"text": LAUNCH.replace("rustorka", "kinozal")}, self.c), "")
        self.assertEqual(cg.decide(JS, {"text": "JSON.stringify(__rkStatus())"}, self.c), "")


if __name__ == "__main__":
    unittest.main()
