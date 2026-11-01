"""Plan step 任务:创建、开始、审批标记、完成/失败/中止与审查记录。

计划步骤复用 Task 持久化,但使用独立的 kind/plan_id/step_id 字段,
由 PlanExecutor 驱动;步骤结果会做有界序列化,避免大对象写入 JSON。
"""
import json
import uuid

from src.Configs import Configs
from src.TeamMetrics import TEAM_METRICS


class TaskPlanStepMixin:
    """计划步骤专用状态流转。"""

    # ── Plan step ──

    @staticmethod
    def _bounded_step_result(value) -> dict:
        """把步骤结果限制为可 JSON 序列化的有界字典(最多 20 个键)。"""
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
                              target_files=None):
        """按 (plan_id, revision, step_id) 幂等创建步骤任务。"""
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
        """开始一个 pending 步骤,返回 (ok, claim_token_or_error)。"""
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
        """步骤失败是终态(不自动重排队),rollback 仅标记人工处理。"""
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
        """计划撤销/重建时中止未完成步骤;已完成步骤保持不变。"""
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
