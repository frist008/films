"""R330 (owner 2026-11-14: «Зачем нам проверять все данные если достаточно только изменённые?»):
the pre-push privacy check looks only at what is new since origin/main; the full pass runs when it must."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from films import db, privacy

SECRET = "SomeOwnerLogin77"


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class ChangedOnly(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.repo, remote = root / "repo", root / "remote.git"
        git(root, "init", "-q", "--bare", str(remote))
        git(root, "init", "-q", "-b", "main", str(self.repo))
        git(self.repo, "config", "user.email", "t@example.com")
        git(self.repo, "config", "user.name", "t")
        git(self.repo, "remote", "add", "origin", str(remote))
        # an old pushed file that already has the value (it would fail a full pass, the changed-only one skips it)
        (self.repo / "old.txt").write_text(f"pushed long ago {SECRET}\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "c1")
        git(self.repo, "push", "-q", "origin", "main")
        self.patches = [mock.patch.object(db, "ROOT", self.repo),
                        mock.patch.object(db, "DB_PATH", self.repo / "none.db"),
                        mock.patch.object(privacy, "STATE", self.repo / "state.json"),
                        mock.patch.object(privacy, "_private_values", lambda: [SECRET])]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_old_pushed_lines_are_not_rechecked(self):
        (self.repo / "old.txt").write_text(f"pushed long ago {SECRET}\nnew clean line\n", encoding="utf-8")
        git(self.repo, "commit", "-qam", "c2")
        self.assertEqual(privacy.check_changed(), [])
        self.assertTrue(privacy.check())          # the full pass still sees it

    def test_new_committed_line_is_caught(self):
        (self.repo / "new.txt").write_text(f"x\n{SECRET.lower()}\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "c2")
        self.assertEqual(len(privacy.check_changed()), 1)

    def test_uncommitted_and_untracked_are_caught(self):
        mail = "someone" + "@" + "gmail.com"   # built here: a literal address in this file would fail the real check
        (self.repo / "old.txt").write_text(f"pushed long ago {SECRET}\nmail me: {mail}\n", encoding="utf-8")
        (self.repo / "untracked.txt").write_text(f"{SECRET}\n", encoding="utf-8")
        probs = privacy.check_changed()
        self.assertTrue(any("old.txt: e-mail" in p for p in probs), probs)
        self.assertTrue(any(p.startswith("untracked.txt: private value") for p in probs), probs)

    def test_squashed_root_commit_compares_with_github_tree(self):
        (self.repo / "new.txt").write_text(f"{SECRET}\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        tree = subprocess.run(["git", "write-tree"], cwd=self.repo, capture_output=True, text=True, check=True).stdout.strip()
        root = subprocess.run(["git", "commit-tree", tree, "-m", "one root"], cwd=self.repo, capture_output=True, text=True,
                              check=True).stdout.strip()
        git(self.repo, "reset", "-q", "--soft", root)
        probs = privacy.check_changed()
        self.assertEqual(probs, ["new.txt: private value «Some…» (config/private.toml)"])   # old.txt is on GitHub already

    def test_frozen_xz_part_is_checked_inside(self):
        import lzma
        (self.repo / "part.jsonl.xz").write_bytes(lzma.compress(f'["{SECRET}"]\n'.encode()))   # R331 archive part
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "c2")
        self.assertTrue(any(p.startswith("part.jsonl.xz: private value") for p in privacy.check_changed()))
        self.assertTrue(any(p.startswith("part.jsonl.xz: private value") for p in privacy.check()))

    def test_full_pass_when_values_changed(self):
        self.assertIn("no clean full pass", privacy.full_needed())
        (self.repo / "old.txt").write_text("clean\n", encoding="utf-8")
        git(self.repo, "commit", "-qam", "clean")
        self.assertEqual(privacy.check(), [])     # a clean full pass records the searched values
        self.assertIsNone(privacy.full_needed())
        self.assertIn("private.toml changed", privacy.full_needed([SECRET, "AnotherValue1"]))


if __name__ == "__main__":
    unittest.main()
