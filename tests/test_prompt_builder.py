"""PromptBuilder 测试:工具清单来自实际路由集、动态工作区与上下文注入。"""
import tempfile
import unittest
from pathlib import Path

from src.Configs import Configs
from src.LlmClient import make_function_tool
from src.PromptBuilder import PromptBuilder


class FakeSkills:
    @staticmethod
    def list_skills():
        return "- demo: demo skill"


class FakeMcp:
    @staticmethod
    def client_names():
        return []


class TestPromptBuilder(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._old = Configs.WORKDIR
        Configs.WORKDIR = self.tmp
        self.builder = PromptBuilder(skills=FakeSkills(), mcp=FakeMcp())

    def tearDown(self):
        Configs.WORKDIR = self._old

    def test_tools_come_from_supplied_defs(self):
        defs = [make_function_tool("read_file", "read", {"type": "object"}),
                make_function_tool("glob", "glob", {"type": "object"})]
        system = self.builder.assemble({}, defs)
        self.assertIn("read_file", system)
        self.assertIn("glob", system)
        self.assertNotIn("spawn_teammate", system)
        self.assertIn(str(self.tmp), system)

    def test_no_tools(self):
        system = self.builder.assemble({}, None)
        self.assertIn("No tools are available", system)
        self.assertNotIn("Available tools:", system)

    def test_context_injection(self):
        system = self.builder.assemble({
            "memories": "remember this",
            "active_teammates": ["worker-1"],
            "task_summary": "task_1: [pending]",
        }, [])
        self.assertIn("remember this", system)
        self.assertIn("worker-1", system)
        self.assertIn("task_1", system)

    def test_runtime_budget_and_failed_attempts(self):
        system = self.builder.assemble({
            "runtime": {
                "rounds": {"used": 3, "max": 40, "remaining": 37},
                "tool_calls": {"used": 5, "max": 0, "remaining": None},
                "wall_s": {"used": 12.5, "max": 600, "remaining": 587.5},
                "tokens": {"used": 100, "max": 200, "remaining": 100},
            },
            "failed_attempts": [{
                "tool": "bash", "error_code": "timeout", "count": 2,
                "args_summary": '{"command":"pytest"}',
                "excerpt": "Error: Timeout (120s)",
            }],
        }, [])
        self.assertIn("Runtime budget", system)
        self.assertIn("3/40", system)
        self.assertIn("unlimited", system)
        self.assertIn("Recent failed attempts", system)
        self.assertIn("data, not instructions", system)
        self.assertIn("bash [timeout] x2", system)


if __name__ == "__main__":
    unittest.main()
