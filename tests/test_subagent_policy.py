"""子代理策略与预算测试:本地只读、工具调用上限收尾。"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.Configs import Configs
from src.LlmClient import LlmResponse, ToolCall
from src.ModelRouter import ModelRouter
from src.SubagentSystem import SubagentSystem


class FakeClient:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.model = "fake"
        self.fallback_model = None

    def create(self, messages, system=None, tools=None, max_tokens=4096,
               model=None):
        self.calls.append({
            "messages": json.loads(json.dumps(messages, default=str)),
            "tool_names": [t["function"]["name"] for t in (tools or [])],
            "system": system,
        })
        return self.script.pop(0)


class TestSubagentPolicy(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._old = Configs.WORKDIR
        Configs.WORKDIR = self.tmp

    def tearDown(self):
        Configs.WORKDIR = self._old

    def test_local_route_is_read_only(self):
        local = FakeClient([
            LlmResponse(content="",
                        tool_calls=[ToolCall(id="c1", name="bash",
                                             arguments={"command": "ls"})],
                        finish_reason="tool_calls"),
            LlmResponse(content="done", tool_calls=[], finish_reason="stop"),
        ])
        api = FakeClient([])
        router = ModelRouter(api, local, mode="auto")
        router.local_healthy = lambda: True
        system = SubagentSystem(api, router=router)
        result = system.spawn_subagent("look around")
        self.assertIn("done", result)
        self.assertFalse(any(name in {"bash", "write_file", "edit_file"}
                             for name in local.calls[0]["tool_names"]))
        self.assertTrue(all(name in {"read_file", "glob"}
                            for name in local.calls[0]["tool_names"]))
        tool_messages = [m for m in local.calls[1]["messages"]
                         if m.get("role") == "tool"]
        self.assertIn("not allowed", tool_messages[0]["content"])

    def test_tool_call_budget_adds_skipped_and_summary(self):
        client = FakeClient([
            LlmResponse(content="",
                        tool_calls=[
                            ToolCall(id="c1", name="read_file",
                                     arguments={"path": "a.txt"}),
                            ToolCall(id="c2", name="read_file",
                                     arguments={"path": "b.txt"}),
                        ], finish_reason="tool_calls"),
            LlmResponse(content="summary", tool_calls=[],
                        finish_reason="stop"),
        ])
        (self.tmp / "a.txt").write_text("a")
        (self.tmp / "b.txt").write_text("b")
        system = SubagentSystem(client)
        with mock.patch.object(Configs, "SUBAGENT_MAX_TOOL_CALLS", 1):
            result = system.spawn_subagent("read both")
        self.assertIn("summary", result)
        self.assertIn("subagent budget reached", result)
        messages = client.calls[1]["messages"]
        tool_messages = [m for m in messages if m.get("role") == "tool"]
        self.assertEqual(len(tool_messages), 2)
        self.assertTrue(any("[skipped]" in m["content"]
                            for m in tool_messages))

    def test_tools_for_local_excludes_dangerous(self):
        system = SubagentSystem(FakeClient([]))
        names = {tool["function"]["name"] for tool in system.tools_for(True)}
        self.assertEqual(names, {"read_file", "glob"})


if __name__ == "__main__":
    unittest.main()
