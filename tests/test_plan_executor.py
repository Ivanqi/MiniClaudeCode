"""PlanBudget / PlanExecutor:执行、审批、失败/replan、resume 与预算。"""
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from src.Configs import Configs
from src.MemoryStore import MemoryStore
from src.PlanSystem import (Plan, PlanBudget, PlanExecutor, PlanStep,
                            PlanStore)
from src.TaskSystem import TaskSystem
from src.TeamMetrics import TeamMetrics
from src.ToolPolicy import TurnBudget


def make_step(step_id, depends_on=(), **kwargs):
    payload = {
        "id": step_id,
        "goal": f"goal {step_id}",
        "success_criteria": f"criteria {step_id}",
        "tool_hints": ["read_file"],
        "depends_on": list(depends_on),
        "rollback": f"undo {step_id}",
    }
    payload.update(kwargs)
    return PlanStep.from_dict(payload)


def make_plan(steps, plan_id="plan_test", revision=1, goal="demo goal"):
    return Plan.from_dict({
        "goal": goal, "id": plan_id, "revision": revision,
        "strategy": "plan_execute", "steps": steps})


def ok(content, tokens=10, artifacts=None):
    return {"status": "completed", "content": content,
            "budget": {"total_tokens": tokens},
            "artifacts": list(artifacts or [])}


class FakeRunner:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []

    def __call__(self, prompt, policy, budget, tool_names, turn_id):
        self.calls.append({
            "prompt": prompt, "policy": policy, "budget": budget,
            "tool_names": list(tool_names), "turn_id": turn_id,
        })
        if not self.responses:
            return ok("default ok")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakePlanner:
    def __init__(self, replans=None, tools=None):
        self.replans = list(replans or [])
        self.tools = list(tools or ["read_file"])
        self.replan_calls = []
        self.last_usage = {}

    def safe_tools(self):
        return list(self.tools)

    def replan(self, previous, failed_step, observation):
        self.replan_calls.append({
            "revision": previous.revision, "step": failed_step.id,
            "observation": observation})
        self.last_usage = {"total_tokens": 13}
        if not self.replans:
            raise AssertionError("unexpected replan")
        item = self.replans.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeApproval:
    def __init__(self, answers=(True,), on_confirm=None):
        self.answers = list(answers)
        self.requests = []
        self.on_confirm = on_confirm

    def confirm(self, request):
        self.requests.append(request)
        if self.on_confirm is not None:
            self.on_confirm(request)
        return self.answers.pop(0) if self.answers else False


class TestPlanBudget(unittest.TestCase):
    def test_runtime_caps_and_absorption(self):
        now = [100.0]
        with mock.patch.object(Configs, "PLAN_MAX_STEPS", 3), \
                mock.patch.object(Configs, "PLAN_MAX_TOTAL_TOKENS", 100), \
                mock.patch.object(Configs, "PLAN_MAX_WALL_SECONDS", 50), \
                mock.patch.object(Configs, "PLAN_STEP_MAX_ROUNDS", 4), \
                mock.patch.object(Configs, "PLAN_STEP_MAX_TOOL_CALLS", 5):
            budget = PlanBudget(clock=lambda: now[0])
        self.assertEqual(budget.max_steps, 3)
        self.assertEqual(budget.step_max_rounds, 4)
        self.assertEqual(budget.step_max_tool_calls, 5)
        sub = budget.step_budget()
        self.assertEqual(sub.max_rounds, 4)
        self.assertEqual(sub.max_tool_calls, 5)
        self.assertEqual(sub.max_total_tokens, 100)
        self.assertEqual(sub.max_wall_s, 50)
        self.assertEqual(budget.absorb(sub), 0)

        budget.record_usage({"total_tokens": 60})
        budget.record_completed_steps(2)
        budget.begin_step()
        self.assertIn("max plan steps", budget.exhausted_reason())
        self.assertEqual(budget.snapshot()["steps_started"], 3)

        now[0] = 149.0
        self.assertEqual(budget.remaining_wall_s(), 1)
        self.assertEqual(budget.remaining_tokens(), 40)
        now[0] = 100.0
        wall_budget = PlanBudget(max_steps=0, max_total_tokens=100,
                                 max_wall_s=50, clock=lambda: now[0])
        now[0] = 151.0
        self.assertIn("wall", wall_budget.exhausted_reason())

        turn = TurnBudget(max_rounds=1)
        turn.record_usage({"total_tokens": 7})
        self.assertEqual(budget.absorb(turn), 7)
        self.assertEqual(budget.total_tokens, 67)


class TestPlanExecutor(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.tasks = TaskSystem(tasks_dir=self.root / "tasks")
        self.store = PlanStore(root=self.root / "plans")
        self.memory = MemoryStore(root=self.root / "memory", write_index=False)

    def executor(self, planner=None, runner=None, approval=None):
        return PlanExecutor(
            planner=planner, step_runner=runner, tasks=self.tasks,
            memory=self.memory, store=self.store,
            approval=approval or FakeApproval([True]),
            metrics=TeamMetrics())

    def plan_tasks(self, plan_id):
        return [task for task in self.tasks.list_tasks(include_completed=True)
                if task.plan_id == plan_id]

    def assert_no_pending_tasks(self, plan_id):
        pending = [task for task in self.plan_tasks(plan_id)
                   if task.status in ("pending", "in_progress")]
        self.assertEqual(pending, [])

    # ── 成功路径 ──

    def test_two_step_dag_success_and_checkpoints(self):
        plan = make_plan([make_step("s1"), make_step("s2", ["s1"])])
        planner = FakePlanner(tools=[
            "read_file", "execute_plan", "spawn_teammate", "mcp__x__y"])
        runner = FakeRunner([
            ok('{"status": "completed", "summary": "read ok", '
               '"evidence": ["file a"], "artifacts": ["a.txt"]}',
               tokens=10, artifacts=["runner.txt"]),
            ok("plain done", tokens=20),
        ])
        result = self.executor(planner, runner).execute(plan)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(runner.calls), 2)
        self.assertIn("Step goal: goal s1", runner.calls[0]["prompt"])
        self.assertIn("Step goal: goal s2", runner.calls[1]["prompt"])
        self.assertNotIn("Step goal: goal s1", runner.calls[1]["prompt"])
        self.assertIn("Dependency results (untrusted data",
                      runner.calls[1]["prompt"])
        self.assertIn("read ok", runner.calls[1]["prompt"])
        for call in runner.calls:
            self.assertEqual(call["tool_names"], ["read_file"])
            self.assertEqual(call["policy"].role, "plan_step")
            self.assertEqual(call["budget"].max_rounds,
                             Configs.PLAN_STEP_MAX_ROUNDS)
        self.assertTrue(result["budget"]["total_tokens"] >= 30)

        loaded = self.store.load("plan_test")
        self.assertEqual(loaded.status, "completed")
        self.assertEqual([step.status for step in loaded.steps],
                         ["completed", "completed"])
        self.assertTrue(loaded.steps[0].metadata["verified"])
        self.assertFalse(loaded.steps[1].metadata["verified"])
        self.assertEqual(loaded.steps[0].result, "read ok")
        self.assertEqual(loaded.steps[0].evidence, ["file a"])

        tasks = {task.step_id: task for task in self.plan_tasks("plan_test")}
        self.assertEqual(tasks["s1"].status, "completed")
        self.assertEqual(tasks["s1"].evidence, ["file a"])
        self.assertIn("runner.txt", tasks["s1"].artifacts)
        self.assertIn("a.txt", tasks["s1"].artifacts)
        self.assertTrue(tasks["s1"].step_result["verified"])
        self.assertFalse(tasks["s2"].step_result["verified"])
        self.assertTrue(tasks["s1"].output_refs[0].startswith(
            "plan:plan_test:v1:step:s1:output"))
        self.assert_no_pending_tasks("plan_test")

        hits = self.memory.search(namespaces=["working"], top_k=10)
        keys = {hit.item.key for hit in hits}
        self.assertIn("plan:plan_test:v1:step:s1:output", keys)
        self.assertIn("plan:plan_test:v1:step:s2:output", keys)

    def test_dependency_results_are_bounded(self):
        first = make_step("s1", status="completed")
        first.result = "X" * 5000
        plan = make_plan([first, make_step("s2", ["s1"])])
        runner = FakeRunner([ok("done")])
        result = self.executor(FakePlanner(), runner).execute(plan)
        self.assertEqual(result["status"], "completed")
        prompt = runner.calls[0]["prompt"]
        self.assertNotIn("X" * 5000, prompt)
        self.assertIn("[truncated]", prompt)
        self.assertLess(len(prompt), 4500)

    # ── 失败分类 ──

    def test_step_outcome_classification(self):
        cases = [
            ("failed_envelope",
             '{"status": "failed", "summary": "no route"}', "failed"),
            ("malformed_envelope", '{"status": "completed"', "failed"),
            ("runtime_error", {"status": "error", "error": "boom"},
             "failed"),
            ("empty_output", "   ", "failed"),
            ("plain_text", "plain summary", "completed"),
        ]
        for index, (name, content, expected) in enumerate(cases):
            with self.subTest(case=name):
                plan_id = f"plan_case_{index}"
                plan = make_plan([make_step("s1")], plan_id=plan_id)
                payload = (content if isinstance(content, dict)
                           else ok(content))
                runner = FakeRunner([payload])
                result = self.executor(None, runner).execute(plan)
                self.assertEqual(result["status"], expected)
                self.assertEqual(len(runner.calls), 1)
                loaded = self.store.load(plan_id)
                self.assertEqual(loaded.steps[0].status, expected)
                task = self.plan_tasks(plan_id)[0]
                if expected == "failed":
                    self.assertEqual(task.rollback_status, "manual_required")
                    self.assertTrue(result["rollback"])
                    self.assertIn("manual handling only", result["summary"])
                else:
                    self.assertFalse(loaded.steps[0].metadata["verified"])
                    self.assertEqual(task.rollback_status, "")
                self.assert_no_pending_tasks(plan_id)

    def test_budget_exhausted_status_does_not_replan(self):
        plan = make_plan([make_step("s1")])
        runner = FakeRunner([{
            "status": "budget_exhausted", "error": "max rounds",
            "budget": {"total_tokens": 3}}])
        planner = FakePlanner([make_plan([make_step("s2")], revision=2)])
        result = self.executor(planner, runner).execute(plan)
        self.assertEqual(result["status"], "failed")
        self.assertIn("max rounds", result["error"])
        self.assertEqual(planner.replan_calls, [])

    # ── 审批 ──

    def test_approval_denied_is_terminal_and_cleans_up(self):
        plan = make_plan([
            make_step("s1", requires_approval=True), make_step("s2")])
        observed = {}

        def on_confirm(request):
            loaded = self.store.load("plan_test")
            observed["plan_status"] = loaded.status
            observed["step_status"] = loaded.steps[0].status
            task = next(item for item in self.plan_tasks("plan_test")
                        if item.step_id == "s1")
            observed["approval_status"] = task.approval_status
            observed["request"] = request

        approval = FakeApproval([False], on_confirm=on_confirm)
        runner = FakeRunner([ok("should not run")])
        planner = FakePlanner([make_plan([make_step("s2")], revision=2)])
        result = self.executor(planner, runner, approval).execute(plan)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(runner.calls, [])
        self.assertEqual(planner.replan_calls, [])
        self.assertEqual(observed["plan_status"], "awaiting_approval")
        self.assertEqual(observed["step_status"], "awaiting_approval")
        self.assertEqual(observed["approval_status"], "pending")
        self.assertEqual(observed["request"].kind, "plan_step")
        self.assertIn("undo s1", observed["request"].details)

        loaded = self.store.load("plan_test")
        self.assertEqual(loaded.status, "failed")
        self.assertEqual([step.status for step in loaded.steps],
                         ["failed", "skipped"])
        tasks = {task.step_id: task for task in self.plan_tasks("plan_test")}
        self.assertEqual(tasks["s1"].approval_status, "denied")
        self.assertEqual(tasks["s1"].status, "failed")
        self.assertEqual(tasks["s2"].status, "failed")
        self.assertEqual(result["rollback"], [])
        self.assert_no_pending_tasks("plan_test")

    def test_approval_approved_continues(self):
        plan = make_plan([make_step("s1", requires_approval=True)])
        approval = FakeApproval([True])
        runner = FakeRunner([ok("approved work")])
        result = self.executor(None, runner, approval).execute(plan)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(runner.calls), 1)
        task = self.plan_tasks("plan_test")[0]
        self.assertEqual(task.approval_status, "approved")
        self.assertEqual(self.store.load("plan_test").status, "completed")

    def test_approval_exception_fails_closed(self):
        class RaisingApproval:
            def confirm(self, request):
                raise RuntimeError("no tty")

        plan = make_plan([make_step("s1", requires_approval=True)])
        runner = FakeRunner([ok("nope")])
        result = self.executor(None, runner, RaisingApproval()).execute(plan)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(runner.calls, [])
        self.assertIn("approval denied", result["error"])

    # ── LoopGuard / plan_loop ──

    @staticmethod
    def loop_stopped(content="loop stopped", tokens=5):
        return {
            "status": "loop_stopped",
            "content": content,
            "closing_reason": "loop stopped: exact_repeat",
            "budget": {"total_tokens": tokens},
            "loop_signals": [{"kind": "exact_repeat", "severity": "stop"}],
        }

    def test_loop_stopped_replans_once_then_plan_loop(self):
        plan = make_plan([make_step("s1")])
        revised = make_plan([make_step("r1")], revision=2)
        planner = FakePlanner([revised])
        runner = FakeRunner([
            self.loop_stopped("stuck"),
            self.loop_stopped("still stuck"),
        ])
        result = self.executor(planner, runner).execute(plan)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(planner.replan_calls), 1)
        self.assertEqual(len(runner.calls), 2)
        current = self.store.load("plan_test")
        self.assertEqual(current.status, "failed")
        self.assertEqual(current.signals["loop"]["kind"], "plan_loop")

    def test_same_failure_signature_detected_across_revisions(self):
        plan = make_plan([make_step("s1")])
        revised = make_plan([make_step("s1")], revision=2)
        planner = FakePlanner([revised])
        runner = FakeRunner([
            {"status": "error", "error": "boom", "budget": {}},
            {"status": "error", "error": "boom", "budget": {}},
        ])
        result = self.executor(planner, runner).execute(plan)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(planner.replan_calls), 1)
        current = self.store.load("plan_test")
        reason = current.signals["loop"]["evidence"]["reason"]
        self.assertIn("same failure repeated", reason)

    def test_replan_with_completed_steps_is_not_zero_progress_loop(self):
        plan = make_plan([
            make_step("s1", status="completed"),
            make_step("s2", ["s1"]),
        ])
        revised = make_plan([make_step("r1")], revision=2)
        planner = FakePlanner([revised])
        runner = FakeRunner([
            {"status": "error", "error": "s2 boom", "budget": {}},
            {"status": "error", "error": "r1 boom", "budget": {}},
        ])
        result = self.executor(planner, runner).execute(plan)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(planner.replan_calls), 1)
        current = self.store.load("plan_test")
        self.assertNotIn("loop", current.signals)

    def test_loop_classification_keeps_reason(self):
        executor = self.executor()
        success, category, outcome, ev, artifacts, meta = \
            executor._classify_step(self.loop_stopped("summary"))
        self.assertFalse(success)
        self.assertEqual(category, "loop")
        self.assertIn("exact_repeat", outcome)
        self.assertEqual(meta["runtime_status"], "loop_stopped")
        self.assertIn("loop stopped", meta["loop_reason"])

    def test_step_prompt_contains_progress_budget_and_failures(self):
        plan = make_plan([make_step("s1"), make_step("s2", ["s1"])])
        plan.signals = {"failures": [{
            "revision": 1, "step_id": "s0", "category": "runtime",
            "error_hash": "x", "error_excerpt": "boom",
        }]}
        runner = FakeRunner([ok("one"), ok("two")])
        result = self.executor(FakePlanner(), runner).execute(plan)
        self.assertEqual(result["status"], "completed")
        prompt = runner.calls[0]["prompt"]
        self.assertIn("Plan progress: 0/2", prompt)
        self.assertIn("attempt 1", prompt)
        self.assertIn("Plan budget:", prompt)
        self.assertIn("Previous failed attempts", prompt)
        self.assertIn("boom", prompt)
        self.assertLess(len(prompt), 4500)

    # ── replan ──

    def test_failure_replans_once_then_stops(self):
        plan = make_plan([make_step("s1"), make_step("s2", ["s1"])])
        revised = make_plan([
            make_step("r1", rollback="undo r1")], revision=2)
        planner = FakePlanner([revised])
        runner = FakeRunner([
            {"status": "error", "error": "boom",
             "budget": {"total_tokens": 7}},
            {"status": "error", "error": "still broken",
             "budget": {"total_tokens": 9}},
        ])
        result = self.executor(planner, runner).execute(plan)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(planner.replan_calls), 1)
        self.assertEqual(planner.replan_calls[0]["step"], "s1")
        self.assertIn("boom", planner.replan_calls[0]["observation"])
        self.assertEqual(len(runner.calls), 2)
        self.assertIn("undo r1", result["summary"])

        current = self.store.load("plan_test")
        self.assertEqual(current.revision, 2)
        self.assertEqual(current.status, "failed")
        history = self.store.load_revision("plan_test", 1)
        self.assertEqual(history.status, "superseded")
        self.assertEqual([step.status for step in history.steps],
                         ["failed", "skipped"])

        versions = {(task.plan_version, task.step_id): task
                    for task in self.plan_tasks("plan_test")}
        self.assertEqual(versions[(1, "s1")].status, "failed")
        self.assertEqual(versions[(1, "s2")].status, "failed")
        self.assertEqual(versions[(2, "r1")].status, "failed")
        self.assertEqual(versions[(1, "s1")].rollback_status,
                         "manual_required")
        self.assert_no_pending_tasks("plan_test")
        self.assertGreaterEqual(result["budget"]["total_tokens"], 29)

    def test_replan_failure_is_terminal(self):
        plan = make_plan([make_step("s1")])
        planner = FakePlanner([ValueError("bad replan json")])
        runner = FakeRunner([{"status": "error", "error": "boom"}])
        result = self.executor(planner, runner).execute(plan)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(planner.replan_calls), 1)
        self.assertIn("replan failed", result["error"])
        self.assertEqual(len(runner.calls), 1)
        self.assert_no_pending_tasks("plan_test")

    def test_disabled_auto_replan_stops_after_failure(self):
        plan = make_plan([make_step("s1")])
        planner = FakePlanner([make_plan([make_step("s2")], revision=2)])
        runner = FakeRunner([{"status": "error", "error": "boom"}])
        with mock.patch.object(Configs, "PLAN_AUTO_REPLAN", False):
            result = self.executor(planner, runner).execute(plan)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(planner.replan_calls, [])
        self.assertEqual(self.store.load("plan_test").revision, 1)

    def test_revision_limit_blocks_replan(self):
        plan = make_plan([make_step("s1")], revision=2)
        planner = FakePlanner([])
        runner = FakeRunner([{"status": "error", "error": "boom"}])
        with mock.patch.object(Configs, "PLAN_MAX_REVISIONS", 2):
            result = self.executor(planner, runner).execute(plan)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(planner.replan_calls, [])
        self.assertIn("revision limit", result["error"])

    # ── 预算 ──

    def test_plan_budget_exhausted_before_materialize(self):
        plan = make_plan([make_step("s1")])
        plan.signals["planner_usage"] = {"total_tokens": 50}
        runner = FakeRunner([ok("no")])
        budget = PlanBudget(max_total_tokens=5)
        result = self.executor(None, runner).execute(plan, budget=budget)
        self.assertEqual(result["status"], "failed")
        self.assertIn("budget exhausted", result["error"])
        self.assertEqual(runner.calls, [])
        self.assertEqual(self.plan_tasks("plan_test"), [])
        self.assertEqual(self.store.load("plan_test").status, "failed")

    def test_step_budget_precheck_stops_before_second_step(self):
        plan = make_plan([make_step("s1"), make_step("s2")])
        runner = FakeRunner([ok("first", tokens=2)])
        budget = PlanBudget(max_steps=1)
        result = self.executor(None, runner).execute(plan, budget=budget)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(runner.calls), 1)
        self.assertIn("max plan steps", result["error"])
        loaded = self.store.load("plan_test")
        self.assertEqual([step.status for step in loaded.steps],
                         ["completed", "skipped"])
        self.assert_no_pending_tasks("plan_test")

    # ── resume / 并发 / 校验 ──

    def test_resume_backfills_completed_step_from_task(self):
        plan = make_plan([make_step("s1")])
        first = self.executor(None, FakeRunner([ok("original")])).execute(plan)
        self.assertEqual(first["status"], "completed")

        loaded = self.store.load("plan_test")
        loaded.steps[0].status = "pending"
        loaded.steps[0].result = ""
        loaded.steps[0].evidence = []
        loaded.steps[0].metadata = {}
        self.store.save(loaded)

        runner = FakeRunner([ok("must not run")])
        result = self.executor(None, runner).execute(loaded)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(runner.calls, [])
        again = self.store.load("plan_test")
        self.assertEqual(again.steps[0].status, "completed")
        self.assertEqual(again.steps[0].result, "original")

    def test_resume_interrupted_step_fails_without_rerun(self):
        plan = make_plan([make_step("s1"), make_step("s2")])
        task = self.tasks.create_plan_step_task(
            "plan_test", 1, "s1", "goal s1", acceptance="criteria s1")
        second = self.tasks.create_plan_step_task(
            "plan_test", 1, "s2", "goal s2", acceptance="criteria s2")
        began, token = self.tasks.begin_step(task.id)
        self.assertTrue(began)
        plan.steps[0].task_id = task.id
        plan.steps[1].task_id = second.id
        plan.steps[0].status = "running"
        plan.status = "running"
        self.store.save(plan)

        runner = FakeRunner([ok("must not run")])
        result = self.executor(None, runner).execute(plan)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(runner.calls, [])
        self.assertIn("interrupted", result["error"])
        self.assertIn("interrupted", result["error"])
        loaded = self.store.load("plan_test")
        self.assertEqual([step.status for step in loaded.steps],
                         ["failed", "skipped"])
        tasks = {item.step_id: item for item in self.plan_tasks("plan_test")}
        self.assertEqual(tasks["s1"].status, "failed")
        self.assertEqual(tasks["s2"].status, "failed")
        self.assert_no_pending_tasks("plan_test")

    def test_resume_backfills_other_completed_tasks_after_interruption(self):
        plan = make_plan([make_step("s1"), make_step("s2")])
        first = self.tasks.create_plan_step_task(
            "plan_test", 1, "s1", "goal s1", acceptance="criteria s1")
        second = self.tasks.create_plan_step_task(
            "plan_test", 1, "s2", "goal s2", acceptance="criteria s2")
        began, _ = self.tasks.begin_step(first.id)
        self.assertTrue(began)
        began, token = self.tasks.begin_step(second.id)
        self.assertTrue(began)
        self.tasks.complete_step(
            second.id, {"status": "completed", "output": "second done"},
            claim_token=token)
        plan.steps[0].task_id = first.id
        plan.steps[0].status = "running"
        plan.steps[1].task_id = second.id
        plan.status = "running"
        self.store.save(plan)

        runner = FakeRunner([ok("must not run")])
        result = self.executor(None, runner).execute(plan)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(runner.calls, [])
        loaded = self.store.load("plan_test")
        self.assertEqual(loaded.steps[0].status, "failed")
        self.assertEqual(loaded.steps[1].status, "completed")
        self.assertEqual(loaded.steps[1].result, "second done")
        self.assert_no_pending_tasks("plan_test")

    def test_invalid_plan_is_rejected_without_runner(self):
        plan = make_plan([make_step("s1"), make_step("s1")])
        runner = FakeRunner([ok("no")])
        result = self.executor(None, runner).execute(plan)
        self.assertEqual(result["status"], "failed")
        self.assertIn("invalid plan", result["error"])
        self.assertEqual(runner.calls, [])
        self.assertEqual(self.plan_tasks("plan_test"), [])

    def test_concurrent_execute_is_rejected(self):
        plan = make_plan([make_step("s1")])
        entered = threading.Event()
        release = threading.Event()

        def runner(prompt, policy, budget, tool_names, turn_id):
            entered.set()
            release.wait(timeout=5)
            return ok("slow done")

        executor = self.executor(None, runner)
        results = {}

        def run():
            results["first"] = executor.execute(plan)

        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(entered.wait(timeout=5))
        try:
            second = executor.execute(plan)
        finally:
            release.set()
            thread.join(timeout=5)
        self.assertEqual(second["status"], "rejected")
        self.assertIn("already executing", second["error"])
        self.assertEqual(results["first"]["status"], "completed")

    def test_execute_by_id_and_missing_plan(self):
        plan = make_plan([make_step("s1")])
        self.store.save(plan)
        runner = FakeRunner([ok("from id")])
        result = self.executor(None, runner).execute("plan_test")
        self.assertEqual(result["status"], "completed")
        missing = self.executor(None, runner).execute("plan_missing")
        self.assertEqual(missing["status"], "failed")
        self.assertIn("cannot load plan", missing["error"])
        self.assertEqual(len(runner.calls), 1)

    def test_all_steps_completed_short_circuits(self):
        plan = make_plan([make_step("s1", status="completed")])
        plan.status = "validated"
        runner = FakeRunner([ok("no")])
        planner = FakePlanner([])
        result = self.executor(planner, runner).execute(plan)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(runner.calls, [])
        self.assertEqual(planner.replan_calls, [])


if __name__ == "__main__":
    unittest.main()
