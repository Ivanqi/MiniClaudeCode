"""PlanSystem 工具契约:成对定义/handler、创建/执行/状态、惰性构造。"""
import json
import tempfile
import unittest
from pathlib import Path

from src.LlmClient import LlmResponse
from src.MemoryStore import MemoryStore
from src.PlanSystem import PlanStore, PlanSystem
from src.TaskSystem import TaskSystem
from src.TeamMetrics import TeamMetrics
from src.ToolPolicy import (PLAN_TOOL_NAMES, READONLY_TOOL_NAMES,
                            SUBAGENT_TOOLS, TEAMMATE_TOOLS)


def plan_payload(goal="demo", steps=None):
    return json.dumps({
        "goal": goal,
        "strategy": "plan_execute",
        "steps": steps or [{
            "id": "s1",
            "goal": "read the file",
            "tool_hints": ["read_file"],
            "depends_on": [],
            "success_criteria": "file read",
            "rollback": None,
            "requires_approval": False,
        }],
    })


class FakeClient:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.fallback_model = None
        self.model = "fake"

    def create(self, messages, system=None, tools=None, max_tokens=4096,
               model=None):
        self.calls.append({"system": system, "tools": tools})
        return LlmResponse(content=self.script.pop(0), tool_calls=[],
                           finish_reason="stop",
                           usage={"total_tokens": 5})


class FakeRunner:
    def __init__(self):
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return {"status": "completed",
                "content": '{"status": "completed", "summary": "ok", '
                           '"evidence": ["e1"]}',
                "budget": {"total_tokens": 4}}


class TestPlanToolContract(unittest.TestCase):
    def test_tool_defs_and_handlers_are_paired(self):
        system = PlanSystem(FakeClient([]))
        defs = {item["function"]["name"] for item in system.tool_defs()}
        handlers = set(system.handlers())
        self.assertEqual(defs, set(PLAN_TOOL_NAMES))
        self.assertEqual(handlers, defs)
        self.assertTrue(all(callable(fn) for fn in system.handlers().values()))

    def test_plan_tools_are_not_readonly_or_agent_tools(self):
        self.assertFalse(PLAN_TOOL_NAMES & READONLY_TOOL_NAMES)
        self.assertFalse(PLAN_TOOL_NAMES & SUBAGENT_TOOLS)
        self.assertFalse(PLAN_TOOL_NAMES & TEAMMATE_TOOLS)


class TestPlanSystem(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.tasks = TaskSystem(tasks_dir=self.root / "tasks")
        self.store = PlanStore(root=self.root / "plans")
        self.memory = MemoryStore(root=self.root / "memory", write_index=False)
        self.runner = FakeRunner()

    def system(self, client=None, step_runner="default"):
        return PlanSystem(
            client or FakeClient([plan_payload()]),
            tasks=self.tasks, memory=self.memory, store=self.store,
            metrics=TeamMetrics(),
            step_runner=(self.runner if step_runner == "default"
                         else step_runner),
            tool_names_provider=lambda: ["read_file", "execute_plan"])

    def test_construction_is_lazy(self):
        client = FakeClient([])
        system = self.system(client)
        self.assertEqual(client.calls, [])
        self.assertFalse((self.root / "plans").exists())
        self.assertFalse((self.root / "memory").exists())
        self.assertEqual(self.tasks.list_tasks(), [])
        self.assertIsNotNone(system.store.root)

    def test_choose_strategy(self):
        system = self.system()
        planned = system.run_choose_strategy("请制定计划重构登录模块")
        self.assertIn("Strategy: plan", planned)
        self.assertIn("Replan triggers", planned)
        simple = system.run_choose_strategy("解释一下这个函数")
        self.assertIn("Strategy: react", simple)
        self.assertIn("Error", system.run_choose_strategy(""))

    def test_create_plan_persists_and_status(self):
        system = self.system()
        message = system.run_create_plan("demo goal")
        self.assertIn("Created plan_", message)
        plans = self.store.list_plans()
        self.assertEqual(len(plans), 1)
        plan = plans[0]
        self.assertEqual(plan.status, "draft")
        self.assertIn("planner_usage", plan.signals)
        self.assertIn("read the file", system.run_plan_status(plan.id))
        self.assertIn(plan.id, system.run_plan_status())
        self.assertIn("No plans", PlanSystem(
            FakeClient([]), tasks=self.tasks,
            store=PlanStore(root=self.root / "empty")).run_plan_status())

    def test_plan_execute_creates_runs_and_counts_usage(self):
        system = self.system(FakeClient([plan_payload()]))
        message = system.run_plan_execute("demo goal")
        self.assertIn("completed", message)
        self.assertIn('"total_tokens": 9', message)
        plan = self.store.list_plans()[0]
        self.assertEqual(plan.status, "completed")
        tasks = [task for task in self.tasks.list_tasks(
            include_completed=True) if task.plan_id == plan.id]
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].status, "completed")
        self.assertEqual(len(self.runner.calls), 1)
        tool_names = self.runner.calls[0]["tool_names"]
        self.assertIn("read_file", tool_names)
        self.assertNotIn("execute_plan", tool_names)
        hits = self.memory.search(namespaces=["working"], top_k=5)
        self.assertTrue(any("step:s1:output" in hit.item.key for hit in hits))

    def test_execute_plan_by_id_and_missing(self):
        system = self.system(FakeClient([plan_payload()]))
        system.run_create_plan("demo goal")
        plan_id = self.store.list_plans()[0].id
        message = system.run_execute_plan(plan_id)
        self.assertIn("completed", message)
        self.assertIn("Error", system.run_execute_plan("plan_missing"))
        self.assertIn("Error", system.run_execute_plan("../escape"))

    def test_execute_requires_step_runner(self):
        system = self.system(step_runner=None)
        self.assertIn("unavailable", system.run_execute_plan("plan_x"))
        self.assertIn("unavailable", system.run_plan_execute("goal"))

    def test_planner_invalid_json_returns_error(self):
        system = self.system(FakeClient(["not json"]))
        message = system.run_create_plan("demo")
        self.assertIn("Error", message)
        self.assertEqual(self.store.list_plans(), [])


if __name__ == "__main__":
    unittest.main()
