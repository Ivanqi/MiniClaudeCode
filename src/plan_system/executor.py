"""PlanExecutor facade:组合执行主流程、单步运行、审批与失败处理。"""
from src.ApprovalGate import ApprovalGate
from src.TeamMetrics import TEAM_METRICS, TeamMetrics
from src.plan_system.approval import PlanApprovalMixin
from src.plan_system.execution import PlanExecutionMixin
from src.plan_system.failure import PlanFailureMixin
from src.plan_system.step_runner import PlanStepRunnerMixin
from src.plan_system.store import PlanStore


class PlanExecutor(PlanExecutionMixin, PlanStepRunnerMixin,
                   PlanApprovalMixin, PlanFailureMixin):
    """执行/恢复一个 Plan:TaskSystem 管 DAG,PlanStore 管版本,Memory 做 checkpoint。

    mixin 分层:
    - execution   入口锁、reconcile、物化与步骤主循环;
    - step_runner 提示词、runner 调用、envelope 分类与 checkpoint;
    - approval    需人工审批步骤的询问与拒绝终态;
    - failure     失败分类、循环检测、replan 与计划终态。
    """

    TERMINAL_FAILURES = frozenset({
        "approval", "budget", "validation", "planner", "interrupted",
        "task_state",
    })
    MAX_DEPENDENCY_CHARS = 2000
    MAX_SUMMARY_CHARS = 4000
    MAX_MEMORY_CHARS = 3000

    def __init__(self, planner=None, step_runner=None,
                 tasks=None, memory=None, store: PlanStore | None = None,
                 approval: ApprovalGate | None = None,
                 metrics: TeamMetrics | None = None):
        self.planner = planner
        self.step_runner = step_runner
        if tasks is None:
            from src.TaskSystem import TASK_SYSTEM
            tasks = TASK_SYSTEM
        self.tasks = tasks
        self.memory = memory
        self.store = store or PlanStore()
        self.approval = approval or ApprovalGate()
        self.metrics = metrics or TEAM_METRICS
