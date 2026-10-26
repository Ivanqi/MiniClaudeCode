"""队友安全测试:名称校验、团队上限、线程失败可见与任务释放。"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.ActiveTeammates import ActiveTeammates
from src.ApprovalGate import ApprovalGate
from src.AuditLog import AuditLog
from src.Configs import Configs
from src.LlmClient import LlmResponse, ToolCall
from src.MessageBus import MessageBus
from src.ProtocolSystem import ProtocolSystem
from src.Sandbox import Sandbox
from src.TaskSystem import TaskSystem
from src.TeammateSystem import TeammateSystem
from src.ToolDispatcher import ToolDispatcher


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
        self.assertIn("Error", system.spawn("../evil", "developer", "p"))
        self.assertIn("reserved", system.spawn("lead", "developer", "p"))
        for index in range(Configs.MAX_TEAMMATES):
            self.team.add(f"worker-{index}")
        self.assertIn("max active teammates", system.spawn("worker-x", "developer", "p"))

    def test_model_failure_sends_failed_result_and_releases_task(self):
        task = self.tasks.create_task("do work")
        first = LlmResponse(
            content="",
            tool_calls=[ToolCall(id="c1", name="claim_task",
                                 arguments={"task_id": task.id})],
            finish_reason="tool_calls")
        system = self.build(FailingClient(first))
        self.team.add("worker")
        system._run_teammate("worker", "developer", "do the task")

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

    def test_plan_timeout_releases_task_and_reports_failed(self):
        task = self.tasks.create_task("plan me")
        client = ScriptClient([
            LlmResponse(content="",
                        tool_calls=[ToolCall(id="c1", name="claim_task",
                                             arguments={"task_id": task.id})],
                        finish_reason="tool_calls"),
            LlmResponse(content="",
                        tool_calls=[ToolCall(id="c2", name="submit_plan",
                                             arguments={"plan": "do it"})],
                        finish_reason="tool_calls"),
        ])
        protocol = ProtocolSystem(bus=self.bus)
        dispatcher = ToolDispatcher(
            hooks=None, audit=AuditLog(directory=self.tmp / "audit"),
            approval=ApprovalGate(interactive=False, auto_approve=set()),
            sandbox=Sandbox(mode="off"))
        system = TeammateSystem(
            client, bus=self.bus, protocol=protocol, tasks=self.tasks,
            teammates=self.team, dispatcher=dispatcher)
        self.team.add("worker", role="developer")
        with mock.patch.object(Configs, "PLAN_APPROVAL_TIMEOUT_SECONDS", 0):
            system._run_teammate("worker", "developer", "do")

        inbox = self.bus.read_inbox("lead")
        results = [msg for msg in inbox if msg.get("type") == "result"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["metadata"]["status"], "failed")
        self.assertIn("plan approval timeout", results[0]["metadata"]["error"])
        loaded = self.tasks.load_task(task.id)
        self.assertEqual(loaded.status, "pending")
        self.assertEqual(loaded.attempts, 0)
        self.assertIsNone(loaded.owner)
        states = list(protocol.pending_requests.values())
        self.assertTrue(any(state.status == "cancelled" for state in states))


class ScriptClient:
    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        self.fallback_model = None

    def create(self, messages, system=None, tools=None, max_tokens=4096,
               model=None):
        self.calls += 1
        return self.script.pop(0)


if __name__ == "__main__":
    unittest.main()
