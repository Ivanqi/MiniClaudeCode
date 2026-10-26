"""队友安全测试:名称校验、团队上限、线程失败可见与任务释放。"""
import tempfile
import unittest
from pathlib import Path

from src.ActiveTeammates import ActiveTeammates
from src.Configs import Configs
from src.LlmClient import LlmResponse, ToolCall
from src.MessageBus import MessageBus
from src.TaskSystem import TaskSystem
from src.TeammateSystem import TeammateSystem


class FailingClient:
    """第一条回复 claim_task;后续调用抛异常,验证 failed 通知。"""

    def __init__(self, first: LlmResponse):
        self.first = first
        self.calls = 0
        self.fallback_model = None

    def create(self, messages, system=None, tools=None, max_tokens=4096,
               model=None):
        self.calls += 1
        if self.calls == 1:
            return self.first
        raise RuntimeError("model down")


class TestTeammateSafety(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._old = Configs.WORKDIR
        Configs.WORKDIR = self.tmp
        self.tasks = TaskSystem(tasks_dir=self.tmp / "tasks")
        self.bus = MessageBus(mailbox_dir=self.tmp / "mail")
        self.team = ActiveTeammates()

    def tearDown(self):
        Configs.WORKDIR = self._old

    def build(self, client):
        return TeammateSystem(
            client, bus=self.bus, tasks=self.tasks, teammates=self.team)

    def test_invalid_name_and_team_cap(self):
        system = self.build(FailingClient(
            LlmResponse(content="", tool_calls=[])))
        self.assertIn("Error", system.spawn("../evil", "r", "p"))
        self.assertIn("reserved", system.spawn("lead", "r", "p"))
        for index in range(Configs.MAX_TEAMMATES):
            self.team.add(f"worker-{index}")
        self.assertIn("max active teammates", system.spawn("worker-x", "r", "p"))

    def test_model_failure_sends_failed_result_and_releases_task(self):
        task = self.tasks.create_task("do work")
        first = LlmResponse(
            content="",
            tool_calls=[ToolCall(id="c1", name="claim_task",
                                 arguments={"task_id": task.id})],
            finish_reason="tool_calls")
        system = self.build(FailingClient(first))
        self.team.add("worker")
        system._run_teammate("worker", "tester", "do the task")

        inbox = self.bus.read_inbox("lead")
        self.assertEqual(len(inbox), 1)
        result = inbox[0]
        self.assertEqual(result["type"], "result")
        self.assertEqual(result["metadata"]["status"], "failed")
        self.assertIn("model down", result["metadata"]["error"])
        self.assertFalse(self.team.contains("worker"))

        reloaded = self.tasks.load_task(task.id)
        self.assertEqual(reloaded.status, "pending")
        self.assertIsNone(reloaded.owner)


if __name__ == "__main__":
    unittest.main()
