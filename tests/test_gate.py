"""films/gate.py: the order is always «all pages of all sources + the owner's questions → the sheet → YouTube» (owner,
2026-10-26: «И так всегда. Навсегда. Ждать всегда все страницы и вопросы ко мне. Потом только таблица, а потом только
ютуб»)."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from films import gate


class GateTest(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.logs = self.d / "logs"
        self.logs.mkdir()
        self.plan = self.d / "PLAN.md"
        self.plan.write_text("## Входящие\n- [ ] R1. something\n", encoding="utf-8")
        self.ok = self.d / "gate_ok.json"

    def log(self, name, lines):
        (self.logs / f"{name}.log").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_collector_running_blocks_the_sheet(self):
        self.log("agent_src_kinozal", ["2026-10-26 12:00:00 PROGRESS 99% — kinozal: run 1 done", "AGENT_SRC_KINOZAL_OK",
                                       "2026-10-26 12:50:00 PROGRESS 0% — kinozal: run 2 topics 1/30000"])
        self.log("agent_rutracker", ["2026-10-26 09:43:00 PROGRESS 100% — done", "AGENT_RUTRACKER_OK"])
        st = gate.check(self.logs, self.plan)
        self.assertEqual((st["open"], st["collectors"]), (False, ["src_kinozal"]))
        with self.assertRaises(SystemExit):
            gate.require_db(self.logs, self.plan)

    def test_open_question_blocks_the_sheet(self):
        self.log("agent_src_toloka", ["x PROGRESS 100%", "AGENT_SRC_TOLOKA_OK"])
        self.plan.write_text("- [ ] R9. ❓ спросить владельца: расписание\n- [x] R8. ❓ answered\n", encoding="utf-8")
        st = gate.check(self.logs, self.plan)
        self.assertFalse(st["open"])
        self.assertEqual(len(st["questions"]), 1)

    def test_youtube_only_after_the_sheet(self):
        self.log("agent_src_toloka", ["x PROGRESS 100%", "AGENT_SRC_TOLOKA_OK"])
        self.assertTrue(gate.check(self.logs, self.plan)["open"])
        with self.assertRaises(SystemExit):                       # no sheet through the gate yet
            gate.require_sheet(self.logs, self.plan, self.ok)
        gate.record_ok(self.ok)
        gate.require_sheet(self.logs, self.plan, self.ok)         # sheet rebuilt after every collector: fine
        later = time.time() + 5                                   # a collector wrote after the sheet
        os.utime(self.logs / "agent_src_toloka.log", (later, later))
        with self.assertRaises(SystemExit):
            gate.require_sheet(self.logs, self.plan, self.ok)


if __name__ == "__main__":
    unittest.main()
