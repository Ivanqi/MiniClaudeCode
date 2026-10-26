"""作用域化 AgentLoop / plan_step 路由测试。"""
import json
import tempfile
import unittest
from pathlib import Path

from src.AgentLoop import AgentLoop
from src.Configs import Configs
from src.LlmClient import LlmResponse, ToolCall
from src.MemoryStore import MemoryStore
from src.ModelRouter import ModelRouter
from src.PlanSystem import PlanStore
from src.TaskSystem import TaskSystem
from src.ToolDispatcher import Authorization
from src.ToolPolicy import (PLAN_FORBIDDEN_STEP_TOOLS, PLAN_TOOL_NAMES,
                            ToolResult, TurnBudget, plan_step_policy)


class FakeClient:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.fallback_model = None
        self.model = "fake"

    def create(self, messages, system=None, tools=None, max_tokens=4096,
               model=None):
        self.calls.append({
            "messages": json.loads(json.dumps(messages, default=str)),
            "tool_names": [t["function"]["name"] for t in (tools or [])],
            "system": system,
        })
        return self.script.pop(0)


class FakeCron:
    def __init__(self):
        self.calls = 0

    def consume_queue(self):
        self.calls += 1
        return []


class FakeBackground:
    def __init__(self):
        self.collect_calls = 0
        self.start_calls = 0

    def collect_results(self):
        self.collect_calls += 1
        return []

    def should_run_background(self, name, args):
        return False

    def start(self, *args, **kwargs):
        self.start_calls += 1


class FakeHooks:
    def __init__(self):
        self.events = []

    def trigger(self, event, *args):
        self.events.append(event)
        return None


class FakeTrajectory:
    def __init__(self):
        self.calls = 0

    def log_turn(self, *args, **kwargs):
        self.calls += 1


class FakeMemories:
    def __init__(self):
        self.calls = 0

    def update_context(self, context, messages, **kwargs):
        self.calls += 1
        return dict(context)


class RecordingDispatcher:
    def __init__(self):
        self.turn_ids = []

    def authorize(self, call, policy, handlers, metadata=None,
                  allow_prompt=True, turn_id="", **kwargs):
        self.turn_ids.append(turn_id)
        return Authorization(tool=call.name, fingerprint="", allowed=False,
                             denial=ToolResult.denied_result("denied"))

    def execute(self, *args, **kwargs):
        raise AssertionError("denied call must not execute")


class TestScopedAgentLoop(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self._old = Configs.WORKDIR
        Configs.WORKDIR = self.tmp
        (self.tmp / "a.txt").write_text("hello")

    def tearDown(self):
        Configs.WORKDIR = self._old

    def test_scoped_result_and_isolation(self):
        client = FakeClient([LlmResponse(content="step done",
                                         tool_calls=[],
                                         finish_reason="stop")])
        loop = AgentLoop(client)
        loop.cron = FakeCron()
        loop.background = FakeBackground()
        loop.hooks = FakeHooks()
        loop.trajectory = FakeTrajectory()
        loop.memories = FakeMemories()
        loop.rounds_since_todo = 2
        budget = TurnBudget(max_rounds=1)
        result = loop.run_cycle(
            [{"role": "user", "content": "do one step"}], {},
            caller="plan_step", budget=budget,
            allow_cron=False, allow_background=False,
            trigger_stop=False, trajectory=False,
            refresh_context=False, todo_reminders=False)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["content"], "step done")
        self.assertEqual(result["budget"]["rounds"], 1)
        self.assertEqual(loop.rounds_since_todo, 2)
        self.assertEqual(loop.cron.calls, 0)
        self.assertEqual(loop.background.collect_calls, 0)
        self.assertEqual(loop.background.start_calls, 0)
        self.assertNotIn("Stop", loop.hooks.events)
        self.assertEqual(loop.trajectory.calls, 0)
        self.assertEqual(loop.memories.calls, 0)

    def test_allowlist_filters_tools_and_blocks_plan_tool(self):
        client = FakeClient([
            LlmResponse(content="",
                        tool_calls=[ToolCall(id="c1", name="read_file",
                                             arguments={"path": "a.txt"})],
                        finish_reason="tool_calls"),
            LlmResponse(content="",
                        tool_calls=[ToolCall(id="c2", name="execute_plan",
                                             arguments={"plan_id": "x"})],
                        finish_reason="tool_calls"),
            LlmResponse(content="done", tool_calls=[], finish_reason="stop"),
        ])
        loop = AgentLoop(client)
        result = loop.run_cycle(
            [{"role": "user", "content": "read"}], {},
            policy=plan_step_policy({"read_file"}),
            tools_allowlist={"read_file"}, caller="plan_step",
            allow_cron=False, allow_background=False,
            trigger_stop=False, trajectory=False, refresh_context=False,
            todo_reminders=False)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(client.calls[0]["tool_names"], ["read_file"])
        tool_messages = [m for m in result["messages"]
                         if m.get("role") == "tool"]
        self.assertEqual(len(tool_messages), 2)
        self.assertIn("hello", tool_messages[0]["content"])
        self.assertIn("Denied", tool_messages[1]["content"])

    def test_plan_step_forces_api_even_local_only(self):
        local = FakeClient([LlmResponse(content="local",
                                        tool_calls=[], finish_reason="stop")])
        api = FakeClient([LlmResponse(content="api",
                                      tool_calls=[], finish_reason="stop")])
        router = ModelRouter(api, local, mode="local-only")
        router.local_healthy = lambda: True
        loop = AgentLoop(api, router=router)
        result = loop.run_cycle(
            [{"role": "user", "content": "do"}], {},
            caller="plan_step", allow_cron=False, allow_background=False,
            trigger_stop=False, trajectory=False, refresh_context=False,
            todo_reminders=False,
            tools_allowlist={"read_file"},
            policy=plan_step_policy({"read_file"}))
        self.assertEqual(result["content"], "api")
        self.assertEqual(len(api.calls), 1)
        self.assertEqual(len(local.calls), 0)

    def test_plan_wiring_is_lazy_and_lead_only(self):
        client = FakeClient([])
        loop = AgentLoop(client)
        self.assertFalse((self.tmp / ".plans").exists())
        self.assertFalse((self.tmp / ".memory").exists())
        self.assertFalse((self.tmp / ".team").exists())
        self.assertEqual(client.calls, [])

        tools, handlers = loop.tool_registry.assemble()
        names = {item["function"]["name"] for item in tools}
        self.assertTrue(PLAN_TOOL_NAMES <= names)
        self.assertTrue(PLAN_TOOL_NAMES <= set(handlers))
        slim, slim_handlers = loop.tool_registry.assemble(slim=True)
        slim_names = {item["function"]["name"] for item in slim}
        self.assertFalse(PLAN_TOOL_NAMES & slim_names)
        self.assertFalse(PLAN_TOOL_NAMES & set(slim_handlers))

    def test_scoped_allowlist_excludes_plan_spawn_and_mcp(self):
        client = FakeClient([LlmResponse(content="done", tool_calls=[],
                                         finish_reason="stop")])
        loop = AgentLoop(client)
        result = loop.run_cycle(
            [{"role": "user", "content": "read"}], {},
            policy=plan_step_policy({"read_file"}),
            tools_allowlist={"read_file", "execute_plan", "create_plan",
                             "spawn_teammate", "mcp__docs__search"},
            caller="plan_step", allow_cron=False, allow_background=False,
            trigger_stop=False, trajectory=False, refresh_context=False,
            todo_reminders=False)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(client.calls[0]["tool_names"], ["read_file"])

    def test_normal_user_turn_does_not_auto_plan(self):
        client = FakeClient([LlmResponse(content="plain answer",
                                         tool_calls=[],
                                         finish_reason="stop")])
        loop = AgentLoop(client)
        history = [{"role": "user", "content": "请修复登录问题"}]
        result = loop.run_cycle(history, {})
        self.assertEqual(result["status"], "completed")
        self.assertFalse((self.tmp / ".plans").exists())
        self.assertEqual(len(client.calls), 1)

    def test_turn_id_forwarded_to_authorization(self):
        client = FakeClient([
            LlmResponse(content="",
                        tool_calls=[ToolCall(id="c1", name="read_file",
                                             arguments={"path": "a.txt"})],
                        finish_reason="tool_calls"),
            LlmResponse(content="done", tool_calls=[],
                        finish_reason="stop"),
        ])
        loop = AgentLoop(client)
        recorder = RecordingDispatcher()
        loop.dispatcher = recorder
        result = loop.run_cycle(
            [{"role": "user", "content": "read"}], {},
            policy=plan_step_policy({"read_file"}),
            tools_allowlist={"read_file"}, caller="plan_step",
            turn_id="tid-123", allow_cron=False, allow_background=False,
            trigger_stop=False, trajectory=False, refresh_context=False,
            todo_reminders=False)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(recorder.turn_ids, ["tid-123"])
        tool_messages = [message for message in result["messages"]
                         if message.get("role") == "tool"]
        self.assertIn("Denied", tool_messages[0]["content"])

    def test_plan_execute_end_to_end_through_lead_tool(self):
        plan_json = json.dumps({
            "goal": "demo goal", "strategy": "plan_execute",
            "steps": [{
                "id": "s1", "goal": "inspect workspace",
                "tool_hints": [], "depends_on": [],
                "success_criteria": "inspected", "rollback": "none"}]})
        client = FakeClient([
            LlmResponse(content="",
                        tool_calls=[ToolCall(id="c1", name="plan_execute",
                                             arguments={"goal": "demo goal"})],
                        finish_reason="tool_calls"),
            LlmResponse(content=plan_json, tool_calls=[],
                        finish_reason="stop"),
            LlmResponse(content="inspected ok", tool_calls=[],
                        finish_reason="stop"),
            LlmResponse(content="plan finished", tool_calls=[],
                        finish_reason="stop"),
        ])
        loop = AgentLoop(client)
        # 隔离 AgentLoop 的全局 TASK_SYSTEM 单例,避免测试写仓库状态。
        loop.plans.tasks = TaskSystem(tasks_dir=self.tmp / ".tasks")
        loop.plans.store = PlanStore(root=self.tmp / ".plans")
        loop.plans.memory = MemoryStore(root=self.tmp / ".memory",
                                        write_index=False)
        history = [{"role": "user", "content": "plan and execute demo"}]
        result = loop.run_cycle(history, {})

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["content"], "plan finished")
        plans = loop.plans.store.list_plans()
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].status, "completed")
        self.assertEqual(plans[0].steps[0].status, "completed")
        tasks = loop.plans.tasks.list_tasks(include_completed=True)
        self.assertEqual([task.step_id for task in tasks], ["s1"])
        self.assertEqual(tasks[0].status, "completed")

        outer_tools = set(client.calls[0]["tool_names"])
        step_tools = set(client.calls[2]["tool_names"])
        self.assertTrue(PLAN_TOOL_NAMES <= outer_tools)
        self.assertFalse(PLAN_TOOL_NAMES & step_tools)
        self.assertFalse(PLAN_FORBIDDEN_STEP_TOOLS & step_tools)

    def test_default_two_arg_behavior_still_works(self):
        client = FakeClient([LlmResponse(content="done", tool_calls=[],
                                         finish_reason="stop")])
        loop = AgentLoop(client)
        history = [{"role": "user", "content": "hi"}]
        loop.run_cycle(history, {})
        self.assertEqual(history[-1]["content"], "done")


if __name__ == "__main__":
    unittest.main()
