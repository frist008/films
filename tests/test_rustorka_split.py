"""rustorka split (R348, владелец 2026-11-23: «Да, сделать с зеркалом»): задание — поровну на rustorka.com и
rustorka.net, разделы не пересекаются, полный список сохраняется для `resume`."""
import unittest

from films.sources import rustorka as R


class Split(unittest.TestCase):
    def test_halves_disjoint_and_complete(self):
        job = {"run": "X-r2", "base": "X", "onlyForums": [str(i) for i in range(7)], "extra": list("abcde"),
               "since": {}, "known": {}}
        a, b = R.split_job(job)
        self.assertEqual((a["run"], b["run"]), ("X-r2a", "X-r2b"))
        self.assertFalse(set(a["onlyForums"]) & set(b["onlyForums"]))
        self.assertEqual(sorted(a["onlyForums"] + b["onlyForums"], key=int), job["onlyForums"])
        self.assertEqual(sorted(a["extra"] + b["extra"]), job["extra"])
        self.assertEqual(a["onlyForumsAll"], job["onlyForums"])
        self.assertEqual(b["extraAll"], job["extra"])

    def test_no_forum_list_refused(self):
        with self.assertRaises(SystemExit):
            R.split_job({"run": "X", "extra": []})


if __name__ == "__main__":
    unittest.main()
