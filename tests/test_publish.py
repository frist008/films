"""films/publish.py (owner, 2026-11-14: «Надо будет после пуша сделать так чтобы в гитхабе был один пуш и история потерта»):
every push is one root commit — GitHub's main and the tag have no history; only with the owner's «да»."""
import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from films import publish


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class PublishTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.repo, self.remote = root / "repo", root / "remote.git"
        git(root, "init", "-q", "--bare", str(self.remote))
        git(root, "init", "-q", "-b", "main", str(self.repo))
        git(self.repo, "config", "user.email", "t@example.com")
        git(self.repo, "config", "user.name", "t")
        git(self.repo, "remote", "add", "origin", str(self.remote))
        for i in range(2):   # an old history already on «GitHub»
            (self.repo / "old.txt").write_text(f"old structure {i}\n", encoding="utf-8")
            git(self.repo, "add", "-A")
            git(self.repo, "commit", "-qm", f"old {i}")
        git(self.repo, "tag", "v1.0.0")
        git(self.repo, "push", "-q", "origin", "main", "v1.0.0")
        self.permit = root / "git_permit.json"
        p = mock.patch.object(publish, "PERMIT", self.permit)
        p.start()
        self.addCleanup(p.stop)

    def allow(self):
        self.permit.write_text(json.dumps({"granted": True, "answer": "Да, коммитить и пушить", "at": time.time(),
                                           "head": git(self.repo, "rev-parse", "HEAD")}), encoding="utf-8")

    def test_github_gets_one_root_commit_and_the_tag(self):
        (self.repo / "old.txt").unlink()
        (self.repo / "new.txt").write_text("new structure\n", encoding="utf-8")
        self.allow()
        res = publish.publish("one root", cwd=self.repo)
        self.assertEqual(git(self.remote, "rev-list", "--count", "main"), "1")
        self.assertEqual(git(self.remote, "rev-list", "--parents", "-n1", "main").count(" "), 0)   # no parent
        self.assertEqual(git(self.remote, "rev-parse", "v1.0.0^{commit}"), git(self.remote, "rev-parse", "main"))
        self.assertEqual(git(self.remote, "ls-tree", "--name-only", "main"), "new.txt")
        self.assertEqual(res["commits"], 1)                                                      # local history gone too
        self.assertIn("Co-Authored-By", git(self.repo, "log", "-1", "--format=%B"))

    def test_only_with_a_fresh_unused_permission(self):
        with self.assertRaises(SystemExit):
            publish.publish("no answer", cwd=self.repo)
        self.allow()
        (self.repo / "a.txt").write_text("a\n", encoding="utf-8")
        publish.publish("first", cwd=self.repo)
        (self.repo / "b.txt").write_text("b\n", encoding="utf-8")
        with self.assertRaises(SystemExit):                    # one «да» — one commit with its push
            publish.publish("second", cwd=self.repo)
        self.assertEqual(git(self.remote, "ls-tree", "--name-only", "main").split(), ["a.txt", "old.txt"])


if __name__ == "__main__":
    unittest.main()
