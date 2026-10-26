"""ToolRegistry 完整性测试:重名冲突、缺失 handler、compact 例外。"""
import unittest

from src.LlmClient import make_function_tool
from src.ToolRegistry import ToolRegistry


class FakeSystem:
    def __init__(self, defs, handlers):
        self._defs = defs
        self._handlers = handlers

    def tool_defs(self):
        return self._defs

    def handlers(self):
        return self._handlers


def tool(name):
    return make_function_tool(name, "d", {"type": "object"})


class TestRegistryIntegrity(unittest.TestCase):
    def test_duplicate_definitions_raise(self):
        registry = ToolRegistry()
        registry.register_system(FakeSystem([tool("x")], {"x": lambda: "a"}))
        registry.register_system(FakeSystem([tool("x")], {"x": lambda: "b"}))
        with self.assertRaises(ValueError):
            registry.assemble()

    def test_definition_without_handler_raises(self):
        registry = ToolRegistry()
        registry.register_system(FakeSystem([tool("y")], {}))
        with self.assertRaises(ValueError):
            registry.assemble()

    def test_compact_is_exempt(self):
        registry = ToolRegistry()
        registry.register_system(FakeSystem([tool("compact")], {}))
        tools, handlers = registry.assemble()
        self.assertEqual([t["function"]["name"] for t in tools], ["compact"])
        self.assertEqual(handlers, {})

    def test_handler_without_definition_is_allowed(self):
        registry = ToolRegistry()
        registry.register_system(FakeSystem([], {"legacy": lambda: "x"}))
        _, handlers = registry.assemble()
        self.assertIn("legacy", handlers)

    def test_slim_keeps_only_readonly(self):
        registry = ToolRegistry()
        registry.register_system(FakeSystem(
            [tool("read_file"), tool("bash")],
            {"read_file": lambda: "x", "bash": lambda: "x"}))
        tools, handlers = registry.assemble(slim=True)
        self.assertEqual([t["function"]["name"] for t in tools], ["read_file"])
        self.assertEqual(list(handlers), ["read_file"])


if __name__ == "__main__":
    unittest.main()
