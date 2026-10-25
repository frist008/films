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


if __name__ == "__main__":
    unittest.main()
