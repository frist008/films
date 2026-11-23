"""R324 (2026-11-13): «В прошлой синхронизации мы что-то не закончили и ты меня обманул что всё закончил?» — the 26–28.10
run closed with `prequels collect` never run. `films update --finish` now refuses while a step of the skill is missing."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from films import db, update


def mem_db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    db.migrate(c)
    c.execute("INSERT INTO runs (kind, started_at, summary) VALUES ('update_db', datetime('now'), '{\"status\": \"open\"}')")
    c.commit()
    return c


class StepsTest(unittest.TestCase):
    def test_finish_refuses_until_every_step(self):
        c = mem_db()
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            for s in update.STEPS:
                if s != "prequels":
                    update.step(c, s, out_dir=out)
            with self.assertRaises(SystemExit) as e:
                update.finish(c, out_dir=out)
            self.assertIn("prequels", str(e.exception))
            # a step left out on purpose needs the reason, and it is kept for the report
            r = update.step(c, "prequels", skip="владелец: не сегодня", out_dir=out)
            self.assertEqual(r["missing"], [])
            self.assertEqual(update.missing_steps(1, out), [])

    def test_unknown_step(self):
        c = mem_db()
        with tempfile.TemporaryDirectory() as d, self.assertRaises(SystemExit):
            update.step(c, "no_such_step", out_dir=Path(d))

    def test_chain_records_steps(self):
        """The chain script records every successful step (so --finish can check it)."""
        text = (db.ROOT / "films" / "update_chain.sh").read_text(encoding="utf-8")
        self.assertIn("films update --step", text)
        for name in ("prequels", "encountered", "trailers"):
            self.assertIn(f'"{name}|', text)
        # R327: ratings again after the steps that make new titles (the sheet refused them without ratings)
        names = [l.strip().strip('"').split("|")[0] for l in text.splitlines() if l.strip().startswith('"') and "|" in l]
        self.assertLess(names.index("match"), names.index("ratings_imdb2"))
        self.assertLess(names.index("ratings_rt2"), names.index("trailers"))


if __name__ == "__main__":
    unittest.main()
