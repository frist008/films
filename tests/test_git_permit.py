""".claude/hooks/git-permit.sh: commit and push only with the owner's explicit permission (owner, 2026-11-03: «давай создадим
хук, что комит и пуш можно только с моего явного разрешения», «Спрашивай каждый раз»)."""
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from tests.hookbash import BASH   # Git bash, not WSL (Python 3.15, 2026-11-17)

HOOK = Path(__file__).resolve().parents[1] / ".claude" / "hooks" / "git-permit.sh"
Q = "1/1 · Коммит и пуш на GitHub: хук и копия базы. Можно?"


class GitPermitTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.flag = Path(self.dir.name) / "data" / "cache" / "locks" / "git_permit.json"
        self.git("init", "-q")
        self.real_commit()

    def git(self, *args):
        subprocess.run(["git", "-C", self.dir.name, "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True,
                       capture_output=True)

    def real_commit(self):
        self.git("commit", "-q", "--allow-empty", "-m", "c")

    def run_hook(self, mode, payload):
        env = dict(os.environ, CLAUDE_PROJECT_DIR=self.dir.name)
        out = subprocess.run([BASH, str(HOOK), mode], input=json.dumps(payload, ensure_ascii=False), capture_output=True,
                             text=True, encoding="utf-8", env=env).stdout
        return json.loads(out)["hookSpecificOutput"]["permissionDecision"] if out.strip() else "allow"

    def bash(self, cmd):
        return self.run_hook("pre", {"tool_name": "Bash", "tool_input": {"command": cmd}})

    def answer(self, text):
        self.run_hook("post", {"tool_name": "AskUserQuestion", "tool_input": {"questions": [{"question": Q}]},
                               "tool_response": {"answers": {Q: text}}})

    def test_no_permission(self):
        self.assertEqual(self.bash("python -X utf8 -m films.locks run git -- git commit -q -m x"), "deny")
        self.assertEqual(self.bash("python -X utf8 -m films.locks run git -- git push origin main"), "deny")

    def test_one_answer_one_commit(self):
        self.answer("Да, коммить и пушить")
        self.assertEqual(self.bash("python -X utf8 -m films.locks run git -- git commit -q -m x"), "allow")
        self.real_commit()   # the commit went through
        self.assertEqual(self.bash("python -X utf8 -m films.locks run git -- git push origin main"), "allow")
        self.assertEqual(self.bash("python -X utf8 -m films.locks run git -- git commit -q -m y"), "deny")   # used

    def test_blocked_attempt_keeps_the_answer(self):
        """2026-11-13 («Да, поправить»): the stage hook denied the first commit attempt, yet the «да» was spent."""
        self.answer("Да, коммитить и пушить")
        self.assertEqual(self.bash("python -X utf8 -m films.locks run git -- git commit -q -m x"), "allow")
        # another hook stopped it: no commit happened → the same answer still allows the commit
        self.assertEqual(self.bash("python -X utf8 -m films.locks run git -- git commit -q -m x"), "allow")
        self.real_commit()
        self.assertEqual(self.bash("python -X utf8 -m films.locks run git -- git commit -q -m z"), "deny")

    def test_permission_written_by_the_old_hook(self):
        self.flag.parent.mkdir(parents=True)
        self.flag.write_text(json.dumps({"granted": True, "at": time.time(), "commit_used": False}), encoding="utf-8")
        self.assertEqual(self.bash("python -X utf8 -m films.locks run git -- git commit -q -m x"), "allow")
        self.real_commit()
        self.assertEqual(self.bash("python -X utf8 -m films.locks run git -- git commit -q -m y"), "deny")

    def test_yes_inside_answer(self):   # the owner's answer 2026-11-03
        self.answer("старая история тоже не нужна на компе, да комит и пуш")
        self.assertEqual(self.bash("python -X utf8 -m films.locks run git -- git commit -q -m x"), "allow")

    def test_no_answer(self):
        self.answer("Нет")
        self.assertEqual(self.bash("python -X utf8 -m films.locks run git -- git commit -q -m x"), "deny")

    def test_expired(self):
        self.flag.parent.mkdir(parents=True)
        self.flag.write_text(json.dumps({"granted": True, "at": time.time() - 3 * 3600, "commit_used": False}), encoding="utf-8")
        self.assertEqual(self.bash("python -X utf8 -m films.locks run git -- git push origin main"), "deny")

    def test_other_commands(self):
        self.assertEqual(self.bash("git status && git log --oneline -3"), "allow")
        self.assertEqual(self.bash('grep -n "git push" docs/x.md'), "allow")


if __name__ == "__main__":
    unittest.main()
