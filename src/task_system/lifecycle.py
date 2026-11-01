"""任务状态流转:认领/完成/失败/释放、心跳租约、回收与集成收尾。

所有状态变更都在 ``_locked`` 内完成,并通过 claim_token 防止旧 owner
在任务被回收/完成后继续写入(lease lost)。
"""
import uuid

from src.Configs import Configs
from src.TeamMetrics import TEAM_METRICS
from src.Terminal import Terminal


class TaskLifecycleMixin:
    """任务生命周期方法与可更新字段白名单。"""

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

    # ── 依赖与租约 ──

    def _can_start_unlocked(self, task_id: str) -> bool:
        """依赖规则:所有 blocker 都存在且已完成(active/archive 均可)。"""
        try:
            task = self._load_unlocked(task_id)
            for dep_id in task.blockedBy:
                path = self._locate_path(dep_id)
                if path is None or self._read_task(path).status != "completed":
                    return False
            return True
        except (OSError, ValueError, TypeError):
            return False

    def can_start(self, task_id: str) -> bool:
        return self._can_start_unlocked(task_id)

    @staticmethod
    def _lease_lost(task, claim_token: str | None) -> bool:
        # 不传 token 保留 lead/旧调用兼容;传了 token 就必须匹配当前租约,
        # 任务被回收/完成后 claim_token 已清空,旧 owner 会在这里被拒绝。
        return claim_token is not None and claim_token != task.claim_token

    # ── 工作项状态流转 ──

    def claim_task(self, task_id: str, owner: str = "agent") -> str:
        """认领 pending 工作项;只有 kind=work 且依赖已完成才允许。"""
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

    def _fail_unlocked(self, task, error: str,
                       requeue: bool = True) -> str:
        """失败重排队(未达上限)或标记 failed;调用方需持锁。"""
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
        """续租;token 不匹配或任务不在 in_progress 时返回 False。"""
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

    def update_fields(self, task_id: str, **fields) -> str:
        """仅允许集成分支/合并/审查等白名单字段更新。"""
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

    # ── 审查与集成 ──

    def begin_review(self, task_id: str, reviewer: str = "reviewer",
                     stale_seconds: float = 600.0) -> tuple[bool, str]:
        """抢占审查权;过期的 reviewing 状态允许重新抢占。"""
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
