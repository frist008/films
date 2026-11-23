"""Pictures to the owner: at most 4 between two question calls (owner, 2026-11-02: «4 картинки, 4 вопроса, зачем ты мне
накидал сразу всё? у меня не такой высокий монитор»); .claude/hooks/picture-guard.sh."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / ".claude" / "hooks" / "picture-guard.sh"
BASH = shutil.which("bash")


@unittest.skipUnless(BASH, "bash is needed for hooks")
class PictureGuardTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def run_hook(self, tool, tool_input):
        out = subprocess.run([BASH, str(HOOK)], input=json.dumps({"tool_name": tool, "tool_input": tool_input}),
                             capture_output=True, text=True, encoding="utf-8",
                             env=os.environ | {"CLAUDE_PROJECT_DIR": self.dir}, timeout=60)
        return json.loads(out.stdout)["hookSpecificOutput"]["permissionDecision"] if out.stdout.strip() else "allow"

    def test_limit_and_reset(self):
        self.assertEqual(self.run_hook("SendUserFile", {"files": ["a", "b", "c"]}), "allow")
        self.assertEqual(self.run_hook("SendUserFile", {"files": ["d", "e"]}), "deny")        # 5 > 4
        self.assertEqual(self.run_hook("SendUserFile", {"files": ["d"]}), "allow")             # 4
        self.assertEqual(self.run_hook("AskUserQuestion", {"questions": []}), "allow")        # resets
        self.assertEqual(self.run_hook("SendUserFile", {"files": ["a", "b", "c", "d"]}), "allow")

    def test_fifteen_at_once_denied(self):
        self.assertEqual(self.run_hook("SendUserFile", {"files": [str(i) for i in range(15)]}), "deny")


if __name__ == "__main__":
    unittest.main()
