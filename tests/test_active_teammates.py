"""ActiveTeammates 元数据/心跳/清理测试。"""
import threading
import unittest

from src.ActiveTeammates import ActiveTeammates


class TestActiveTeammates(unittest.TestCase):
    def setUp(self):
        self.team = ActiveTeammates()

    def test_metadata_and_backward_compatible_names(self):
        self.team.add("worker", role="developer")
        self.assertEqual(self.team.names(), ["worker"])
        self.assertTrue(self.team.contains("worker"))
        info = self.team.get("worker")
        self.assertEqual(info.role, "developer")
        self.team.set_task("worker", "task_1", "token")
        self.assertEqual(self.team.get("worker").task_id, "task_1")
        self.team.set_status("worker", "running")
        self.assertEqual(self.team.get("worker").status, "running")
        self.team.clear_task("worker")
        self.assertIsNone(self.team.get("worker").task_id)
        self.team.touch("worker")
        snapshot = self.team.snapshot()
        self.assertEqual(snapshot[0]["name"], "worker")

    def test_stale_and_live(self):
        self.team.add("worker")
        info = self.team.get("worker")
        self.assertEqual(self.team.stale(0, now=info.last_seen), ["worker"])
        self.assertEqual(self.team.live_names(), ["worker"])

    def test_prune_dead_threads(self):
        thread = threading.Thread(target=lambda: None)
        thread.start()
        thread.join()
        self.team.add("dead", thread=thread)
        self.assertEqual(self.team.prune_dead(), ["dead"])
        self.assertFalse(self.team.contains("dead"))

    def test_remove(self):
        self.team.add("worker")
        self.team.remove("worker")
        self.assertEqual(self.team.names(), [])


if __name__ == "__main__":
    unittest.main()
