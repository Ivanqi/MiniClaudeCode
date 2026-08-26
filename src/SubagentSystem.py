"""子代理系统:task 工具背后的聚焦式子代理。

子代理只暴露最小工具集(bash/文件操作),循环执行直到
不再产生工具调用,最后只返回文本摘要,不向外发送过程消息。
"""
from src.BasicOper import BASIC_OPER, BasicOper
from src.Configs import Configs
from src.HookSystem import HOOK_SYSTEM, HookSystem
from src.LlmClient import (assistant_message, make_function_tool, tool_message,
                           user_message)

SUB_ROUND_LIMIT = 30


class SubagentSystem:
    """子代理系统:spawn_subagent 是 task 工具的实现。"""

    SUB_SYSTEM = (
        f"You are a coding subagent at {Configs.WORKDIR}. "
        "Complete the task, then return a concise final summary. "
        "Do not spawn more agents."
    )

    def __init__(self, client, hooks: HookSystem = HOOK_SYSTEM,
                 basic: BasicOper = BASIC_OPER):
        self.client = client
        self.hooks = hooks
        self.basic = basic

    # ── 子代理工具集 ──

    def sub_tools(self) -> list[dict]:
        return [
            make_function_tool("bash", "Run a shell command.",
                {"type": "object",
                 "properties": {"command": {"type": "string"}},
                 "required": ["command"]}),
            make_function_tool("read_file", "Read file contents.",
                {"type": "object",
                 "properties": {"path": {"type": "string"},
                                "limit": {"type": "integer"},
                                "offset": {"type": "integer"}},
                 "required": ["path"]}),
            make_function_tool("write_file", "Write content to a file.",
                {"type": "object",
                 "properties": {"path": {"type": "string"},
                                "content": {"type": "string"}},
                 "required": ["path", "content"]}),
            make_function_tool("edit_file", "Replace exact text in a file once.",
                {"type": "object",
                 "properties": {"path": {"type": "string"},
                                "old_text": {"type": "string"},
                                "new_text": {"type": "string"}},
                 "required": ["path", "old_text", "new_text"]}),
            make_function_tool("glob", "Find files matching a glob pattern.",
                {"type": "object",
                 "properties": {"pattern": {"type": "string"}},
                 "required": ["pattern"]}),
        ]

    def sub_handlers(self) -> dict:
        return {
            "bash": self.basic.run_bash, "read_file": self.basic.run_read,
            "write_file": self.basic.run_write, "edit_file": self.basic.run_edit,
            "glob": self.basic.run_glob,
        }

    # ── 子代理循环 ──

    def spawn_subagent(self, description: str) -> str:
        messages = [user_message(description)]
        tools = self.sub_tools()
        handlers = self.sub_handlers()
        for _ in range(SUB_ROUND_LIMIT):
            response = self.client.create(
                messages, system=self.SUB_SYSTEM, tools=tools,
                max_tokens=Configs.SUBAGENT_MAX_TOKENS)
            messages.append(assistant_message(response))
            if not response.has_tool_calls:
                break
            results = []
            for call in response.tool_calls:
                blocked = self.hooks.trigger("PreToolUse", call)
                if blocked:
                    output = str(blocked)
                else:
                    handler = handlers.get(call.name)
                    output = self.basic.call_tool_handler(handler, call.arguments, call.name)
                    self.hooks.trigger("PostToolUse", call, output)
                results.append(tool_message(call.id, str(output)))
            messages.extend(results)
        for msg in reversed(messages):
            if msg.get("role") == "assistant":
                text = msg.get("content")
                if isinstance(text, str) and text.strip():
                    return text.strip()
        return "Subagent finished without a text summary."

    # ── 工具层:task ──

    def run_task(self, description: str) -> str:
        return self.spawn_subagent(description)

    def tool_defs(self) -> list[dict]:
        return [
            make_function_tool("task",
                "Launch a focused subagent. Returns only its final summary.",
                {"type": "object",
                 "properties": {"description": {"type": "string"}},
                 "required": ["description"]}),
        ]

    def handlers(self) -> dict:
        return {"task": self.run_task}
