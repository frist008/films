"""films/locks.py: re-entrant in one process, exclusive across processes, `run` passes the exit code (owner, 2026-10-26:
«понять критические области и не пытаться с ними работать параллельно и придумать семафоры»)."""
import subprocess
import sys
import time
import unittest

from films import locks


class LocksTest(unittest.TestCase):
    def setUp(self):
        # a temp lock folder, the live data/cache/locks untouched (review round 11); subprocesses get it by the env var
        import os
        import tempfile
        from pathlib import Path
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        old_dir, old_env = locks.DIR, os.environ.get("FILMS_LOCK_DIR")
        locks.DIR = Path(tmp.name)
        os.environ["FILMS_LOCK_DIR"] = tmp.name
        self.addCleanup(setattr, locks, "DIR", old_dir)
        self.addCleanup(lambda: os.environ.pop("FILMS_LOCK_DIR") if old_env is None
                        else os.environ.__setitem__("FILMS_LOCK_DIR", old_env))

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
