"""Worktree 系统:隔离的 git worktree 的创建、删除与保留。

worktree 名称会成为文件系统路径,因此校验规则保持严格,
创建/删除/保留三个入口共用同一套校验。
"""
import json
import re
import subprocess
import time
from pathlib import Path

from src.Configs import Configs
from src.LlmClient import make_function_tool
from src.TaskSystem import TASK_SYSTEM, TaskSystem
from src.Terminal import Terminal

VALID_WT_NAME = re.compile(r'^[A-Za-z0-9._-]{1,64}$')
GIT_TIMEOUT_SECONDS = 30


class WorktreeSystem:
    """Worktree 系统:名称校验 + git 操作 + 事件日志。"""

    def __init__(self, tasks: TaskSystem = TASK_SYSTEM,
                 worktrees_dir: Path | None = None):
        self.tasks = tasks
        self.worktrees_dir = worktrees_dir or Configs.WORKTREES_DIR
        self.worktrees_dir.mkdir(exist_ok=True)

    # ── 名称校验 ──

    @staticmethod
    def validate_worktree_name(name: str) -> str | None:
        """校验名称合法,返回错误描述或 None。"""
        if not name:
            return "Worktree name cannot be empty"
        if name in (".", ".."):
            return f"'{name}' is not a valid worktree name"
        if not VALID_WT_NAME.match(name):
            return (f"Invalid worktree name '{name}': "
                    "only letters, digits, dots, underscores, dashes (1-64 chars)")
        return None

    # ── git 操作 ──

    def run_git(self, args: list[str], cwd: Path | None = None) -> tuple[bool, str]:
        try:
            r = subprocess.run(["git"] + args, cwd=cwd or Configs.WORKDIR,
                               capture_output=True, text=True, timeout=GIT_TIMEOUT_SECONDS)
            out = (r.stdout + r.stderr).strip()
            return r.returncode == 0, out[:5000] if out else "(no output)"
        except subprocess.TimeoutExpired:
            return False, "Error: git timeout"

    def log_event(self, event_type: str, worktree_name: str, task_id: str = ""):
        event = {"type": event_type, "worktree": worktree_name,
                 "task_id": task_id, "ts": time.time()}
        events_file = self.worktrees_dir / "events.jsonl"
        with open(events_file, "a") as f:
            f.write(json.dumps(event) + "\n")

    # ── 业务操作 ──

    def create_worktree(self, name: str, task_id: str = "") -> str:
        # 工具层校验是安全边界的一部分:在 git 看到名字之前先拦截非法名称。
        err = self.validate_worktree_name(name)
        if err:
            return f"Error: {err}"
        if task_id:
            try:
                self.tasks.load_task(task_id)
            except FileNotFoundError:
                return f"Error: task {task_id} not found"
        path = self.worktrees_dir / name
        if path.exists():
            return f"Worktree '{name}' already exists at {path}"
        ok, result = self.run_git(["worktree", "add", str(path), "-b", f"wt/{name}", "HEAD"])
        if not ok:
            return f"Git error: {result}"
        if task_id:
            self.bind_task_to_worktree(task_id, name)
        self.log_event("create", name, task_id)
        Terminal.info("worktree", f"created: {name} at {path}", Terminal.YELLOW)
        return f"Worktree '{name}' created at {path}"

    def bind_task_to_worktree(self, task_id: str, worktree_name: str):
        setter = getattr(self.tasks, "set_worktree", None)
        if setter is not None:
            setter(task_id, worktree_name)
            return
        task = self.tasks.load_task(task_id)
        task.worktree = worktree_name
        self.tasks.save_task(task)

    def remove_worktree(self, name: str, discard_changes: bool = False) -> str:
        err = self.validate_worktree_name(name)
        if err:
            return err
        path = self.worktrees_dir / name
        if not path.exists():
            return f"Worktree '{name}' not found"
        try:
            active = [task.id for task in self.tasks.list_tasks()
                      if task.worktree == name and task.status == "in_progress"]
        except Exception:
            active = []
        if active and not discard_changes:
            return (f"Worktree '{name}' has in-progress task(s): "
                    f"{', '.join(active)}. Complete them first, or use "
                    "discard_changes=true after confirmation.")
        if not discard_changes:
            files, commits = self._count_worktree_changes(path)
            if files < 0:
                return "Cannot verify status. Use discard_changes=true to force."
            if files > 0 or commits > 0:
                return (f"Worktree '{name}' has {files} file(s), {commits} commit(s). "
                        "Use discard_changes=true or keep_worktree.")
        ok1, _ = self.run_git(["worktree", "remove", str(path), "--force"])
        if not ok1:
            return f"Failed to remove worktree '{name}'"
        self.run_git(["branch", "-D", f"wt/{name}"])
        self.log_event("remove", name)
        Terminal.info("worktree", f"removed: {name}", Terminal.YELLOW)
        return f"Worktree '{name}' removed"

    def keep_worktree(self, name: str) -> str:
        err = self.validate_worktree_name(name)
        if err:
            return err
        self.log_event("keep", name)
        return f"Worktree '{name}' kept for review (branch: wt/{name})"

    def _count_worktree_changes(self, path: Path) -> tuple[int, int]:
        """统计 worktree 内的未提交文件数与未推送提交数。"""
        try:
            r1 = subprocess.run(["git", "status", "--porcelain"],
                                cwd=path, capture_output=True, text=True, timeout=10)
            files = len([l for l in r1.stdout.strip().splitlines() if l.strip()])
            r2 = subprocess.run(["git", "log", "@{push}..HEAD", "--oneline"],
                                cwd=path, capture_output=True, text=True, timeout=10)
            commits = len([l for l in r2.stdout.strip().splitlines() if l.strip()])
            return files, commits
        except Exception:
            return -1, -1

    # ── 工具层 ──

    def run_create_worktree(self, name: str, task_id: str = "") -> str:
        return self.create_worktree(name, task_id)

    def run_remove_worktree(self, name: str, discard_changes: bool = False) -> str:
        return self.remove_worktree(name, discard_changes)

    def run_keep_worktree(self, name: str) -> str:
        return self.keep_worktree(name)

    def tool_defs(self) -> list[dict]:
        return [
            make_function_tool("create_worktree", "Create an isolated git worktree.",
                {"type": "object",
                 "properties": {"name": {"type": "string"},
                                "task_id": {"type": "string"}},
                 "required": ["name"]}),
            make_function_tool("remove_worktree",
                "Remove a worktree. Refuses if changes exist.",
                {"type": "object",
                 "properties": {"name": {"type": "string"},
                                "discard_changes": {"type": "boolean"}},
                 "required": ["name"]}),
            make_function_tool("keep_worktree", "Keep a worktree for manual review.",
                {"type": "object",
                 "properties": {"name": {"type": "string"}},
                 "required": ["name"]}),
        ]

    def handlers(self) -> dict:
        return {
            "create_worktree": self.run_create_worktree,
            "remove_worktree": self.run_remove_worktree,
            "keep_worktree": self.run_keep_worktree,
        }


# 全局单例,复用全局任务系统。
WORKTREE_SYSTEM = WorktreeSystem()
