"""Plan 安全/配置:保护路径、控制面确认、惰性目录、运行时上限。"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.Configs import Configs
from src.LlmClient import LlmResponse
from src.PlanSystem import PlanStore, PlanSystem
from src.SecurityPolicy import ALLOW, CONFIRM, DENY, classify_path
from src.TaskSystem import TaskSystem
from src.ToolPolicy import (PLAN_FORBIDDEN_STEP_TOOLS, PLAN_TOOL_NAMES,
                            plan_step_policy)

REPO_ROOT = Path(__file__).resolve().parents[1]


class FakeClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0
        self.fallback_model = None
        self.model = "fake"

    def create(self, messages, system=None, tools=None, max_tokens=4096,
               model=None):
        self.calls += 1
        return LlmResponse(content=self.payload, tool_calls=[],
                           finish_reason="stop")


class RecordingRunner:
    def __init__(self):
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return {"status": "completed", "content": "ok",
                "budget": {"total_tokens": 1}}


class TestPlanPathPolicy(unittest.TestCase):
    def test_plans_write_is_denied(self):
        tmp = Path(tempfile.mkdtemp())
        decision = classify_path(".plans/plan_x.json", tmp, write=True)
        self.assertEqual(decision.verdict, DENY)
        self.assertIn("protected", decision.reason)
        read = classify_path(".plans/plan_x.json", tmp, write=False)
        self.assertEqual(read.verdict, ALLOW)

    def test_plan_system_is_control_plane(self):
        confirm = classify_path("src/PlanSystem.py", REPO_ROOT, write=True)
        self.assertEqual(confirm.verdict, CONFIRM)
        self.assertEqual(
            classify_path("src/PlanSystem.py", REPO_ROOT, write=False).verdict,
            ALLOW)
        self.assertEqual(
            classify_path("src/StrategyRouter.py", REPO_ROOT,
                          write=True).verdict, CONFIRM)

    def test_plan_step_policy_forbidden_tools(self):
        policy = plan_step_policy({"read_file", "execute_plan",
                                   "spawn_teammate"})
        self.assertFalse(policy.allows_tool("execute_plan")[0])
        self.assertFalse(policy.allows_tool("spawn_teammate")[0])
        self.assertTrue(policy.allows_tool("read_file")[0])
        self.assertFalse(policy.allows_tool("mcp__x__y")[0])
        self.assertTrue(PLAN_TOOL_NAMES <= PLAN_FORBIDDEN_STEP_TOOLS)


class TestLazyAndCaps(unittest.TestCase):
    def test_plan_store_root_is_lazy(self):
        root = Path(tempfile.mkdtemp()) / "plans"
        store = PlanStore(root=root)
        self.assertEqual(store.list_plans(), [])
        self.assertFalse(root.exists())
        self.assertEqual(store.root, root)
        self.assertFalse(root.exists())

    def test_plan_system_construction_has_no_io(self):
        root = Path(tempfile.mkdtemp())
        tasks = TaskSystem(tasks_dir=root / "tasks")
        store = PlanStore(root=root / "plans")
        client = FakeClient("{}")
        PlanSystem(client, tasks=tasks, store=store,
                   tool_names_provider=lambda: ["read_file"])
        self.assertFalse((root / "plans").exists())
        self.assertEqual(client.calls, 0)

    def test_caps_are_read_at_execution_time(self):
        root = Path(tempfile.mkdtemp())
        tasks = TaskSystem(tasks_dir=root / "tasks")
        store = PlanStore(root=root / "plans")
        payload = json.dumps({
            "goal": "demo", "strategy": "plan_execute",
            "steps": [{"id": "s1", "goal": "read", "tool_hints": ["read_file"],
                       "depends_on": [], "success_criteria": "done"}]})
        runner = RecordingRunner()
        system = PlanSystem(FakeClient(payload), tasks=tasks, store=store,
                            step_runner=runner,
                            tool_names_provider=lambda: ["read_file"])
        with mock.patch.object(Configs, "PLAN_STEP_MAX_ROUNDS", 2), \
                mock.patch.object(Configs, "PLAN_STEP_MAX_TOOL_CALLS", 3), \
                mock.patch.object(Configs, "PLAN_MAX_TOTAL_TOKENS", 1234):
            message = system.run_plan_execute("demo")
        self.assertIn("completed", message)
        budget = runner.calls[0]["budget"]
        self.assertEqual(budget.max_rounds, 2)
        self.assertEqual(budget.max_tool_calls, 3)
        self.assertLessEqual(budget.max_total_tokens, 1234)
        self.assertIn('"max_total_tokens": 1234', message)


if __name__ == "__main__":
    unittest.main()
