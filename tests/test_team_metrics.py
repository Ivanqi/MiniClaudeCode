"""团队预算与指标测试。"""
import json
import tempfile
import unittest
from pathlib import Path

from src.TeamMetrics import TeamBudget, TeamMetrics


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class TestTeamMetrics(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.budget = TeamBudget(
            max_tokens=100, max_spawns=2, max_tasks=3,
            max_wall_seconds=60)
        self.metrics = TeamMetrics(budget=self.budget, clock=self.clock)

    def test_spawn_reserve_and_rollback(self):
        self.assertTrue(self.metrics.reserve_spawn())
        self.assertTrue(self.metrics.reserve_spawn())
        self.assertFalse(self.metrics.reserve_spawn())
        self.metrics.rollback_spawn()
        self.assertTrue(self.metrics.reserve_spawn())

    def test_task_reserve_and_wall(self):
        self.assertTrue(self.metrics.reserve_task())
        self.assertTrue(self.metrics.reserve_task())
        self.clock.now += 61
        ok, reason = self.metrics.can_claim()
        self.assertFalse(ok)
        self.assertIn("wall", reason)

    def test_token_budget(self):
        self.metrics.add_usage({"total_tokens": 60}, source="teammate")
        self.metrics.add_usage({"prompt_tokens": 30,
                                "completion_tokens": 20}, source="reviewer")
        self.assertEqual(self.budget.tokens, 110)
        ok, reason = self.metrics.can_spawn()
        self.assertFalse(ok)
        self.assertIn("token", reason)
        snapshot = self.metrics.snapshot()
        self.assertEqual(snapshot["counters"]["tokens"], 110)
        self.assertEqual(snapshot["counters"]["tokens.teammate"], 60)

    def test_snapshot_and_status(self):
        self.metrics.record("task_claimed")
        self.metrics.record("task_completed", 2)
        snapshot = self.metrics.snapshot()
        self.assertEqual(snapshot["counters"]["task_claimed"], 1)
        self.assertEqual(snapshot["counters"]["task_completed"], 2)
        self.assertIn("Team session", self.metrics.status_text())

    def test_flush_is_lazy_and_atomic(self):
        target = Path(tempfile.mkdtemp()) / "team" / "metrics.json"
        self.assertFalse(target.exists())
        self.metrics.record("teammate_spawned")
        self.assertEqual(self.metrics.flush(target), target)
        self.assertTrue(target.exists())
        data = json.loads(target.read_text())
        self.assertEqual(data["counters"]["teammate_spawned"], 1)
        self.assertIn("budget", data)

    def test_reset(self):
        self.metrics.record("x")
        self.metrics.add_usage({"total_tokens": 10})
        self.metrics.reset()
        self.assertEqual(self.metrics.snapshot()["counters"], {})
        self.assertEqual(self.metrics.budget.tokens, 0)


if __name__ == "__main__":
    unittest.main()
