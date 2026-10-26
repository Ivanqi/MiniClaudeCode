"""ReviewSystem 测试:只读工具、结构化 verdict、blocked、in-flight。"""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from src.ApprovalGate import ApprovalGate
from src.AuditLog import AuditLog
from src.LlmClient import LlmResponse
from src.ReviewSystem import ReviewSystem
from src.Roles import RoleRegistry
from src.Sandbox import Sandbox
from src.SubagentSystem import SubagentSystem
from src.TaskSystem import TaskSystem
from src.TeamMetrics import TeamBudget, TeamMetrics
from src.ToolDispatcher import ToolDispatcher
from src.WorktreeSystem import WorktreeSystem


class FakeReviewer:
    def __init__(self, content):
        self.content = content
        self.calls = []
        self.fallback_model = None

    def create(self, messages, system=None, tools=None, max_tokens=4096,
               model=None):
        self.calls.append({"tool_names": [t["function"]["name"]
                                          for t in (tools or [])],
                           "system": system})
        return LlmResponse(content=self.content, tool_calls=[],
                           finish_reason="stop")


@unittest.skipIf(shutil.which("git") is None, "git unavailable")
class TestReviewSystem(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", cwd=self.repo)
        self.git("config", "user.email", "t@example.com", cwd=self.repo)
        self.git("config", "user.name", "T", cwd=self.repo)
        (self.repo / "base.txt").write_text("base\n")
        self.git("add", ".", cwd=self.repo)
        self.git("commit", "-m", "base", cwd=self.repo)
        self.tasks = TaskSystem(tasks_dir=self.root / "tasks")
        self.worktrees = WorktreeSystem(
            tasks=self.tasks, worktrees_dir=self.root / "worktrees",
            repo_root=self.repo)
        self.metrics = TeamMetrics(budget=TeamBudget())
        self.registry = RoleRegistry(path=self.root / "roles.json")
        self.task = self.tasks.create_task("feature", scope="demo",
                                           acceptance="file exists")
        self.worktrees.create_worktree("feat", task_id=self.task.id)
        (self.root / "worktrees" / "feat" / "feature.txt").write_text("x\n")
        self.tasks.claim_task(self.task.id, owner="worker")

    @staticmethod
    def git(*args, cwd):
        result = subprocess.run(["git"] + list(args), cwd=cwd,
                                capture_output=True, text=True)
        if result.returncode != 0:
            raise AssertionError(result.stderr or result.stdout)

    def build(self, content):
        client = FakeReviewer(content)
        dispatcher = ToolDispatcher(
            hooks=None, audit=AuditLog(directory=self.root / "audit"),
            approval=ApprovalGate(interactive=False, auto_approve=set()),
            sandbox=Sandbox(mode="off"))
        subagents = SubagentSystem(
            client, tasks=self.tasks, dispatcher=dispatcher,
            metrics=self.metrics)
        review = ReviewSystem(
            subagents, tasks=self.tasks, worktrees=self.worktrees,
            roles=self.registry, metrics=self.metrics)
        return review, client

    def test_pass_records_review_and_uses_readonly_tools(self):
        review, client = self.build(json.dumps({
            "verdict": "pass", "findings": ["looks good"],
            "evidence": ["feature.txt"]}))
        result = review.review_task(self.task.id)
        self.assertIn("Review pass", result)
        loaded = self.tasks.load_task(self.task.id)
        self.assertEqual(loaded.status, "in_progress")
        self.assertEqual(loaded.review_status, "pass")
        self.assertTrue(loaded.reviewed_head)
        self.assertEqual(set(client.calls[0]["tool_names"]),
                         {"read_file", "glob", "list_tasks"})
        self.assertIn("untrusted data", client.calls[0]["system"])

    def test_invalid_json_becomes_blocked(self):
        review, _ = self.build("not json at all")
        result = review.review_task(self.task.id)
        self.assertIn("blocked", result)
        self.assertEqual(self.tasks.load_task(self.task.id).review_status,
                         "blocked")

    def test_pass_without_evidence_becomes_blocked(self):
        review, _ = self.build(json.dumps({
            "verdict": "pass", "findings": ["ok"], "evidence": []}))
        review.review_task(self.task.id)
        self.assertEqual(self.tasks.load_task(self.task.id).review_status,
                         "blocked")

    def test_pending_or_missing_worktree_rejected(self):
        review, _ = self.build("{}")
        pending = self.tasks.create_task("pending")
        self.assertIn("pending", review.review_task(pending.id))
        bare = self.tasks.create_task("bare")
        self.tasks.claim_task(bare.id)
        self.assertIn("no worktree", review.review_task(bare.id))

    def test_in_flight_guard(self):
        review, _ = self.build(json.dumps({
            "verdict": "fail", "findings": ["x"], "evidence": ["base.txt"]}))
        ok, _ = self.tasks.begin_review(self.task.id)
        self.assertTrue(ok)
        result = review.review_task(self.task.id)
        self.assertIn("in progress", result)


if __name__ == "__main__":
    unittest.main()
