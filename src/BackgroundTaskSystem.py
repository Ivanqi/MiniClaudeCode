"""后台任务系统:慢速工具的异步执行与结果回收。

慢速工具先返回占位的 tool_result,主循环可以继续推进;
真实输出之后以 task_notification 的形式注入回对话。
"""
import threading

from src.BasicOper import BASIC_OPER, BasicOper
from src.HookSystem import HOOK_SYSTEM, HookSystem
from src.LlmClient import ToolCall
from src.Terminal import Terminal


class BackgroundTaskSystem:
    """后台任务系统:识别慢速命令、异步执行、收集完成通知。"""

    SLOW_KEYWORDS = ["install", "build", "test", "deploy", "compile",
                     "docker build", "pip install", "npm install",
                     "cargo build", "pytest", "make"]

    def __init__(self, hooks: HookSystem = HOOK_SYSTEM,
                 basic: BasicOper = BASIC_OPER):
        self.hooks = hooks
        self.basic = basic
        self._counter = 0
        self._tasks: dict[str, dict] = {}
        self._results: dict[str, str] = {}
        self._lock = threading.Lock()

    @classmethod
    def is_slow_operation(cls, tool_name: str, tool_input: dict) -> bool:
        if tool_name != "bash":
            return False
        command = tool_input.get("command", "").lower()
        return any(keyword in command for keyword in cls.SLOW_KEYWORDS)

    @classmethod
    def should_run_background(cls, tool_name: str, tool_input: dict) -> bool:
        if tool_name != "bash":
            return False
        return bool(tool_input.get("run_in_background")) \
            or cls.is_slow_operation(tool_name, tool_input)

    def start(self, tool_call: ToolCall, handlers: dict) -> str:
        """异步执行一个工具调用,返回后台任务 id。"""
        self._counter += 1
        bg_id = f"bg_{self._counter:04d}"
        command = tool_call.arguments.get("command", tool_call.name)

        def worker():
            handler = handlers.get(tool_call.name)
            result = self.basic.call_tool_handler(
                handler, tool_call.arguments, tool_call.name)
            self.hooks.trigger("PostToolUse", tool_call, result)
            with self._lock:
                self._tasks[bg_id]["status"] = "completed"
                self._results[bg_id] = str(result)

        with self._lock:
            self._tasks[bg_id] = {
                "tool_use_id": tool_call.id,
                "command": command,
                "status": "running",
            }
        threading.Thread(target=worker, daemon=True).start()
        Terminal.info("background", f"{bg_id}: {str(command)[:60]}", Terminal.YELLOW)
        return bg_id

    def collect_results(self) -> list[str]:
        """收集已完成的后台任务,格式化为 task_notification 文本。"""
        with self._lock:
            ready = [bg_id for bg_id, task in self._tasks.items()
                     if task["status"] == "completed"]
        notifications = []
        for bg_id in ready:
            with self._lock:
                task = self._tasks.pop(bg_id)
                output = self._results.pop(bg_id, "")
            summary = output[:200] if len(output) > 200 else output
            notifications.append(
                f"<task_notification>\n"
                f"  <task_id>{bg_id}</task_id>\n"
                f"  <status>completed</status>\n"
                f"  <command>{task['command']}</command>\n"
                f"  <summary>{summary}</summary>\n"
                f"</task_notification>")
        return notifications


# 全局单例。
BACKGROUND_TASKS = BackgroundTaskSystem()
