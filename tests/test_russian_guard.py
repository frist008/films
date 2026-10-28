"""Hook russian-guard.sh: the next-step box (wrap_up) only in Russian (owner, 2026-10-28: «шо? опять баг скилов что на англ
начал общатся? фикси баг и спрашивай нормально»)."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

HOOK = Path(__file__).resolve().parents[1] / ".claude" / "hooks" / "russian-guard.sh"


def denied(**ti):
    payload = json.dumps({"tool_name": "mcp__ccd_turn__wrap_up", "tool_input": ti}, ensure_ascii=False)
    out = subprocess.run(["bash", str(HOOK)], input=payload, capture_output=True, text=True, encoding="utf-8").stdout
    return '"deny"' in out


@unittest.skipUnless(shutil.which("bash"), "bash is needed for the hook")
class RussianGuardTest(unittest.TestCase):
    def test_english_box_denied(self):
        self.assertTrue(denied(recap="Stages A, B and C are done.", ask="Start the survey now?",
                               actions=[{"label": "Start survey"}]))

    def test_english_button_denied(self):
        self.assertTrue(denied(recap="Этапы готовы.", ask="Начать опрос?", actions=[{"label": "Later"}]))

    def test_russian_box_allowed(self):
        self.assertFalse(denied(recap="Этапы A, B и C готовы, коммит f818e8f.", ask="Начать опрос по весам?",
                                actions=[{"label": "Начать опрос"}, {"label": "Позже"}]))


if __name__ == "__main__":
    unittest.main()
