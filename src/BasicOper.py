"""基础操作工具:文件读写、bash、glob、会话 todo。

文件工具被限制在工作目录(或指定 cwd)之内,bash 保持开放,
由 HookSystem 中的权限钩子负责拦截危险命令。
"""
import ast
import glob as glob_module
import json
import os
import signal
import subprocess
import time
from pathlib import Path

from src.Configs import Configs
from src.LlmClient import make_function_tool
from src.Terminal import Terminal

BASH_TIMEOUT_SECONDS = 120
OUTPUT_LIMIT_CHARS = 50000


class BasicOper:
    """基础操作工具:提供工具定义、处理器与通用调用辅助。"""

    def __init__(self):
        self.current_todos: list[dict] = []

    # ── 路径安全 ──

    @staticmethod
    def safe_path(p: str, cwd: Path | None = None) -> Path:
        """解析路径并保证其位于 base 目录之内,否则抛 ValueError。"""
        base = Path(cwd or Configs.WORKDIR).resolve()
        path = (base / p).resolve()
        if not path.is_relative_to(base):
            raise ValueError(f"Path escapes workspace: {p}")
        return path

    # ── 基础处理器 ──

    @staticmethod
    def _kill_process_group(proc: subprocess.Popen):
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except OSError:
                pass

    def run_bash_process(self, command: str, cwd: Path | None = None,
                         timeout: int | None = None, env: dict | None = None,
                         argv: list[str] | None = None,
                         cancel_event=None) -> tuple[str, int, bool]:
        """执行 shell 命令,返回 (文本, 退出码, 是否截断)。

        argv 可由 Sandbox 包装;cancel_event 用于后台取消(Popen 进程组 kill)。
        退出码:-1 超时,-2 取消。
        """
        limit = (Configs.BASH_TIMEOUT_DEFAULT if timeout is None else timeout)
        limit = max(1, min(int(limit), Configs.BASH_TIMEOUT_MAX))
        argv = argv or ["/bin/sh", "-c", command]
        try:
            proc = subprocess.Popen(
                argv, cwd=str(cwd or Configs.WORKDIR),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, errors="replace", env=env,
                start_new_session=True)
        except (OSError, ValueError) as exc:
            return f"Error: {exc}", -1, False

        deadline = time.monotonic() + limit
        timed_out = cancelled = False
        output = ""
        while True:
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                self._kill_process_group(proc)
                break
            if time.monotonic() >= deadline:
                timed_out = True
                self._kill_process_group(proc)
                break
            try:
                output = proc.communicate(timeout=0.2)[0]
                break
            except subprocess.TimeoutExpired:
                continue
        if timed_out or cancelled:
            try:
                output = proc.communicate(timeout=2)[0]
            except subprocess.TimeoutExpired:
                output = ""

        if timed_out:
            return f"Error: Timeout ({limit}s)", -1, False
        if cancelled:
            return "Error: Cancelled", -2, False
        text = (output or "").strip()
        truncated = len(text) > OUTPUT_LIMIT_CHARS
        if truncated:
            text = (text[:OUTPUT_LIMIT_CHARS]
                    + f"\n...[truncated {len(text) - OUTPUT_LIMIT_CHARS} chars]")
        return (text or "(no output)"), proc.returncode, truncated

    def run_bash(self, command: str, cwd: Path | None = None,
                 run_in_background: bool = False, timeout: int | None = None,
                 env: dict | None = None, argv: list[str] | None = None,
                 cancel_event=None) -> str:
        # run_in_background 由调度层(BackgroundTaskSystem)消费,直接执行时忽略。
        text, _, _ = self.run_bash_process(
            command, cwd=cwd, timeout=timeout, env=env, argv=argv,
            cancel_event=cancel_event)
        return text

    def run_read(self, path: str, limit: int | None = None,
                 offset: int = 0, cwd: Path | None = None) -> str:
        try:
            lines = self.safe_path(path, cwd).read_text(
                errors="replace").splitlines()
            offset = max(int(offset or 0), 0)
            max_lines = Configs.READ_FILE_MAX_LINES
            limit = max_lines if limit is None else int(limit)
            if limit <= 0:
                limit = max_lines
            limit = min(limit, max_lines)
            lines = lines[offset:]
            if limit < len(lines):
                lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
            return "\n".join(lines)
        except Exception as e:
            return f"Error: {e}"

    def run_write(self, path: str, content: str, cwd: Path | None = None) -> str:
        try:
            fp = self.safe_path(path, cwd)
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(content)
            return f"Wrote {len(content)} bytes to {path}"
        except Exception as e:
            return f"Error: {e}"

    def run_edit(self, path: str, old_text: str, new_text: str,
                 cwd: Path | None = None) -> str:
        try:
            fp = self.safe_path(path, cwd)
            text = fp.read_text()
            if old_text not in text:
                return f"Error: text not found in {path}"
            fp.write_text(text.replace(old_text, new_text, 1))
            return f"Edited {path}"
        except Exception as e:
            return f"Error: {e}"

    def run_glob(self, pattern: str, cwd: Path | None = None) -> str:
        try:
            base = Path(cwd or Configs.WORKDIR).resolve()
            results = []
            for match in glob_module.glob(pattern, root_dir=base):
                if (base / match).resolve().is_relative_to(base):
                    results.append(match)
            return "\n".join(results) if results else "(no matches)"
        except Exception as e:
            return f"Error: {e}"

    # ── todo ──

    def run_todo_write(self, todos: list) -> str:
        todos, error = self._normalize_todos(todos)
        if error:
            return error
        self.current_todos = todos
        Terminal.info("todo", f"updated {len(self.current_todos)} item(s)", Terminal.YELLOW)
        return f"Updated {len(self.current_todos)} todos"

    @staticmethod
    def _normalize_todos(todos):
        """校验并规整 todos 输入(允许 JSON 数组字符串)。"""
        if isinstance(todos, str):
            try:
                todos = json.loads(todos)
            except json.JSONDecodeError:
                try:
                    todos = ast.literal_eval(todos)
                except (SyntaxError, ValueError):
                    return None, "Error: todos must be a list or JSON array string"
        if not isinstance(todos, list):
            return None, "Error: todos must be a list"
        for i, todo in enumerate(todos):
            if not isinstance(todo, dict):
                return None, f"Error: todos[{i}] must be an object"
            if "content" not in todo or "status" not in todo:
                return None, f"Error: todos[{i}] missing 'content' or 'status'"
            if todo["status"] not in ("pending", "in_progress", "completed"):
                return None, f"Error: todos[{i}] has invalid status '{todo['status']}'"
        return todos, None

    # ── 通用调用辅助 ──

    @staticmethod
    def call_tool_handler(handler, args: dict, name: str) -> str:
        """按工具名分发参数;处理器缺失或签名不匹配时给出友好错误。"""
        if not handler:
            return f"Unknown: {name}"
        try:
            return handler(**(args or {}))
        except TypeError as e:
            return f"Error: {e}"

    # ── 工具定义与处理器 ──

    def tool_defs(self) -> list[dict]:
        return [
            make_function_tool("bash", "Run a shell command.",
                {"type": "object",
                 "properties": {"command": {"type": "string"},
                                "timeout": {"type": "integer", "minimum": 1,
                                            "maximum": Configs.BASH_TIMEOUT_MAX},
                                "run_in_background": {"type": "boolean"}},
                 "required": ["command"]}),
            make_function_tool("read_file", "Read file contents.",
                {"type": "object",
                 "properties": {"path": {"type": "string"},
                                "limit": {"type": "integer", "minimum": 1,
                                          "maximum": Configs.READ_FILE_MAX_LINES},
                                "offset": {"type": "integer", "minimum": 0}},
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
            make_function_tool("todo_write",
                "Create and manage a task list for the current session.",
                {"type": "object",
                 "properties": {"todos": {"type": "array",
                     "items": {"type": "object",
                               "properties": {
                                   "content": {"type": "string"},
                                   "status": {"type": "string",
                                              "enum": ["pending", "in_progress", "completed"]}},
                               "required": ["content", "status"]}}},
                 "required": ["todos"]}),
        ]

    def handlers(self) -> dict:
        return {
            "bash": self.run_bash, "read_file": self.run_read,
            "write_file": self.run_write, "edit_file": self.run_edit,
            "glob": self.run_glob, "todo_write": self.run_todo_write,
        }


# 全局单例:被 AgentLoop、TeammateSystem、SubagentSystem、HookSystem 共用。
BASIC_OPER = BasicOper()
