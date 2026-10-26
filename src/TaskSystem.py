"""任务系统:文件持久化的任务记录与生命周期管理。

- 活跃任务在 tasks_dir/task_*.json;completed 任务移动到 tasks_dir/archive/;
- claim/complete/fail/release/heartbeat 在文件锁内完成,避免并发认领;
- load/get/can_start 同时识别 active/archive,完成依赖仍能解锁后续任务;
- create_task 支持 idempotency_key,重复创建返回已有任务而不是追加。
"""
import json
import random
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime
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
    idempotency_key: str | None = None
    created_at: float | None = None
    updated_at: float | None = None
    completed_at: float | None = None


class TaskSystem:
    """任务系统:负责任务文件读写与状态流转。"""

    def __init__(self, tasks_dir: Path | None = None):
        self.tasks_dir = tasks_dir or Configs.tasks_dir()
        self.archive_dir = self.tasks_dir / "archive"
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self._lock_path = self.tasks_dir / ".lock"
        self._rlock = threading.RLock()

    # ── 底层读写 ──

    def _task_path(self, task_id: str) -> Path:
        err = validate_task_id(task_id)
        if err:
            raise ValueError(err)
        return self.tasks_dir / f"{task_id}.json"

    def _archive_path(self, task_id: str) -> Path:
        err = validate_task_id(task_id)
        if err:
            raise ValueError(err)
        return self.archive_dir / f"{task_id}.json"

    def _locate_path(self, task_id: str) -> Path | None:
        active = self._task_path(task_id)
        if active.exists():
            return active
        archived = self._archive_path(task_id)
        if archived.exists():
            return archived
        return None

    @contextmanager
    def _locked(self):
        with self._rlock:
            with file_lock(self._lock_path):
                yield

    @staticmethod
    def _read_task(path: Path) -> Task:
        return Task(**json.loads(path.read_text(encoding="utf-8")))

    def _load_unlocked(self, task_id: str) -> Task:
        path = self._locate_path(task_id)
        if path is None:
            raise FileNotFoundError(f"task {task_id} not found")
        return self._read_task(path)

    def _save_unlocked(self, task: Task):
        if task.status == "completed":
            target = self._archive_path(task.id)
            other = self._task_path(task.id)
        else:
            target = self._task_path(task.id)
            other = self._archive_path(task.id)
        atomic_write_text(target,
                          json.dumps(asdict(task), indent=2, ensure_ascii=False))
        other.unlink(missing_ok=True)

    def _list_unlocked(self, include_archived: bool = False) -> list[Task]:
        tasks = []
        directories = [self.tasks_dir]
        if include_archived:
            directories.append(self.archive_dir)
        for directory in directories:
            if not directory.exists():
                continue
            for path in sorted(directory.glob("task_*.json")):
                try:
                    tasks.append(self._read_task(path))
                except (OSError, json.JSONDecodeError, TypeError, ValueError):
                    continue
        # 同一 id 以 active 目录为准(archive 只补 completed)
        deduped, seen = [], set()
        for task in tasks:
            if task.id in seen:
                continue
            seen.add(task.id)
            deduped.append(task)
        return deduped

    @staticmethod
    def _now() -> float:
        return time.time()

    # ── 创建与存储 ──

    def _find_idempotency_unlocked(self, key: str) -> Task | None:
        for task in self._list_unlocked(include_archived=True):
            if task.idempotency_key == key:
                return task
        return None

    def _validate_idempotency_key(self, key: str | None) -> str | None:
        if key is None:
            return None
        value = str(key).strip()
        if not value:
            return None
        if len(value) > 256 or any(ord(char) < 32 for char in value):
            raise ValueError("idempotency_key must be printable and <=256 chars")
        return value

    def create_task(self, subject: str, description: str = "",
                    blockedBy: list[str] | None = None,
                    idempotency_key: str | None = None) -> Task:
        key = self._validate_idempotency_key(idempotency_key)
        now = self._now()
        with self._locked():
            if key is not None:
                existing = self._find_idempotency_unlocked(key)
                if existing is not None:
                    return existing
            task_id = f"task_{int(time.time())}_{random.randint(0, 9999):04d}"
            while self._task_path(task_id).exists() \
                    or self._archive_path(task_id).exists():
                task_id = f"task_{int(time.time())}_{random.randint(0, 9999):04d}"
            task = Task(
                id=task_id, subject=subject, description=description,
                status="pending", owner=None, blockedBy=blockedBy or [],
                idempotency_key=key, created_at=now, updated_at=now,
            )
            self._save_unlocked(task)
            return task

    def find_by_idempotency_key(self, key: str) -> Task | None:
        value = self._validate_idempotency_key(key)
        if value is None:
            return None
        return self._find_idempotency_unlocked(value)

    def save_task(self, task: Task):
        task.updated_at = self._now()
        with self._locked():
            self._save_unlocked(task)

    def load_task(self, task_id: str) -> Task:
        return self._load_unlocked(task_id)

    def list_tasks(self, include_completed: bool = False) -> list[Task]:
        tasks = self._list_unlocked(include_archived=include_completed)
        if include_completed:
            return tasks
        return [task for task in tasks if task.status != "completed"]

    def get_task_json(self, task_id: str) -> str:
        return json.dumps(asdict(self.load_task(task_id)), indent=2,
                          ensure_ascii=False)

    def recent_completed(self, limit: int = 5) -> list[Task]:
        tasks = [task for task in self._list_unlocked(include_archived=True)
                 if task.status == "completed"]
        tasks.sort(key=lambda task: (
            task.completed_at or task.updated_at or task.created_at or 0),
            reverse=True)
        return tasks[:max(0, int(limit))]

    def board_summary(self, active_limit: int = 8,
                      recent_limit: int = 3) -> str:
        active = self.list_tasks()
        active.sort(key=lambda task: (
            {"in_progress": 0, "pending": 1}.get(task.status, 2),
            -(task.updated_at or 0)))
        recent = self.recent_completed(recent_limit)
        if not active and not recent:
            return "No tasks."
        lines = []
        if active:
            lines.append("Active tasks:")
            for task in active[:max(1, int(active_limit))]:
                lines.append(f"  {task.id}: {task.subject} [{task.status}]")
        if recent:
            lines.append("Recently completed:")
            for task in recent:
                stamp = task.completed_at or task.updated_at
                when = (datetime.fromtimestamp(stamp).strftime("%Y-%m-%d %H:%M")
                        if stamp else "unknown")
                lines.append(f"  {task.id}: {task.subject} ({when})")
        return "\n".join(lines)

    def archive_completed(self) -> list[str]:
        moved = []
        with self._locked():
            for task in self._list_unlocked():
                if task.status == "completed":
                    self._save_unlocked(task)
                    moved.append(task.id)
        return moved

    # ── 状态流转 ──

    def _can_start_unlocked(self, task_id: str) -> bool:
        """依赖规则:所有 blocker 都存在且已完成(active/archive 均可)。"""
        try:
            task = self._load_unlocked(task_id)
            for dep_id in task.blockedBy:
                path = self._locate_path(dep_id)
                if path is None or self._read_task(path).status != "completed":
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
                        path = self._locate_path(dep_id)
                    except ValueError:
                        path = None
                    if path is None:
                        missing.append(dep_id)
                    elif self._read_task(path).status != "completed":
                        deps.append(dep_id)
                parts = []
                if deps:
                    parts.append(f"blocked by: {deps}")
                if missing:
                    parts.append(f"missing deps: {missing}")
                return "Cannot start — " + ", ".join(parts)
            now = self._now()
            task.owner = owner
            task.status = "in_progress"
            task.lease_until = now + Configs.TASK_LEASE_SECONDS
            task.heartbeat_at = now
            task.updated_at = now
            self._save_unlocked(task)
            Terminal.info("claim", f"{task.subject} → in_progress",
                          Terminal.CYAN)
            return f"Claimed {task.id} ({task.subject})"

    def complete_task(self, task_id: str) -> str:
        with self._locked():
            task = self._load_unlocked(task_id)
            if task.status != "in_progress":
                return f"Task {task_id} is {task.status}, cannot complete"
            now = self._now()
            task.status = "completed"
            task.completed_at = now
            task.updated_at = now
            task.lease_until = None
            task.heartbeat_at = None
            self._save_unlocked(task)  # status=completed → archive
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
            return self._fail_unlocked(task, error, requeue)

    def _fail_unlocked(self, task: Task, error: str,
                       requeue: bool = True) -> str:
        task.attempts += 1
        task.last_error = (error or "")[:500]
        task.owner = None
        task.lease_until = None
        task.heartbeat_at = None
        task.updated_at = self._now()
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
            task.updated_at = self._now()
            self._save_unlocked(task)
            return f"Released {task_id} back to pending"

    def heartbeat(self, task_id: str) -> bool:
        with self._locked():
            task = self._load_unlocked(task_id)
            if task.status != "in_progress":
                return False
            now = self._now()
            task.lease_until = now + Configs.TASK_LEASE_SECONDS
            task.heartbeat_at = now
            task.updated_at = now
            self._save_unlocked(task)
            return True

    def reclaim_expired(self) -> list[str]:
        """把租约过期的 in_progress 任务重排队/标记失败,返回任务 ID。"""
        reclaimed = []
        now = self._now()
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
            task.updated_at = self._now()
            self._save_unlocked(task)
            return f"Task {task_id} worktree set to {worktree_name}"

    # ── 工具层(供 ToolRegistry 注册) ──

    def run_create_task(self, subject: str, description: str = "",
                        blockedBy: list[str] | None = None,
                        idempotency_key: str | None = None) -> str:
        existing = (self.find_by_idempotency_key(idempotency_key)
                    if idempotency_key else None)
        task = self.create_task(subject, description, blockedBy,
                                idempotency_key)
        deps = f" (blockedBy: {', '.join(blockedBy)})" if blockedBy else ""
        Terminal.info("create", f"{task.subject}{deps}", Terminal.BLUE)
        prefix = "Existing" if existing is not None else "Created"
        return f"{prefix} {task.id}: {task.subject}{deps}"

    def run_list_tasks(self, include_completed: bool = False) -> str:
        tasks = self.list_tasks(include_completed=include_completed)
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
                                "blockedBy": {"type": "array", "items": {"type": "string"}},
                                "idempotency_key": {"type": "string"}},
                 "required": ["subject"]}),
            make_function_tool("list_tasks", "List tasks.",
                {"type": "object",
                 "properties": {"include_completed": {"type": "boolean"}},
                 "required": []}),
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
