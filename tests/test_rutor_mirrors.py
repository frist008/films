"""rutor через зеркала (R338, владелец 2026-11-23: «всегда можешь через зеркала … через зеркала ещё можно параллельно
работать чтобы было быстрее»): адрес в базе — rutor.info, запрос — через первое живое зеркало; лежащее пропускается."""
import os
import unittest
from unittest import mock

import requests

from films.sources import rutor

MS = ["http://rutor.info", "http://rutor.is", "http://new-rutor.org"]


class Resp:
    status_code = 200
    encoding = None
    text = "<html>ok</html>"


class MirrorTest(unittest.TestCase):
    def setUp(self):
        rutor._dead.clear()
        self.p = [mock.patch.object(rutor, "settings", return_value={"rutor": {"mirrors": MS}}),
                  mock.patch.object(rutor, "DELAY", 0), mock.patch.object(rutor.time, "sleep")]
        for p in self.p:
            p.start()

    def tearDown(self):
        for p in self.p:
            p.stop()
        rutor._dead.clear()

    def test_dead_mirror_skipped_to_next(self):
        calls = []

        def fake(url, timeout):
            calls.append(url)
            if url.startswith(("http://rutor.info", "http://rutor.is")):
                raise requests.ConnectionError("timed out")
            return Resp()
        with mock.patch.object(rutor._session, "get", side_effect=fake):
            self.assertEqual(rutor.get("http://rutor.info/torrent/1114162"), "<html>ok</html>")
            self.assertEqual(calls[-1], "http://new-rutor.org/torrent/1114162")
            calls.clear()
            rutor.get("http://rutor.info/torrent/1")   # лежащие помечены — сразу на живое
            self.assertEqual(calls, ["http://new-rutor.org/torrent/1"])

    def test_pinned_mirror_first(self):
        with mock.patch.dict(os.environ, {"RUTOR_MIRROR": "2"}):
            self.assertEqual(rutor.mirrors()[0], "http://new-rutor.org")
        self.assertNotIn("http://rutor.org", rutor.mirrors())   # платный сайт другого владельца — не зеркало

    def test_mirror_month_name(self):
        self.assertEqual(rutor.parse_date("01&nbsp;Мая&nbsp;26").month, 5)


if __name__ == "__main__":
    unittest.main()
