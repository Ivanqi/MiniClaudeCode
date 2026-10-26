"""记忆工具与接线测试:策略、确认、Memories 注入、AgentLoop 注册。"""
import tempfile
import unittest
from pathlib import Path

from src.AgentLoop import AgentLoop
from src.ApprovalGate import ApprovalGate
from src.AuditLog import AuditLog
from src.Configs import Configs
from src.LlmClient import ToolCall
from src.Memories import Memories
from src.MemoryStore import MemoryStore
from src.Sandbox import Sandbox
from src.ToolDispatcher import ToolDispatcher
from src.ToolPolicy import (DEFAULT_CONFIRM_TOOLS, lead_policy, local_policy,
                            subagent_policy, teammate_policy)


class FakeClient:
    def __init__(self, script=None):
        self.script = list(script or [])
        self.calls = []
        self.fallback_model = None

    def create(self, messages, system=None, tools=None, max_tokens=4096,
               model=None):
        self.calls.append({"tool_names": [t["function"]["name"]
                                          for t in (tools or [])]})
        return self.script.pop(0)


class FakeTasks:
    @staticmethod
    def board_summary():
        return "Active tasks:\n  task_1: test"


class TestMemoryTools(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self._old = Configs.WORKDIR
        Configs.WORKDIR = self.tmp
        self.audit = AuditLog(directory=self.tmp / "audit")
        self.store = MemoryStore(root=self.tmp / "memory", audit=self.audit,
                                 touch_interval=0)
        self.handlers = self.store.handlers()

    def tearDown(self):
        Configs.WORKDIR = self._old

    def dispatcher(self, approve=frozenset()):
        return ToolDispatcher(
            hooks=None, audit=self.audit,
            approval=ApprovalGate(interactive=False,
                                  auto_approve=set(approve)),
            sandbox=Sandbox(mode="off"))

    def test_policy_matrix(self):
        self.assertTrue(local_policy().allows_tool("recall")[0])
        self.assertFalse(local_policy().allows_tool("remember")[0])
        self.assertFalse(local_policy().allows_tool("forget")[0])
        self.assertIn("forget", DEFAULT_CONFIRM_TOOLS)
        self.assertFalse(subagent_policy().allows_tool("remember")[0])
        self.assertFalse(teammate_policy(None).allows_tool("recall")[0])
        self.assertTrue(lead_policy().allows_tool("remember")[0])

    def test_remember_is_audited_without_prompt(self):
        dispatcher = self.dispatcher()
        call = ToolCall(id="m1", name="remember", arguments={
            "content": "User likes tea", "key": "user:drink",
            "source": "user", "source_kind": "user",
        })
        result = dispatcher.execute(call, lead_policy(), self.handlers)
        self.assertTrue(result.ok, result.content)
        self.assertIn("Created", result.content)
        self.assertIsNotNone(self.store.get("semantic", "user:drink"))
        import json
        events = [json.loads(line) for line in
                  (self.tmp / "audit" / "tool_calls.jsonl")
                  .read_text().splitlines()]
        self.assertTrue(any(event.get("tool") == "remember"
                            for event in events))
        self.assertTrue(any(event.get("event") == "memory_upsert"
                            for event in events))

    def test_forget_requires_confirmation(self):
        self.store.upsert("semantic", "forget me", key="gone",
                          source="user", source_kind="user")
        call = ToolCall(id="m2", name="forget",
                        arguments={"namespace": "semantic", "key": "gone"})
        denied = self.dispatcher().execute(call, lead_policy(), self.handlers)
        self.assertTrue(denied.denied)
        self.assertIsNotNone(self.store.get("semantic", "gone"))

        allowed = self.dispatcher(approve={"forget"}).execute(
            call, lead_policy(), self.handlers)
        self.assertTrue(allowed.ok, allowed.content)
        self.assertIsNone(self.store.get("semantic", "gone"))

    def test_recall_allowed_for_local(self):
        self.store.upsert("semantic", "Python preference",
                          key="user:python", source="user",
                          source_kind="user")
        call = ToolCall(id="m3", name="recall",
                        arguments={"query": "python"})
        result = self.dispatcher().execute(call, local_policy(), self.handlers)
        self.assertTrue(result.ok, result.content)
        self.assertIn("Python preference", result.content)

    def test_memories_injects_top_k_not_raw_index(self):
        self.store.upsert("semantic", "Python is preferred",
                          key="user:python", source="user",
                          source_kind="user")
        (self.tmp / "memory" / "MEMORY.md").write_text("RAW_INDEX_SECRET")
        memories = Memories(memory=self.store, tasks=FakeTasks())
        context = memories.update_context({}, [
            {"role": "user", "content": "which language does the user like?"},
            {"role": "user", "content": "[Budget reached: summarize]"},
        ])
        self.assertIn("Python is preferred", context["memories"])
        self.assertIn("untrusted data", context["memories"])
        self.assertNotIn("RAW_INDEX_SECRET", context["memories"])
        self.assertIn("task_1", context["task_summary"])

    def test_agentloop_registers_memory_tools_without_io(self):
        loop = AgentLoop(FakeClient())
        full, _ = loop.tool_registry.assemble()
        names = {tool["function"]["name"] for tool in full}
        for name in ("remember", "recall", "forget",
                     "search_transcripts", "recall_transcript",
                     "review_task", "plan_integration",
                     "preflight_integration", "apply_integration",
                     "seal_worktree", "team_status"):
            self.assertIn(name, names)
        slim, _ = loop.tool_registry.assemble(slim=True)
        slim_names = {tool["function"]["name"] for tool in slim}
        self.assertIn("recall", slim_names)
        self.assertNotIn("remember", slim_names)
        self.assertNotIn("forget", slim_names)
        self.assertNotIn("search_transcripts", slim_names)
        self.assertFalse((self.tmp / ".memory").exists())
        self.assertFalse((self.tmp / ".team").exists())
        loop._startup_maintenance()
        self.assertTrue((self.tmp / ".memory" / "MEMORY.md").exists())


if __name__ == "__main__":
    unittest.main()
