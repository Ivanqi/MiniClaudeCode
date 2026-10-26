"""ToolPolicy / TurnBudget / ToolResult 单元测试。"""
import tempfile
import unittest
from pathlib import Path

from src.Configs import Configs
from src.LlmClient import ToolCall
from src.ToolPolicy import (READONLY_TOOL_NAMES, TurnBudget, ToolResult,
                            lead_policy, local_policy, skipped_tool_messages,
                            subagent_local_policy, subagent_policy,
                            teammate_policy)


class TestPolicyFactories(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._old = Configs.WORKDIR
        Configs.WORKDIR = self.tmp

    def tearDown(self):
        Configs.WORKDIR = self._old

    def test_local_is_read_only(self):
        policy = local_policy()
        self.assertTrue(policy.allows_tool("read_file")[0])
        self.assertFalse(policy.allows_tool("bash")[0])
        self.assertFalse(policy.allows_tool("write_file")[0])
        self.assertTrue(policy.allowed <= READONLY_TOOL_NAMES)

    def test_subagent_local_has_no_bash_or_write(self):
        policy = subagent_local_policy()
        for name in ("bash", "write_file", "edit_file"):
            self.assertFalse(policy.allows_tool(name)[0], name)
        self.assertTrue(policy.allows_tool("read_file")[0])
        self.assertFalse(subagent_policy().read_only)

    def test_teammate_without_worktree_cannot_write(self):
        policy = teammate_policy(None)
        self.assertTrue(policy.read_only)
        self.assertFalse(policy.allows_tool("write_file")[0])
        self.assertEqual(policy.bash_mode, "read_only")
        self.assertFalse(policy.path_is_writable(self.tmp / "x.txt"))

    def test_teammate_with_worktree_writes_only_there(self):
        wt = Path(tempfile.mkdtemp()).resolve()
        policy = teammate_policy(wt)
        self.assertTrue(policy.path_is_writable(wt / "a.txt"))
        self.assertFalse(policy.path_is_writable(self.tmp / "a.txt"))
        self.assertEqual(policy.root(), wt)
        self.assertFalse(policy.allows_tool("spawn_teammate")[0])

    def test_lead_has_workspace_write(self):
        policy = lead_policy()
        self.assertTrue(policy.path_is_writable(self.tmp / "a.txt"))
        self.assertTrue(policy.allows_tool("bash")[0])

    def test_confirmation_rules(self):
        policy = lead_policy()
        self.assertTrue(policy.needs_confirmation("remove_worktree", {})[0] is False)
        self.assertTrue(
            policy.needs_confirmation("remove_worktree",
                                      {"discard_changes": True})[0])
        self.assertTrue(policy.needs_confirmation(
            "mcp__deploy__trigger", {},
            {"annotations": {"destructiveHint": True}})[0])
        self.assertFalse(policy.needs_confirmation(
            "mcp__docs__search", {},
            {"annotations": {"readOnlyHint": True}})[0])
        # 无注解的 MCP 工具保守要求确认
        self.assertTrue(policy.needs_confirmation("mcp__x__y", {}, {})[0])


class TestTurnBudget(unittest.TestCase):
    def test_rounds_and_tool_calls(self):
        budget = TurnBudget(max_rounds=2, max_tool_calls=0,
                            max_wall_s=999, max_total_tokens=0)
        self.assertEqual(budget.exhausted_reason(), "")
        budget.record_round()
        self.assertEqual(budget.exhausted_reason(), "")
        budget.record_round()
        self.assertIn("rounds", budget.exhausted_reason())

        calls = TurnBudget(max_rounds=0, max_tool_calls=1,
                           max_wall_s=999, max_total_tokens=0)
        self.assertEqual(calls.exhausted_reason(), "")
        calls.reserve_tool_call()
        self.assertIn("tool calls", calls.exhausted_reason())

    def test_tokens(self):
        budget = TurnBudget(max_rounds=0, max_tool_calls=0,
                            max_wall_s=0, max_total_tokens=100)
        budget.record_usage({"total_tokens": 60})
        self.assertEqual(budget.exhausted_reason(), "")
        budget.record_usage({"prompt_tokens": 30, "completion_tokens": 20})
        self.assertIn("tokens", budget.exhausted_reason())
        self.assertEqual(budget.total_tokens, 110)

    def test_wall_clock(self):
        budget = TurnBudget(max_rounds=0, max_tool_calls=0,
                            max_wall_s=1, max_total_tokens=0)
        budget.started_at -= 2
        self.assertIn("wall", budget.exhausted_reason())

    def test_runtime_snapshot_exposes_remaining(self):
        budget = TurnBudget(max_rounds=3, max_tool_calls=5,
                            max_wall_s=10, max_total_tokens=100)
        budget.record_round()
        budget.reserve_tool_call()
        budget.record_usage({"total_tokens": 30})
        snapshot = budget.runtime_snapshot()
        self.assertEqual(snapshot["rounds"]["remaining"], 2)
        self.assertEqual(snapshot["tool_calls"]["remaining"], 4)
        self.assertEqual(snapshot["tokens"]["remaining"], 70)
        self.assertEqual(snapshot["wall_s"]["max"], 10)
        self.assertIsNotNone(snapshot["wall_s"]["remaining"])


class TestToolResult(unittest.TestCase):
    def test_render_and_denied(self):
        self.assertEqual(str(ToolResult.success("ok")), "ok")
        denied = ToolResult.denied_result("nope")
        self.assertFalse(denied.ok)
        self.assertTrue(denied.denied)
        self.assertIn("Denied", denied.content)

    def test_skipped_messages_cover_every_call(self):
        calls = [ToolCall(id="c1", name="bash", arguments={}),
                 ToolCall(id="c2", name="glob", arguments={})]
        messages = skipped_tool_messages(calls, "budget")
        self.assertEqual([m["tool_call_id"] for m in messages], ["c1", "c2"])
        self.assertTrue(all("[skipped]" in m["content"] for m in messages))


if __name__ == "__main__":
    unittest.main()
