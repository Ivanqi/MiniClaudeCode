"""计划步骤的人工审批:先持久化 awaiting 状态,再询问 ApprovalGate。"""
import time

from src.ApprovalGate import ApprovalRequest
from src.Terminal import Terminal


class PlanApprovalMixin:
    """requires_approval 步骤的审批流程与拒绝终态。"""

    # ── 审批 ──

    def _request_approval(self, plan, step, task) -> bool:
        """持久化 awaiting 状态后询问;任何异常/非交互都按拒绝处理。"""
        try:
            self.tasks.set_approval(task.id, "pending")
        except (OSError, ValueError):
            pass
        step.status = "awaiting_approval"
        plan.status = "awaiting_approval"
        plan.updated_at = time.time()
        try:
            self.store.save(plan)
        except (OSError, ValueError) as exc:
            Terminal.info("plan", f"cannot persist approval state: {exc}",
                          Terminal.YELLOW)
        request = ApprovalRequest(
            tool="plan_step",
            summary=f"Execute plan step '{step.id}': {step.goal}",
            reason=(f"plan {plan.id} revision {plan.revision} marks this "
                    "step as requiring approval"),
            details=(f"Success criteria: {step.success_criteria}\n"
                     f"Rollback (manual only): {step.rollback or '(none)'}"),
            kind="plan_step",
        )
        try:
            approved = bool(self.approval.confirm(request))
        except Exception:
            approved = False
        try:
            self.tasks.set_approval(task.id, "approved" if approved
                                    else "denied")
        except (OSError, ValueError):
            pass
        step.status = "running" if approved else "failed"
        plan.status = "running" if approved else "failed"
        try:
            self.store.save(plan)
        except (OSError, ValueError):
            pass
        return not approved

    def _handle_approval_denied(self, plan, step, task, budget) -> dict:
        """审批拒绝是终态,不触发 replan。"""
        error = f"approval denied for step {step.id}"
        step.result = error
        step.metadata = dict(step.metadata or {})
        step.metadata.update({"category": "approval", "verified": False})
        try:
            self.tasks.fail_step(task.id, error, claim_token=task.claim_token,
                                 rollback_status="")
        except (OSError, ValueError):
            pass
        self.metrics.record("plan_step_failed")
        return self._finish_failed(plan, error, budget, category="approval")
