"""Worktree 元数据/base_sha/seal 测试(真实临时 git 仓库)。"""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from src.TaskSystem import TaskSystem
from src.WorktreeSystem import WorktreeSystem


@unittest.skipIf(shutil.which("git") is None, "git unavailable")
class TestWorktreeMeta(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", cwd=self.repo)
        self.git("config", "user.email", "test@example.com", cwd=self.repo)
        self.git("config", "user.name", "Tester", cwd=self.repo)
        (self.repo / "base.txt").write_text("base\n")
        self.git("add", ".", cwd=self.repo)
        self.git("commit", "-m", "base", cwd=self.repo)
        self.tasks = TaskSystem(tasks_dir=self.root / "tasks")
        self.worktrees = WorktreeSystem(
            tasks=self.tasks, worktrees_dir=self.root / "worktrees",
            repo_root=self.repo)

    @staticmethod
    def git(*args, cwd):
        result = subprocess.run(["git"] + list(args), cwd=cwd,
                                capture_output=True, text=True)
        if result.returncode != 0:
            raise AssertionError(result.stderr or result.stdout)
        return result.stdout.strip()

    def test_create_records_base_and_seal_commits(self):
        task = self.tasks.create_task("feature")
        message = self.worktrees.create_worktree("feat", task_id=task.id)
        self.assertIn("created", message)
        path = self.root / "worktrees" / "feat"
        info = self.worktrees.worktree_info("feat")
        self.assertTrue(info["base_sha"])
        self.assertEqual(info["branch"], "wt/feat")
        self.assertEqual(self.tasks.load_task(task.id).base_sha,
                         info["base_sha"])
        self.assertTrue(self.worktrees.is_clean("feat"))
        self.assertEqual(self.worktrees.ahead_count("feat"), 0)

        (path / "feature.txt").write_text("feature\n")
        self.assertFalse(self.worktrees.is_clean("feat"))
        ok, detail, sha = self.worktrees.commit_worktree(
            "feat", task.id, "seal feature")
        self.assertTrue(ok, detail)
        self.assertTrue(sha)
        self.assertTrue(self.worktrees.is_clean("feat"))
        self.assertEqual(self.worktrees.head_sha("feat"), sha)
        self.assertEqual(self.worktrees.ahead_count("feat"), 1)
        # main 分支不受影响
        self.assertFalse((self.repo / "feature.txt").exists())

    def test_sensitive_file_is_not_sealed(self):
        task = self.tasks.create_task("secret")
        self.worktrees.create_worktree("secret", task_id=task.id)
        path = self.root / "worktrees" / "secret"
        (path / ".env").write_text("DEEPSEEK_API_KEY=sk-test-1234567890\n")
        ok, detail, sha = self.worktrees.commit_worktree(
            "secret", task.id, "seal secret")
        self.assertFalse(ok)
        self.assertIn("sensitive", detail)
        self.assertEqual(sha, "")
        self.assertTrue((path / ".env").exists())

    def test_commits_block_removal_and_discard_cleans_meta(self):
        task = self.tasks.create_task("feature")
        self.worktrees.create_worktree("feat", task_id=task.id)
        path = self.root / "worktrees" / "feat"
        (path / "x.txt").write_text("x\n")
        ok, detail, _ = self.worktrees.commit_worktree("feat", task.id)
        self.assertTrue(ok, detail)
        blocked = self.worktrees.remove_worktree("feat")
        self.assertIn("commit", blocked)
        removed = self.worktrees.remove_worktree("feat", discard_changes=True)
        self.assertIn("removed", removed)
        self.assertFalse(self.worktrees._meta_path("feat").exists())
        self.assertFalse(path.exists())

    def test_meta_name_is_reserved(self):
        self.assertIsNotNone(
            self.worktrees.validate_worktree_name(".meta"))


if __name__ == "__main__":
    unittest.main()
