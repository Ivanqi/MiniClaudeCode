"""钩子系统 + 权限管道。

钩子刻意放在工具处理器之外:循环可以在不改动单个工具的情况下
增加权限、日志、停止等行为。注册的回调返回非 None 时,
该返回值会作为拦截结果(如"Permission denied")回传给模型。
"""
from src.BasicOper import BASIC_OPER, BasicOper
from src.Configs import Configs
from src.LlmClient import ToolCall
from src.Terminal import Terminal

HOOK_EVENTS = ("UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop")


class HookSystem:
    """钩子系统:按事件名注册回调,按注册顺序依次触发。"""

    DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if="]
    DESTRUCTIVE = ["rm ", "> /etc/", "chmod 777"]

    def __init__(self, basic: BasicOper = BASIC_OPER):
        self.basic = basic
        self._hooks: dict[str, list] = {event: [] for event in HOOK_EVENTS}
        self._register_defaults()

    # ── 注册与触发 ──

    def register(self, event: str, callback):
        self._hooks[event].append(callback)

    def trigger(self, event: str, *args):
        """依次触发回调;返回第一个非 None 的结果(拦截值)。"""
        for callback in self._hooks[event]:
            result = callback(*args)
            if result is not None:
                return result
        return None

    def _register_defaults(self):
        self.register("UserPromptSubmit", self.user_prompt_hook)
        self.register("PreToolUse", self.permission_hook)
        self.register("PreToolUse", self.log_hook)
        self.register("PostToolUse", self.large_output_hook)
        self.register("Stop", self.stop_hook)

    # ── 内置钩子 ──

    def permission_hook(self, block: ToolCall):
        """权限层在分发之前看到原始工具调用:可拒绝、询问用户或放行。"""
        if block.name == "bash":
            command = block.arguments.get("command", "")
            for pattern in self.DENY_LIST:
                if pattern in command:
                    return f"Permission denied: '{pattern}' is on the deny list"
            if any(token in command for token in self.DESTRUCTIVE):
                Terminal.print(f"\n{Terminal.YELLOW}[permission] destructive command{Terminal.RESET}")
                Terminal.print(f"  {command}")
                choice = input("  Allow? [y/N] ").strip().lower()
                if choice not in ("y", "yes"):
                    return "Permission denied by user"
        if block.name in ("write_file", "edit_file"):
            path = block.arguments.get("path", "")
            try:
                self.basic.safe_path(path)
            except Exception:
                return f"Permission denied: path escapes workspace: {path}"
        if block.name.startswith("mcp__") and "deploy" in block.name:
            Terminal.print(f"\n{Terminal.YELLOW}[permission] MCP destructive-looking tool: "
                           f"{block.name}{Terminal.RESET}")
            choice = input("  Allow? [y/N] ").strip().lower()
            if choice not in ("y", "yes"):
                return "Permission denied by user"
        return None

    def log_hook(self, block: ToolCall):
        Terminal.print(f"{Terminal.GRAY}[HOOK] {block.name}{Terminal.RESET}")
        return None

    def large_output_hook(self, block: ToolCall, output):
        if len(str(output)) > 100000:
            Terminal.print(f"{Terminal.YELLOW}[HOOK] large output from {block.name}: "
                           f"{len(str(output))} chars{Terminal.RESET}")
        return None

    def user_prompt_hook(self, query: str):
        Terminal.print(f"{Terminal.GRAY}[HOOK] UserPromptSubmit: {Configs.WORKDIR}{Terminal.RESET}")
        return None

    def stop_hook(self, messages: list):
        tool_count = sum(1 for msg in messages if msg.get("role") == "tool")
        Terminal.print(f"{Terminal.GRAY}[HOOK] Stop: {tool_count} tool result(s){Terminal.RESET}")
        return None


# 全局单例(导入时注册默认钩子)。
HOOK_SYSTEM = HookSystem()
