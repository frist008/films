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

    def test_previous_run_not_compared(self):
        # 2026-10-28: agent_trailers.log — the morning run's «100 %», its marker, then a new run → «НЕТ ДВИЖЕНИЯ 154 мин»
        log = LOG + ["AGENT_TRAILERS_OK", "PROGRESS 0% ETA ? — поиск трейлеров", "  25/2029 acc 2"]
        self.assertIsNone(watchdog.no_movement_min(log, at("23:59:00")))

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

    def test_crash_before_marker(self):
        # review 2026-10-28: agent_trailers2.log — `originals` crashed, the «;» chain still wrote the marker
        log = ["PROGRESS 100% — x", "AGENT_TRAILERS2_OK", "PROGRESS 85% — originals", "Traceback (most recent call last):",
               "AttributeError: 'int' object has no attribute 'strip'", "AGENT_TRAILERS2_OK"]
        self.assertTrue(watchdog.finished(log, "agent_trailers2"))
        self.assertTrue(watchdog.crashed(log))
        self.assertFalse(watchdog.crashed(log[:2] + ["PROGRESS 100% — y", "AGENT_TRAILERS2_OK"]))
        self.assertTrue(watchdog.finished(["PROGRESS 50% — z", "2026-10-28 15:40:00 AGENT_TRAILERS3_FAILED"], "agent_trailers3"))
        # review round 10: a note naming the marker is not one
        self.assertFalse(watchdog.crashed(["PROGRESS 76% — originals",
                                           "16:11 остановлен (AGENT_TRAILERS3_FAILED не ставлю: остановлен намеренно)"]))

    def test_marker_then_heartbeat_note(self):
        # agent_update_full.log, 23:55: marker in the middle of the STOPPED line, nothing after it but a non-PROGRESS note
        log = ["2026-10-25 23:54:11 PROGRESS 94% — sheet", "23:55:35 STOPPED by main AGENT_UPDATE_FULL_OK", "note"]
        self.assertTrue(watchdog.finished(log, "agent_update_full"))


class ReportLinesTest(unittest.TestCase):
    def test_process_line_by_start(self):
        # review 2026-10-28: «▶ 41,12846,…» — the tail of the trailer search command showed only ids
        cmd = r'"C:\Python312\python.exe" -X utf8 -m films.sources.youtube trailers --recheck-days 30 --ids ' + ",".join(
            str(i) for i in range(12000, 12100))
        s = watchdog.short_cmd(cmd)
        self.assertTrue(s.startswith("films.sources.youtube trailers --recheck-days 30"))
        self.assertLessEqual(len(s), 110)
        s = watchdog.short_cmd(r"C:\Users\x\AppData\Local\Microsoft\WindowsApps\python.exe -X utf8 "
                               "C:/Users/x/AppData/Local/Temp/claude/D--work-films/s/scratchpad/main_reparse_voice.py --all")
        self.assertEqual(s, "main_reparse_voice.py --all")

    def test_wait_ignores_an_old_runs_marker(self):
        # 2026-10-28: a chain waited with `grep -q AGENT_TRAILERS_OK` on the reused log — the morning marker started it early
        import tempfile
        from pathlib import Path
        from unittest import mock
        d = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, d, True)   # review 2026-10-28: the folders were left in %TEMP%
        f = d / "agent_trailers.log"
        f.write_text("PROGRESS 100% — x\nAGENT_TRAILERS_OK\nPROGRESS 0% ETA ? — поиск\n  25/2029 acc 2\n", encoding="utf-8")
        calls = []

        def tick(_):
            calls.append(1)
            f.write_text(f.read_text(encoding="utf-8") + "PROGRESS 100% — готово\nAGENT_TRAILERS_OK\n", encoding="utf-8")
        with mock.patch.object(watchdog.time, "sleep", side_effect=tick):
            watchdog.wait_finished("agent_trailers", logs=d)
        self.assertEqual(len(calls), 1)   # it waited for the new marker

    def test_bridge_silence_is_not_a_stall(self):
        self.assertIn("bridge", watchdog.QUIET_LOGS)


class OwnerTableTest(unittest.TestCase):
    def test_table_rows(self):
        # owner, 2026-10-26: «таймеров которые проверяжют бекграунд агентов и сообщающих таблицей что они делают и сколько
        # всего осталось» — one row per agent: what, done, left, finishes, state
        import os
        import tempfile
        from pathlib import Path
        d = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, d, True)   # review 2026-10-28: the folders were left in %TEMP%
        now = time.time()
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - 60))
        (d / "agent_rutracker.log").write_text(f"{stamp} PROGRESS 3% ETA 21:24 — rutracker: 127/4224 запросов\n", encoding="utf-8")
        (d / "agent_old.log").write_text(f"{stamp} PROGRESS 100% — x\nAGENT_OLD_OK\n", encoding="utf-8")
        os.utime(d / "agent_old.log", (now - 3 * 3600, now - 3 * 3600))
        t = watchdog.owner_table(now, d)
        self.assertIn("| Кто | Что делает | Готово | Осталось | Закончит | Состояние |", t)
        self.assertIn("| rutracker | rutracker: 127/4224 запросов | 3% | 4097 из 4224 | 21:24 | работает |", t)
        self.assertNotIn("old", t)      # finished long ago — not shown

    def test_counter_lines_without_progress(self):
        # 2026-10-28: the trailer search (old code) wrote only counters — the owner saw «0 %, ?» for hours
        import tempfile
        from pathlib import Path
        d = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, d, True)   # review 2026-10-28: the folders were left in %TEMP%
        (d / "agent_trailers.log").write_text("PROGRESS 0% ETA ? — поиск трейлеров\n  25/2029 acc 2 amb 4 nf 19 4 min\n"
                                              "  250/2029 acc 18 amb 33 nf 199 req 1073 cache 23 38 min\n", encoding="utf-8")
        t = watchdog.owner_table(time.time(), d)
        self.assertIn("| trailers | 250/2029 (строка счётчика, без PROGRESS) | 12% | 1779 из 2029 |", t)
        self.assertIsNone(watchdog.counter_after_progress(["  5/10 x", "2026-10-28 10:00:00 PROGRESS 50% — 5/10"]))
        # the forecast from the elapsed minutes (review 2026-10-28: «Закончит ?»): 250 in 38 min → 1779 more ≈ 270 min
        self.assertEqual(watchdog.counter_after_progress(["  250/2029 acc 18 req 1073 cache 23 38 min"]), (250, 2029, 38.0))
        self.assertNotIn("| ? | работает |", t)
        self.assertEqual(watchdog.counter_after_progress(["  pages 500/1050"]), (500, 1050, None))   # rutor pages
        # an old PROGRESS line + fresh counters is movement, not «НЕТ ДВИЖЕНИЯ» (2026-10-28, src_rutor_linked)
        old = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 3600))
        (d / "agent_src_x.log").write_text(f"{old} PROGRESS 0% ETA ? — rutor pages\n  pages 900/1050\n", encoding="utf-8")
        self.assertIn("| src_x | 900/1050 (строка счётчика, без PROGRESS) | 85% | 150 из 1050 | ? | работает |",
                      watchdog.owner_table(time.time(), d))


if __name__ == "__main__":
    unittest.main()
