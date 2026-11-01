"""PlanSystem:lead 侧 Plan 工具的实现(策略选择、创建、执行与状态查询)。"""
import json

from src.ApprovalGate import ApprovalGate
from src.StrategyRouter import StrategyRouter
from src.TeamMetrics import TEAM_METRICS, TeamMetrics
from src.plan_system.budget import PlanBudget
from src.plan_system.executor import PlanExecutor
from src.plan_system.models import Plan
from src.plan_system.planner import Planner
from src.plan_system.store import PlanStore
from src.plan_system.tools import PlanToolsMixin


class PlanSystem(PlanToolsMixin):
    """Plan 工具的 lead 侧实现:策略选择、创建、执行与状态查询。

    构造零 I/O / 零 LLM / 零线程;`.plans` 目录在真正 save/load 时惰性创建。
    """

    def __init__(self, client, router=None, tasks=None, memory=None,
                 store: PlanStore | None = None,
                 approval: ApprovalGate | None = None,
                 metrics: TeamMetrics | None = None,
                 step_runner=None, tool_names_provider=None):
        self.client = client
        self.router = router
        if tasks is None:
            from src.TaskSystem import TASK_SYSTEM
            tasks = TASK_SYSTEM
        self.tasks = tasks
        self.memory = memory
        self.store = store or PlanStore()
        self.approval = approval or ApprovalGate()
        self.metrics = metrics or TEAM_METRICS
        self.step_runner = step_runner
        self.tool_names_provider = tool_names_provider
        self.strategies = StrategyRouter()

    # ── 内部构造 ──

    def _tool_names(self) -> list[str]:
        provider = self.tool_names_provider
        try:
            names = provider() if callable(provider) else (provider or [])
        except Exception:
            names = []
        return sorted(set(names or ()))

    def _planner(self) -> Planner:
        """每次调用创建独立 Planner,隔离 last_usage 与校验状态。"""
        return Planner(self.client, self.tool_names_provider,
                       metrics=self.metrics)

    def _executor(self, planner: Planner) -> PlanExecutor:
        return PlanExecutor(
            planner=planner, step_runner=self.step_runner, tasks=self.tasks,
            memory=self.memory, store=self.store, approval=self.approval,
            metrics=self.metrics)

    @staticmethod
    def _budget_for(plan: Plan) -> PlanBudget:
        # PlanExecutor.execute 会把 plan.signals["planner_usage"] 计入,
        # 这里只负责创建执行时读取上限的预算对象。
        return PlanBudget()

    @staticmethod
    def _format_plan(plan: Plan) -> str:
        """人类可读计划摘要:状态、目标、每步结果/证据/回滚提示。"""
        lines = [f"{plan.id} revision {plan.revision} [{plan.status}]",
                 f"Goal: {plan.goal}", f"Strategy: {plan.strategy}"]
        if plan.error:
            lines.append(f"Error: {plan.error}")
        for step in plan.steps:
            line = f"  {step.id} [{step.status}] {step.goal}"
            if step.result:
                line += f" -> {step.result[:200]}"
            if step.evidence:
                line += " | evidence: " + "; ".join(
                    entry[:100] for entry in step.evidence[:3])
            lines.append(line)
            if step.rollback and step.status == "failed":
                lines.append(f"    rollback (manual only): {step.rollback}")
        return "\n".join(lines)

    # ── 工具实现 ──

    def run_choose_strategy(self, goal: str, active_tasks: int | None = None,
                            active_teammates: bool | None = None,
                            prior_failure: bool = False,
                            prior_revision: int = 0) -> str:
        """只给出 react/hybrid/plan 建议,不创建也不执行计划。"""
        if not str(goal or "").strip():
            return "Error: goal is required"
        if active_tasks is None:
            try:
                active_tasks = len([task for task in self.tasks.list_tasks()
                                    if task.kind == "work"])
            except Exception:
                active_tasks = 0
        if active_teammates is None:
            try:
                from src.ActiveTeammates import ACTIVE_TEAMMATES
                active_teammates = bool(ACTIVE_TEAMMATES.names())
            except Exception:
                active_teammates = False
        decision = self.strategies.recommend(
            goal, active_tasks=int(active_tasks),
            active_teammates=bool(active_teammates),
            prior_revision=int(prior_revision),
            prior_failure=bool(prior_failure))
        triggers = ", ".join(decision.replan_triggers) or "(none)"
        return (f"Strategy: {decision.strategy}\nReason: {decision.reason}\n"
                f"Complexity: {decision.complexity:.2f}\n"
                f"Replan triggers: {triggers}")

    def run_create_plan(self, goal: str, constraints: str = "") -> str:
        """创建并保存计划(不执行);规划器 usage 一并写入 signals。"""
        if not str(goal or "").strip():
            return "Error: goal is required"
        planner = self._planner()
        try:
            plan = planner.create(goal, constraints)
        except Exception as exc:
            return f"Error: failed to create plan: {type(exc).__name__}: {exc}"
        plan.signals["planner_usage"] = dict(planner.last_usage or {})
        try:
            self.store.save(plan)
        except (OSError, ValueError) as exc:
            return f"Error: failed to save plan: {exc}"
        self.metrics.record("plan_created")
        return (f"Created {plan.id} revision {plan.revision} with "
                f"{len(plan.steps)} steps.\n" + self._format_plan(plan))

    def run_execute_plan(self, plan_id: str) -> str:
        if self.step_runner is None:
            return "Error: plan step execution is unavailable"
        try:
            plan = self.store.load(str(plan_id))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return f"Error: {exc}"
        planner = self._planner()
        result = self._executor(planner).execute(
            plan, budget=self._budget_for(plan))
        return self._format_result(result)

    def run_plan_execute(self, goal: str, constraints: str = "") -> str:
        """一步完成“创建 + 保存 + 执行”。"""
        if not str(goal or "").strip():
            return "Error: goal is required"
        if self.step_runner is None:
            return "Error: plan step execution is unavailable"
        planner = self._planner()
        try:
            plan = planner.create(goal, constraints)
        except Exception as exc:
            return f"Error: failed to create plan: {type(exc).__name__}: {exc}"
        plan.signals["planner_usage"] = dict(planner.last_usage or {})
        try:
            self.store.save(plan)
        except (OSError, ValueError) as exc:
            return f"Error: failed to save plan: {exc}"
        self.metrics.record("plan_created")
        result = self._executor(planner).execute(
            plan, budget=self._budget_for(plan))
        return self._format_result(result)

    def run_plan_status(self, plan_id: str = "") -> str:
        """查看单个计划详情;不传 id 时列出最近计划。"""
        if str(plan_id or "").strip():
            try:
                return self._format_plan(self.store.load(str(plan_id)))
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                return f"Error: {exc}"
        plans = self.store.list_plans(limit=10)
        if not plans:
            return "No plans."
        return "\n".join(
            f"{plan.id} revision {plan.revision} [{plan.status}] "
            f"{plan.goal[:100]}" for plan in plans)

    @staticmethod
    def _format_result(result: dict) -> str:
        text = result.get("summary") or (
            f"Plan execution {result.get('status')}: "
            f"{result.get('error', '')}")
        return text
