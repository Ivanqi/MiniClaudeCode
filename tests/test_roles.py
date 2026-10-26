"""RoleSpec/RoleRegistry 测试:内置、覆盖、安全不变量。"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.ActiveTeammates import ActiveTeammates
from src.ApprovalGate import ApprovalGate
from src.AuditLog import AuditLog
from src.Configs import Configs
from src.MessageBus import MessageBus
from src.Roles import (KNOWN_TEAMMATE_TOOLS, RoleError, RoleRegistry,
                       RoleSpec)
from src.Sandbox import Sandbox
from src.TaskSystem import TaskSystem
from src.TeamMetrics import TeamBudget, TeamMetrics
from src.TeammateSystem import TeammateSystem
from src.ToolDispatcher import ToolDispatcher
from src.ToolPolicy import BASH_DENY, TEAMMATE_TOOLS


class TestRoles(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.roles_path = self.tmp / "roles.json"

    def write_roles(self, payload):
        self.roles_path.write_text(json.dumps(payload))

    def test_builtins(self):
        registry = RoleRegistry(path=self.roles_path)
        developer = registry.resolve("developer")
        reviewer = registry.resolve("reviewer")
        self.assertTrue(developer.can_claim)
        self.assertTrue(developer.requires_sandbox)
        self.assertEqual(developer.write_scope, "worktree")
        self.assertFalse(reviewer.can_claim)
        self.assertTrue(reviewer.read_only)
        self.assertTrue(reviewer.allowed_tools
                        <= {"read_file", "glob", "list_tasks"})
        self.assertEqual(registry.spawnable_names(), ["developer"])

    def test_overlay_override_and_new_role(self):
        self.write_roles({
            "schema_version": 1,
            "roles": {
                "developer": {
                    "allowed_tools": ["read_file", "glob"],
                    "write_scope": "none",
                    "read_only": True,
                    "bash_mode": "read_only",
                    "acceptance": ["read only"],
                },
                "researcher": {
                    "allowed_tools": ["read_file", "glob", "list_tasks"],
                    "write_scope": "none",
                    "read_only": True,
                    "bash_mode": "deny",
                    "can_claim": False,
                },
            },
        })
        registry = RoleRegistry(path=self.roles_path)
        developer = registry.resolve("developer")
        self.assertEqual(developer.allowed_tools,
                         frozenset({"read_file", "glob"}))
        self.assertEqual(registry.resolve("researcher").name, "researcher")

    def test_unknown_tool_is_skipped_with_error(self):
        self.write_roles({
            "schema_version": 1,
            "roles": {"bad": {"allowed_tools": ["not_a_tool"]}},
        })
        registry = RoleRegistry(path=self.roles_path)
        with self.assertRaises(RoleError):
            registry.resolve("bad")
        self.assertTrue(registry.errors)
        # 内置仍可用
        self.assertEqual(registry.resolve("developer").name, "developer")

    def test_malformed_json_keeps_builtins(self):
        self.roles_path.write_text("{not json")
        registry = RoleRegistry(path=self.roles_path)
        self.assertEqual(registry.resolve("developer").name, "developer")
        self.assertTrue(registry.errors)

    def test_reviewer_cannot_be_made_writable(self):
        self.write_roles({
            "schema_version": 1,
            "roles": {"reviewer": {
                "allowed_tools": ["read_file", "write_file"],
                "write_scope": "worktree",
                "read_only": False,
                "bash_mode": "full",
                "can_claim": True,
            }},
        })
        registry = RoleRegistry(path=self.roles_path)
        # 非法覆盖被跳过,内置 reviewer 保持只读
        reviewer = registry.resolve("reviewer")
        self.assertTrue(reviewer.read_only)
        self.assertFalse(reviewer.can_claim)
        self.assertTrue(registry.errors)

    def test_worktree_full_bash_forces_sandbox(self):
        self.write_roles({
            "schema_version": 1,
            "roles": {"custom": {
                "allowed_tools": ["bash", "read_file"],
                "write_scope": "worktree",
                "read_only": False,
                "bash_mode": "full",
                "requires_sandbox": False,
                "can_claim": True,
            }},
        })
        spec = RoleRegistry(path=self.roles_path).resolve("custom")
        self.assertTrue(spec.requires_sandbox)
        policy = spec.to_policy(self.tmp)
        self.assertTrue(policy.requires_sandbox)
        self.assertFalse(policy.read_only)

    def test_policies(self):
        registry = RoleRegistry(path=self.roles_path)
        developer = registry.resolve("developer")
        worktree = self.tmp / "wt"
        policy = developer.to_policy(worktree)
        self.assertFalse(policy.read_only)
        self.assertEqual(policy.root(), worktree)
        self.assertTrue(policy.requires_sandbox)
        readonly = developer.to_policy(None)
        self.assertTrue(readonly.read_only)
        self.assertEqual(readonly.writable_roots, ())
        reviewer = registry.resolve("reviewer").to_policy(worktree)
        self.assertTrue(reviewer.read_only)
        self.assertEqual(reviewer.bash_mode, BASH_DENY)
        self.assertEqual(reviewer.root(), worktree)

    def test_reserved_and_invalid_names(self):
        lead = RoleSpec.from_dict("lead", {"allowed_tools": ["read_file"]})
        self.assertTrue(lead.validate())
        spec = RoleSpec.from_dict("bad name", {"allowed_tools": ["read_file"]})
        self.assertTrue(spec.validate())

    def test_strict_mode_raises(self):
        self.write_roles({"schema_version": 1,
                          "roles": {"bad": {"allowed_tools": ["nope"]}}})
        with self.assertRaises(RoleError):
            RoleRegistry(path=self.roles_path, strict=True).resolve("bad")

    def test_known_tools(self):
        self.assertIn("bash", KNOWN_TEAMMATE_TOOLS)
        self.assertIn("list_tasks", KNOWN_TEAMMATE_TOOLS)

    def test_role_filters_exposed_tools_and_spawn_errors(self):
        from src.LlmClient import LlmResponse

        class FakeClient:
            def __init__(self):
                self.calls = []

            def create(self, messages, system=None, tools=None,
                       max_tokens=4096, model=None):
                self.calls.append([tool["function"]["name"]
                                   for tool in (tools or [])])
                return LlmResponse(content="done", tool_calls=[],
                                   finish_reason="stop")

        client = FakeClient()
        bus = MessageBus(mailbox_dir=self.tmp / "mail")
        tasks = TaskSystem(tasks_dir=self.tmp / "tasks")
        registry = RoleRegistry(path=self.roles_path)
        dispatcher = ToolDispatcher(
            hooks=None, audit=AuditLog(directory=self.tmp / "audit"),
            approval=ApprovalGate(interactive=False, auto_approve=set()),
            sandbox=Sandbox(mode="off"))
        system = TeammateSystem(
            client, bus=bus, tasks=tasks, teammates=ActiveTeammates(),
            roles=registry, dispatcher=dispatcher)
        spawn_def = next(tool for tool in system.tool_defs()
                         if tool["function"]["name"] == "spawn_teammate")
        self.assertEqual(
            spawn_def["function"]["parameters"]["properties"]["role"]["enum"],
            ["developer"])
        self.assertIn("unknown role", system.spawn("x", "nope", "p"))
        self.assertIn("review-only", system.spawn("x", "reviewer", "p"))
        self.assertIn("Team session", system.run_team_status())
        self.assertIn("team_status",
                      {tool["function"]["name"]
                       for tool in system.tool_defs()})

        with mock.patch.object(Configs, "IDLE_TIMEOUT", 0):
            system._run_teammate("worker", "developer", "do")
            self.assertEqual(set(client.calls[0]), set(TEAMMATE_TOOLS))
            system._run_teammate("reviewer", "reviewer", "review")
        self.assertEqual(set(client.calls[1]),
                         {"read_file", "glob", "list_tasks"})

    def test_spawn_respects_team_budget(self):
        class FakeClient:
            def create(self, *args, **kwargs):
                raise RuntimeError("not called")

        registry = RoleRegistry(path=self.roles_path)
        metrics = TeamMetrics(budget=TeamBudget(max_spawns=1))
        system = TeammateSystem(
            FakeClient(), bus=MessageBus(mailbox_dir=self.tmp / "mail"),
            tasks=TaskSystem(tasks_dir=self.tmp / "tasks"),
            teammates=ActiveTeammates(), roles=registry, metrics=metrics,
            dispatcher=ToolDispatcher(
                hooks=None, audit=AuditLog(directory=self.tmp / "audit"),
                approval=ApprovalGate(interactive=False, auto_approve=set()),
                sandbox=Sandbox(mode="off")))
        with mock.patch("src.TeammateSystem.threading.Thread") as thread_cls:
            thread_cls.return_value.start.return_value = None
            first = system.spawn("a", "developer", "p")
            second = system.spawn("b", "developer", "p")
        self.assertIn("spawned", first)
        self.assertIn("budget", second)


if __name__ == "__main__":
    unittest.main()
