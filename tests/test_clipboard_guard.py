"""Hook clipboard-guard.sh: nothing is written to the owner's clipboard (owner, 2026-10-26: «не пиши в буфер обмена! запомни»;
the rutracker collector had been put there with Set-Clipboard)."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

HOOK = Path(__file__).resolve().parents[1] / ".claude" / "hooks" / "clipboard-guard.sh"


def denied(tool: str, **ti) -> bool:
    out = subprocess.run(["bash", str(HOOK)], input=json.dumps({"tool_name": tool, "tool_input": ti}),
                         capture_output=True, text=True, encoding="utf-8").stdout
    return '"deny"' in out


@unittest.skipUnless(shutil.which("bash"), "bash is needed for the hook")
class ClipboardGuardTest(unittest.TestCase):
    def test_writes_denied(self):
        self.assertTrue(denied("PowerShell", command="Get-Content -Raw run.js | Set-Clipboard"))
        self.assertTrue(denied("Bash", command="cat run.js | clip"))
        self.assertTrue(denied("Bash", command="cat run.js | clip.exe"))
        self.assertTrue(denied("Bash", command="python -c 'import pyperclip; pyperclip.copy(1)'"))
        self.assertTrue(denied("PowerShell", command="[System.Windows.Forms.Clipboard]::SetText('x')"))
        self.assertTrue(denied("mcp__claude-in-chrome__javascript_tool", text="navigator.clipboard.writeText('x')"))

    def test_other_commands_allowed(self):
        self.assertFalse(denied("PowerShell", command="Get-Clipboard -Raw"))
        self.assertFalse(denied("Bash", command="grep -n clipboard films/*.py; ls clips/"))
        self.assertFalse(denied("Bash", command="python -m films.bridge"))


if __name__ == "__main__":
    unittest.main()
