"""PlanExecutor 主执行流程:入口锁、reconcile、物化步骤任务与顺序执行。

同一 plan 的进程内执行用锁串行化;跨进程的持久化互斥由 PlanStore
文件锁负责。TaskSystem 是 DAG/执行状态的权威来源,Plan 文件只记录定义。
"""
import json
import threading

from src.plan_system.budget import PlanBudget
from src.plan_system.models import PLAN_ID_RE, Plan, PlanStep
from src.plan_system.validator import PlanValidator

# 进程内按 plan id 串行化执行;跨进程的持久化互斥由 PlanStore 文件锁负责。
_PLAN_EXEC_LOCKS: dict[str, threading.Lock] = {}
_PLAN_EXEC_LOCKS_GUARD = threading.Lock()


def _plan_exec_lock(plan_id: str) -> threading.Lock:
    """取得某 plan 的进程内执行锁(不存在则创建)。"""
    with _PLAN_EXEC_LOCKS_GUARD:
        lock = _PLAN_EXEC_LOCKS.get(plan_id)
        if lock is None:
            lock = threading.Lock()
            _PLAN_EXEC_LOCKS[plan_id] = lock
        return lock


class PlanExecutionMixin:
    """执行入口、状态 reconcile、步骤任务物化与主循环。"""

    # ── 入口 ──

    def execute(self, plan: Plan | str,
                budget: PlanBudget | None = None) -> dict:
        """执行计划;plan 可以是对象或已保存的 plan id。"""
        if isinstance(plan, str):
            try:
                plan = self.store.load(plan)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                return self._result(None, "failed", budget,
                                    error=f"cannot load plan: {exc}")
        if not isinstance(plan, Plan):
            return self._result(None, "failed", budget,
                                error="plan must be a Plan or plan id")
        if not PLAN_ID_RE.match(plan.id or ""):
            return self._result(plan, "failed", budget,
                                error=f"invalid plan id: {plan.id!r}")
        lock = _plan_exec_lock(plan.id)
        if not lock.acquire(blocking=False):
            return self._result(
                plan, "rejected", budget,
                error=f"plan {plan.id} is already executing")
        try:
            budget = budget or PlanBudget()
            budget.record_usage((plan.signals or {}).get("planner_usage"))
            return self._execute_locked(plan, budget)
        finally:
            lock.release()

    # ── 主执行 ──

    def _execute_locked(self, plan: Plan, budget: PlanBudget) -> dict:
        """执行已校验计划;逐步骤处理预算、审批、失败与 replan。"""
        errors = PlanValidator.validate(plan)
        if errors:
            return self._finish_failed(
                plan, "invalid plan: " + "; ".join(errors), budget)

        reconcile_failure = self._reconcile(plan, budget)
        if reconcile_failure:
            return self._finish_failed(plan, reconcile_failure, budget)
        if any(step.status == "failed" for step in plan.steps):
            failed = next(step for step in plan.steps
                          if step.status == "failed")
            return self._finish_failed(
                plan, f"step {failed.id} failed: {failed.result}", budget)

        if all(step.status == "completed" for step in plan.steps):
            return self._finish_completed(plan, budget)

        reason = budget.exhausted_reason()
        if reason:
            return self._finish_failed(
                plan, f"plan budget exhausted: {reason}", budget,
                category="budget")

        self._cleanup_orphan_tasks(plan)
        try:
            task_by_step = self._materialize(plan)
        except Exception as exc:
            return self._finish_failed(
                plan, f"cannot materialize step tasks: {exc}", budget,
                category="validation")

        plan.status = "running"
        plan.error = ""
        self.store.save(plan)

        for step in PlanValidator.topological_order(plan):
            if step.status == "completed":
                continue
            reason = budget.exhausted_reason()
            if reason:
                return self._finish_failed(
                    plan, f"plan budget exhausted: {reason}", budget,
                    category="budget")
            self._cleanup_orphan_tasks(plan)
            task_id = task_by_step.get(step.id) or step.task_id
            if not task_id:
                return self._finish_failed(
                    plan, f"step {step.id} has no task", budget,
                    category="task_state")
            try:
                task = self.tasks.load_task(task_id)
            except (OSError, ValueError) as exc:
                return self._finish_failed(
                    plan, f"cannot load task for step {step.id}: {exc}",
                    budget, category="task_state")
            if task.status == "completed":
                self._fill_step_from_task(step, task)
                budget.record_completed_steps(1)
                self.store.save(plan)
                continue
            if task.status != "pending":
                return self._finish_failed(
                    plan, f"step {step.id} task is {task.status}", budget,
                    category="task_state")

            if task.requires_approval:
                denied = self._request_approval(plan, step, task)
                if denied:
                    return self._handle_approval_denied(
                        plan, step, task, budget)

            ok, token = self.tasks.begin_step(task.id)
            if not ok:
                return self._finish_failed(
                    plan, f"cannot start step {step.id}: {token}", budget,
                    category="task_state")
            step.status = "running"
            step.attempts += 1
            plan.status = "running"
            self.store.save(plan)

            budget.begin_step()
            step_budget = budget.step_budget()
            tool_names = self._tool_names(step)
            prompt = self._step_prompt(plan, step, budget)
            result = self._run_runner(prompt, step, tool_names, step_budget)
            budget.absorb(result.get("budget"))

            success, category, outcome, evidence, artifacts, meta = \
                self._classify_step(result)
            if success:
                if self._complete_step(plan, step, task, token, outcome,
                                       evidence, artifacts, meta):
                    continue
                return self._finish_failed(
                    plan, f"cannot complete step {step.id}: {step.result}",
                    budget, category="task_state")
            return self._handle_step_failure(
                plan, step, task, token, outcome, category, budget)

        return self._finish_completed(plan, budget)

    # ── reconcile / materialize ──

    def _reconcile(self, plan: Plan, budget: PlanBudget) -> str:
        """以 Task 完成状态为准回填步骤;中断的 in_progress 步骤保守失败。

        即使发现中断/失败步骤,也继续回填其余已完成步骤,避免终态把
        实际已完成的 Task 错标为 skipped。
        """
        completed = 0
        failure = ""
        for step in plan.steps:
            if not step.task_id:
                if step.status == "completed":
                    completed += 1
                continue
            try:
                task = self.tasks.load_task(step.task_id)
            except FileNotFoundError:
                step.task_id = ""
                if step.status == "completed":
                    completed += 1
                continue
            except (OSError, ValueError) as exc:
                failure = failure or f"cannot reconcile step {step.id}: {exc}"
                continue
            if task.status == "completed":
                self._fill_step_from_task(step, task)
                completed += 1
            elif task.status == "in_progress":
                try:
                    self.tasks.abort_plan_step(
                        task.id, "interrupted in-progress step; not re-run")
                except (OSError, ValueError):
                    pass
                step.status = "failed"
                step.result = "interrupted in-progress step; not re-run"
                step.metadata = dict(step.metadata or {})
                step.metadata.update({"category": "interrupted",
                                      "verified": False})
                failure = failure or (
                    f"step {step.id} was interrupted while in progress; "
                    "marked failed conservatively (not re-run)")
            elif task.status == "failed":
                step.status = "failed"
                step.result = task.last_error or step.result or "failed"
                step.metadata = dict(step.metadata or {})
                step.metadata.update({"category": "interrupted",
                                      "verified": False})
                failure = failure or (
                    f"step {step.id} previously failed: {task.last_error}")
            else:
                # Task 状态权威:plan 里遗留的 running/failed 回退为 pending。
                if step.status != "pending":
                    step.status = "pending"
                    step.result = ""
                    step.evidence = []
                    step.artifacts = []
                    step.metadata = {}
        budget.record_completed_steps(completed)
        return failure

    @staticmethod
    def _fill_step_from_task(step: PlanStep, task):
        """用已完成 Task 的结果/证据回填 PlanStep。"""
        step.status = "completed"
        payload = dict(task.step_result or {})
        step.result = str(payload.get("output")
                          or payload.get("summary") or "")[:4000]
        step.evidence = list(task.evidence or [])
        step.artifacts = list(task.artifacts or [])
        meta = dict(step.metadata or {})
        meta["verified"] = bool(payload.get("verified", False))
        meta["runtime_status"] = payload.get("runtime_status", "completed")
        step.metadata = meta

    def _materialize(self, plan: Plan) -> dict[str, str]:
        """按稳定拓扑序创建/复用 plan_step 任务,返回 step id → task id。"""
        task_by_step: dict[str, str] = {}
        for step in PlanValidator.topological_order(plan):
            if step.status == "completed":
                if step.task_id:
                    task_by_step[step.id] = step.task_id
                continue
            if step.task_id:
                try:
                    self.tasks.load_task(step.task_id)
                except (OSError, ValueError):
                    step.task_id = ""
            if not step.task_id:
                blocked = [task_by_step[dep] for dep in step.depends_on
                           if dep in task_by_step]
                task = self.tasks.create_plan_step_task(
                    plan.id, plan.revision, step.id, step.goal,
                    description=f"Plan {plan.id} revision {plan.revision}: "
                                f"{step.goal}",
                    acceptance=step.success_criteria, blocked_by=blocked,
                    requires_approval=step.requires_approval,
                    rollback=step.rollback or "")
                step.task_id = task.id
            task_by_step[step.id] = step.task_id
        self.store.save(plan)
        return task_by_step

    def _cleanup_orphan_tasks(self, plan: Plan):
        """中止不属于当前 revision/step 集合的未完成任务。"""
        step_by_id = {step.id: step for step in plan.steps}
        try:
            tasks = self.tasks.list_tasks()
        except (OSError, ValueError):
            return
        for task in tasks:
            if task.kind != "plan_step" or task.plan_id != plan.id:
                continue
            if task.status not in ("pending", "in_progress"):
                continue
            step = step_by_id.get(task.step_id)
            keep = (task.plan_version == plan.revision and step is not None
                    and step.status != "completed"
                    and (not step.task_id or step.task_id == task.id))
            if keep:
                continue
            try:
                self.tasks.abort_plan_step(
                    task.id, "orphaned plan step (old revision or unknown step)")
            except (OSError, ValueError):
                pass
