"""plan_guard.owner_action_unasked: нужен вход / «Allow» владельца — только блокирующим AskUserQuestion (владелец,
2026-11-23: «Если где-то нужен вход всегда должен быть блокирующий ask question чтобы я знал. Иначе я не вижу вопроса и
думаю что у тебя всё хорошо» — сборщик rustorka ждал входа, а просьба была только текстом)."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("plan_guard", Path(__file__).resolve().parents[1] / ".claude" / "hooks" / "plan_guard.py")
pg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pg)

TEXT = "rustorka не собирается: сборщик пишет «не выполнен вход». Войдёшь в rustorka во вкладке Chrome?"


class LoginAskTest(unittest.TestCase):
    def test_text_only_is_blocked(self):
        self.assertTrue(pg.owner_action_unasked(TEXT, ["PowerShell", "mcp__ccd_turn__wrap_up"]))
        self.assertTrue(pg.owner_action_unasked("Нажми «Allow» в окне Chrome.", []))

    def test_with_ask_passes(self):
        self.assertEqual(pg.owner_action_unasked(TEXT, ["AskUserQuestion"]), "")

    def test_unrelated_text_passes(self):
        self.assertEqual(pg.owner_action_unasked("Сбор идёт, вход уже выполнен, всё работает.", []), "")

    def test_rule_description_passes(self):
        # рассказ о правиле, а не просьба (ложное срабатывание 2026-11-23)
        self.assertEqual(pg.owner_action_unasked(
            "Теперь, если нужен вход на сайт или окно «Allow» в Chrome, я сразу задаю вопрос, например «Нужен вход на "
            "rustorka — войдёшь?». Проверка не даст закончить ответ с такой просьбой.", []), "")

    def test_turn_tools_from_transcript(self):
        rows = [
            {"type": "user", "message": {"content": "старое"}},
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "AskUserQuestion", "input": {}}]}},
            {"type": "user", "message": {"content": "новое сообщение владельца"}},
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "PowerShell", "input": {}}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}},
        ]
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8") as f:
            f.write("\n".join(json.dumps(r, ensure_ascii=False) for r in rows))
        try:
            self.assertEqual(pg._turn_tools(f.name), ["PowerShell"])   # вопрос прошлого хода не считается
        finally:
            Path(f.name).unlink()


if __name__ == "__main__":
    unittest.main()
