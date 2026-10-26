"""ToolDispatcher 测试:授权、确认、备份、审计、后台预授权。"""
import json
import tempfile
import unittest
from pathlib import Path

from src.ApprovalGate import ApprovalGate
from src.AuditLog import AuditLog
from src.Configs import Configs
from src.LlmClient import ToolCall
from src.Sandbox import Sandbox
from src.ToolDispatcher import ToolDispatcher
from src.ToolPolicy import lead_policy, teammate_policy


class BlockingHook:
    def trigger(self, event, *args):
        return "blocked by hook" if event == "PreToolUse" else None


class TestDispatcher(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()).resolve()
        self._old_workdir = Configs.WORKDIR
        Configs.WORKDIR = self.root
        self.audit = AuditLog(directory=self.root / "audit")
        self.dispatcher = self.make_dispatcher()
        self.handlers = {"echo_tool": lambda: "ok", "boom": self._boom}

    def tearDown(self):
        Configs.WORKDIR = self._old_workdir

    @staticmethod
    def _boom():
        raise ValueError("boom")

    def make_dispatcher(self, **kwargs):
        return ToolDispatcher(
            hooks=kwargs.pop("hooks", None), audit=self.audit,
            approval=kwargs.pop("approval", ApprovalGate(
                interactive=False, auto_approve=set())),
            sandbox=kwargs.pop("sandbox", Sandbox(mode="off")), **kwargs)

    def call(self, name, **arguments):
        return ToolCall(id=f"call_{name}", name=name, arguments=arguments)

    def run_tool(self, name, policy=None, **arguments):
        return self.dispatcher.execute(
            self.call(name, **arguments), policy or lead_policy(),
            self.handlers)

    def test_unknown_tool(self):
        result = self.run_tool("nope")
        self.assertFalse(result.ok)
        self.assertEqual(result.content, "Unknown: nope")

    def test_handler_exception_is_contained(self):
        result = self.run_tool("boom")
        self.assertFalse(result.ok)
        self.assertIn("Error: ValueError: boom", result.content)

    def test_sensitive_read_and_path_escape_denied(self):
        (self.root / ".env").write_text("SECRET=1")
        result = self.run_tool("read_file", path=".env")
        self.assertTrue(result.denied)
        self.assertIn("sensitive", result.content)
        result = self.run_tool("write_file", path="../escape.txt", content="x")
        self.assertTrue(result.denied)
        self.assertFalse((self.root.parent / "escape.txt").exists())

    def test_write_backup_and_content(self):
        target = self.root / "a.txt"
        target.write_text("old")
        result = self.run_tool("write_file", path="a.txt", content="new")
        self.assertTrue(result.ok, result.content)
        self.assertEqual(target.read_text(), "new")
        self.assertEqual(len(result.artifacts), 1)
        backup = Path(result.artifacts[0])
        self.assertEqual(backup.read_text(), "old")

    def test_control_plane_write_requires_confirmation(self):
        control = self.root / "src" / "HookSystem.py"
        control.parent.mkdir(parents=True)
        control.write_text("old")
        result = self.run_tool("write_file", path="src/HookSystem.py",
                               content="new")
        self.assertTrue(result.denied)
        self.assertEqual(control.read_text(), "old")

        auto = self.make_dispatcher(approval=ApprovalGate(
            interactive=False, auto_approve={"write_file"}))
        result = auto.execute(
            self.call("write_file", path="src/HookSystem.py", content="new"),
            lead_policy(), self.handlers)
        self.assertTrue(result.ok, result.content)
        self.assertEqual(control.read_text(), "new")
        self.assertTrue(Path(result.artifacts[0]).exists())

    def test_bash_exit_code_and_sensitive_command(self):
        result = self.run_tool("bash", command="false")
        self.assertFalse(result.ok)
        self.assertEqual(result.exit_code, 1)
        self.assertIn("Exit code 1", result.content)
        result = self.run_tool("bash", command="cat .env")
        self.assertTrue(result.denied)

    def test_hooks_can_block(self):
        dispatcher = self.make_dispatcher(hooks=BlockingHook())
        result = dispatcher.execute(
            self.call("echo_tool"), lead_policy(), self.handlers)
        self.assertTrue(result.denied)
        self.assertIn("blocked by hook", result.content)

    def test_fingerprint_and_policy_snapshot_are_enforced(self):
        policy = lead_policy()
        original = self.call("echo_tool")
        auth = self.dispatcher.authorize(original, policy, self.handlers)
        self.assertTrue(auth.allowed)
        changed = ToolCall(id="call_echo_tool", name="echo_tool",
                           arguments={"extra": "x"})
        result = self.dispatcher.execute(
            changed, policy, self.handlers, authorization=auth)
        self.assertTrue(result.denied)
        self.assertIn("fingerprint", result.content)

        other_root = Path(tempfile.mkdtemp()).resolve()
        other_policy = teammate_policy(other_root)
        result = self.dispatcher.execute(
            original, other_policy, self.handlers, authorization=auth)
        self.assertTrue(result.denied)
        self.assertIn("root changed", result.content)

    def test_teammate_write_constraints(self):
        worktree = Path(tempfile.mkdtemp()).resolve()
        policy = teammate_policy(worktree)
        result = self.dispatcher.execute(
            self.call("write_file", path="inside.txt", content="x"),
            policy, self.handlers)
        self.assertTrue(result.ok, result.content)
        self.assertTrue((worktree / "inside.txt").exists())
        result = self.dispatcher.execute(
            self.call("write_file", path="../outside.txt", content="x"),
            policy, self.handlers)
        self.assertTrue(result.denied)

        no_wt = teammate_policy(None)
        result = self.dispatcher.execute(
            self.call("write_file", path="x.txt", content="x"), no_wt,
            self.handlers)
        self.assertTrue(result.denied)

    def test_teammate_without_worktree_bash_is_read_only(self):
        policy = teammate_policy(None)
        result = self.dispatcher.execute(
            self.call("bash", command="ls"), policy, self.handlers)
        self.assertTrue(result.ok, result.content)
        result = self.dispatcher.execute(
            self.call("bash", command="rm x.txt"), policy, self.handlers)
        self.assertTrue(result.denied)

    def test_mcp_annotations(self):
        handlers = {"mcp__deploy__trigger": lambda: "triggered",
                    "mcp__docs__search": lambda: "found"}
        policy = lead_policy()
        call = ToolCall(id="m1", name="mcp__deploy__trigger", arguments={})
        result = self.dispatcher.execute(
            call, policy, handlers,
            metadata={"annotations": {"destructiveHint": True}})
        self.assertTrue(result.denied)
        result = self.dispatcher.execute(
            call, policy, handlers, metadata={})
        self.assertTrue(result.denied)

        read_call = ToolCall(id="m2", name="mcp__docs__search", arguments={})
        result = self.dispatcher.execute(
            read_call, policy, handlers,
            metadata={"annotations": {"readOnlyHint": True}})
        self.assertTrue(result.ok, result.content)

    def test_output_redaction_and_audit(self):
        secret = "sk-abcdefghijklmnopqrst"
        self.handlers["leak"] = lambda: secret
        result = self.run_tool("leak")
        self.assertNotIn(secret, result.content)
        self.assertIn("[REDACTED]", result.content)
        text = (self.root / "audit" / "tool_calls.jsonl").read_text()
        self.assertNotIn(secret, text)
        events = [json.loads(line)["event"] for line in text.splitlines()]
        self.assertIn("tool_decision", events)
        self.assertIn("tool_result", events)


if __name__ == "__main__":
    unittest.main()
