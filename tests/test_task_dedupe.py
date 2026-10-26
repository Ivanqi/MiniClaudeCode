"""任务指纹去重与 claim token fencing 测试。"""
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from src.Configs import Configs
from src.TaskSystem import TaskSystem, canonical_fingerprint


class TestTaskDedupe(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.tasks = TaskSystem(tasks_dir=self.tmp)

    def test_exact_fingerprint_reuses(self):
        first = self.tasks.create_task_result(
            "implement login", scope="auth",
            target_files=["src/auth.py"], acceptance="tests pass")
        second = self.tasks.create_task_result(
            "implement login", scope="auth",
            target_files=["src/auth.py"], acceptance="tests pass")
        self.assertTrue(first.created)
        self.assertTrue(second.reused)
        self.assertEqual(second.reason, "fingerprint")
        self.assertEqual(second.task.id, first.task.id)
        self.assertEqual(len(self.tasks.list_tasks()), 1)

    def test_acceptance_is_part_of_fingerprint(self):
        self.tasks.create_task_result(
            "implement login", scope="auth",
            target_files=["src/auth.py"], acceptance="tests")
        result = self.tasks.create_task_result(
            "implement login", scope="auth",
            target_files=["src/auth.py"], acceptance="docs")
        self.assertTrue(result.created)
        self.assertNotEqual(result.task.acceptance, "")

    def test_fuzzy_warning_still_creates(self):
        first = self.tasks.create_task_result(
            "implement login page", scope="auth",
            target_files=["src/auth.py"], acceptance="tests")
        with mock.patch.object(Configs, "TASK_DEDUPE_FUZZY_THRESHOLD", 0.5):
            second = self.tasks.create_task_result(
                "implement login page", scope="auth",
                target_files=["src/auth.py"], acceptance="docs")
        self.assertTrue(second.created)
        self.assertTrue(second.similar)
        self.assertEqual(second.task.duplicate_of, first.task.id)
        self.assertGreater(second.task.dedupe_score, 0)

    def test_no_fuzzy_without_targets(self):
        self.tasks.create_task("implement login", scope="auth")
        result = self.tasks.create_task_result(
            "implement login", scope="auth", acceptance="other")
        self.assertEqual(result.similar, [])

    def test_completed_and_failed_not_reused(self):
        first = self.tasks.create_task("work", scope="s")
        self.tasks.claim_task(first.id)
        self.tasks.complete_task(first.id)
        result = self.tasks.create_task_result("work", scope="s")
        self.assertTrue(result.created)
        self.assertNotEqual(result.task.id, first.id)

        failed = self.tasks.create_task("other", scope="s")
        failed.status = "failed"
        self.tasks.save_task(failed)
        again = self.tasks.create_task_result("other", scope="s")
        self.assertTrue(again.created)

    def test_idempotency_precedence(self):
        first = self.tasks.create_task(
            "build", idempotency_key="build-1", scope="s")
        second = self.tasks.create_task(
            "build with different text", idempotency_key="build-1",
            scope="other")
        self.assertEqual(second.id, first.id)

    def test_claim_token_fencing(self):
        task = self.tasks.create_task("token")
        self.tasks.claim_task(task.id, owner="worker")
        token = self.tasks.load_task(task.id).claim_token
        self.assertTrue(token)
        wrong = self.tasks.complete_task(task.id, claim_token="wrong")
        self.assertIn("Lease lost", wrong)
        self.assertEqual(self.tasks.load_task(task.id).status, "in_progress")
        ok = self.tasks.complete_task(task.id, claim_token=token)
        self.assertIn("Completed", ok)
        self.assertIsNone(self.tasks.load_task(task.id).claim_token)

    def test_reclaim_clears_token_and_stale_owner_is_rejected(self):
        task = self.tasks.create_task("lease")
        self.tasks.claim_task(task.id, owner="worker")
        token = self.tasks.load_task(task.id).claim_token
        loaded = self.tasks.load_task(task.id)
        loaded.lease_until = 0
        self.tasks.save_task(loaded)
        self.assertEqual(self.tasks.reclaim_expired(), [task.id])
        self.assertIsNone(self.tasks.load_task(task.id).claim_token)
        self.assertIn("Lease lost",
                      self.tasks.fail_task(task.id, "late", claim_token=token))

    def test_live_owners_and_grace_are_skipped(self):
        task = self.tasks.create_task("lease")
        self.tasks.claim_task(task.id, owner="worker")
        loaded = self.tasks.load_task(task.id)
        loaded.lease_until = time.time() - 10
        self.tasks.save_task(loaded)
        self.assertEqual(
            self.tasks.reclaim_expired(live_owners=["worker"]), [])
        self.assertEqual(
            self.tasks.reclaim_expired(grace_seconds=60), [])
        self.assertEqual(self.tasks.reclaim_expired(), [task.id])

    def test_integration_task_cannot_be_claimed(self):
        task = self.tasks.create_task("merge", kind="integration")
        self.assertIn("kind 'integration'", self.tasks.claim_task(task.id))

    def test_fingerprint_is_stable(self):
        left = canonical_fingerprint("A", "s", ["b.py", "a.py"], ["t"], "ok")
        right = canonical_fingerprint("a", "S", ["a.py", "b.py"], ["t"], "OK")
        self.assertEqual(left, right)


if __name__ == "__main__":
    unittest.main()
