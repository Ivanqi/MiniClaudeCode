"""工具注册表:把各系统提供的工具与 MCP 工具合并为统一工具池。

每个系统实现 tool_defs()/handlers() 两个接口;
新增工具 = 在所属系统里实现 + 注册该系统,无需改动调度代码。
"""
from src.McpSystem import MCP_SYSTEM, McpSystem


class ToolRegistry:
    """工具注册表:聚合内置工具与 MCP 工具。"""

    def __init__(self, mcp: McpSystem = MCP_SYSTEM):
        self.mcp = mcp
        self._systems: list = []

    def register_system(self, system):
        """注册一个提供 tool_defs()/handlers() 的系统。"""
        self._systems.append(system)

    def assemble(self) -> tuple[list[dict], dict]:
        """合并内置工具 + 所有已连接 MCP 工具,返回 (定义列表, 处理器字典)。"""
        tools, handlers = [], {}
        for system in self._systems:
            tools.extend(system.tool_defs())
            handlers.update(system.handlers())
        mcp_tools, mcp_handlers = self.mcp.merged_tools()
        tools.extend(mcp_tools)
        handlers.update(mcp_handlers)
        return tools, handlers
