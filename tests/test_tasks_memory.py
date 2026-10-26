"""任务记忆测试:归档、依赖、idempotency、看板摘要。"""
import json
import tempfile
import threading
import unittest
from pathlib import Path

from src.TaskSystem import TaskSystem


class TestTaskMemory(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.tasks = TaskSystem(tasks_dir=self.tmp)

    def test_completed_task_moves_to_archive_and_still_loads(self):
        task = self.tasks.create_task("work")
        self.tasks.claim_task(task.id, owner="worker")
        self.tasks.complete_task(task.id)
        self.assertFalse((self.tmp / f"{task.id}.json").exists())
        archived = self.tmp / "archive" / f"{task.id}.json"
        self.assertTrue(archived.exists())
        loaded = self.tasks.load_task(task.id)
        self.assertEqual(loaded.status, "completed")
        self.assertIsNotNone(loaded.completed_at)
        self.assertIn(task.id, self.tasks.get_task_json(task.id))

    def test_list_defaults_to_active_only(self):
        active = self.tasks.create_task("active")
        done = self.tasks.create_task("done")
        self.tasks.claim_task(done.id, owner="w")
        self.tasks.complete_task(done.id)
        self.assertEqual([task.id for task in self.tasks.list_tasks()],
                         [active.id])
        both = self.tasks.list_tasks(include_completed=True)
        self.assertEqual({task.id for task in both}, {active.id, done.id})

    def test_archived_dependency_unblocks(self):
        dep = self.tasks.create_task("dep")
        main = self.tasks.create_task("main", blockedBy=[dep.id])
        self.tasks.claim_task(dep.id)
        self.tasks.complete_task(dep.id)
        self.assertTrue(self.tasks.can_start(main.id))
        claim = self.tasks.claim_task(main.id)
        self.assertIn("Claimed", claim)

    def test_idempotency_key_returns_existing(self):
        first = self.tasks.create_task("build", idempotency_key="build-42")
        second = self.tasks.create_task("build again",
                                        idempotency_key="build-42")
        self.assertEqual(first.id, second.id)
        self.assertEqual(len(self.tasks.list_tasks()), 1)
        self.tasks.claim_task(first.id)
        self.tasks.complete_task(first.id)
        third = self.tasks.create_task("build later",
                                       idempotency_key="build-42")
        self.assertEqual(third.id, first.id)
        self.assertEqual(third.status, "completed")

    def test_concurrent_idempotent_create_one_task(self):
        barrier = threading.Barrier(2)
        ids = []

        def worker():
            barrier.wait()
            ids.append(self.tasks.create_task(
                "race", idempotency_key="same-key").id)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(len(self.tasks.list_tasks()), 1)

    def test_board_summary(self):
        active = self.tasks.create_task("investigate")
        done = self.tasks.create_task("finished")
        self.tasks.claim_task(done.id)
        self.tasks.complete_task(done.id)
        summary = self.tasks.board_summary()
        self.assertIn(active.id, summary)
        self.assertIn(done.id, summary)
        self.assertIn("Recently completed", summary)

    def test_legacy_task_json_without_new_fields(self):
        legacy = {
            "id": "task_1_abc", "subject": "legacy", "description": "",
            "status": "pending", "owner": None,
            "blockedBy": [], "worktree": None,
        }
        (self.tmp / "task_1_abc.json").write_text(json.dumps(legacy))
        task = self.tasks.load_task("task_1_abc")
        self.assertEqual(task.subject, "legacy")
        self.assertIsNone(task.idempotency_key)
        self.assertIn("task_1_abc", [t.id for t in self.tasks.list_tasks()])

    def test_archive_completed_migrates_active_records(self):
        task = self.tasks.create_task("old")
        task.status = "completed"
        self.tasks.save_task(task)  # save_task routes completed to archive
        self.assertTrue((self.tmp / "archive" / f"{task.id}.json").exists())
        self.assertFalse((self.tmp / f"{task.id}.json").exists())


if __name__ == "__main__":
    unittest.main()
