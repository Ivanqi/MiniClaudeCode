"""AgentLoop × LoopGuard 集成:stop/nudge、消息配对、审计、负记忆与 watchdog。"""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from src.AgentLoop import AgentLoop
from src.Configs import Configs
from src.LlmClient import LlmResponse, ToolCall, user_message
from src.TrajectoryLogger import TrajectoryLogger


class FakeClient:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.fallback_model = None

    def create(self, messages, system=None, tools=None, max_tokens=4096,
               model=None):
        self.calls.append({
            "messages": json.loads(json.dumps(messages, default=str)),
            "system": system,
            "tool_names": [t["function"]["name"] for t in (tools or [])],
            "max_tokens": max_tokens,
        })
        return self.script.pop(0)


def tool_call(call_id, name="glob", **arguments):
    return LlmResponse(
        content="", tool_calls=[ToolCall(id=call_id, name=name,
                                         arguments=arguments)],
        finish_reason="tool_calls")


def multi_call(call_ids, name="bash", **arguments):
    return LlmResponse(
        content="",
        tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments)
                    for call_id in call_ids],
        finish_reason="tool_calls")


def text(content="done"):
    return LlmResponse(content=content, tool_calls=[], finish_reason="stop")


class FakeWatchdog:
    def __init__(self, **kwargs):
        self.cancel_event = threading.Event()
        self.reason = "fake timeout"
        self._cancelled = False

    def start(self):
        pass

    def stop(self):
        pass

    def set_phase(self, phase, timeout=None):
        if phase == "tool":
            self._cancelled = True

    @property
    def cancelled(self):
        return self._cancelled


class TestAgentLoopGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._old = Configs.WORKDIR
        Configs.WORKDIR = self.tmp

    def tearDown(self):
        Configs.WORKDIR = self._old

    @staticmethod
    def assert_tool_pairing(test, history, expected):
        assistant_ids = []
        for message in history:
            if message.get("role") == "assistant" and message.get("tool_calls"):
                assistant_ids.extend(
                    call["id"] for call in message["tool_calls"])
        tool_ids = [m["tool_call_id"] for m in history
                    if m.get("role") == "tool"]
        test.assertEqual(len(assistant_ids), expected)
        test.assertEqual(sorted(assistant_ids), sorted(tool_ids))

    def test_three_identical_calls_stop_with_audit_memory_and_trajectory(self):
        client = FakeClient([
            tool_call("c1"), tool_call("c2"), tool_call("c3"),
            text("summary"),
        ])
        loop = AgentLoop(client)
        loop.trajectory = TrajectoryLogger(
            directory=self.tmp / "traj", enabled=True)
        history = [user_message("loop test")]
        result = loop.run_cycle(history, {})

        self.assertEqual(result["status"], "loop_stopped")
        self.assertIn("exact_repeat", result["closing_reason"])
        self.assertEqual(len(client.calls), 4)
        self.assertEqual(client.calls[-1]["tool_names"], [])
        self.assertIn("Runtime budget", client.calls[-1]["system"])
        self.assert_tool_pairing(self, history, expected=3)
        self.assertTrue(any("[LoopGuard]" in str(m.get("content"))
                            for m in history))

        events = [json.loads(line) for line in
                  loop.audit.path().read_text().splitlines() if line.strip()]
        self.assertTrue(any(event.get("event") == "loop_signal"
                            for event in events))
        self.assertTrue(any(event.get("event") == "loop_stopped"
                            for event in events))

        hits = loop.memory.search(
            "", tags=["loop_guard"], namespaces=["working"], top_k=5)
        self.assertTrue(hits, "stop 应写一条 working 负记忆")

        files = list((self.tmp / "traj").glob("*.jsonl"))
        self.assertTrue(files)
        record = json.loads(files[0].read_text())
        self.assertEqual(record["metadata"]["outcome"], "loop_stopped")
        self.assertTrue(record["metadata"]["loop_signals"])

    def test_nudge_only_does_not_persist_negative_memory(self):
        client = FakeClient([
            tool_call("c1"), tool_call("c2"), text("done"),
        ])
        loop = AgentLoop(client)
        history = [user_message("loop test")]
        result = loop.run_cycle(history, {})

        self.assertEqual(result["status"], "completed")
        self.assertTrue(any("[LoopGuard]" in str(m.get("content"))
                            for m in history))
        self.assertTrue(all(signal["severity"] in ("nudge", "reflect")
                            for signal in result["loop_signals"]))
        hits = loop.memory.search(
            "", tags=["loop_guard"], namespaces=["working"], top_k=5)
        self.assertFalse(hits)

    def test_failed_attempts_are_injected_and_audited(self):
        client = FakeClient([
            tool_call("b1", name="bash", command="exit 1"),
            tool_call("b2", name="bash", command="exit 2"),
            text("done"),
        ])
        loop = AgentLoop(client)
        history = [user_message("run failing commands")]
        result = loop.run_cycle(history, {})

        self.assertEqual(result["status"], "completed")
        self.assertIn("Recent failed attempts", client.calls[1]["system"])
        self.assertIn("bash_exit", client.calls[1]["system"])
        events = [json.loads(line) for line in
                  loop.audit.path().read_text().splitlines() if line.strip()]
        self.assertTrue(any(event.get("event") == "tool_result"
                            and event.get("error_code") == "bash_exit"
                            for event in events))

    def test_mid_batch_stop_fills_skipped_messages(self):
        client = FakeClient([
            multi_call(["c1", "c2"], command="echo x"),
            multi_call(["c3", "c4", "c5"], command="echo x"),
            text("summary"),
        ])
        loop = AgentLoop(client)
        history = [user_message("loop test")]
        result = loop.run_cycle(history, {})

        self.assertEqual(result["status"], "loop_stopped")
        self.assert_tool_pairing(self, history, expected=5)
        tool_messages = [m for m in history if m.get("role") == "tool"]
        skipped = [m for m in tool_messages if "[skipped]" in m["content"]]
        self.assertEqual(len(skipped), 2)
        self.assertEqual(len(client.calls), 3)

    def test_watchdog_cancellation_skips_tools_and_closes(self):
        client = FakeClient([tool_call("c1"), text("summary")])
        loop = AgentLoop(client)
        fake = FakeWatchdog()
        history = [user_message("watchdog test")]
        with mock.patch("src.agent_loop.cycle.LoopWatchdog",
                        lambda **kwargs: fake):
            result = loop.run_cycle(history, {})

        self.assertEqual(result["status"], "loop_stopped")
        self.assertIn("watchdog: fake timeout", result["closing_reason"])
        self.assertTrue(any(signal["kind"] == "watchdog"
                            for signal in result["loop_signals"]))
        tool_messages = [m for m in history if m.get("role") == "tool"]
        self.assertEqual(len(tool_messages), 1)
        self.assertIn("[skipped]", tool_messages[0]["content"])
        self.assertIn("watchdog", tool_messages[0]["content"])


if __name__ == "__main__":
    unittest.main()
