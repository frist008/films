"""plan_guard.watchdog_table_missing: a watchdog check ends with the `films.watchdog --table` table (owner, 2026-10-28:
«не вижу от сторожа таблицы с окончанием времени работы которую ты должен оставлять при использовании сторожа»)."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("plan_guard", Path(__file__).resolve().parents[1] / ".claude" / "hooks" / "plan_guard.py")
pg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pg)

TABLE = "| Кто | Что делает | Готово | Осталось | Закончит | Состояние |\n|---|---|---|---|---|---|\n| a | b | 100% | 0 | — | закончил |"


def user(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def tool(cmd):
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": cmd}}]}}


def result():
    return {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}}


def say(text):
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


class WatchdogTableTest(unittest.TestCase):
    def check(self, rows):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8") as f:
            f.write("\n".join(json.dumps(r, ensure_ascii=False) for r in rows))
        self.addCleanup(Path(f.name).unlink)
        return pg.watchdog_table_missing(f.name)

    def test_cron_check_without_table(self):   # the 2026-10-28 20:37 reply: a summary, no table
        self.assertTrue(self.check([user("Проверка фоновых агентов и команд (правило владельца, раз в 30 минут): …"),
                                    tool("python -X utf8 -m films.watchdog --table"), result(),
                                    say("Сторож 20:37: все агенты закончили.")]))

    def test_cron_check_with_table(self):
        self.assertFalse(self.check([user("Проверка фоновых агентов и команд …"),
                                     tool("python -X utf8 -m films.watchdog --table"), result(),
                                     say("Сторож 20:37:\n\n" + TABLE + "\n\nИтог: зависших нет.")]))

    def test_watchdog_run_in_any_turn(self):
        self.assertTrue(self.check([user("как дела?"), tool("python -X utf8 -m films.watchdog"), result(), say("Всё идёт.")]))

    def test_other_turn(self):
        self.assertFalse(self.check([user("как дела?"), tool("git status"), result(), say("Всё идёт.")]))

    def test_only_this_turn(self):   # an earlier check does not count for a later turn
        self.assertFalse(self.check([user("Проверка фоновых агентов …"), say(TABLE), user("ок"), say("Хорошо.")]))


class AskLaterTest(unittest.TestCase):
    def test_promise_caught(self):   # the 2026-10-28 20:38 reply
        self.assertTrue(pg.ASK_LATER.search("Вот ссылки к ближайшим четырём, сами вопросы задам следующим сообщением."))
        self.assertTrue(pg.ASK_LATER.search("спрошу позже"))

    def test_plain_text(self):
        self.assertFalse(pg.ASK_LATER.search("Ответы записаны: «Жатва» — английский трейлер."))

    def test_last_text(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8") as f:
            f.write("\n".join(json.dumps(r, ensure_ascii=False) for r in [say("первое"), user("x"), say("второе")]))
        self.addCleanup(Path(f.name).unlink)
        self.assertEqual(pg.last_text(f.name), "второе")


if __name__ == "__main__":
    unittest.main()
