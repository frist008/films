"""plan_guard.git_ask_needed и films.stage: про коммит и пуш спрашивать только в конце этапа (владелец, 2026-11-22:
«харе меня задрачивать с каждым изменением на комит пуш … у нас нельзя так часто комитить и пушить!»)."""
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from films import stage

spec = importlib.util.spec_from_file_location("plan_guard", Path(__file__).resolve().parents[1] / ".claude" / "hooks" / "plan_guard.py")
pg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pg)


class CommitOnlyPerStageTest(unittest.TestCase):
    def test_small_change_between_stages_not_asked(self):
        self.assertFalse(pg.git_ask_needed(True, "0", False, False))
        self.assertFalse(pg.git_ask_needed(False, "2", False, False))

    def test_closed_stage_asked_unless_refused(self):
        self.assertTrue(pg.git_ask_needed(True, "0", False, True))
        self.assertFalse(pg.git_ask_needed(True, "0", True, True))
        self.assertFalse(pg.git_ask_needed(False, "0", False, True))   # всё уже на GitHub

    def test_stage_done_marks_closed_and_publish_clears(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(stage, "PATH", Path(d) / "stage.json"), \
                 mock.patch.object(stage, "CLOSED", Path(d) / "stage_closed.json"), \
                 mock.patch("films.cleanup.check", return_value=[]):
                stage.start("тест")
                stage.done()
                self.assertTrue((Path(d) / "stage_closed.json").exists())
                stage.clear_closed()
                self.assertFalse((Path(d) / "stage_closed.json").exists())


if __name__ == "__main__":
    unittest.main()
