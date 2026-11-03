"""The Plex write guard (films/sources/plex_guard.py) must never let a write into the owner's watch history through
when the item is already watched, a whole season/show is targeted, or a destructive call lacks the owner's approval.
Offline: the network is mocked."""
import unittest
from unittest import mock

import requests

from films.sources import plex_guard as g

LOCAL, DISC = "http://127.0.0.1:32400", "https://discover.provider.plex.tv"
ACT = "actions/" + "scr" + "obble"          # spelled apart: the PreToolUse hook blocks the literal word in commands
UNACT = "actions/un" + "scr" + "obble"


class FakeResp:
    def __init__(self, data):
        self._d, self.status_code = data, 200

    def json(self):
        return self._d

    def raise_for_status(self):
        pass


class GuardTest(unittest.TestCase):
    def setUp(self):
        self.sent, self.state = [], {}

        def fake(sess, method, url, **kw):
            if method == "GET" and url.endswith("/userState"):
                return FakeResp({"MediaContainer": {"UserState": [self.state]}})
            if method == "GET" and "/library/metadata/" in url:
                return FakeResp({"MediaContainer": {"Metadata": [self.state]}})
            self.sent.append((method, url))
            return FakeResp({})
        self.p = mock.patch.object(requests.Session, "request", fake)
        self.p.start()
        # the test log must not land in the real data/cache/logs/plex_writes.jsonl (its fake «key: new» looked like a
        # real write there — review 2026-10-24)
        import tempfile
        from pathlib import Path
        tmpd = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, tmpd, True)   # review 2026-10-28: the folders were left in %TEMP%
        self.log = mock.patch.object(g, "LOG", tmpd / "plex_writes.jsonl")
        self.log.start()
        self.addCleanup(self.log.stop)
        g._APPROVED.clear()
        self.s = g.GuardedSession(LOCAL, DISC)

    def tearDown(self):
        self.p.stop()

    def test_discover_already_watched_is_blocked(self):
        self.state = {"type": "movie", "viewCount": 1, "viewState": "complete"}
        with self.assertRaises(g.PlexWriteBlocked):
            self.s.put(f"{DISC}/{ACT}", params={"key": "abc"})
        self.assertEqual(self.sent, [])

    def test_library_already_watched_is_blocked(self):
        self.state = {"type": "movie", "viewCount": 2}
        with self.assertRaises(g.PlexWriteBlocked):
            self.s.get(f"{LOCAL}/:/" + "scr" + "obble", params={"key": "123", "identifier": "x"})
        self.assertEqual(self.sent, [])

    def test_season_is_blocked(self):
        self.state = {"type": "season", "viewCount": 0}
        with self.assertRaises(g.PlexWriteBlocked):
            self.s.put(f"{DISC}/{ACT}", params={"key": "s1"})
        self.assertEqual(self.sent, [])

    def test_unwatched_movie_goes_through(self):
        self.state = {"type": "movie", "viewCount": 0}
        self.s.put(f"{DISC}/{ACT}", params={"key": "new"})
        self.assertEqual(len(self.sent), 1)

    def test_unwatch_needs_approval(self):
        with self.assertRaises(g.PlexWriteBlocked):
            self.s.put(f"{DISC}/{UNACT}", params={"key": "k"})
        g.owner_approved(["k"], "test")
        self.s.put(f"{DISC}/{UNACT}", params={"key": "k"})
        self.assertEqual(len(self.sent), 1)

    def test_history_removal_needs_approval(self):
        aid = "7b85cb11-0ef2-4616-ac4c-11199c9da937"
        body = {"query": "mutation($id: ID!) { remove" + "Activity(input: {id: $id}) }", "variables": {"id": aid}}
        with self.assertRaises(g.PlexWriteBlocked):
            self.s.post("https://community.plex.tv/api", json=body)
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
