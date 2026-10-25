"""Hook no-memory.sh: nothing of this project goes to user-level Claude dirs — memory, skills, hooks, agents, commands,
CLAUDE.md, settings (owner, 2026-10-26: «в мемори глобальной ничего не должно быть что касается этого проекта! создать хук
запрещающий этому проекту создавать глобальные скилы или хуки»). The 2026-10-24 version matched only Users/<name>/.claude
and missed this account's memory in AppData/Roaming/MultiDeskClaude-Profiles/<profile>/.claude."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

HOOK = Path(__file__).resolve().parents[1] / ".claude" / "hooks" / "no-memory.sh"
PROFILE = "C:\\Users\\u\\AppData\\Roaming\\MultiDeskClaude-Profiles\\Home\\.claude"


def denied(tool: str, **ti) -> bool:
    out = subprocess.run(["bash", str(HOOK)], input=json.dumps({"tool_name": tool, "tool_input": ti}),
                         capture_output=True, text=True, encoding="utf-8").stdout
    return '"deny"' in out


@unittest.skipUnless(shutil.which("bash"), "bash is needed for the hook")
class NoGlobalTest(unittest.TestCase):
    def test_profile_memory(self):
        self.assertTrue(denied("Write", file_path=PROFILE + "\\projects\\D--work-films\\memory\\x.md"))

    def test_global_skill_hook_settings(self):
        self.assertTrue(denied("Write", file_path="C:\\Users\\u\\.claude\\skills\\films\\SKILL.md"))
        self.assertTrue(denied("Edit", file_path="C:/Users/u/.claude/settings.json"))
        self.assertTrue(denied("Bash", command="mkdir -p ~/.claude/hooks && cp x.sh ~/.claude/hooks/"))
        self.assertTrue(denied("PowerShell", command="Set-Content $env:USERPROFILE\\.claude\\CLAUDE.md x"))
        self.assertTrue(denied("Bash", command="echo x > /c/Users/u/AppData/Roaming/MultiDeskClaude-Profiles/B/.claude/agents/a.md"))
        self.assertTrue(denied("Write", file_path=PROFILE + "\\commands\\films.md"))

    def test_project_dir_allowed(self):
        self.assertFalse(denied("Edit", file_path="D:\\work\\films\\.claude\\hooks\\no-memory.sh"))
        self.assertFalse(denied("Bash", command="cat > .claude/skills/x/SKILL.md"))
        self.assertFalse(denied("Write", file_path="D:/work/films/.claude/settings.json"))

    def test_reading_allowed(self):
        self.assertFalse(denied("Bash", command="ls ~/.claude/skills; cat C:/Users/u/.claude/settings.json"))

    def test_other_project_memory_not_ours(self):
        self.assertFalse(denied("Write", file_path="C:\\Users\\u\\.claude\\projects\\D--work-maps\\memory\\a.md"))


if __name__ == "__main__":
    unittest.main()
