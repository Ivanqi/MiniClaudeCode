"""MCP 客户端:发现并调用单个 MCP 服务器的工具(教学用 mock 实现)。

MCP 被建模为"晚绑定"工具:先 connect,再把发现的服务器工具
以 mcp__server__tool 命名合并进常规工具池。
"""


class MCPClient:
    """单个 MCP 服务器的客户端,持有其工具定义与处理器。"""

    def __init__(self, name: str):
        self.name = name
        self.tools: list[dict] = []
        self._handlers: dict[str, callable] = {}

    def register(self, tool_defs: list[dict], handlers: dict[str, callable]):
        self.tools = tool_defs
        self._handlers = handlers

    def call_tool(self, tool_name: str, args: dict) -> str:
        handler = self._handlers.get(tool_name)
        if not handler:
            return f"MCP error: unknown tool '{tool_name}'"
        try:
            return handler(**args)
        except Exception as e:
            return f"MCP error: {e}"
