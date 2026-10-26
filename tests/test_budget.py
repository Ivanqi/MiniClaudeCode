"""AgentLoop 预算测试:轮数/工具调用/token 收尾与消息配对。"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.AgentLoop import AgentLoop
from src.Configs import Configs
from src.LlmClient import LlmResponse, ToolCall, user_message


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
        content="",
        tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments)],
        finish_reason="tool_calls")


def text(content="done"):
    return LlmResponse(content=content, tool_calls=[], finish_reason="stop")


class TestTurnBudget(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._old_workdir = Configs.WORKDIR
        Configs.WORKDIR = self.tmp

    def tearDown(self):
        Configs.WORKDIR = self._old_workdir

    def test_max_rounds_forces_one_no_tool_summary(self):
        script = [tool_call("c0"), tool_call("c1"), tool_call("c2"),
                  text("summary")]
        client = FakeClient(script)
        loop = AgentLoop(client)
        history = [user_message("keep going")]
        with mock.patch.object(Configs, "TURN_MAX_ROUNDS", 3), \
                mock.patch.object(Configs, "TURN_MAX_TOOL_CALLS", 100), \
                mock.patch.object(Configs, "TURN_MAX_WALL_SECONDS", 999), \
                mock.patch.object(Configs, "TURN_MAX_TOTAL_TOKENS", 0):
            loop.run_cycle(history, {})
        self.assertEqual(len(client.calls), 4)  # 3 轮 + 1 次收尾
        self.assertEqual(client.calls[-1]["tool_names"], [])
        self.assertEqual(history[-1]["role"], "assistant")
        self.assertEqual(history[-1]["content"], "summary")
        self._assert_tool_pairing(history, expected=3)

    def test_max_tool_calls_fills_skipped_messages(self):
        response = LlmResponse(
            content="",
            tool_calls=[ToolCall(id="c1", name="glob", arguments={"pattern": "*"}),
                        ToolCall(id="c2", name="glob", arguments={"pattern": "*"})],
            finish_reason="tool_calls")
        client = FakeClient([response, text("summary")])
        loop = AgentLoop(client)
        history = [user_message("go")]
        with mock.patch.object(Configs, "TURN_MAX_ROUNDS", 10), \
                mock.patch.object(Configs, "TURN_MAX_TOOL_CALLS", 1), \
                mock.patch.object(Configs, "TURN_MAX_WALL_SECONDS", 999), \
                mock.patch.object(Configs, "TURN_MAX_TOTAL_TOKENS", 0):
            loop.run_cycle(history, {})
        tool_messages = [m for m in history if m.get("role") == "tool"]
        self.assertEqual(len(tool_messages), 2)
        self.assertTrue(any("[skipped]" in m["content"] for m in tool_messages))
        self.assertEqual(client.calls[-1]["tool_names"], [])
        self._assert_tool_pairing(history, expected=2)

    def test_token_budget_triggers_summary(self):
        response = LlmResponse(
            content="",
            tool_calls=[ToolCall(id="c1", name="glob", arguments={"pattern": "*"})],
            finish_reason="tool_calls", usage={"total_tokens": 20})
        client = FakeClient([response, text("summary")])
        loop = AgentLoop(client)
        history = [user_message("go")]
        with mock.patch.object(Configs, "TURN_MAX_ROUNDS", 10), \
                mock.patch.object(Configs, "TURN_MAX_TOOL_CALLS", 100), \
                mock.patch.object(Configs, "TURN_MAX_WALL_SECONDS", 999), \
                mock.patch.object(Configs, "TURN_MAX_TOTAL_TOKENS", 10):
            loop.run_cycle(history, {})
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(client.calls[-1]["tool_names"], [])
        self.assertIn("summary", history[-1]["content"])

    def test_normal_finish_does_not_add_closing_call(self):
        client = FakeClient([text("done")])
        loop = AgentLoop(client)
        history = [user_message("hello")]
        with mock.patch.object(Configs, "TURN_MAX_ROUNDS", 3), \
                mock.patch.object(Configs, "TURN_MAX_TOOL_CALLS", 100), \
                mock.patch.object(Configs, "TURN_MAX_WALL_SECONDS", 999), \
                mock.patch.object(Configs, "TURN_MAX_TOTAL_TOKENS", 0):
            loop.run_cycle(history, {})
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(history[-1]["content"], "done")

    def test_denied_tool_is_a_tool_message(self):
        (self.tmp / ".env").write_text("SECRET=1")
        client = FakeClient([tool_call("c1", name="read_file", path=".env"),
                             text("done")])
        loop = AgentLoop(client)
        history = [user_message("read env")]
        loop.run_cycle(history, {})
        tool_message = next(m for m in history if m.get("role") == "tool")
        self.assertIn("Denied", tool_message["content"])
        self.assertNotIn("SECRET=1", json.dumps(history))

    def _assert_tool_pairing(self, history, expected):
        assistant_ids = []
        for message in history:
            if message.get("role") == "assistant" and message.get("tool_calls"):
                assistant_ids.extend(call["id"] for call in message["tool_calls"])
        tool_ids = [m["tool_call_id"] for m in history
                    if m.get("role") == "tool"]
        self.assertEqual(len(assistant_ids), expected)
        self.assertEqual(sorted(assistant_ids), sorted(tool_ids))


if __name__ == "__main__":
    unittest.main()
