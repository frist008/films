"""Watchdog: «no movement» is flagged after 10 minutes (owner, 2026-10-25: «Пиши сразу, 10 минут и 0 % движения — это
уже плохо»). Lines are the real ones of agent_update_full.log that evening."""
import time
import unittest

from films import watchdog

LOG = [
    "2026-10-25 23:05:15 PROGRESS 86% ETA 00:45 — trailer language check 252/378; then sheet rebuild + playlists",
    "2026-10-25 23:12:50 PROGRESS 87% ETA 01:00 — trailer language check 252/378 (alternative search on hard ones)",
    "2026-10-25 23:14:15 PROGRESS 87% ETA 01:00 — trailer language check 252/378 (alternative search on hard ones)",
]


def at(hms: str) -> float:
    return time.mktime(time.strptime(f"2026-10-25 {hms}", "%Y-%m-%d %H:%M:%S"))


class NoMovementTest(unittest.TestCase):
    def test_counter_still_for_ten_minutes(self):
        # the percent moved 86 → 87, the counter did not: still since 23:12:50 → at 23:23 it is 10 min
        self.assertIsNone(watchdog.no_movement_min(LOG, at("23:16:00")))
        self.assertEqual(watchdog.no_movement_min(LOG, at("23:23:00")), 10)

    def test_same_numbers_from_the_start(self):
        log = [LOG[1], LOG[2]]
        self.assertEqual(watchdog.no_movement_min(log, at("23:25:00")), 12)

    def test_moved(self):
        log = LOG + ["2026-10-25 23:22:09 PROGRESS 89% ETA 00:50 — trailer language check 299/378"]
        self.assertIsNone(watchdog.no_movement_min(log, at("23:26:00")))

    def test_eta_digits_ignored(self):
        log = ["2026-10-25 23:00:00 PROGRESS 50% ETA 00:45 — x 1/2", "2026-10-25 23:05:00 PROGRESS 50% ETA 01:10 — x 1/2"]
        self.assertEqual(watchdog.no_movement_min(log, at("23:11:00")), 11)


class FinishedTest(unittest.TestCase):
    def test_new_run_after_old_marker(self):
        # agent_rutracker.log, 2026-10-26: the old run's marker, then the new run's PROGRESS — the new run is not done
        log = ["2026-10-24 22:00:00 PROGRESS 100% — done", "AGENT_RUTRACKER_OK",
               "2026-10-26 00:45:06 PROGRESS 0% ETA ? — rutracker: ждёт запуска"]
        self.assertFalse(watchdog.finished(log, "agent_rutracker"))

    def test_marker_after_progress(self):
        log = ["2026-10-26 03:00:00 PROGRESS 100% — rutracker 4224/4224", "2026-10-26 03:00:00 AGENT_RUTRACKER_OK"]
        self.assertTrue(watchdog.finished(log, "agent_rutracker"))

    def test_marker_then_heartbeat_note(self):
        # agent_update_full.log, 23:55: marker in the middle of the STOPPED line, nothing after it but a non-PROGRESS note
        log = ["2026-10-25 23:54:11 PROGRESS 94% — sheet", "23:55:35 STOPPED by main AGENT_UPDATE_FULL_OK", "note"]
        self.assertTrue(watchdog.finished(log, "agent_update_full"))


class OwnerTableTest(unittest.TestCase):
    def test_table_rows(self):
        # owner, 2026-10-26: «таймеров которые проверяжют бекграунд агентов и сообщающих таблицей что они делают и сколько
        # всего осталось» — one row per agent: what, done, left, finishes, state
        import os
        import tempfile
        from pathlib import Path
        d = Path(tempfile.mkdtemp())
        now = time.time()
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - 60))
        (d / "agent_rutracker.log").write_text(f"{stamp} PROGRESS 3% ETA 21:24 — rutracker: 127/4224 запросов\n", encoding="utf-8")
        (d / "agent_old.log").write_text(f"{stamp} PROGRESS 100% — x\nAGENT_OLD_OK\n", encoding="utf-8")
        os.utime(d / "agent_old.log", (now - 3 * 3600, now - 3 * 3600))
        t = watchdog.owner_table(now, d)
        self.assertIn("| Кто | Что делает | Готово | Осталось | Закончит | Состояние |", t)
        self.assertIn("| rutracker | rutracker: 127/4224 запросов | 3% | 4097 из 4224 | 21:24 | работает |", t)
        self.assertNotIn("old", t)      # finished long ago — not shown


if __name__ == "__main__":
    unittest.main()
