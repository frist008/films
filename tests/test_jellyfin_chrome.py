"""Jellyfin «видел» through the Chrome tab (owner, 2026-10-26: no API key, «у тебя есть доступ в хром»):
films/browser/jellyfin_guard.js never re-marks a played item and has no un-mark; .claude/hooks/jellyfin-chrome-guard.sh
lets through only the guard text + one __jfGuard.mark call."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "films" / "browser" / "jellyfin_guard.js"
HOOK = ROOT / ".claude" / "hooks" / "jellyfin-chrome-guard.sh"
NODE = shutil.which("node")
ID_PLAYED, ID_NEW = "a" * 32, "b" * 32

FAKE = """
const state = {%(played)s: true, %(new)s: false}; const calls = [];
global.window = global;
window.ApiClient = {
  getCurrentUser: async () => ({Name: 'owner', Id: 'u1'}),
  getItem: async (u, id) => ({UserData: {Played: !!state[id]}}),
  markPlayed: async (u, id) => { calls.push(id); state[id] = true; },
};
""" % {"played": json.dumps(ID_PLAYED), "new": json.dumps(ID_NEW)}


def run_js(tail: str) -> dict:
    src = FAKE + JS.read_text(encoding="utf-8") + tail
    out = subprocess.run([NODE, "-e", src], capture_output=True, text=True, encoding="utf-8", timeout=60)
    return json.loads(out.stdout.strip().splitlines()[-1])


@unittest.skipUnless(NODE, "node is needed for the JS guard")
class GuardJsTest(unittest.TestCase):
    def test_played_item_skipped_new_marked(self):
        r = run_js(f"__jfGuard.mark([{json.dumps(ID_PLAYED)}, {json.dumps(ID_NEW)}], 'owner')"
                   ".then(o => console.log(JSON.stringify({o, calls})));")
        self.assertEqual(r["calls"], [ID_NEW])                       # the played one is never touched
        self.assertEqual(r["o"][0], {"item_id": ID_PLAYED, "already": True})
        self.assertEqual(r["o"][1], {"item_id": ID_NEW, "played": True})

    def test_wrong_user_writes_nothing(self):
        r = run_js(f"__jfGuard.mark([{json.dumps(ID_NEW)}], 'someone')"
                   ".then(o => console.log(JSON.stringify({o, calls})));")
        self.assertEqual((r["o"]["error"], r["calls"]), ("wrong user", []))

    def test_no_unmark_in_guard(self):
        self.assertNotRegex(JS.read_text(encoding="utf-8"), r"(?i)unplayed|markUnplayed|DELETE")


def hook_denies(tool: str, **ti) -> bool:
    payload = json.dumps({"tool_name": tool, "tool_input": ti}, ensure_ascii=False)
    out = subprocess.run(["bash", str(HOOK)], input=payload, capture_output=True, text=True, encoding="utf-8",
                         cwd=ROOT).stdout
    return '"deny"' in out


@unittest.skipUnless(shutil.which("bash"), "bash is needed for the hook")
class HookTest(unittest.TestCase):
    JT = "mcp__claude-in-chrome__javascript_tool"

    def test_guard_call_allowed(self):
        text = JS.read_text(encoding="utf-8") + f'\n__jfGuard.mark(["{ID_NEW}"], "owner");\n'
        self.assertFalse(hook_denies(self.JT, action="javascript_exec", text=text))

    def test_several_titles_one_call_allowed(self):
        # R274 / review 2026-10-28: a season or a list of films is one call — one confirmation, not one per item
        from films.sources import jellyfin_chrome
        from unittest import mock
        with mock.patch.object(jellyfin_chrome.db, "private", return_value={"jellyfin": {"user": "owner"}}):
            text = jellyfin_chrome.code([ID_NEW, ID_PLAYED, "c" * 32])
        self.assertEqual(text[len(JS.read_text(encoding="utf-8")):].count("__jfGuard.mark("), 1)
        self.assertFalse(hook_denies(self.JT, action="javascript_exec", text=text))

    def test_raw_mark_denied(self):
        self.assertTrue(hook_denies(self.JT, text=f"ApiClient.markPlayed(u, '{ID_NEW}', new Date())"))

    def test_guard_plus_extra_denied(self):
        text = JS.read_text(encoding="utf-8") + f'\n__jfGuard.mark(["{ID_NEW}"], "owner"); ApiClient.markPlayed(1,2)'
        self.assertTrue(hook_denies(self.JT, text=text))

    def test_unmark_denied(self):
        self.assertTrue(hook_denies(self.JT, text="ApiClient.markUnplayed(u, id)"))

    def test_other_js_untouched(self):
        self.assertFalse(hook_denies(self.JT, text="document.title"))

    def test_edit_guard_denied(self):
        self.assertTrue(hook_denies("Edit", file_path=str(JS), old_string="a", new_string="b"))


if __name__ == "__main__":
    unittest.main()
