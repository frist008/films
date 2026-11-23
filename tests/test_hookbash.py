"""Hook tests run hooks in Git's bash, never in a bare `bash` that Windows resolves to WSL (tests/hookbash.py)."""
import re
import subprocess
import unittest
from pathlib import Path

from tests.hookbash import BASH

TESTS = Path(__file__).resolve().parent


class HookBash(unittest.TestCase):
    def test_no_bare_bash_in_tests(self):
        bad = [p.name for p in TESTS.glob("*.py") if p.name != "hookbash.py" and re.search(r"""\[\s*["']bash["']\s*,""", p.read_text(encoding="utf-8"))]
        self.assertEqual(bad, [], "use `from tests.hookbash import BASH`: a bare bash is WSL's bash.exe on Windows")

    def test_hook_bash_finds_python(self):
        out = subprocess.run([BASH, "-c", "python -X utf8 -c 'print(1)'"], capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), "1", out.stderr)


if __name__ == "__main__":
    unittest.main()
