"""任务系统:文件持久化的任务记录与生命周期管理。

任务是细粒度的持久记录,保存在 tasks_dir 下的 JSON 文件中。
所有权、依赖关系等上层机制都构建在同一个文件态之上。
"""
import json
import random
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from src.Configs import Configs
from src.LlmClient import make_function_tool
from src.Terminal import Terminal


@dataclass
class Task:
    id: str
    subject: str
    description: str
    status: str
    owner: str | None
    blockedBy: list[str] = field(default_factory=list)
    worktree: str | None = None


class TaskSystem:
    """任务系统:负责任务文件读写与状态流转。"""

    def __init__(self, tasks_dir: Path | None = None):
        self.tasks_dir = tasks_dir or Configs.TASKS_DIR
        self.tasks_dir.mkdir(exist_ok=True)

    # ── 底层读写 ──

    def _task_path(self, task_id: str) -> Path:
        return self.tasks_dir / f"{task_id}.json"

    # 创建任务
    def create_task(self, subject: str, description: str = "",
                    blockedBy: list[str] | None = None) -> Task:
        task = Task(
            id=f"task_{int(time.time())}_{random.randint(0, 9999):04d}",
            subject=subject, description=description,
            status="pending", owner=None, blockedBy=blockedBy or [],
        )
        self.save_task(task)
        return task

    # 任务存储
    def save_task(self, task: Task):
        self._task_path(task.id).write_text(json.dumps(asdict(task), indent=2))

    def load_task(self, task_id: str) -> Task:
        return Task(**json.loads(self._task_path(task_id).read_text()))

    def list_tasks(self) -> list[Task]:
        return [Task(**json.loads(p.read_text()))
                for p in sorted(self.tasks_dir.glob("task_*.json"))]

    def get_task_json(self, task_id: str) -> str:
        return json.dumps(asdict(self.load_task(task_id)), indent=2)

    # ── 状态流转 ──

    def can_start(self, task_id: str) -> bool:
        """依赖规则:所有 blocker 都必须存在且已完成,任务才能被认领。"""
        task = self.load_task(task_id)
        for dep_id in task.blockedBy:
            if not self._task_path(dep_id).exists():
                return False
            if self.load_task(dep_id).status != "completed":
                return False
        return True

    def claim_task(self, task_id: str, owner: str = "agent") -> str:
        task = self.load_task(task_id)
        if task.status != "pending":
            return f"Task {task_id} is {task.status}, cannot claim"
        if task.owner:
            return f"Task {task_id} already owned by {task.owner}"
        if not self.can_start(task_id):
            deps = [d for d in task.blockedBy
                    if self._task_path(d).exists() and self.load_task(d).status != "completed"]
            missing = [d for d in task.blockedBy if not self._task_path(d).exists()]
            parts = []
            if deps: parts.append(f"blocked by: {deps}")
            if missing: parts.append(f"missing deps: {missing}")
            return "Cannot start — " + ", ".join(parts)
        task.owner = owner
        task.status = "in_progress"
        self.save_task(task)
        Terminal.info("claim", f"{task.subject} → in_progress", Terminal.CYAN)
        return f"Claimed {task.id} ({task.subject})"

    def complete_task(self, task_id: str) -> str:
        task = self.load_task(task_id)
        if task.status != "in_progress":
            return f"Task {task_id} is {task.status}, cannot complete"
        task.status = "completed"
        self.save_task(task)
        unblocked = [t.subject for t in self.list_tasks()
                     if t.status == "pending" and t.blockedBy and self.can_start(t.id)]
        Terminal.info("complete", f"{task.subject} ✓", Terminal.GREEN)
        msg = f"Completed {task.id} ({task.subject})"
        if unblocked:
            msg += f"\nUnblocked: {', '.join(unblocked)}"
        return msg

    # ── 工具层(供 ToolRegistry 注册) ──

    def run_create_task(self, subject: str, description: str = "",
                        blockedBy: list[str] | None = None) -> str:
        task = self.create_task(subject, description, blockedBy)
        deps = f" (blockedBy: {', '.join(blockedBy)})" if blockedBy else ""
        Terminal.info("create", f"{task.subject}{deps}", Terminal.BLUE)
        return f"Created {task.id}: {task.subject}{deps}"

    def run_list_tasks(self) -> str:
        tasks = self.list_tasks()
        if not tasks:
            return "No tasks."
        return "\n".join(
            f"  {t.id}: {t.subject} [{t.status}]"
            + (f" (wt:{t.worktree})" if t.worktree else "")
            for t in tasks)

    def run_get_task(self, task_id: str) -> str:
        try:
            return self.get_task_json(task_id)
        except FileNotFoundError:
            return f"Error: task {task_id} not found"

    def run_claim_task(self, task_id: str) -> str:
        try:
            return self.claim_task(task_id, owner="agent")
        except FileNotFoundError:
            return f"Error: task {task_id} not found"

    def run_complete_task(self, task_id: str) -> str:
        try:
            return self.complete_task(task_id)
        except FileNotFoundError:
            return f"Error: task {task_id} not found"

    def tool_defs(self) -> list[dict]:
        return [
            make_function_tool("create_task", "Create a task.",
                {"type": "object",
                 "properties": {"subject": {"type": "string"},
                                "description": {"type": "string"},
                                "blockedBy": {"type": "array", "items": {"type": "string"}}},
                 "required": ["subject"]}),
            make_function_tool("list_tasks", "List all tasks.",
                {"type": "object", "properties": {}, "required": []}),
            make_function_tool("get_task", "Get full task details.",
                {"type": "object",
                 "properties": {"task_id": {"type": "string"}},
                 "required": ["task_id"]}),
            make_function_tool("claim_task", "Claim a pending task.",
                {"type": "object",
                 "properties": {"task_id": {"type": "string"}},
                 "required": ["task_id"]}),
            make_function_tool("complete_task", "Complete an in-progress task.",
                {"type": "object",
                 "properties": {"task_id": {"type": "string"}},
                 "required": ["task_id"]}),
        ]

    def handlers(self) -> dict:
        return {
            "create_task": self.run_create_task,
            "list_tasks": self.run_list_tasks,
            "get_task": self.run_get_task,
            "claim_task": self.run_claim_task,
            "complete_task": self.run_complete_task,
        }


# 全局单例。
TASK_SYSTEM = TaskSystem()
