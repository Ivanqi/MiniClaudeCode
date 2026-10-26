"""任务状态原子性测试:并发认领、租约回收、失败/释放。"""
import tempfile
import threading
import time
import unittest
from pathlib import Path

from src.Configs import Configs
from src.TaskSystem import TaskSystem


class TestTaskConcurrency(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.tasks = TaskSystem(tasks_dir=self.tmp)

    def _expire(self, task_id):
        task = self.tasks.load_task(task_id)
        task.lease_until = time.time() - 1
        self.tasks.save_task(task)

    def test_two_threads_only_one_claims(self):
        task = self.tasks.create_task("race")
        barrier = threading.Barrier(2)
        results = []

        def worker(name):
            barrier.wait()
            results.append(self.tasks.claim_task(task.id, owner=name))

        threads = [threading.Thread(target=worker, args=(f"w{i}",))
                   for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        claimed = [result for result in results
                   if result.startswith("Claimed")]
        self.assertEqual(len(claimed), 1, results)
        loaded = self.tasks.load_task(task.id)
        self.assertEqual(loaded.status, "in_progress")
        self.assertIn(loaded.owner, {"w0", "w1"})

    def test_expired_lease_requeues_then_fails_after_max_attempts(self):
        task = self.tasks.create_task("lease")
        for attempt in range(1, Configs.TASK_MAX_ATTEMPTS + 1):
            self.tasks.claim_task(task.id, owner="worker")
            self._expire(task.id)
            reclaimed = self.tasks.reclaim_expired()
            self.assertIn(task.id, reclaimed)
            loaded = self.tasks.load_task(task.id)
            self.assertEqual(loaded.attempts, attempt)
            self.assertIsNone(loaded.owner)
            if attempt < Configs.TASK_MAX_ATTEMPTS:
                self.assertEqual(loaded.status, "pending")
            else:
                self.assertEqual(loaded.status, "failed")

    def test_fail_and_release(self):
        task = self.tasks.create_task("lifecycle")
        self.tasks.claim_task(task.id, owner="worker")
        self.assertIn("Requeued", self.tasks.fail_task(task.id, "boom"))
        loaded = self.tasks.load_task(task.id)
        self.assertEqual(loaded.status, "pending")
        self.assertEqual(loaded.attempts, 1)
        self.assertEqual(loaded.last_error, "boom")

        self.tasks.claim_task(task.id, owner="worker")
        self.assertIn("Released", self.tasks.release_task(task.id))
        self.assertEqual(self.tasks.load_task(task.id).status, "pending")

    def test_invalid_task_id_rejected(self):
        with self.assertRaises(ValueError):
            self.tasks.load_task("../escape")
        with self.assertRaises(ValueError):
            self.tasks.claim_task("bad-id")

    def test_set_worktree(self):
        task = self.tasks.create_task("wt")
        self.tasks.set_worktree(task.id, "feat-x")
        self.assertEqual(self.tasks.load_task(task.id).worktree, "feat-x")

    def test_no_expired_leases(self):
        self.assertEqual(self.tasks.reclaim_expired(), [])
        self.assertIn("No expired", self.tasks.run_reclaim_tasks())


if __name__ == "__main__":
    unittest.main()
