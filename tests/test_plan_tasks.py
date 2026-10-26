"""TaskSystem plan_step 任务测试。"""
import json
import tempfile
import unittest
from pathlib import Path

from src.TaskSystem import TaskSystem


class TestPlanStepTasks(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.tasks = TaskSystem(tasks_dir=self.tmp)

    def make(self, step_id, deps=None, requires_approval=False):
        return self.tasks.create_plan_step_task(
            "plan_test", 1, step_id, f"step {step_id}",
            blocked_by=deps or [], acceptance="done",
            requires_approval=requires_approval, rollback="undo")

    def test_create_fields_and_claim_rejection(self):
        task = self.make("s1")
        self.assertEqual(task.kind, "plan_step")
        self.assertEqual(task.plan_id, "plan_test")
        self.assertEqual(task.step_id, "s1")
        self.assertEqual(task.plan_version, 1)
        self.assertTrue(task.requires_approval is False)
        self.assertEqual(task.rollback, "undo")
        self.assertIn("plan_step", self.tasks.claim_task(task.id))

    def test_dag_and_archive_aware_dependencies(self):
        first = self.make("s1")
        second = self.make("s2", deps=[first.id])
        ok, message = self.tasks.begin_step(second.id)
        self.assertFalse(ok)
        self.assertIn("dependencies", message)
        ok, token = self.tasks.begin_step(first.id)
        self.assertTrue(ok)
        self.assertTrue(token)
        self.assertIn("Completed", self.tasks.complete_step(
            first.id, {"status": "completed", "output": "a"},
            claim_token=token))
        ok, token = self.tasks.begin_step(second.id)
        self.assertTrue(ok)

    def test_claim_token_fencing_and_fields(self):
        task = self.make("s1")
        ok, token = self.tasks.begin_step(task.id)
        self.assertTrue(ok)
        self.assertIn("Lease lost", self.tasks.complete_step(
            task.id, {"output": "x"}, claim_token="wrong"))
        self.assertIn("Completed", self.tasks.complete_step(
            task.id, {"status": "completed", "output": "x"},
            evidence=["e1"], artifacts=["a1"], output_refs=["m1"],
            claim_token=token))
        loaded = self.tasks.load_task(task.id)
        self.assertEqual(loaded.status, "completed")
        self.assertEqual(loaded.step_result["output"], "x")
        self.assertEqual(loaded.evidence, ["e1"])
        self.assertEqual(loaded.artifacts, ["a1"])
        self.assertEqual(loaded.output_refs, ["m1"])
        self.assertFalse((self.tmp / f"{task.id}.json").exists())

    def test_fail_does_not_requeue_and_reclaim_skips_plan_steps(self):
        task = self.make("s1")
        ok, token = self.tasks.begin_step(task.id)
        self.tasks.fail_step(task.id, "boom", claim_token=token)
        loaded = self.tasks.load_task(task.id)
        self.assertEqual(loaded.status, "failed")
        self.assertEqual(loaded.last_error, "boom")
        self.assertEqual(loaded.rollback_status, "manual_required")

        other = self.make("s2")
        ok, token = self.tasks.begin_step(other.id)
        loaded = self.tasks.load_task(other.id)
        loaded.lease_until = 0
        self.tasks.save_task(loaded)
        self.assertEqual(self.tasks.reclaim_expired(), [])
        self.assertEqual(self.tasks.load_task(other.id).status, "in_progress")

    def test_abort_and_approval(self):
        task = self.make("s1")
        self.tasks.set_approval(task.id, "denied")
        self.assertEqual(self.tasks.load_task(task.id).approval_status,
                         "denied")
        self.assertIn("Aborted", self.tasks.abort_plan_step(task.id, "stop"))
        loaded = self.tasks.load_task(task.id)
        self.assertEqual(loaded.status, "failed")
        self.assertEqual(loaded.last_error, "stop")

    def test_idempotency_reuses_step_task(self):
        first = self.make("s1")
        second = self.make("s1")
        self.assertEqual(first.id, second.id)
        self.assertEqual(len([task for task in self.tasks.list_tasks(
            include_completed=True) if task.plan_id == "plan_test"]), 1)

    def test_legacy_task_json_defaults(self):
        legacy = {
            "id": "task_1_abc", "subject": "legacy", "description": "",
            "status": "pending", "owner": None, "blockedBy": [],
            "worktree": None,
        }
        (self.tmp / "task_1_abc.json").write_text(json.dumps(legacy))
        task = self.tasks.load_task("task_1_abc")
        self.assertEqual(task.plan_id, "")
        self.assertEqual(task.step_result, {})
        self.assertEqual(task.evidence, [])


if __name__ == "__main__":
    unittest.main()
