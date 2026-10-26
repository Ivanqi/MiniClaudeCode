"""ToolCircuitBreaker:阈值、冷却、半开探测与 dispatcher 集成。"""
import json
import tempfile
import unittest
from pathlib import Path

from src.AuditLog import AuditLog
from src.Configs import Configs
from src.LlmClient import ToolCall
from src.LoopGuard import ToolCircuitBreaker
from src.Sandbox import Sandbox
from src.ToolDispatcher import ToolDispatcher
from src.ToolPolicy import ToolResult, lead_policy


class FakeClock:
    def __init__(self, now=100.0):
        self.now = float(now)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)


class RecordingApproval:
    def __init__(self):
        self.requests = []

    def confirm(self, request):
        self.requests.append(request)
        return True


class TestCircuitBreaker(unittest.TestCase):
    def test_open_cooldown_half_open_and_close(self):
        clock = FakeClock()
        breaker = ToolCircuitBreaker(
            failure_threshold=3, cooldown_s=10,
            max_cooldown_s=40, clock=clock)
        for _ in range(2):
            self.assertTrue(breaker.allow("bash")[0])
            self.assertTrue(breaker.acquire("bash")[0])
            breaker.record("bash", ToolResult.failure(
                "Error: Timeout", error_code="timeout"))
            self.assertEqual(breaker.state("bash"), "closed")
        self.assertTrue(breaker.acquire("bash")[0])
        breaker.record("bash", ToolResult.failure(
            "Error: Timeout", error_code="timeout"))
        self.assertEqual(breaker.state("bash"), "open")
        self.assertFalse(breaker.allow("bash")[0])
        self.assertFalse(breaker.acquire("bash")[0])

        clock.advance(10)
        self.assertEqual(breaker.state("bash"), "half_open")
        self.assertTrue(breaker.allow("bash")[0])
        self.assertTrue(breaker.acquire("bash")[0])
        self.assertFalse(breaker.acquire("bash")[0])  # 只会有一个探测
        breaker.record("bash", ToolResult.failure(
            "Error: Timeout", error_code="timeout"))
        self.assertEqual(breaker.state("bash"), "open")
        clock.advance(19)
        self.assertFalse(breaker.allow("bash")[0])  # 第二次冷却已退避到 20s
        clock.advance(1)
        self.assertTrue(breaker.acquire("bash")[0])
        breaker.record("bash", ToolResult.success("ok"))
        self.assertEqual(breaker.state("bash"), "closed")

    def test_business_failures_do_not_open(self):
        breaker = ToolCircuitBreaker(failure_threshold=2)
        for _ in range(5):
            breaker.record("bash", ToolResult.failure(
                "Exit code 1", exit_code=1, error_code="bash_exit"))
            breaker.record("read_file", ToolResult.failure(
                "Error: no such file", error_code="not_found"))
            breaker.record("write_file", ToolResult.denied_result("nope"))
        self.assertEqual(breaker.state("bash"), "closed")
        self.assertEqual(breaker.state("read_file"), "closed")
        self.assertEqual(breaker.state("write_file"), "closed")

    def test_success_closes_and_restores_cooldown(self):
        clock = FakeClock()
        breaker = ToolCircuitBreaker(
            failure_threshold=1, cooldown_s=10, clock=clock)
        breaker.record("mcp", ToolResult.failure(
            "Error: MCP error", error_code="mcp_error"))
        self.assertEqual(breaker.state("mcp"), "open")
        clock.advance(10)
        self.assertTrue(breaker.acquire("mcp")[0])
        breaker.record("mcp", ToolResult.success("ok"))
        self.assertEqual(breaker.state("mcp"), "closed")
        # reset 后再失败仍从基础冷却开始
        breaker.record("mcp", ToolResult.failure(
            "Error: MCP error", error_code="mcp_error"))
        self.assertEqual(breaker.state("mcp"), "open")
        clock.advance(10)
        self.assertEqual(breaker.state("mcp"), "half_open")


class TestDispatcherCircuit(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._old = Configs.WORKDIR
        Configs.WORKDIR = self.tmp
        self.approval = RecordingApproval()
        self.audit = AuditLog(directory=self.tmp / ".audit")
        self.breaker = ToolCircuitBreaker(failure_threshold=2, cooldown_s=60)
        self.dispatcher = ToolDispatcher(
            audit=self.audit, approval=self.approval,
            sandbox=Sandbox(mode="off"), circuit_breaker=self.breaker)
        self.calls = []

    def tearDown(self):
        Configs.WORKDIR = self._old

    def test_open_circuit_blocks_handler(self):
        def boom(**kwargs):
            self.calls.append(1)
            raise RuntimeError("boom")

        policy = lead_policy()
        call = ToolCall(id="t1", name="mytool", arguments={})
        first = self.dispatcher.execute(call, policy, {"mytool": boom})
        second = self.dispatcher.execute(call, policy, {"mytool": boom})
        third = self.dispatcher.execute(call, policy, {"mytool": boom})
        self.assertFalse(first.ok)
        self.assertEqual(first.error_code, "exception")
        self.assertFalse(second.ok)
        self.assertEqual(third.error_code, "circuit_open")
        self.assertEqual(len(self.calls), 2)
        events = [json.loads(line) for line in
                  self.audit.path().read_text().splitlines() if line.strip()]
        self.assertTrue(any(event.get("decision") == "circuit_open"
                            for event in events))

    def test_authorize_short_circuits_before_approval(self):
        self.breaker.record_failure("forget", "timeout")
        self.breaker.record_failure("forget", "timeout")
        call = ToolCall(id="f1", name="forget", arguments={"namespace": "working"})
        auth = self.dispatcher.authorize(
            call, lead_policy(), {"forget": lambda **kwargs: "ok"})
        self.assertFalse(auth.allowed)
        self.assertEqual(auth.denial.error_code, "circuit_open")
        self.assertEqual(self.approval.requests, [])


if __name__ == "__main__":
    unittest.main()
