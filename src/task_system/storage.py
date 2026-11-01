"""任务文件存储:读写、列表、看板以及 create_task 的去重逻辑。

- 活跃任务在 tasks_dir/task_*.json;completed 任务移动到 tasks_dir/archive/;
- 所有读写都在线程锁 + 跨进程文件锁内完成;
- create_task 依次尝试 idempotency_key、精确 fingerprint、模糊相似度,
  保证重复派发不会追加新任务。
"""
import json
import random
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from src.Configs import Configs
from src.FileLock import atomic_write_text, file_lock
from src.MemoryStore import normalize_text, tokenize
from src.SecurityPolicy import validate_task_id
from src.TeamMetrics import TEAM_METRICS
from src.task_system.models import (CreateResult, Task, canonical_fingerprint,
                                    normalize_targets, token_set)


class TaskStorageMixin:
    """任务文件读写与创建去重;由 TaskSystem 与其他 mixin 组合。"""

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
        """活跃目录优先,其次归档目录;不存在返回 None。"""
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
        """completed 任务写归档目录并清理活跃副本,其余反之。"""
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

    def _active_work_unlocked(self) -> list[Task]:
        """参与去重的活跃工作项;已完成/已失败任务不阻止再次派发。"""
        return [task for task in self._list_unlocked()
                if task.kind == "work"
                and task.status in ("pending", "in_progress")]

    def _find_fingerprint_unlocked(self, fingerprint: str) -> Task | None:
        if not fingerprint:
            return None
        for task in self._active_work_unlocked():
            if task.fingerprint == fingerprint:
                return task
        return None

    def _similar_unlocked(self, subject: str, scope: str,
                          targets: list[str],
                          limit: int = 5) -> list[tuple[str, str, float]]:
        """同 scope 且目标文件有交集时按 Jaccard 相似度筛选。"""
        if not targets:
            return []
        threshold = max(0.0, min(1.0, Configs.TASK_DEDUPE_FUZZY_THRESHOLD))
        scope_norm = normalize_text(scope).casefold()
        target_set = set(targets)
        query_tokens = tokenize(" ".join([subject or "", scope or "",
                                          *targets]))
        if not query_tokens:
            return []
        results = []
        for task in self._active_work_unlocked():
            if normalize_text(task.scope).casefold() != scope_norm:
                continue
            if not target_set & set(task.target_files or []):
                continue
            tokens = token_set(task)
            union = query_tokens | tokens
            if not union:
                continue
            score = len(query_tokens & tokens) / len(union)
            if score >= threshold:
                results.append((task.id, task.subject, round(score, 4)))
        results.sort(key=lambda item: (-item[2], item[0]))
        return results[:max(1, int(limit))]

    def find_by_fingerprint(self, fingerprint: str) -> Task | None:
        with self._locked():
            return self._find_fingerprint_unlocked(fingerprint)

    def find_similar(self, subject: str, scope: str = "",
                     target_files=None,
                     limit: int = 5) -> list[tuple[str, str, float]]:
        targets = normalize_targets(target_files)
        with self._locked():
            return self._similar_unlocked(subject, scope, targets, limit)

    def create_task_result(self, subject: str, description: str = "",
                           blockedBy: list[str] | None = None,
                           idempotency_key: str | None = None,
                           scope: str = "", target_files=None,
                           acceptance: str = "",
                           kind: str = "work") -> CreateResult:
        """创建任务;命中幂等键/指纹时返回已有任务(reused=True)。"""
        if kind not in ("work", "integration", "plan_step"):
            raise ValueError(f"invalid task kind: {kind}")
        key = self._validate_idempotency_key(idempotency_key)
        targets = normalize_targets(target_files)
        acceptance = normalize_text(acceptance)[:2000]
        scope = normalize_text(scope)[:500]
        fingerprint = canonical_fingerprint(
            subject, scope, targets, blockedBy, acceptance)
        now = self._now()
        with self._locked():
            if key is not None:
                existing = self._find_idempotency_unlocked(key)
                if existing is not None:
                    TEAM_METRICS.record("task_deduped")
                    return CreateResult(task=existing, reused=True,
                                        reason="idempotency_key")
            exact = self._find_fingerprint_unlocked(fingerprint)
            if exact is not None:
                TEAM_METRICS.record("task_deduped")
                return CreateResult(task=exact, reused=True,
                                    reason="fingerprint")
            similar = self._similar_unlocked(subject, scope, targets)
            task_id = f"task_{int(time.time())}_{random.randint(0, 9999):04d}"
            while self._task_path(task_id).exists() \
                    or self._archive_path(task_id).exists():
                task_id = f"task_{int(time.time())}_{random.randint(0, 9999):04d}"
            task = Task(
                id=task_id, subject=subject, description=description,
                status="pending", owner=None, blockedBy=blockedBy or [],
                idempotency_key=key, created_at=now, updated_at=now,
                kind=kind, scope=scope, target_files=targets,
                acceptance=acceptance, fingerprint=fingerprint,
                duplicate_of=similar[0][0] if similar else None,
                dedupe_score=similar[0][2] if similar else 0.0,
            )
            self._save_unlocked(task)
            TEAM_METRICS.record("task_created")
            return CreateResult(task=task, created=True, reason="created",
                                similar=similar)

    def create_task(self, subject: str, description: str = "",
                    blockedBy: list[str] | None = None,
                    idempotency_key: str | None = None,
                    scope: str = "", target_files=None,
                    acceptance: str = "",
                    kind: str = "work") -> Task:
        return self.create_task_result(
            subject, description, blockedBy, idempotency_key, scope,
            target_files, acceptance, kind).task

    def find_by_idempotency_key(self, key: str) -> Task | None:
        value = self._validate_idempotency_key(key)
        if value is None:
            return None
        with self._locked():
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
        """人类可读任务看板:in_progress 优先,最近完成按时间倒序。"""
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
        """把仍留在活跃目录的 completed 任务迁移到 archive。"""
        moved = []
        with self._locked():
            for task in self._list_unlocked():
                if task.status == "completed":
                    self._save_unlocked(task)
                    moved.append(task.id)
        return moved
