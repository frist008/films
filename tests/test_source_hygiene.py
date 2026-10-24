"""Source files must not contain control characters.

A regex written through a shell heredoc once lost its backslash: «\\b» became a backspace byte (0x08) in films/rank.py and
films/sources/trailer_lang.py, so «DV», «4K» and the AI-voice markers silently never matched (found 2026-10-25).
Run: python -X utf8 -m unittest tests.test_source_hygiene
"""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOWED = {"\n", "\r", "\t"}


class TestNoControlChars(unittest.TestCase):
    def test_sources(self):
        bad = []
        for p in list((ROOT / "films").rglob("*.py")) + list((ROOT / "films").rglob("*.js")) + list((ROOT / ".claude").rglob("*.py")) \
                + list((ROOT / ".claude").rglob("*.sh")) + list((ROOT / "config").glob("*.toml")):
            text = p.read_text(encoding="utf-8", errors="replace")
            for n, line in enumerate(text.splitlines(), 1):
                if any(ord(c) < 32 and c not in ALLOWED for c in line):
                    bad.append(f"{p.relative_to(ROOT)}:{n}")
        self.assertEqual(bad, [], "control characters (a lost backslash?) in: " + ", ".join(bad[:20]))


if __name__ == "__main__":
    unittest.main()
