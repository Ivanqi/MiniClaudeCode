"""MCP 元数据、超时与输出上限测试。"""
import time
import unittest
from unittest import mock

from src.Configs import Configs
from src.McpClient import MCPClient
from src.McpSystem import McpSystem


class TestMcpMetadata(unittest.TestCase):
    def test_mock_annotations(self):
        mcp = McpSystem()
        mcp.connect("docs")
        mcp.connect("deploy")
        mcp.merged_tools()
        self.assertTrue(mcp.metadata("mcp__docs__search")
                        ["annotations"]["readOnlyHint"])
        self.assertTrue(mcp.metadata("mcp__deploy__trigger")
                        ["annotations"]["destructiveHint"])
        self.assertTrue(mcp.metadata("mcp__deploy__status")
                        ["annotations"]["readOnlyHint"])

    def test_description_fallback(self):
        mcp = McpSystem()
        client = MCPClient("custom")
        client.register(
            tool_defs=[{"name": "lookup",
                        "description": "Read-only lookup. (readOnly)",
                        "inputSchema": {"type": "object"}}],
            handlers={"lookup": lambda: "x"})
        mcp.clients["custom"] = client
        mcp.merged_tools()
        self.assertTrue(mcp.metadata("mcp__custom__lookup")
                        ["annotations"]["readOnlyHint"])

    def test_unannotated_tool_has_no_hints(self):
        mcp = McpSystem()
        client = MCPClient("plain")
        client.register(
            tool_defs=[{"name": "do", "description": "Do something.",
                        "inputSchema": {"type": "object"}}],
            handlers={"do": lambda: "x"})
        mcp.clients["plain"] = client
        mcp.merged_tools()
        self.assertEqual(
            mcp.metadata("mcp__plain__do")["annotations"], {})


class TestMcpClient(unittest.TestCase):
    def test_timeout(self):
        client = MCPClient("slow", timeout=0.05)
        client.register(
            tool_defs=[{"name": "slow", "description": "slow",
                        "inputSchema": {"type": "object"}}],
            handlers={"slow": lambda: time.sleep(0.3) or "late"})
        self.assertIn("timeout", client.call_tool("slow", {}))

    def test_output_truncated(self):
        client = MCPClient("big", timeout=1)
        client.register(
            tool_defs=[{"name": "big", "description": "big",
                        "inputSchema": {"type": "object"}}],
            handlers={"big": lambda: "x" * 500})
        with mock.patch.object(Configs, "MCP_MAX_OUTPUT_CHARS", 100):
            result = client.call_tool("big", {})
        self.assertIn("[truncated", result)
        self.assertLess(len(result), 200)

    def test_zero_argument_handler(self):
        client = MCPClient("zero", timeout=1)
        client.register(
            tool_defs=[{"name": "v", "description": "v",
                        "inputSchema": {"type": "object"}}],
            handlers={"v": lambda: "value"})
        self.assertEqual(client.call_tool("v", {}), "value")

    def test_unknown_tool(self):
        client = MCPClient("x", timeout=1)
        self.assertIn("unknown tool", client.call_tool("nope", {}))


if __name__ == "__main__":
    unittest.main()
