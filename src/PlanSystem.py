"""兼容入口:PlanSystem 实现已拆分到 src/plan_system/。

保留这个模块是为了不改变 `from src.PlanSystem import ...` 的既有调用。
各功能模块:
- models.py     —— Plan/PlanStep、Schema 常量与输出契约
- validator.py  —— 结构/依赖/工具白名单校验
- store.py      —— 版本化持久化与历史归档
- budget.py     —— 计划级预算与步骤子预算
- planner.py    —— 计划生成与 replan
- execution.py  —— 入口锁、reconcile、物化与主循环
- step_runner.py—— 单步提示词、runner 调用与结果分类
- approval.py   —— 步骤人工审批
- failure.py    —— 失败处理、循环检测与终态
- executor.py   —— PlanExecutor facade
- system.py     —— PlanSystem lead 侧工具实现
"""
from src.plan_system import (PLAN_ID_RE, PLAN_SCHEMA_VERSION, PLAN_STATUSES,
                             PLAN_STRATEGIES, PLANNER_SYSTEM,
                             STEP_COMPLETED_STATUSES, STEP_FAILED_STATUSES,
                             STEP_ID_RE, STEP_OUTPUT_CONTRACT, STEP_STATUSES,
                             Plan, PlanBudget, PlanExecutor, PlanStep,
                             PlanStore, PlanSystem, PlanValidator, Planner,
                             bounded, new_plan_id, usage_tokens)

__all__ = [
    "PLAN_ID_RE",
    "PLAN_SCHEMA_VERSION",
    "PLAN_STATUSES",
    "PLAN_STRATEGIES",
    "PLANNER_SYSTEM",
    "STEP_COMPLETED_STATUSES",
    "STEP_FAILED_STATUSES",
    "STEP_ID_RE",
    "STEP_OUTPUT_CONTRACT",
    "STEP_STATUSES",
    "Plan",
    "PlanBudget",
    "PlanExecutor",
    "PlanStep",
    "PlanStore",
    "PlanSystem",
    "PlanValidator",
    "Planner",
    "bounded",
    "new_plan_id",
    "usage_tokens",
]
