"""Merge queue 测试:真实临时 git 仓库,覆盖冲突/验收/人工门/回滚。"""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from src.ApprovalGate import ApprovalGate
from src.AuditLog import AuditLog
from src.Configs import Configs
from src.IntegrationSystem import IntegrationSystem
from src.LlmClient import ToolCall
from src.Sandbox import Sandbox
from src.TaskSystem import TaskSystem
from src.ToolDispatcher import ToolDispatcher
from src.ToolPolicy import lead_policy
from src.WorktreeSystem import WorktreeSystem


@unittest.skipIf(shutil.which("git") is None, "git unavailable")
class TestIntegrationSystem(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self._old = Configs.WORKDIR
        Configs.WORKDIR = self.root
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", cwd=self.repo)
        self.git("config", "user.email", "t@example.com", cwd=self.repo)
        self.git("config", "user.name", "T", cwd=self.repo)
        (self.repo / "shared.txt").write_text("base\n")
        self.git("add", ".", cwd=self.repo)
        self.git("commit", "-m", "base", cwd=self.repo)
        self.tasks = TaskSystem(tasks_dir=self.root / "tasks")
        self.worktrees = WorktreeSystem(
            tasks=self.tasks, worktrees_dir=self.root / "worktrees",
            repo_root=self.repo)
        self.dispatcher = self.make_dispatcher({"apply_integration"})
        self.integration = IntegrationSystem(
            tasks=self.tasks, worktrees=self.worktrees,
            dispatcher=self.dispatcher)

    def tearDown(self):
        Configs.WORKDIR = self._old

    @staticmethod
    def git(*args, cwd):
        result = subprocess.run(["git"] + list(args), cwd=cwd,
                                capture_output=True, text=True)
        if result.returncode != 0:
            raise AssertionError(result.stderr or result.stdout)
        return result.stdout.strip()

    def make_dispatcher(self, auto):
        return ToolDispatcher(
            hooks=None, audit=AuditLog(directory=self.root / "audit"),
            approval=ApprovalGate(interactive=False, auto_approve=set(auto)),
            sandbox=Sandbox(mode="off"))

    def make_source(self, name, files: dict):
        task = self.tasks.create_task(f"work {name}", scope="demo")
        self.worktrees.create_worktree(name, task_id=task.id)
        path = self.root / "worktrees" / name
        for filename, content in files.items():
            (path / filename).write_text(content)
        ok, detail, sha = self.worktrees.commit_worktree(name, task.id)
        self.assertTrue(ok, detail)
        self.tasks.claim_task(task.id, owner=name)
        self.tasks.complete_task(task.id)
        return task

    def plan(self, sources, acceptance):
        message = self.integration.plan_integration(
            [source.id for source in sources], acceptance)
        self.assertNotIn("Error", message, message)
        return next(task for task in self.tasks.list_tasks()
                    if task.kind == "integration" and task.status != "completed")

    def apply(self, task_id):
        call = ToolCall(id="apply", name="apply_integration",
                        arguments={"task_id": task_id})
        return self.dispatcher.execute(
            call, lead_policy(), self.integration.handlers())

    def test_preflight_and_apply_leave_main_untouched(self):
        left = self.make_source("left", {"a.txt": "a\n"})
        right = self.make_source("right", {"b.txt": "b\n"})
        integration = self.plan([left, right],
                                "test -f a.txt && test -f b.txt")
        preflight = self.integration.preflight_integration(integration.id)
        self.assertIn("verified", preflight, preflight)
        loaded = self.tasks.load_task(integration.id)
        self.assertEqual(loaded.merge_status, "preflight_verified")
        self.assertFalse((self.repo / "a.txt").exists())
        self.assertFalse((self.repo / "b.txt").exists())

        result = self.apply(integration.id)
        self.assertTrue(result.ok, result.content)
        self.assertIn("main untouched", result.content)
        self.assertFalse((self.repo / "a.txt").exists())
        branch = f"integration/{integration.id}"
        self.assertIn(branch, self.git("branch", "--list", cwd=self.repo))
        path = self.root / "worktrees" / f"integration-{integration.id}"
        self.assertTrue((path / "a.txt").exists())
        self.assertTrue((path / "b.txt").exists())
        done = self.tasks.load_task(integration.id)
        self.assertEqual(done.status, "completed")
        self.assertEqual(done.merge_status, "applied")
        self.assertEqual(done.approved_by, "auto")
        self.assertTrue(done.merged_commit)

    def test_conflict_blocks_apply(self):
        left = self.make_source("left", {"shared.txt": "left\n"})
        right = self.make_source("right", {"shared.txt": "right\n"})
        integration = self.plan([left, right], "true")
        result = self.integration.preflight_integration(integration.id)
        self.assertIn("conflict", result.lower())
        loaded = self.tasks.load_task(integration.id)
        self.assertEqual(loaded.merge_status, "preflight_failed")
        self.assertIn("shared.txt", loaded.conflict_files)
        applied = self.apply(integration.id)
        self.assertTrue(applied.denied or not applied.ok)
        self.assertFalse((self.repo / "shared.txt").read_text() != "base\n")

    def test_acceptance_failure_blocks_apply(self):
        source = self.make_source("left", {"a.txt": "a\n"})
        integration = self.plan([source], "false")
        result = self.integration.preflight_integration(integration.id)
        self.assertIn("failed", result.lower())
        loaded = self.tasks.load_task(integration.id)
        self.assertEqual(loaded.merge_status, "preflight_failed")
        self.assertEqual(loaded.preflight_exit_code, 1)

    def test_dirty_source_requires_seal(self):
        source = self.make_source("left", {"a.txt": "a\n"})
        integration = self.plan([source], "true")
        (self.root / "worktrees" / "left" / "dirty.txt").write_text("dirty\n")
        result = self.integration.preflight_integration(integration.id)
        self.assertIn("seal_worktree", result)

    def test_source_head_change_invalidates_preflight(self):
        source = self.make_source("left", {"a.txt": "a\n"})
        integration = self.plan([source], "true")
        self.assertIn("verified",
                      self.integration.preflight_integration(integration.id))
        path = self.root / "worktrees" / "left"
        (path / "later.txt").write_text("later\n")
        ok, detail, _ = self.worktrees.commit_worktree("left", source.id)
        self.assertTrue(ok, detail)
        result = self.apply(integration.id)
        self.assertTrue(result.denied or not result.ok)
        self.assertIn("changed", result.content.lower())

    def test_human_approval_denied(self):
        source = self.make_source("left", {"a.txt": "a\n"})
        integration = self.plan([source], "true")
        self.assertIn("verified",
                      self.integration.preflight_integration(integration.id))
        denied_system = IntegrationSystem(
            tasks=self.tasks, worktrees=self.worktrees,
            dispatcher=self.make_dispatcher(set()))
        call = ToolCall(id="apply", name="apply_integration",
                        arguments={"task_id": integration.id})
        result = denied_system.dispatcher.execute(
            call, lead_policy(), denied_system.handlers())
        self.assertTrue(result.denied)
        loaded = self.tasks.load_task(integration.id)
        self.assertEqual(loaded.merge_status, "preflight_verified")
        self.assertNotIn(f"integration/{integration.id}",
                         self.git("branch", "--list", cwd=self.repo))


if __name__ == "__main__":
    unittest.main()
