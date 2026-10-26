"""Worktree 删除保护测试:有 in-progress 绑定任务时拒绝。"""
import tempfile
import unittest
from pathlib import Path

from src.TaskSystem import TaskSystem
from src.WorktreeSystem import WorktreeSystem


class TestWorktreeGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.worktrees_dir = self.tmp / "worktrees"
        self.tasks = TaskSystem(tasks_dir=self.tmp / "tasks")
        self.worktrees = WorktreeSystem(
            tasks=self.tasks, worktrees_dir=self.worktrees_dir)

    def test_in_progress_task_blocks_removal(self):
        (self.worktrees_dir / "feat").mkdir(parents=True)
        task = self.tasks.create_task("work")
        self.tasks.set_worktree(task.id, "feat")
        self.tasks.claim_task(task.id, owner="worker")
        result = self.worktrees.remove_worktree("feat")
        self.assertIn("in-progress", result)

    def test_completed_task_does_not_block(self):
        (self.worktrees_dir / "feat2").mkdir(parents=True)
        task = self.tasks.create_task("work")
        self.tasks.set_worktree(task.id, "feat2")
        self.tasks.claim_task(task.id, owner="worker")
        self.tasks.complete_task(task.id)
        # 任务已完成,保护逻辑不再拦截;后续 git 删除失败是另一回事,
        # 这里只断言错误信息不再是 in-progress 保护。
        result = self.worktrees.remove_worktree("feat2")
        self.assertNotIn("in-progress", result)


if __name__ == "__main__":
    unittest.main()
