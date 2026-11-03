"""Hook tab-close-guard.sh: no Chrome tab is closed while a collector runs (owner, 2026-10-27: «Тебе надо запретить так
закрывать вкладки чтобы к другим терять контекст» — closing one tab dissolved the group and the collector tabs fell out)."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / ".claude" / "hooks" / "tab-close-guard.sh"


def run(tool="mcp__claude-in-chrome__tabs_close_mcp", logs=None):
    env = dict(os.environ, CLAUDE_PROJECT_DIR=str(ROOT))
    code = ("import json,sys; from films import gate, db; from pathlib import Path; "
            f"gate.LOGS = Path({str(logs)!r})") if logs else None
    payload = json.dumps({"tool_name": tool, "tool_input": {"tabId": 1}})
    if logs:   # point the gate at a scratch logs dir through a sitecustomize-free shim
        shim = Path(tempfile.mkdtemp())
        __import__("atexit").register(__import__("shutil").rmtree, shim, True)   # review 2026-10-28: the folders were left in %TEMP%
        (shim / "films").mkdir()
        for name in ("__init__.py", "db.py", "watchdog.py", "gate.py"):
            src = (ROOT / "films" / name).read_text(encoding="utf-8")
            if name == "gate.py":
                src = src.replace('LOGS = db.ROOT / "data" / "logs"', f"LOGS = __import__('pathlib').Path({str(logs)!r})")
            (shim / "films" / name).write_text(src, encoding="utf-8")
        env["CLAUDE_PROJECT_DIR"] = str(shim)
    out = subprocess.run(["bash", str(HOOK)], input=payload, capture_output=True, text=True, encoding="utf-8", env=env).stdout
    return '"deny"' in out


@unittest.skipUnless(shutil.which("bash"), "bash is needed for the hook")
class TabCloseGuardTest(unittest.TestCase):
    def test_denied_while_collecting(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, d, True)   # review 2026-10-28: the folders were left in %TEMP%
        (d / "agent_src_kinozal.log").write_text("x PROGRESS 30% — kinozal topics 9000/30110\n", encoding="utf-8")
        self.assertTrue(run(logs=d))

    def test_allowed_when_all_done(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, d, True)   # review 2026-10-28: the folders were left in %TEMP%
        (d / "agent_src_kinozal.log").write_text("x PROGRESS 100%\nAGENT_SRC_KINOZAL_OK\n", encoding="utf-8")
        self.assertFalse(run(logs=d))

    def test_other_tools_untouched(self):
        self.assertFalse(run(tool="mcp__claude-in-chrome__navigate"))


if __name__ == "__main__":
    unittest.main()
