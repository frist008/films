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


import sys
import tempfile

sys.path.insert(0, str(HOOK.parent))
import lang_check  # noqa: E402


def transcript(text):
    f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8")
    f.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}, ensure_ascii=False))
    f.close()
    return f.name


def denied_bash(prev):
    path = transcript(prev)
    try:
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}, "transcript_path": path}, ensure_ascii=False)
        out = subprocess.run(["bash", str(HOOK)], input=payload, capture_output=True, text=True, encoding="utf-8").stdout
    finally:
        Path(path).unlink()
    return '"deny"' in out


class NotesBetweenToolsTest(unittest.TestCase):
    """owner, 2026-10-28: «опять на англ заговорил? исправляй баг» — the notes between tool calls were English."""

    def test_real_english_notes(self):
        for t in ("Running both rechecks on a fresh DB copy to see what moves:",
                  "To limit the blast radius, the new TMDB path shifts only when the episode counts confirm it.",
                  "The test case was wrong: seasons 3 and 4 both have 11 episodes, so only the part name can decide here."):
            self.assertTrue(lang_check.not_russian(t), t)

    def test_russian_with_code(self):
        for t in ("Добавляю в `_shift_seasons` сверку по числу серий: «[1-8] 2024» → S5 (films/match.py).",
                  "Чиню сезоны «Клинка»: Kimetsu no Yaiba Hashira Geiko — S5 TMDB.",
                  "Ссылки: [Brod KZ](https://www.youtube.com/watch?v=NG7Rsryw8FA)", "OK"):
            self.assertFalse(lang_check.not_russian(t), t)

    @unittest.skipUnless(shutil.which("bash"), "bash is needed for the hook")
    def test_hook_on_any_tool(self):
        self.assertTrue(denied_bash("Running both rechecks on a fresh DB copy to see what moves:"))
        self.assertFalse(denied_bash("Запускаю обе перепроверки на свежей копии базы."))


if __name__ == "__main__":
    unittest.main()
