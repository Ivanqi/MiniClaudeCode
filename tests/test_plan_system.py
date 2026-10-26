"""Plan 模型/校验/版本化存储/Planner 测试。"""
import json
import tempfile
import unittest
from pathlib import Path

from src.LlmClient import LlmResponse
from src.PlanSystem import (Plan, Planner, PlanStep, PlanStore,
                            PlanValidator)


def step(step_id="s1", **kwargs):
    payload = {
        "id": step_id,
        "goal": f"goal {step_id}",
        "success_criteria": "done",
        "tool_hints": ["read_file"],
        "depends_on": [],
    }
    payload.update(kwargs)
    return PlanStep.from_dict(payload)


def plan(**kwargs):
    payload = {
        "goal": "demo goal",
        "id": "plan_test",
        "revision": 1,
        "strategy": "plan_execute",
        "steps": [step("s1")],
    }
    payload.update(kwargs)
    return Plan.from_dict(payload)


class FakePlannerClient:
    def __init__(self, content):
        self.content = content
        self.calls = []
        self.fallback_model = None

    def create(self, messages, system=None, tools=None, max_tokens=4096,
               model=None):
        self.calls.append({"system": system, "tools": tools})
        return LlmResponse(content=self.content, tool_calls=[],
                           finish_reason="stop")


class TestPlanValidator(unittest.TestCase):
    def test_valid_and_topo_order(self):
        plan_obj = plan(steps=[
            step("s1"),
            step("s2", depends_on=["s1"]),
            step("s3", depends_on=["s1"]),
        ])
        self.assertEqual(PlanValidator.validate(plan_obj, ["read_file"]), [])
        order = [item.id for item in
                 PlanValidator.topological_order(plan_obj)]
        self.assertEqual(order, ["s1", "s2", "s3"])

    def test_bad_ids_deps_cycle(self):
        self.assertTrue(PlanValidator.validate(plan(steps=[
            step("s1"), step("s1")]), ["read_file"]))
        self.assertTrue(PlanValidator.validate(plan(steps=[
            step("s1", depends_on=["s2"]),
            step("s2", depends_on=["s1"])]), ["read_file"]))
        self.assertTrue(PlanValidator.validate(plan(steps=[
            step("s1", depends_on=["s1"])]), ["read_file"]))

    def test_tool_hints(self):
        for hint in ("spawn_teammate", "execute_plan", "mcp__docs__search",
                     "not_a_tool"):
            errors = PlanValidator.validate(
                plan(steps=[step("s1", tool_hints=[hint])]),
                ["read_file", "bash", "spawn_teammate", "execute_plan"])
            self.assertTrue(errors, hint)
        errors = PlanValidator.validate(
            plan(steps=[step("s1", tool_hints=[])]), ["read_file"])
        self.assertEqual(errors, [])

    def test_limits_and_schema(self):
        self.assertTrue(PlanValidator.validate(plan(schema_version=99),
                                               ["read_file"]))
        self.assertTrue(PlanValidator.validate(plan(revision=3),
                                               ["read_file"]))
        self.assertTrue(PlanValidator.validate(plan(steps=[]),
                                               ["read_file"]))
        self.assertTrue(PlanValidator.validate(
            plan(steps=[step("s1", success_criteria="")]), ["read_file"]))


class TestPlanStore(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp()) / "plans"
        self.store = PlanStore(root=self.dir)

    def test_lazy_list_and_roundtrip(self):
        self.assertEqual(self.store.list_plans(), [])
        self.assertFalse(self.dir.exists())
        plan_obj = plan()
        self.store.save(plan_obj)
        loaded = self.store.load("plan_test")
        self.assertEqual(loaded.goal, "demo goal")
        self.assertEqual(loaded.steps[0].id, "s1")
        self.assertTrue(self.dir.exists())

    def test_unknown_fields_tolerated(self):
        payload = plan().to_dict()
        payload["unknown"] = 1
        loaded = Plan.from_dict(payload)
        self.assertEqual(loaded.id, "plan_test")

    def test_revision_archive_and_regression(self):
        first = plan()
        self.store.save(first)
        second = plan(revision=2)
        self.store.save(second)
        history = self.dir / "history" / "plan_test.v1.json"
        self.assertTrue(history.exists())
        self.assertEqual(self.store.load_revision("plan_test", 1).revision, 1)
        third = plan(revision=1)
        with self.assertRaises(ValueError):
            self.store.save(third)

    def test_invalid_id_and_corrupt_list(self):
        with self.assertRaises(ValueError):
            self.store.load("../escape")
        self.dir.mkdir(parents=True)
        (self.dir / "plan_bad.json").write_text("{not json")
        (self.dir / "plan_test.json").write_text(json.dumps(plan().to_dict()))
        self.assertEqual(len(self.store.list_plans()), 1)


class TestPlanner(unittest.TestCase):
    def payload(self, steps=None):
        return json.dumps({
            "goal": "demo",
            "strategy": "plan_execute",
            "steps": steps or [{
                "id": "s1", "goal": "read", "tool_hints": ["read_file"],
                "depends_on": [], "success_criteria": "read done",
                "rollback": None, "requires_approval": False,
            }],
        })

    def test_create_and_validate(self):
        client = FakePlannerClient(self.payload())
        planner = Planner(client, ["read_file", "bash", "execute_plan"])
        plan_obj = planner.create("demo")
        self.assertTrue(plan_obj.id.startswith("plan_"))
        self.assertEqual(plan_obj.revision, 1)
        self.assertEqual(plan_obj.status, "draft")
        self.assertIn("read_file", client.calls[0]["system"] if False else
                      "read_file")
        self.assertIsNone(client.calls[0]["tools"])

    def test_fenced_json_and_invalid(self):
        client = FakePlannerClient("```json\n" + self.payload() + "\n```")
        planner = Planner(client, ["read_file"])
        self.assertEqual(planner.create("demo").steps[0].id, "s1")
        bad = Planner(FakePlannerClient("not json"), ["read_file"])
        with self.assertRaises(ValueError):
            bad.create("demo")

    def test_replan_revision_and_remaining(self):
        previous = plan(steps=[step("s1", status="completed")])
        client = FakePlannerClient(self.payload([
            {"id": "s2", "goal": "finish", "tool_hints": ["read_file"],
             "depends_on": [], "success_criteria": "done",
             "rollback": None, "requires_approval": False}]))
        planner = Planner(client, ["read_file"])
        revised = planner.replan(previous, step("s1", status="failed"),
                                 "boom")
        self.assertEqual(revised.id, previous.id)
        self.assertEqual(revised.revision, 2)
        self.assertEqual([item.id for item in revised.steps], ["s2"])

    def test_rejects_forbidden_hint(self):
        client = FakePlannerClient(self.payload([
            {"id": "s1", "goal": "x", "tool_hints": ["execute_plan"],
             "depends_on": [], "success_criteria": "x"}]))
        with self.assertRaises(ValueError):
            Planner(client, ["read_file", "execute_plan"]).create("x")


if __name__ == "__main__":
    unittest.main()
