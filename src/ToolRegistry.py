"""工具注册表:把各系统提供的工具与 MCP 工具合并为统一工具池。

每个系统实现 tool_defs()/handlers() 两个接口;
新增工具 = 在所属系统里实现 + 注册该系统,无需改动调度代码。
"""
from src.McpSystem import MCP_SYSTEM, McpSystem
from src.ToolPolicy import READONLY_TOOL_NAMES as POLICY_READONLY_TOOL_NAMES


class ToolRegistry:
    """工具注册表:聚合内置工具与 MCP 工具。"""

    # 本地小模型只读工具白名单(单一定义在 ToolPolicy,避免两处漂移)。
    READONLY_TOOL_NAMES = POLICY_READONLY_TOOL_NAMES
    # compact 由 AgentLoop 特判,故意没有 handler。
    HANDLERLESS_TOOLS = {"compact"}

    def __init__(self, mcp: McpSystem = MCP_SYSTEM):
        self.mcp = mcp
        self._systems: list = []

    def register_system(self, system):
        """注册一个提供 tool_defs()/handlers() 的系统。"""
        self._systems.append(system)

    @staticmethod
    def _system_name(system) -> str:
        return type(system).__name__

    def _assemble_systems(self) -> tuple[list[dict], dict]:
        """合并系统工具并做重名/handler 完整性校验。"""
        tools, handlers = [], {}
        def_sources, handler_sources = {}, {}
        for system in self._systems:
            source = self._system_name(system)
            for tool_def in system.tool_defs():
                name = tool_def["function"]["name"]
                if name in def_sources:
                    raise ValueError(
                        f"duplicate tool definition '{name}' from "
                        f"{def_sources[name]} and {source}")
                def_sources[name] = source
                tools.append(tool_def)
            for name, handler in system.handlers().items():
                if name in handler_sources:
                    raise ValueError(
                        f"duplicate tool handler '{name}' from "
                        f"{handler_sources[name]} and {source}")
                handler_sources[name] = source
                handlers[name] = handler
        missing = [name for name in def_sources
                   if name not in handlers
                   and name not in self.HANDLERLESS_TOOLS]
        if missing:
            raise ValueError(
                "tool definitions without handlers: " + ", ".join(sorted(missing)))
        return tools, handlers

    def assemble(self, slim: bool = False,
                 allowlist: set[str] | None = None) -> tuple[list[dict], dict]:
        """合并内置工具 + 所有已连接 MCP 工具,返回 (定义列表, 处理器字典)。

        slim=True:只保留只读白名单工具(不含 MCP 工具,能力未知一律排除),
        供本地小模型调用。allowlist 非 None 时再按工具名收窄(Plan step 用)。
        """
        tools, handlers = self._assemble_systems()
        if slim:
            tools = [t for t in tools
                     if t["function"]["name"] in self.READONLY_TOOL_NAMES]
            handlers = {name: handler for name, handler in handlers.items()
                        if name in self.READONLY_TOOL_NAMES}
        else:
            mcp_tools, mcp_handlers = self.mcp.merged_tools()
            existing = {t["function"]["name"] for t in tools}
            for tool_def in mcp_tools:
                name = tool_def["function"]["name"]
                if name in existing or name in handlers:
                    raise ValueError(f"duplicate MCP tool name '{name}'")
                existing.add(name)
                tools.append(tool_def)
            handlers.update(mcp_handlers)
        if allowlist is not None:
            names = set(allowlist)
            tools = [t for t in tools
                     if t["function"]["name"] in names]
            handlers = {name: handler for name, handler in handlers.items()
                        if name in names}
        return tools, handlers

    def metadata(self, name: str) -> dict:
        getter = getattr(self.mcp, "metadata", None)
        return getter(name) if getter else {}
