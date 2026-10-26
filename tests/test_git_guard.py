"""Hook git-guard.sh: git writes only under the `git` semaphore (owner, 2026-10-26: «правило которое будет блокировать
обращение к гиту вот так параллельно и чтобы не было гонок» — two background pushes raced: «cannot lock ref»)."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

HOOK = Path(__file__).resolve().parents[1] / ".claude" / "hooks" / "git-guard.sh"
L = "python -X utf8 -m films.locks run git -- "


def denied(cmd: str, tool: str = "Bash") -> bool:
    out = subprocess.run(["bash", str(HOOK)], input=json.dumps({"tool_name": tool, "tool_input": {"command": cmd}}),
                         capture_output=True, text=True, encoding="utf-8").stdout
    return '"deny"' in out


@unittest.skipUnless(shutil.which("bash"), "bash is needed for the hook")
class GitGuardTest(unittest.TestCase):
    def test_bare_writes_denied(self):
        self.assertTrue(denied("cd /d/work/films && git add PLAN.md && git commit -q -m x"))
        self.assertTrue(denied('git -c core.sshCommand="ssh -o X=1" push -q origin main'))
        self.assertTrue(denied("git push origin main", tool="PowerShell"))
        # one under the lock, the next one bare
        self.assertTrue(denied(L + "git add PLAN.md && git push origin main"))

    def test_locked_allowed(self):
        self.assertFalse(denied(L + "git add PLAN.md"))
        self.assertFalse(denied("cd /d/work/films && " + L + "git commit -q -m x && " + L + "git push -q origin main"))
        self.assertFalse(denied(L + 'git -c core.sshCommand="C:/Windows/System32/OpenSSH/ssh.exe -o A=1" push -q origin main'))

    def test_reads_allowed(self):
        self.assertFalse(denied("git status --short && git log origin/main..HEAD --oneline | wc -l && git diff PLAN.md"))
        self.assertFalse(denied("grep -n 'git push' docs/x.md; cat .claude/hooks/git-guard.sh"))


if __name__ == "__main__":
    unittest.main()
