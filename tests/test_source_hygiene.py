"""Source and data files must not contain control characters.

A regex written through a shell heredoc once lost its backslash: «\\b» became a backspace byte (0x08) in films/rank.py and
films/sources/trailer_lang.py, so «DV», «4K» and the AI-voice markers silently never matched (found 2026-10-25); the same
happened to a path «…\\bonus» in data/disk_map.json (stored by JSON as the escape «\\b», so JSON values are checked decoded).
Run: python -X utf8 -m unittest tests.test_source_hygiene
"""
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOWED = {"\n", "\r", "\t"}


def _strings(o):
    if isinstance(o, str):
        yield o
    elif isinstance(o, dict):
        for k, v in o.items():
            yield from _strings(k)
            yield from _strings(v)
    elif isinstance(o, list):
        for v in o:
            yield from _strings(v)


class TestNoControlChars(unittest.TestCase):
    def test_sources(self):
        bad = []
        for p in list((ROOT / "films").rglob("*.py")) + list((ROOT / "films").rglob("*.js")) + list((ROOT / ".claude").rglob("*.py")) \
                + list((ROOT / ".claude").rglob("*.sh")) + list((ROOT / "config").glob("*.toml")) \
                + list(ROOT.glob("*.md")) + list((ROOT / ".claude").rglob("*.md")) + list((ROOT / "docs").glob("*.md")):
            text = p.read_text(encoding="utf-8", errors="replace")
            for n, line in enumerate(text.splitlines(), 1):
                if any(ord(c) < 32 and c not in ALLOWED for c in line):
                    bad.append(f"{p.relative_to(ROOT)}:{n}")
        self.assertEqual(bad, [], "control characters (a lost backslash?) in: " + ", ".join(bad[:20]))

    def test_data_json(self):
        bad = []
        for p in (ROOT / "data").glob("*.json"):
            for s in _strings(json.loads(p.read_text(encoding="utf-8"))):
                if any(ord(c) < 32 and c not in ALLOWED for c in s):
                    bad.append(f"{p.relative_to(ROOT)}: {s[:60]!r}")
        self.assertEqual(bad, [], "control characters in JSON values: " + ", ".join(bad[:20]))


if __name__ == "__main__":
    unittest.main()
