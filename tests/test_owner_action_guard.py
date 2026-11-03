"""Hook owner-action-guard.sh: the rules never say the owner does something without his permission (owner, 2026-10-26:
«ты пишешь в правила ничего никогда что я что-то делаю, создавай хук, это запрещено писать без моего разрешения»)."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

HOOK = Path(__file__).resolve().parents[1] / ".claude" / "hooks" / "owner-action-guard.sh"


def denied(tool: str, **ti) -> bool:
    out = subprocess.run(["bash", str(HOOK)], input=json.dumps({"tool_name": tool, "tool_input": ti}, ensure_ascii=False),
                         capture_output=True, text=True, encoding="utf-8").stdout
    return '"deny"' in out


@unittest.skipUnless(shutil.which("bash"), "bash is needed for the hook")
class OwnerActionGuardTest(unittest.TestCase):
    def test_owner_actions_denied(self):
        # the very lines removed 2026-10-26
        for line in ("владелец сам открывает run.js в Блокноте → Ctrl+A, Ctrl+C",
                     "Запасной путь — один вызов в Chrome (владелец у компьютера, жмёт «Allow» один раз)",
                     "Ключи в веб-формы не вводить — значения вставляет владелец.",
                     "Jellyfin «films» (владелец создаёт в Панели управления → Ключи API)",
                     "Нажать «Войти» — владельцу нужно нажать самому"):
            self.assertTrue(denied("Edit", file_path="D:\\work\\films\\.claude\\skills\\update-db\\SKILL.md", new_string=line), line)
        self.assertTrue(denied("Write", file_path="D:/work/films/CLAUDE.md", content="ok\nвладелец входит в rutracker\n"))
        self.assertTrue(denied("Edit", file_path="docs/google-setup.md", new_string="Дальше владелец подтверждает домен"))

    def test_allowed(self):
        f = "D:/work/films/.claude/skills/owner-rules/SKILL.md"
        self.assertFalse(denied("Edit", file_path=f, new_string="Claude запускает run.js одним вызовом javascript_tool"))
        self.assertFalse(denied("Edit", file_path=f, new_string="(владелец, 2026-10-24: «чтобы не забыть, что остановил»)"))
        self.assertFalse(denied("Edit", file_path=f, new_string="владелец разрешил 2026-10-27: «ключ создам сам» — владелец создаёт ключ"))
        # PLAN.md is a log of what happened, not a rule
        self.assertFalse(denied("Edit", file_path="D:/work/films/PLAN.md", new_string="владелец вставил сборщик в консоль"))


@unittest.skipUnless(shutil.which("bash"), "bash is needed for the hook")
class RulesCleanTest(unittest.TestCase):
    def test_no_owner_actions_in_rules(self):
        # «то что где-то сейчас есть удалить» — every rule file as it is now passes the hook
        root = Path(__file__).resolve().parents[1]
        files = [root / "CLAUDE.md", *sorted((root / ".claude" / "skills").rglob("*.md")), *sorted((root / "docs").rglob("*.md"))]
        bad = [str(f.relative_to(root)) for f in files
               if denied("Write", file_path=str(f), content=f.read_text(encoding="utf-8"))]
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
