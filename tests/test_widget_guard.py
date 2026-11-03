"""Hook widget-guard.sh: a chat widget must not reference resources its sandbox blocks (owner, 2026-10-26: «хоть проверяй
что присылаешь» — TMDB photos showed as empty frames)."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

HOOK = Path(__file__).resolve().parents[1] / ".claude" / "hooks" / "widget-guard.sh"


def denied(code: str) -> bool:
    payload = {"tool_name": "mcp__visualize__show_widget", "tool_input": {"widget_code": code}}
    out = subprocess.run(["bash", str(HOOK)], input=json.dumps(payload, ensure_ascii=False),
                         capture_output=True, text=True, encoding="utf-8").stdout
    return '"deny"' in out


@unittest.skipUnless(shutil.which("bash"), "bash is needed for the hook")
class WidgetGuardTest(unittest.TestCase):
    def test_tmdb_photo_denied(self):
        # the exact case of 2026-10-26
        self.assertTrue(denied('<img src="https://image.tmdb.org/t/p/w185/wo2hJpn04vbtmh0B9utCFdsQhxM.jpg" alt="x">'))

    def test_local_path_denied(self):
        self.assertTrue(denied('<img src="1892.jpg">'))
        self.assertTrue(denied('<div style="background:url(file:///D:/x.jpg)"></div>'))

    def test_allowed(self):
        self.assertFalse(denied('<img src="data:image/jpeg;base64,AAAA">'))
        self.assertFalse(denied('<script src="https://cdnjs.cloudflare.com/ajax/libs/chart.js/4.4.1/chart.umd.min.js"></script>'))
        self.assertFalse(denied('<a href="https://www.imdb.com/title/tt0111161/">IMDb</a> <i class="ti ti-star"></i>'))


if __name__ == "__main__":
    unittest.main()
