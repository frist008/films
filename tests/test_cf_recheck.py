"""Owner, 2026-11-23: «Не нужна, ты врешь. Всегда когда ты видишь что она нужна ставить таймер на минуту и смотреть не
проверилось ли оно само» — a Cloudflare question only after the tab was reloaded and ≥ 60 s passed."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from films import cf_recheck
from tests.hookbash import BASH

ROOT = Path(__file__).resolve().parent.parent


class AskProblemTest(unittest.TestCase):
    def test_rules(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "cf.json"
            q = ["Kinozal: нужна проверка Cloudflare «я не робот». Прошёл?"]
            self.assertIn("Нет отметки", cf_recheck.ask_problem(q, p, now=1000))
            cf_recheck.mark("kinozal", p, now=1000)
            self.assertIn("нужно ≥ 60", cf_recheck.ask_problem(q, p, now=1030))
            self.assertIsNone(cf_recheck.ask_problem(q, p, now=1061))
            self.assertIn("Нет отметки", cf_recheck.ask_problem(q, p, now=1000 + 16 * 60))
            self.assertIsNone(cf_recheck.ask_problem(["YouTube сейчас свободен?"], p, now=5))


class HookTest(unittest.TestCase):
    def test_hook_denies_without_mark(self):
        mark = cf_recheck.MARK
        saved = mark.read_text(encoding="utf-8") if mark.exists() else None
        try:
            if mark.exists():
                mark.unlink()
            inp = json.dumps({"tool_name": "AskUserQuestion", "tool_input": {"questions": [
                {"question": "1/1 · Нужна проверка Cloudflare. Прошёл?", "options": []}]}}, ensure_ascii=False)
            out = subprocess.run([BASH, str(ROOT / ".claude/hooks/cf-ask-guard.sh")], input=inp.encode("utf-8"),
                                 capture_output=True, timeout=60).stdout.decode("utf-8")
            self.assertIn('"deny"', out)
        finally:
            if saved is not None:
                mark.write_text(saved, encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
