"""后台任务系统:慢速工具的异步执行、状态查询与结果回收。

与主循环的分工:
- 主线程先完成 dispatcher.authorize(可能弹确认),再把 Authorization 冻结给 worker;
- worker 只做 execute_pre_authorized,绝不弹确认;
- 状态机 queued → running → completed/failed/cancelled/timed_out,
  异常/超时/取消都产生通知,不再出现"永远 running"。
"""
import queue
import threading
import time

from src.Configs import Configs
from src.LlmClient import make_function_tool
from src.SecurityPolicy import redact
from src.Terminal import Terminal
from src.ToolPolicy import ToolResult, infer_error_code

_ACTIVE_STATES = {"queued", "running"}
_FINISHED_STATES = {"completed", "failed", "cancelled", "timed_out"}
_RETAIN_FINISHED = 50


class BackgroundTaskSystem:
    """后台任务系统:识别慢速命令、异步执行、收集完成通知。"""

    SLOW_KEYWORDS = ["install", "build", "test", "deploy", "compile",
                     "docker build", "pip install", "npm install",
                     "cargo build", "pytest", "make"]

    def __init__(self, max_concurrent: int | None = None,
                 max_queued: int | None = None,
                 timeout: int | None = None):
        self.max_concurrent = (Configs.BACKGROUND_MAX_CONCURRENT
                               if max_concurrent is None else max_concurrent)
        self.max_queued = (Configs.BACKGROUND_MAX_QUEUED
                           if max_queued is None else max_queued)
        self.timeout = (Configs.BACKGROUND_TASK_TIMEOUT_SECONDS
                        if timeout is None else timeout)
        self._semaphore = threading.BoundedSemaphore(
            max(1, self.max_concurrent))
        self._counter = 0
        self._tasks: dict[str, dict] = {}
        self._lock = threading.Lock()

    # ── 识别 ──

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

    # ── 启动 ──

    def start(self, tool_call, handlers: dict, dispatcher=None,
              policy=None, authorization=None, turn_id: str = "") -> str | None:
        """异步启动;容量已满返回 None(调用方可同步执行同一授权)。"""
        with self._lock:
            active = sum(1 for task in self._tasks.values()
                         if task["status"] in _ACTIVE_STATES)
            if active >= self.max_concurrent + self.max_queued:
                return None
            self._counter += 1
            bg_id = f"bg_{self._counter:04d}"
            self._tasks[bg_id] = {
                "tool_use_id": getattr(tool_call, "id", ""),
                "tool": tool_call.name,
                "command": str(tool_call.arguments.get(
                    "command", tool_call.name))[:200],
                "status": "queued",
                "created_at": time.monotonic(),
                "started_at": None,
                "duration_ms": None,
                "exit_code": None,
                "error_code": None,
                "output": "",
                "error": None,
                "notified": False,
                "cancel_event": threading.Event(),
            }
        threading.Thread(
            target=self._worker,
            args=(bg_id, tool_call, handlers, dispatcher, policy,
                  authorization, turn_id),
            daemon=True).start()
        Terminal.info("background", f"{bg_id}: "
                      f"{redact(str(tool_call.arguments.get('command', tool_call.name)))[:60]}",
                      Terminal.YELLOW)
        return bg_id

    def _worker(self, bg_id: str, tool_call, handlers: dict, dispatcher,
                policy, authorization, turn_id: str):
        try:
            with self._semaphore:
                with self._lock:
                    task = self._tasks.get(bg_id)
                    if task is None or task["status"] == "cancelled":
                        return
                    task["status"] = "running"
                    task["started_at"] = time.monotonic()
                result = self._run_with_timeout(
                    bg_id, tool_call, handlers, dispatcher, policy,
                    authorization, turn_id)
                with self._lock:
                    task = self._tasks.get(bg_id)
                    if task is None:
                        return
                    if task["status"] == "cancelled":
                        return  # cancel() 已经写入终态
                    task["output"] = redact(result.content)
                    task["exit_code"] = result.exit_code
                    task["error_code"] = (result.error_code
                                          or ("" if result.ok else
                                              infer_error_code(
                                                  tool_call.name, result)))
                    if task["status"] == "timed_out":
                        return  # _run_with_timeout 已写入 timed_out
                    task["error"] = redact(result.error) if result.error else None
                    task["duration_ms"] = int(
                        (time.monotonic() - (task["started_at"] or
                                             time.monotonic())) * 1000)
                    task["status"] = "completed" if result.ok else "failed"
        except Exception as exc:  # 兜底:worker 永远要落地终态
            with self._lock:
                task = self._tasks.get(bg_id)
                if task is not None and task["status"] not in _FINISHED_STATES:
                    task["status"] = "failed"
                    task["error"] = f"{type(exc).__name__}: {exc}"
                    task["error_code"] = "exception"
                    task["duration_ms"] = int(
                        (time.monotonic() - (task["started_at"] or
                                             task["created_at"])) * 1000)
            self._note_breaker(dispatcher, tool_call.name, "exception")

    def _run_with_timeout(self, bg_id, tool_call, handlers, dispatcher,
                          policy, authorization, turn_id) -> ToolResult:
        outcome: queue.Queue = queue.Queue(maxsize=1)

        def invoke():
            try:
                if dispatcher is not None and authorization is not None:
                    result = dispatcher.execute_pre_authorized(
                        tool_call, policy, handlers, authorization,
                        turn_id=turn_id, bg_id=bg_id,
                        cancel_event=self._tasks[bg_id]["cancel_event"])
                else:
                    result = self._legacy_execute(tool_call, handlers)
                outcome.put(result)
            except Exception as exc:  # dispatcher 内部已兜底,这里再防一层
                outcome.put(ToolResult.failure(
                    f"Error: {type(exc).__name__}: {exc}", error=str(exc),
                    error_code="exception"))

        thread = threading.Thread(target=invoke, daemon=True)
        thread.start()
        try:
            return outcome.get(timeout=self.timeout)
        except queue.Empty:
            self._tasks[bg_id]["cancel_event"].set()
            with self._lock:
                task = self._tasks.get(bg_id)
                if task is not None and task["status"] != "cancelled":
                    task["status"] = "timed_out"
                    task["error"] = f"timeout after {self.timeout}s"
                    task["error_code"] = "timeout"
                    task["duration_ms"] = int(
                        (time.monotonic() - (task["started_at"] or
                                             time.monotonic())) * 1000)
            self._note_breaker(dispatcher, tool_call.name, "timeout")
            return ToolResult.failure(
                f"Error: Timeout ({self.timeout}s)", error="timeout",
                error_code="timeout")

    @staticmethod
    def _legacy_execute(tool_call, handlers: dict) -> ToolResult:
        handler = (handlers or {}).get(tool_call.name)
        if handler is None:
            return ToolResult.failure(
                f"Unknown: {tool_call.name}", error="unknown tool",
                error_code="unknown_tool")
        try:
            raw = handler(**(tool_call.arguments or {}))
        except Exception as exc:
            return ToolResult.failure(
                f"Error: {type(exc).__name__}: {exc}", error=str(exc),
                error_code="exception")
        return ToolResult(content=str(raw))

    @staticmethod
    def _note_breaker(dispatcher, tool: str, error_code: str):
        breaker = getattr(dispatcher, "circuit_breaker", None)
        if breaker is not None:
            breaker.record_failure(tool, error_code)

    # ── 查询与取消 ──

    def collect_results(self) -> list[str]:
        """收集尚未通知的终态任务;保留任务供 get 查询。"""
        notifications = []
        with self._lock:
            ready = [bg_id for bg_id, task in self._tasks.items()
                     if task["status"] in _FINISHED_STATES
                     and not task["notified"]]
            for bg_id in ready:
                task = self._tasks[bg_id]
                task["notified"] = True
                summary = redact(task["output"])[:200] or "(no output)"
                lines = [
                    "<task_notification>",
                    f"  <task_id>{bg_id}</task_id>",
                    f"  <status>{task['status']}</status>",
                    f"  <command>{task['command']}</command>",
                ]
                if task["exit_code"] is not None:
                    lines.append(f"  <exit_code>{task['exit_code']}</exit_code>")
                if task.get("error_code"):
                    lines.append(
                        f"  <error_code>{task['error_code']}</error_code>")
                if task["duration_ms"] is not None:
                    lines.append(
                        f"  <duration_ms>{task['duration_ms']}</duration_ms>")
                if task["error"]:
                    lines.append(f"  <error>{redact(task['error'])[:200]}</error>")
                lines.append(f"  <summary>{summary}</summary>")
                lines.append("</task_notification>")
                notifications.append("\n".join(lines))
            self._prune_locked()
        return notifications

    def get(self, bg_id: str, include_output: bool = True) -> str:
        with self._lock:
            task = self._tasks.get(bg_id)
            if task is None:
                return f"Error: background task {bg_id} not found"
            snapshot = dict(task)
        lines = [f"{bg_id}: {snapshot['status']}",
                 f"  tool: {snapshot['tool']}",
                 f"  command: {snapshot['command']}"]
        if snapshot["exit_code"] is not None:
            lines.append(f"  exit_code: {snapshot['exit_code']}")
        if snapshot.get("error_code"):
            lines.append(f"  error_code: {snapshot['error_code']}")
        if snapshot["duration_ms"] is not None:
            lines.append(f"  duration_ms: {snapshot['duration_ms']}")
        if snapshot["error"]:
            lines.append(f"  error: {redact(snapshot['error'])}")
        if include_output and snapshot["output"]:
            lines.append("  output:")
            lines.append(redact(snapshot["output"])[:2000])
        return "\n".join(lines)

    def cancel(self, bg_id: str) -> str:
        with self._lock:
            task = self._tasks.get(bg_id)
            if task is None:
                return f"Error: background task {bg_id} not found"
            if task["status"] in _FINISHED_STATES:
                return f"Task {bg_id} is already {task['status']}"
            task["cancel_event"].set()
            task["status"] = "cancelled"
            task["error"] = "cancelled by request"
            task["error_code"] = "cancelled"
            task["duration_ms"] = int(
                (time.monotonic() - (task["started_at"] or
                                     task["created_at"])) * 1000)
        return f"Cancelled {bg_id}"

    def _prune_locked(self):
        finished = [bg_id for bg_id, task in self._tasks.items()
                    if task["status"] in _FINISHED_STATES and task["notified"]]
        if len(finished) > _RETAIN_FINISHED:
            for bg_id in finished[:-_RETAIN_FINISHED]:
                self._tasks.pop(bg_id, None)

    # ── 工具层 ──

    def run_get_background_task(self, task_id: str) -> str:
        return self.get(task_id)

    def run_cancel_background_task(self, task_id: str) -> str:
        return self.cancel(task_id)

    def tool_defs(self) -> list[dict]:
        return [
            make_function_tool("get_background_task",
                "Get the status and output of a background task.",
                {"type": "object",
                 "properties": {"task_id": {"type": "string"}},
                 "required": ["task_id"]}),
            make_function_tool("cancel_background_task",
                "Cancel a queued or running background task.",
                {"type": "object",
                 "properties": {"task_id": {"type": "string"}},
                 "required": ["task_id"]}),
        ]

    def handlers(self) -> dict:
        return {
            "get_background_task": self.run_get_background_task,
            "cancel_background_task": self.run_cancel_background_task,
        }


# 全局单例。
BACKGROUND_TASKS = BackgroundTaskSystem()
