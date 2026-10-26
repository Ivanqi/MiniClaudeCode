"""任务系统:文件持久化的任务记录与生命周期管理。

任务是细粒度的持久记录,保存在 tasks_dir 下的 JSON 文件中。
认领/完成/失败/释放都在 fcntl 文件锁 + 进程内 RLock 内完成,
避免两个队友"读 → 检查 → 写"同时认领同一任务;in_progress 任务
带 lease/heartbeat,lead 可以回收过期任务。
"""
import json
import random
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path

from src.Configs import Configs
from src.FileLock import atomic_write_text, file_lock
from src.LlmClient import make_function_tool
from src.SecurityPolicy import validate_task_id
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
    lease_until: float | None = None
    heartbeat_at: float | None = None
    attempts: int = 0
    last_error: str | None = None


class TaskSystem:
    """任务系统:负责任务文件读写与状态流转。"""

    def __init__(self, tasks_dir: Path | None = None):
        self.tasks_dir = tasks_dir or Configs.TASKS_DIR
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self._lock_path = self.tasks_dir / ".lock"
        self._rlock = threading.RLock()

    # ── 底层读写 ──

    def _task_path(self, task_id: str) -> Path:
        err = validate_task_id(task_id)
        if err:
            raise ValueError(err)
        return self.tasks_dir / f"{task_id}.json"

    @contextmanager
    def _locked(self):
        with self._rlock:
            with file_lock(self._lock_path):
                yield

    def _load_unlocked(self, task_id: str) -> Task:
        return Task(**json.loads(self._task_path(task_id).read_text()))

    def _save_unlocked(self, task: Task):
        atomic_write_text(self._task_path(task.id),
                          json.dumps(asdict(task), indent=2, ensure_ascii=False))

    def _list_unlocked(self) -> list[Task]:
        tasks = []
        for path in sorted(self.tasks_dir.glob("task_*.json")):
            try:
                tasks.append(Task(**json.loads(path.read_text())))
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                continue
        return tasks

    # ── 创建与存储 ──

    def create_task(self, subject: str, description: str = "",
                    blockedBy: list[str] | None = None) -> Task:
        with self._locked():
            task_id = f"task_{int(time.time())}_{random.randint(0, 9999):04d}"
            while self._task_path(task_id).exists():
                task_id = f"task_{int(time.time())}_{random.randint(0, 9999):04d}"
            task = Task(
                id=task_id, subject=subject, description=description,
                status="pending", owner=None, blockedBy=blockedBy or [],
            )
            self._save_unlocked(task)
            return task

    def save_task(self, task: Task):
        with self._locked():
            self._save_unlocked(task)

    def load_task(self, task_id: str) -> Task:
        return self._load_unlocked(task_id)

    def list_tasks(self) -> list[Task]:
        return self._list_unlocked()

    def get_task_json(self, task_id: str) -> str:
        return json.dumps(asdict(self.load_task(task_id)), indent=2,
                          ensure_ascii=False)

    # ── 状态流转 ──

    def _can_start_unlocked(self, task_id: str) -> bool:
        """依赖规则:所有 blocker 都必须存在且已完成。"""
        try:
            task = self._load_unlocked(task_id)
            for dep_id in task.blockedBy:
                if not self._task_path(dep_id).exists():
                    return False
                if self._load_unlocked(dep_id).status != "completed":
                    return False
            return True
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return False

    def can_start(self, task_id: str) -> bool:
        return self._can_start_unlocked(task_id)

    def claim_task(self, task_id: str, owner: str = "agent") -> str:
        with self._locked():
            task = self._load_unlocked(task_id)
            if task.status != "pending":
                return f"Task {task_id} is {task.status}, cannot claim"
            if task.owner:
                return f"Task {task_id} already owned by {task.owner}"
            if not self._can_start_unlocked(task_id):
                deps, missing = [], []
                for dep_id in task.blockedBy:
                    try:
                        if not self._task_path(dep_id).exists():
                            missing.append(dep_id)
                        elif self._load_unlocked(dep_id).status != "completed":
                            deps.append(dep_id)
                    except ValueError:
                        missing.append(dep_id)
                parts = []
                if deps:
                    parts.append(f"blocked by: {deps}")
                if missing:
                    parts.append(f"missing deps: {missing}")
                return "Cannot start — " + ", ".join(parts)
            now = time.time()
            task.owner = owner
            task.status = "in_progress"
            task.lease_until = now + Configs.TASK_LEASE_SECONDS
            task.heartbeat_at = now
            self._save_unlocked(task)
            Terminal.info("claim", f"{task.subject} → in_progress",
                          Terminal.CYAN)
            return f"Claimed {task.id} ({task.subject})"

    def complete_task(self, task_id: str) -> str:
        with self._locked():
            task = self._load_unlocked(task_id)
            if task.status != "in_progress":
                return f"Task {task_id} is {task.status}, cannot complete"
            task.status = "completed"
            task.lease_until = None
            task.heartbeat_at = None
            self._save_unlocked(task)
            unblocked = [t.subject for t in self._list_unlocked()
                         if t.status == "pending" and t.blockedBy
                         and self._can_start_unlocked(t.id)]
            Terminal.info("complete", f"{task.subject} ✓", Terminal.GREEN)
            msg = f"Completed {task.id} ({task.subject})"
            if unblocked:
                msg += f"\nUnblocked: {', '.join(unblocked)}"
            return msg

    def fail_task(self, task_id: str, error: str = "",
                  requeue: bool = True) -> str:
        with self._locked():
            task = self._load_unlocked(task_id)
            if task.status == "completed":
                return f"Task {task_id} is completed, cannot fail"
            message = self._fail_unlocked(task, error, requeue)
            return message

    def _fail_unlocked(self, task: Task, error: str,
                       requeue: bool = True) -> str:
        task.attempts += 1
        task.last_error = (error or "")[:500]
        task.owner = None
        task.lease_until = None
        task.heartbeat_at = None
        if requeue and task.attempts < Configs.TASK_MAX_ATTEMPTS:
            task.status = "pending"
            message = (f"Requeued {task.id} after failure "
                       f"(attempt {task.attempts})")
        else:
            task.status = "failed"
            message = (f"Failed {task.id} after {task.attempts} attempt(s): "
                       f"{task.last_error}")
        self._save_unlocked(task)
        return message

    def release_task(self, task_id: str) -> str:
        with self._locked():
            task = self._load_unlocked(task_id)
            if task.status == "completed":
                return f"Task {task_id} is completed, cannot release"
            if task.status == "pending" and not task.owner:
                return f"Task {task_id} is already pending"
            task.status = "pending"
            task.owner = None
            task.lease_until = None
            task.heartbeat_at = None
            self._save_unlocked(task)
            return f"Released {task_id} back to pending"

    def heartbeat(self, task_id: str) -> bool:
        with self._locked():
            task = self._load_unlocked(task_id)
            if task.status != "in_progress":
                return False
            now = time.time()
            task.lease_until = now + Configs.TASK_LEASE_SECONDS
            task.heartbeat_at = now
            self._save_unlocked(task)
            return True

    def reclaim_expired(self) -> list[str]:
        """把租约过期的 in_progress 任务重排队/标记失败,返回任务 ID。"""
        reclaimed = []
        now = time.time()
        with self._locked():
            for task in self._list_unlocked():
                if (task.status == "in_progress"
                        and task.lease_until is not None
                        and task.lease_until < now):
                    self._fail_unlocked(task, "task lease expired")
                    reclaimed.append(task.id)
        for task_id in reclaimed:
            Terminal.info("task", f"reclaimed expired lease: {task_id}",
                          Terminal.YELLOW)
        return reclaimed

    def set_worktree(self, task_id: str, worktree_name: str | None) -> str:
        with self._locked():
            task = self._load_unlocked(task_id)
            task.worktree = worktree_name
            self._save_unlocked(task)
            return f"Task {task_id} worktree set to {worktree_name}"

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
            + (f" owner:{t.owner}" if t.owner else "")
            + (f" (wt:{t.worktree})" if t.worktree else "")
            for t in tasks)

    def run_get_task(self, task_id: str) -> str:
        try:
            return self.get_task_json(task_id)
        except FileNotFoundError:
            return f"Error: task {task_id} not found"
        except ValueError as exc:
            return f"Error: {exc}"

    def run_claim_task(self, task_id: str) -> str:
        try:
            return self.claim_task(task_id, owner="agent")
        except FileNotFoundError:
            return f"Error: task {task_id} not found"
        except ValueError as exc:
            return f"Error: {exc}"

    def run_complete_task(self, task_id: str) -> str:
        try:
            return self.complete_task(task_id)
        except FileNotFoundError:
            return f"Error: task {task_id} not found"
        except ValueError as exc:
            return f"Error: {exc}"

    def run_reclaim_tasks(self) -> str:
        reclaimed = self.reclaim_expired()
        if not reclaimed:
            return "No expired task leases."
        return "Reclaimed: " + ", ".join(reclaimed)

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
            make_function_tool("reclaim_tasks",
                "Release tasks whose lease expired (stuck teammates).",
                {"type": "object", "properties": {}, "required": []}),
        ]

    def handlers(self) -> dict:
        return {
            "create_task": self.run_create_task,
            "list_tasks": self.run_list_tasks,
            "get_task": self.run_get_task,
            "claim_task": self.run_claim_task,
            "complete_task": self.run_complete_task,
            "reclaim_tasks": self.run_reclaim_tasks,
        }


# 全局单例。
TASK_SYSTEM = TaskSystem()
