"""任务系统:文件持久化的任务记录与生命周期管理。

- 活跃任务在 tasks_dir/task_*.json;completed 任务移动到 tasks_dir/archive/;
- claim/complete/fail/release/heartbeat 在文件锁内完成,避免并发认领;
- load/get/can_start 同时识别 active/archive,完成依赖仍能解锁后续任务;
- create_task 支持 idempotency_key,重复创建返回已有任务而不是追加。
"""
import hashlib
import json
import random
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from src.Configs import Configs
from src.FileLock import atomic_write_text, file_lock
from src.LlmClient import make_function_tool
from src.MemoryStore import normalize_text, tokenize
from src.SecurityPolicy import validate_task_id
from src.TeamMetrics import TEAM_METRICS
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
    kind: str = "work"                  # work | integration
    scope: str = ""
    target_files: list[str] = field(default_factory=list)
    acceptance: str = ""
    fingerprint: str = ""
    duplicate_of: str | None = None
    dedupe_score: float = 0.0
    claim_token: str | None = None
    base_ref: str = ""
    base_sha: str = ""
    source_tasks: list[str] = field(default_factory=list)
    integration_worktree: str | None = None
    integration_branch: str | None = None
    acceptance_command: str = ""
    merge_status: str = ""
    conflict_files: list[str] = field(default_factory=list)
    preflight_base_sha: str = ""
    source_heads: dict = field(default_factory=dict)
    preflight_exit_code: int | None = None
    preflight_output: str = ""
    apply_exit_code: int | None = None
    apply_output: str = ""
    merged_commit: str = ""
    approved_by: str = ""
    approved_at: float | None = None
    review_status: str = ""
    review_findings: list[str] = field(default_factory=list)
    reviewed_at: float | None = None
    reviewed_by: str = ""
    reviewed_head: str = ""
    # ── Plan step ──
    plan_id: str = ""
    step_id: str = ""
    plan_version: int = 0
    step_result: dict = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    requires_approval: bool = False
    approval_status: str = ""
    rollback: str = ""
    rollback_status: str = ""
    input_refs: list[str] = field(default_factory=list)
    output_refs: list[str] = field(default_factory=list)


@dataclass
class CreateResult:
    task: Task
    created: bool = False
    reused: bool = False
    reason: str = ""
    similar: list[tuple[str, str, float]] = field(default_factory=list)


def _normalize_targets(target_files) -> list[str]:
    if target_files is None:
        return []
    if isinstance(target_files, str):
        target_files = [target_files]
    targets, seen = [], set()
    for item in target_files:
        value = normalize_text(str(item)).strip()
        if not value or value in seen:
            continue
        seen.add(value)
        targets.append(value[:300])
        if len(targets) >= 100:
            break
    return targets


def canonical_fingerprint(subject: str, scope: str = "",
                          target_files=None, blocked_by=None,
                          acceptance: str = "") -> str:
    payload = {
        "subject": normalize_text(subject).casefold(),
        "scope": normalize_text(scope).casefold(),
        "targets": sorted(_normalize_targets(target_files)),
        "blockedBy": sorted(str(item) for item in (blocked_by or [])),
        "acceptance": normalize_text(acceptance).casefold(),
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _token_set(task: Task) -> set[str]:
    text = " ".join([task.subject or "", task.scope or "",
                     *list(task.target_files or [])])
    return tokenize(text)


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

    def _active_work_unlocked(self) -> list[Task]:
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
            tokens = _token_set(task)
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
        targets = _normalize_targets(target_files)
        with self._locked():
            return self._similar_unlocked(subject, scope, targets, limit)

    def create_task_result(self, subject: str, description: str = "",
                           blockedBy: list[str] | None = None,
                           idempotency_key: str | None = None,
                           scope: str = "", target_files=None,
                           acceptance: str = "",
                           kind: str = "work") -> CreateResult:
        if kind not in ("work", "integration", "plan_step"):
            raise ValueError(f"invalid task kind: {kind}")
        key = self._validate_idempotency_key(idempotency_key)
        targets = _normalize_targets(target_files)
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

    @staticmethod
    def _lease_lost(task: Task, claim_token: str | None) -> bool:
        # 不传 token 保留 lead/旧调用兼容;传了 token 就必须匹配当前租约,
        # 任务被回收/完成后 claim_token 已清空,旧 owner 会在这里被拒绝。
        return claim_token is not None and claim_token != task.claim_token

    def claim_task(self, task_id: str, owner: str = "agent") -> str:
        with self._locked():
            task = self._load_unlocked(task_id)
            if task.kind != "work":
                return (f"Task {task_id} has kind '{task.kind}'; "
                        "only work tasks can be claimed")
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
            task.claim_token = uuid.uuid4().hex
            task.lease_until = now + Configs.TASK_LEASE_SECONDS
            task.heartbeat_at = now
            task.updated_at = now
            self._save_unlocked(task)
            TEAM_METRICS.record("task_claimed")
            Terminal.info("claim", f"{task.subject} → in_progress",
                          Terminal.CYAN)
            return f"Claimed {task.id} ({task.subject})"

    def complete_task(self, task_id: str,
                      claim_token: str | None = None) -> str:
        with self._locked():
            task = self._load_unlocked(task_id)
            if self._lease_lost(task, claim_token):
                return f"Lease lost for task {task_id}; cannot complete"
            if task.status != "in_progress":
                return f"Task {task_id} is {task.status}, cannot complete"
            now = self._now()
            task.status = "completed"
            task.completed_at = now
            task.updated_at = now
            task.lease_until = None
            task.heartbeat_at = None
            task.claim_token = None
            self._save_unlocked(task)  # status=completed → archive
            TEAM_METRICS.record("task_completed")
            unblocked = [t.subject for t in self._list_unlocked()
                         if t.status == "pending" and t.blockedBy
                         and self._can_start_unlocked(t.id)]
            Terminal.info("complete", f"{task.subject} ✓", Terminal.GREEN)
            msg = f"Completed {task.id} ({task.subject})"
            if unblocked:
                msg += f"\nUnblocked: {', '.join(unblocked)}"
            return msg

    def fail_task(self, task_id: str, error: str = "",
                  requeue: bool = True,
                  claim_token: str | None = None) -> str:
        with self._locked():
            task = self._load_unlocked(task_id)
            if self._lease_lost(task, claim_token):
                return f"Lease lost for task {task_id}; cannot fail"
            if task.status == "completed":
                return f"Task {task_id} is completed, cannot fail"
            return self._fail_unlocked(task, error, requeue)

    def _fail_unlocked(self, task: Task, error: str,
                       requeue: bool = True) -> str:
        task.attempts += 1
        task.last_error = (error or "")[:500]
        task.owner = None
        task.claim_token = None
        task.lease_until = None
        task.heartbeat_at = None
        task.updated_at = self._now()
        if requeue and task.attempts < Configs.TASK_MAX_ATTEMPTS:
            task.status = "pending"
            TEAM_METRICS.record("task_requeued")
            message = (f"Requeued {task.id} after failure "
                       f"(attempt {task.attempts})")
        else:
            task.status = "failed"
            TEAM_METRICS.record("task_failed")
            message = (f"Failed {task.id} after {task.attempts} attempt(s): "
                       f"{task.last_error}")
        self._save_unlocked(task)
        return message

    def release_task(self, task_id: str,
                     claim_token: str | None = None) -> str:
        with self._locked():
            task = self._load_unlocked(task_id)
            if self._lease_lost(task, claim_token):
                return f"Lease lost for task {task_id}; cannot release"
            if task.status == "completed":
                return f"Task {task_id} is completed, cannot release"
            if task.status == "pending" and not task.owner:
                return f"Task {task_id} is already pending"
            task.status = "pending"
            task.owner = None
            task.claim_token = None
            task.lease_until = None
            task.heartbeat_at = None
            task.updated_at = self._now()
            self._save_unlocked(task)
            return f"Released {task_id} back to pending"

    def heartbeat(self, task_id: str,
                  claim_token: str | None = None) -> bool:
        with self._locked():
            task = self._load_unlocked(task_id)
            if self._lease_lost(task, claim_token):
                return False
            if task.status != "in_progress":
                return False
            now = self._now()
            task.lease_until = now + Configs.TASK_LEASE_SECONDS
            task.heartbeat_at = now
            task.updated_at = now
            self._save_unlocked(task)
            return True

    def reclaim_expired(self, live_owners=None,
                        grace_seconds: float = 0.0) -> list[str]:
        """回收过期租约;live_owners 中的 owner 跳过,grace 留出宽限。"""
        reclaimed = []
        now = self._now()
        live = set(live_owners or ())
        cutoff = now - max(0.0, float(grace_seconds or 0.0))
        with self._locked():
            for task in self._list_unlocked():
                if task.kind == "plan_step":
                    continue  # lead 管理的长步骤,不由队友 idle 回收
                if task.status != "in_progress" or task.lease_until is None:
                    continue
                if task.owner and task.owner in live:
                    continue
                if task.lease_until < cutoff:
                    self._fail_unlocked(task, "task lease expired")
                    TEAM_METRICS.record("task_reclaimed")
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

    _UPDATE_STRING_FIELDS = {
        "base_ref", "base_sha", "integration_worktree",
        "integration_branch", "acceptance_command", "merge_status",
        "merged_commit", "approved_by", "review_status", "reviewed_by",
        "reviewed_head", "preflight_base_sha", "preflight_output",
        "apply_output",
    }
    _UPDATE_LIST_FIELDS = {"source_tasks", "conflict_files",
                           "review_findings"}
    _UPDATE_INT_FIELDS = {"preflight_exit_code", "apply_exit_code"}
    _UPDATE_FLOAT_FIELDS = {"approved_at", "reviewed_at"}

    def update_fields(self, task_id: str, **fields) -> str:
        allowed = (self._UPDATE_STRING_FIELDS | self._UPDATE_LIST_FIELDS
                   | self._UPDATE_INT_FIELDS | self._UPDATE_FLOAT_FIELDS
                   | {"source_heads"})
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"cannot update task fields: {sorted(unknown)}")
        with self._locked():
            task = self._load_unlocked(task_id)
            for key, value in fields.items():
                if key in self._UPDATE_STRING_FIELDS:
                    value = str(value or "")[:20000]
                elif key in self._UPDATE_LIST_FIELDS:
                    value = [str(item)[:2000] for item in (value or [])][:200]
                elif key in self._UPDATE_INT_FIELDS:
                    value = None if value is None else int(value)
                elif key in self._UPDATE_FLOAT_FIELDS:
                    value = None if value is None else float(value)
                elif key == "source_heads":
                    value = {str(k): str(v) for k, v in (value or {}).items()}
                setattr(task, key, value)
            task.updated_at = self._now()
            self._save_unlocked(task)
        return f"Updated {task_id}: {', '.join(sorted(fields))}"

    def begin_review(self, task_id: str, reviewer: str = "reviewer",
                     stale_seconds: float = 600.0) -> tuple[bool, str]:
        with self._locked():
            task = self._load_unlocked(task_id)
            if task.status == "pending":
                return False, "task is pending"
            now = self._now()
            if (task.review_status == "reviewing" and task.reviewed_at
                    and now - task.reviewed_at < stale_seconds):
                return False, "review already in progress"
            task.review_status = "reviewing"
            task.reviewed_by = reviewer
            task.reviewed_at = now
            task.updated_at = now
            self._save_unlocked(task)
            return True, ""

    def complete_integration(self, task_id: str) -> str:
        """集成任务由 lead 工具完成后归档(pending → completed)。"""
        with self._locked():
            task = self._load_unlocked(task_id)
            if task.kind != "integration":
                return f"Task {task_id} is not an integration task"
            if task.status == "completed":
                return f"Task {task_id} is already completed"
            now = self._now()
            task.status = "completed"
            task.completed_at = now
            task.updated_at = now
            task.claim_token = None
            self._save_unlocked(task)
            TEAM_METRICS.record("task_completed")
        return f"Completed integration {task_id}"

    # ── Plan step ──

    @staticmethod
    def _bounded_step_result(value) -> dict:
        if isinstance(value, dict):
            source = value
        elif value is None:
            source = {}
        else:
            source = {"output": value}
        result = {}
        for key, item in list(source.items())[:20]:
            name = str(key)[:100]
            if isinstance(item, str):
                result[name] = item[:4000]
            elif isinstance(item, (int, float, bool)) or item is None:
                result[name] = item
            else:
                result[name] = json.dumps(
                    item, ensure_ascii=False, default=str)[:4000]
        return result

    def create_plan_step_task(self, plan_id: str, revision: int,
                              step_id: str, subject: str,
                              description: str = "",
                              acceptance: str = "",
                              blocked_by: list[str] | None = None,
                              requires_approval: bool = False,
                              rollback: str = "",
                              target_files=None) -> Task:
        key = f"plan:{plan_id}:v{int(revision)}:step:{step_id}"
        result = self.create_task_result(
            subject, description, blocked_by or [], idempotency_key=key,
            scope=f"plan:{plan_id}", target_files=target_files,
            acceptance=acceptance, kind="plan_step")
        with self._locked():
            task = self._load_unlocked(result.task.id)
            task.plan_id = str(plan_id)[:200]
            task.step_id = str(step_id)[:64]
            task.plan_version = int(revision)
            task.requires_approval = bool(requires_approval)
            task.rollback = str(rollback or "")[:4000]
            self._save_unlocked(task)
            return task

    def begin_step(self, task_id: str) -> tuple[bool, str]:
        with self._locked():
            task = self._load_unlocked(task_id)
            if task.kind != "plan_step":
                return False, f"task {task_id} is not a plan step"
            if task.status == "completed":
                return False, "step already completed"
            if task.status != "pending":
                return False, f"step is {task.status}"
            if not self._can_start_unlocked(task_id):
                return False, "step dependencies are not completed"
            now = self._now()
            task.owner = "plan-executor"
            task.status = "in_progress"
            task.claim_token = uuid.uuid4().hex
            task.lease_until = now + Configs.TASK_LEASE_SECONDS
            task.heartbeat_at = now
            task.updated_at = now
            self._save_unlocked(task)
            return True, task.claim_token

    def set_approval(self, task_id: str, status: str) -> str:
        with self._locked():
            task = self._load_unlocked(task_id)
            task.approval_status = str(status or "")[:100]
            task.updated_at = self._now()
            self._save_unlocked(task)
        return f"Approval {task_id}: {status}"

    def complete_step(self, task_id: str, result,
                      evidence: list[str] | None = None,
                      artifacts: list[str] | None = None,
                      output_refs: list[str] | None = None,
                      claim_token: str | None = None) -> str:
        with self._locked():
            task = self._load_unlocked(task_id)
            if self._lease_lost(task, claim_token):
                return f"Lease lost for task {task_id}; cannot complete"
            if task.kind != "plan_step":
                return f"Task {task_id} is not a plan step"
            if task.status != "in_progress":
                return f"Task {task_id} is {task.status}, cannot complete"
            now = self._now()
            task.step_result = self._bounded_step_result(result)
            task.evidence = [str(item)[:2000]
                             for item in (evidence or [])][:100]
            task.artifacts = [str(item)[:2000]
                              for item in (artifacts or [])][:100]
            task.output_refs = [str(item)[:300]
                                for item in (output_refs or [])][:100]
            task.status = "completed"
            task.completed_at = now
            task.updated_at = now
            task.lease_until = None
            task.heartbeat_at = None
            task.claim_token = None
            self._save_unlocked(task)
            TEAM_METRICS.record("task_completed")
        return f"Completed step {task_id}"

    def fail_step(self, task_id: str, error: str = "",
                  claim_token: str | None = None,
                  rollback_status: str = "manual_required") -> str:
        with self._locked():
            task = self._load_unlocked(task_id)
            if self._lease_lost(task, claim_token):
                return f"Lease lost for task {task_id}; cannot fail"
            if task.kind != "plan_step":
                return f"Task {task_id} is not a plan step"
            if task.status == "completed":
                return f"Task {task_id} is completed, cannot fail"
            task.status = "failed"
            task.last_error = str(error or "")[:500]
            task.step_result = self._bounded_step_result(
                {"status": "failed", "output": str(error or "")})
            task.rollback_status = str(rollback_status or "")[:100]
            task.owner = None
            task.claim_token = None
            task.lease_until = None
            task.heartbeat_at = None
            task.updated_at = self._now()
            self._save_unlocked(task)
            TEAM_METRICS.record("task_failed")
        return f"Failed step {task_id}: {error}"

    def abort_plan_step(self, task_id: str,
                        reason: str = "plan aborted") -> str:
        with self._locked():
            task = self._load_unlocked(task_id)
            if task.kind != "plan_step" or task.status == "completed":
                return f"Task {task_id} is not abortable"
            if task.status != "failed":
                TEAM_METRICS.record("task_failed")
            task.status = "failed"
            task.last_error = str(reason or "")[:500]
            task.owner = None
            task.claim_token = None
            task.lease_until = None
            task.heartbeat_at = None
            task.updated_at = self._now()
            self._save_unlocked(task)
        return f"Aborted step {task_id}"

    def set_review(self, task_id: str, status: str,
                   findings: list[str] | None = None,
                   reviewer: str = "reviewer",
                   reviewed_head: str = "") -> str:
        if status not in ("pass", "fail", "blocked"):
            raise ValueError(f"invalid review status: {status}")
        with self._locked():
            task = self._load_unlocked(task_id)
            task.review_status = status
            task.review_findings = [str(item)[:2000]
                                    for item in (findings or [])][:50]
            task.reviewed_by = reviewer
            task.reviewed_head = str(reviewed_head or "")[:200]
            task.reviewed_at = self._now()
            task.updated_at = task.reviewed_at
            self._save_unlocked(task)
        return f"Review {status} for {task_id}"

    # ── 工具层(供 ToolRegistry 注册) ──

    def run_create_task(self, subject: str, description: str = "",
                        blockedBy: list[str] | None = None,
                        idempotency_key: str | None = None,
                        scope: str = "", target_files=None,
                        acceptance: str = "") -> str:
        try:
            result = self.create_task_result(
                subject, description, blockedBy, idempotency_key,
                scope=scope, target_files=target_files,
                acceptance=acceptance)
        except (TypeError, ValueError) as exc:
            return f"Error: {exc}"
        task = result.task
        deps = f" (blockedBy: {', '.join(blockedBy)})" if blockedBy else ""
        Terminal.info("create", f"{task.subject}{deps}", Terminal.BLUE)
        prefix = "Reused" if result.reused else "Created"
        message = f"{prefix} {task.id}: {task.subject}{deps}"
        if result.reason == "fingerprint":
            message += " (fingerprint match)"
        if result.similar:
            parts = [f"{task_id} {title} ({score:.2f})"
                     for task_id, title, score in result.similar[:5]]
            message += "\nSimilar: " + "; ".join(parts)
        return message

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
                                "idempotency_key": {"type": "string"},
                                "scope": {"type": "string"},
                                "target_files": {"type": "array",
                                                 "items": {"type": "string"}},
                                "acceptance": {"type": "string"}},
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
