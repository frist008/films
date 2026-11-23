"""Tests of the collectors must not touch the live data/cache (found by the review 2026-10-28: tests/test_toloka.py ran
toloka.ingest with the real CACHE and overwrote data/cache/toloka/forums.json with a one-forum fixture).

Runs the test modules that call a source's ingest / queries in a subprocess and checks that no file under the sources'
live cache folders was created, changed or removed.
Run:  python -X utf8 -m unittest tests.test_live_cache_untouched
"""
import hashlib
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCES = ("toloka", "kinozal", "rustorka", "rutracker", "mazepa", "nnmclub")
MODULES = ("tests.test_toloka", "tests.test_kinozal", "tests.test_rustorka", "tests.test_rutracker",
           "tests.test_mazepa", "tests.test_nnmclub")


def snapshot() -> dict[str, str]:
    out = {}
    for s in SOURCES:
        d = ROOT / "data" / "cache" / s
        if not d.is_dir():
            continue
        for p in d.rglob("*"):
            if p.is_file():
                # mtime too: a rewrite with the same bytes (the fixture over itself) is still a write
                out[str(p.relative_to(ROOT))] = f"{p.stat().st_mtime_ns}:{hashlib.sha1(p.read_bytes()).hexdigest()}"
    return out


@unittest.skipIf(os.environ.get("FILMS_CACHE_GUARD_CHILD"), "the guard's own subprocess")
class LiveCacheUntouchedTest(unittest.TestCase):
    def test_collector_tests_leave_live_cache_alone(self):
        mods = [m for m in MODULES if (ROOT / (m.replace(".", "/") + ".py")).exists()]
        before = snapshot()
        env = dict(os.environ, FILMS_CACHE_GUARD_CHILD="1", PYTHONUTF8="1")
        r = subprocess.run([sys.executable, "-X", "utf8", "-m", "unittest", "-q", *mods], cwd=ROOT, env=env,
                           capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        after = snapshot()
        changed = sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))
        self.assertEqual(changed, [], "тесты изменили живой кэш")


if __name__ == "__main__":
    unittest.main()
