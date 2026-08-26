"""MCP 系统:管理多个 MCP 服务器连接,并把其工具合并进工具池。

MCP 工具定义使用 MCP 约定的 inputSchema 字段,
在 merged_tools() 中被转换为 DeepSeek 所需的 function 工具格式。
"""
import re

from src.Configs import Configs
from src.LlmClient import make_function_tool
from src.McpClient import MCPClient
from src.Terminal import Terminal

_DISALLOWED_CHARS = re.compile(r'[^a-zA-Z0-9_-]')


def _mock_server_docs() -> MCPClient:
    client = MCPClient("docs")
    client.register(
        tool_defs=[
            {"name": "search", "description": "Search documentation. (readOnly)",
             "inputSchema": {"type": "object",
                             "properties": {"query": {"type": "string"}},
                             "required": ["query"]}},
            {"name": "get_version", "description": "Get API version. (readOnly)",
             "inputSchema": {"type": "object", "properties": {},
                             "required": []}},
        ],
        handlers={
            "search": lambda query: f"[docs] Found 3 results for '{query}'",
            "get_version": lambda: "[docs] API v2.1.0",
        })
    return client


def _mock_server_deploy() -> MCPClient:
    client = MCPClient("deploy")
    client.register(
        tool_defs=[
            {"name": "trigger",
             "description": "Trigger a deployment. (destructive — requires approval in real CC)",
             "inputSchema": {"type": "object",
                             "properties": {"service": {"type": "string"}},
                             "required": ["service"]}},
            {"name": "status", "description": "Check deployment status. (readOnly)",
             "inputSchema": {"type": "object",
                             "properties": {"service": {"type": "string"}},
                             "required": ["service"]}},
        ],
        handlers={
            "trigger": lambda service: f"[deploy] Triggered: {service}",
            "status": lambda service: f"[deploy] {service}: running (v1.4.2)",
        })
    return client


class McpSystem:
    """MCP 系统:连接管理、工具发现与工具池合并。"""

    MOCK_SERVERS = {
        "docs": _mock_server_docs,
        "deploy": _mock_server_deploy,
    }

    def __init__(self):
        self.clients: dict[str, MCPClient] = {}

    @staticmethod
    def normalize_mcp_name(name: str) -> str:
        """把非 [a-zA-Z0-9_-] 字符替换为下划线,生成安全的工具名前缀。"""
        return _DISALLOWED_CHARS.sub('_', name)

    def connect(self, name: str) -> str:
        if name in self.clients:
            return f"MCP server '{name}' already connected"
        factory = self.MOCK_SERVERS.get(name)
        if not factory:
            available = ", ".join(self.MOCK_SERVERS.keys())
            return f"Unknown server '{name}'. Available: {available}"
        mcp_client = factory()
        self.clients[name] = mcp_client
        tool_names = [t["name"] for t in mcp_client.tools]
        Terminal.info("mcp", f"connected: {name} → {tool_names}", Terminal.RED)
        return (f"Connected to MCP server '{name}'. "
                f"Discovered {len(mcp_client.tools)} tools: {', '.join(tool_names)}")

    def client_names(self) -> list[str]:
        return list(self.clients.keys())

    def merged_tools(self) -> tuple[list[dict], dict]:
        """把所有已连接 MCP 服务器的工具合并为 (定义列表, 处理器字典)。"""
        tools, handlers = [], {}
        for server_name, mcp_client in self.clients.items():
            safe_server = self.normalize_mcp_name(server_name)
            for tool_def in mcp_client.tools:
                safe_tool = self.normalize_mcp_name(tool_def["name"])
                prefixed = f"mcp__{safe_server}__{safe_tool}"
                tools.append(make_function_tool(
                    prefixed,
                    tool_def.get("description", ""),
                    tool_def.get("inputSchema", {})))
                handlers[prefixed] = (
                    lambda *, c=mcp_client, t=tool_def["name"], **kw: c.call_tool(t, kw))
        return tools, handlers

    # ── 工具层 ──

    def run_connect_mcp(self, name: str) -> str:
        return self.connect(name)

    def tool_defs(self) -> list[dict]:
        return [
            make_function_tool("connect_mcp",
                "Connect to an MCP server (docs, deploy) and discover tools.",
                {"type": "object",
                 "properties": {"name": {"type": "string"}},
                 "required": ["name"]}),
        ]

    def handlers(self) -> dict:
        return {"connect_mcp": self.run_connect_mcp}


# 全局单例。
MCP_SYSTEM = McpSystem()
