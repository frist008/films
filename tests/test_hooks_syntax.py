"""Hooks must at least parse (2026-10-25: a single quote inside the bash-quoted Python of agent-watchdog.sh made every
CronCreate/CronDelete silently skip the watchdog marker). Checks `bash -n` of every hook and compiles the Python that the
hooks embed with `python … -c '…'`."""
import pathlib
import re
import shutil
import subprocess
import unittest

HOOKS = pathlib.Path(__file__).resolve().parent.parent / ".claude" / "hooks"


def embedded_python(text: str) -> list[str]:
    # python [-X utf8] -c '<code>' — the code runs to the next single quote (bash single quotes cannot be escaped)
    return re.findall(r"python3?(?:\s+-X\s+utf8)?\s+-c\s+'([^']*)'", text)


class HooksSyntaxTest(unittest.TestCase):
    def test_bash_parses(self):
        bash = shutil.which("bash")
        if not bash:
            self.skipTest("no bash")
        for f in sorted(HOOKS.glob("*.sh")):
            r = subprocess.run([bash, "-n", str(f)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, f"{f.name}: {r.stderr}")

    def test_embedded_python_compiles(self):
        n = 0
        for f in sorted(HOOKS.glob("*.sh")):
            for i, code in enumerate(embedded_python(f.read_text(encoding="utf-8"))):
                n += 1
                try:
                    compile(code, f"{f.name}#{i}", "exec")
                except SyntaxError as e:
                    self.fail(f"{f.name} embedded python #{i}: {e}")
        self.assertGreater(n, 5)

    def test_python_hooks_compile(self):
        for f in sorted(HOOKS.glob("*.py")):
            compile(f.read_text(encoding="utf-8"), f.name, "exec")


if __name__ == "__main__":
    unittest.main()
