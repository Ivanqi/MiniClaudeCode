"""工具注册表:把各系统提供的工具与 MCP 工具合并为统一工具池。

每个系统实现 tool_defs()/handlers() 两个接口;
新增工具 = 在所属系统里实现 + 注册该系统,无需改动调度代码。
"""
from src.McpSystem import MCP_SYSTEM, McpSystem


class ToolRegistry:
    """工具注册表:聚合内置工具与 MCP 工具。"""

    # 本地小模型只读工具白名单(Phase 4):
    # 纯信息工具,不含 bash/写操作/多代理 —— 3B 级模型误生成 bash 的风险最大,
    # 只给它能安全执行的只读面;模型若幻觉出不存在的工具名,
    # slim handlers 里查不到 → 报错 → 触发质量信号升级 API。
    READONLY_TOOL_NAMES = {"read_file", "glob", "list_tasks", "get_task",
                           "list_crons", "load_skill"}

    def __init__(self, mcp: McpSystem = MCP_SYSTEM):
        self.mcp = mcp
        self._systems: list = []

    def register_system(self, system):
        """注册一个提供 tool_defs()/handlers() 的系统。"""
        self._systems.append(system)

    def assemble(self, slim: bool = False) -> tuple[list[dict], dict]:
        """合并内置工具 + 所有已连接 MCP 工具,返回 (定义列表, 处理器字典)。

        slim=True:只保留只读白名单工具(不含 MCP 工具,能力未知一律排除),
        供本地小模型调用。
        """
        tools, handlers = [], {}
        for system in self._systems:
            tools.extend(system.tool_defs())
            handlers.update(system.handlers())
        if slim:
            tools = [t for t in tools
                     if t["function"]["name"] in self.READONLY_TOOL_NAMES]
            handlers = {name: handler for name, handler in handlers.items()
                        if name in self.READONLY_TOOL_NAMES}
            return tools, handlers
        mcp_tools, mcp_handlers = self.mcp.merged_tools()
        tools.extend(mcp_tools)
        handlers.update(mcp_handlers)
        return tools, handlers
