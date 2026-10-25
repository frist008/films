"""Hook youtube-guard.sh: browser work on YouTube only after the owner said it is free (owner, 2026-10-26: «ютуб пока занят …
если нужен будет ютуб, то спроси свободен ли он, запиши в скил и хук»)."""
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

HOOK = Path(__file__).resolve().parents[1] / ".claude" / "hooks" / "youtube-guard.sh"
Q = "1/1 · YouTube сейчас свободен? Нужен для плейлистов трейлеров"


@unittest.skipUnless(shutil.which("bash"), "bash is needed for the hook")
class YoutubeGuardTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.flag = Path(self.dir) / "data" / "cache" / "locks" / "youtube_free.json"

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def run_hook(self, mode, payload):
        env = dict(os.environ, CLAUDE_PROJECT_DIR=self.dir)
        return subprocess.run(["bash", str(HOOK), mode], input=json.dumps(payload, ensure_ascii=False),
                              capture_output=True, text=True, encoding="utf-8", env=env).stdout

    def denied(self, tool, **ti):
        return '"deny"' in self.run_hook("pre", {"tool_name": tool, "tool_input": ti})

    def answer(self, a, resp_as_text=False):
        resp = f'"{Q}"="{a}"' if resp_as_text else {"answers": {Q: a}}
        self.run_hook("post", {"tool_name": "AskUserQuestion", "tool_input": {"questions": [{"question": Q}]},
                               "tool_response": resp})

    def test_youtube_needs_answer(self):
        self.assertTrue(self.denied("mcp__claude-in-chrome__navigate", url="https://www.youtube.com/feed/playlists", tabId=5))
        self.assertTrue(self.denied("mcp__claude-in-chrome__javascript_tool", tabId=9, text="window.__plRun()"))

    def test_free_answer_allows_two_hours(self):
        self.answer("Да, свободен")
        self.assertFalse(self.denied("mcp__claude-in-chrome__navigate", url="https://www.youtube.com/", tabId=5))
        st = json.loads(self.flag.read_text(encoding="utf-8"))
        st["at"] = time.time() - 3 * 3600
        self.flag.write_text(json.dumps(st), encoding="utf-8")
        self.assertTrue(self.denied("mcp__claude-in-chrome__navigate", url="https://www.youtube.com/", tabId=5))

    def test_busy_answer_blocks(self):
        self.answer("Да, свободен")
        self.answer("Нет, занят", resp_as_text=True)
        self.assertTrue(self.denied("mcp__claude-in-chrome__navigate", url="https://youtube.com/", tabId=5))

    def test_tab_remembered(self):
        self.answer("Да, свободен")
        self.denied("mcp__claude-in-chrome__navigate", url="https://www.youtube.com/", tabId=7)
        self.flag.unlink()
        self.assertTrue(self.denied("mcp__claude-in-chrome__get_page_text", tabId=7))
        self.assertFalse(self.denied("mcp__claude-in-chrome__navigate", url="https://rutracker.org/forum/", tabId=7))
        self.assertFalse(self.denied("mcp__claude-in-chrome__get_page_text", tabId=7))

    def test_other_sites_free(self):
        self.assertFalse(self.denied("mcp__claude-in-chrome__navigate", url="https://rutracker.org/forum/index.php", tabId=3))
        self.assertFalse(self.denied("mcp__claude-in-chrome__javascript_tool", tabId=3, text="__rtStatus()"))
        self.assertFalse(self.denied("mcp__claude-in-chrome__navigate", url="https://tv.google.com/", tabId=4))

    def test_batch(self):
        acts = [{"name": "navigate", "input": {"tabId": 2, "url": "https://m.youtube.com/"}}]
        self.assertTrue(self.denied("mcp__claude-in-chrome__browser_batch", actions=acts))


if __name__ == "__main__":
    unittest.main()
