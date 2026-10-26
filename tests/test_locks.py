"""films/locks.py: re-entrant in one process, exclusive across processes, `run` passes the exit code (owner, 2026-10-26:
«понять критические области и не пытаться с ними работать параллельно и придумать семафоры»)."""
import subprocess
import sys
import time
import unittest

from films import locks


class LocksTest(unittest.TestCase):
    def test_reentrant_same_process(self):
        with locks.lock("test_reent", timeout=2):
            with locks.lock("test_reent", timeout=2):      # match_releases inside an outer lock('match')
                pass
            self.assertTrue((locks.DIR / "test_reent.lock").exists())
        self.assertFalse((locks.DIR / "test_reent.lock").exists())

    def test_exclusive_across_processes(self):
        with locks.lock("test_excl", timeout=2):
            t = time.time()
            r = subprocess.run([sys.executable, "-X", "utf8", "-c",
                                "from films import locks\ntry:\n  with locks.lock('test_excl', timeout=1): print('GOT')\n"
                                "except TimeoutError: print('BUSY')"], capture_output=True, text=True)
            self.assertEqual(r.stdout.strip(), "BUSY")
            self.assertGreaterEqual(time.time() - t, 1)

    def test_run_exit_code(self):
        self.assertEqual(locks.run("test_run", [sys.executable, "-c", "import sys; sys.exit(3)"]), 3)
        self.assertFalse((locks.DIR / "test_run.lock").exists())


if __name__ == "__main__":
    unittest.main()
