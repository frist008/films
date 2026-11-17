"""Russian names in Jellyfin (owner, 2026-11-01): films/browser/jellyfin_names.js renames only Movie / Series / Season,
refuses an episode or an item renamed since the plan, locks Name, drops the Trickplay DTO Jellyfin cannot read back, and
has no played-state call; films.sources.jellyfin_names plans from titles.title_ru / config and skips Cyrillic names."""
import json
import shutil
import sqlite3
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from films.sources import jellyfin_names as jn

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "films" / "browser" / "jellyfin_names.js"
NODE = shutil.which("node")

FAKE = """
const items = {
  m1: {Type: 'Movie', Name: 'FLOW', LockedFields: [], Trickplay: {x: 1}},
  e1: {Type: 'Episode', Name: 'Pilot', LockedFields: []},
  m2: {Type: 'Movie', Name: 'Other', LockedFields: []},
};
const sent = [];
global.window = global;
window.ApiClient = {
  getCurrentUser: async () => ({Name: 'owner', Id: 'u1'}),
  getItem: async (u, id) => JSON.parse(JSON.stringify(items[id])),
  updateItem: async (it) => { sent.push(it); const k = Object.keys(items).find(k => items[k].Type === it.Type);
                              items[k] = {...it}; },
};
"""


def run_js(tail: str) -> dict:
    src = FAKE + JS.read_text(encoding="utf-8") + tail
    out = subprocess.run([NODE, "-e", src], capture_output=True, text=True, encoding="utf-8", timeout=60)
    return json.loads(out.stdout.strip().splitlines()[-1])


@unittest.skipUnless(NODE, "node is needed for the JS script")
class NamesJsTest(unittest.TestCase):
    def test_rename_lock_refuse(self):
        r = run_js("""__jfNames.rename([
            {id: 'm1', kind: 'movie', old: 'FLOW', new: 'Поток'},
            {id: 'e1', kind: 'episode', old: 'Pilot', new: 'Пилот'},
            {id: 'm2', kind: 'movie', old: 'Changed', new: 'X'}], 'owner')
            .then(o => console.log(JSON.stringify({o, sent})));""")
        self.assertTrue(r["o"][0]["confirmed"])
        self.assertEqual(r["sent"][0]["LockedFields"], ["Name"])
        self.assertNotIn("Trickplay", r["sent"][0])
        self.assertIn("error", r["o"][1])                      # an episode is never renamed
        self.assertIn("error", r["o"][2])                      # renamed since the plan — left alone
        self.assertEqual(len(r["sent"]), 1)

    def test_wrong_user(self):
        r = run_js("__jfNames.rename([], 'someone').then(o => console.log(JSON.stringify(o)));")
        self.assertEqual(r["error"], "wrong user")

    def test_no_played_calls(self):
        self.assertNotRegex(JS.read_text(encoding="utf-8"), r"(?i)markPlayed|markUnplayed|PlayedItems|UserData")


class PlanTest(unittest.TestCase):
    def test_plan(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript("""CREATE TABLE titles (id INTEGER, title_ru TEXT);
            CREATE TABLE jellyfin_items (item_id TEXT, title_id INTEGER);
            INSERT INTO titles VALUES (1, 'Поток'), (2, 'Echo');
            INSERT INTO jellyfin_items VALUES ('a', 1), ('b', 2);""")
        items = [{"item_id": "a", "kind": "movie", "name": "FLOW"},
                 {"item_id": "b", "kind": "series", "name": "Echo"},
                 {"item_id": "c", "kind": "movie", "name": "Luna"},
                 {"item_id": "d", "kind": "episode", "name": "Pilot"},
                 {"item_id": "e", "kind": "season", "name": "Сезон 1"}]
        with mock.patch.object(jn, "_items", return_value=items), \
                mock.patch.object(jn, "_manual", return_value={"Luna": "Луна"}):
            p = jn.plan(conn)
        self.assertEqual([(x["item_id"], x["new"]) for x in p["rename"]], [("a", "Поток"), ("c", "Луна")])
        self.assertEqual([x["item_id"] for x in p["no_name"]], ["b"])   # Latin title_ru is not a Russian name


if __name__ == "__main__":
    unittest.main()
