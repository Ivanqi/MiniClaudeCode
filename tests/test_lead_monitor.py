"""LeadMonitor 测试:不读邮箱、live owner 不回收、stale shutdown、启停。"""
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from src.ActiveTeammates import ActiveTeammates
from src.Configs import Configs
from src.LeadMonitor import LeadMonitor
from src.MessageBus import MessageBus
from src.ProtocolSystem import ProtocolSystem
from src.TaskSystem import TaskSystem
from src.TeamMetrics import TeamBudget, TeamMetrics


class TestLeadMonitor(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._old = Configs.WORKDIR
        Configs.WORKDIR = self.tmp
        self.bus = MessageBus(mailbox_dir=self.tmp / "mail")
        self.protocol = ProtocolSystem(bus=self.bus)
        self.tasks = TaskSystem(tasks_dir=self.tmp / "tasks")
        self.teammates = ActiveTeammates()
        self.metrics = TeamMetrics(budget=TeamBudget())
        self.monitor = LeadMonitor(
            protocol=self.protocol, tasks=self.tasks,
            teammates=self.teammates, bus=self.bus,
            metrics=self.metrics, interval=0.01)

    def tearDown(self):
        self.monitor.stop()
        Configs.WORKDIR = self._old

    def test_does_not_consume_lead_inbox(self):
        self.bus.send("worker", "lead", "message", "message")
        self.monitor.tick_once()
        messages = self.bus.read_inbox("lead")
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["content"], "message")

    def test_live_owner_is_not_reclaimed(self):
        task = self.tasks.create_task("work")
        self.tasks.claim_task(task.id, owner="worker")
        loaded = self.tasks.load_task(task.id)
        loaded.lease_until = 0
        self.tasks.save_task(loaded)
        self.teammates.add("worker")
        with mock.patch.object(Configs, "TEAM_RECLAIM_GRACE_SECONDS", 0):
            result = self.monitor.tick_once()
        self.assertEqual(result["reclaimed"], [])
        self.assertEqual(self.tasks.load_task(task.id).status, "in_progress")

        self.teammates.remove("worker")
        with mock.patch.object(Configs, "TEAM_RECLAIM_GRACE_SECONDS", 0):
            result = self.monitor.tick_once()
        self.assertIn(task.id, result["reclaimed"])
        self.assertEqual(self.tasks.load_task(task.id).status, "pending")

    def test_stale_teammate_gets_shutdown_not_killed(self):
        self.teammates.add("worker")
        info = self.teammates.get("worker")
        info.last_seen = time.monotonic() - 1000
        with mock.patch.object(Configs, "TEAMMATE_STALE_SECONDS", 1):
            result = self.monitor.tick_once()
        self.assertEqual(result["stale_shutdowns"], ["worker"])
        inbox = self.bus.read_inbox("worker")
        self.assertEqual(inbox[0]["type"], "shutdown_request")
        self.assertEqual(self.protocol.pending_count(), 1)
        # 只发请求,不杀线程/注册表
        self.assertTrue(self.teammates.contains("worker"))

    def test_start_stop(self):
        self.monitor.start()
        time.sleep(0.05)
        self.assertIsNotNone(self.monitor.last_tick)
        self.monitor.stop()
        self.assertIsNone(self.monitor._thread)
        self.assertTrue((self.tmp / ".team" / "metrics.json").exists())


if __name__ == "__main__":
    unittest.main()
