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


if __name__ == "__main__":
    unittest.main()
