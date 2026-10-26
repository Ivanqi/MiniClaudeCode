"""MCP 客户端:发现并调用单个 MCP 服务器的工具(教学用 mock 实现)。

MCP 被建模为"晚绑定"工具:先 connect,再把发现的服务器工具
以 mcp__server__tool 命名合并进常规工具池。
调用带超时与输出上限,避免不可信服务器拖死主循环或灌入超大结果。
"""
import json
import queue
import threading

from src.Configs import Configs


class MCPClient:
    """单个 MCP 服务器的客户端,持有其工具定义与处理器。"""

    def __init__(self, name: str, timeout: float | None = None):
        self.name = name
        self.tools: list[dict] = []
        self._handlers: dict[str, callable] = {}
        self.timeout = (Configs.MCP_CALL_TIMEOUT
                        if timeout is None else timeout)

    def register(self, tool_defs: list[dict], handlers: dict[str, callable]):
        self.tools = tool_defs
        self._handlers = handlers

    def tool_metadata(self, tool_name: str) -> dict:
        for tool_def in self.tools:
            if tool_def.get("name") == tool_name:
                return {"annotations": dict(tool_def.get("annotations") or {})}
        return {}

    def call_tool(self, tool_name: str, args: dict | None = None) -> str:
        handler = self._handlers.get(tool_name)
        if not handler:
            return f"MCP error: unknown tool '{tool_name}'"

        outcome: queue.Queue = queue.Queue(maxsize=1)

        def invoke():
            try:
                outcome.put(handler(**(args or {})))
            except Exception as exc:
                outcome.put(f"MCP error: {exc}")

        threading.Thread(target=invoke, daemon=True).start()
        try:
            result = outcome.get(timeout=self.timeout)
        except queue.Empty:
            return f"MCP error: timeout ({self.timeout}s)"
        if isinstance(result, str):
            text = result
        else:
            text = json.dumps(result, ensure_ascii=False, default=str)
        limit = Configs.MCP_MAX_OUTPUT_CHARS
        if len(text) > limit:
            text = text[:limit] + f"...[truncated {len(text) - limit} chars]"
        return text
