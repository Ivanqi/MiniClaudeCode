"""Plan 结构/依赖/工具白名单校验。"""
from src.Configs import Configs
from src.ToolPolicy import PLAN_FORBIDDEN_STEP_TOOLS
from src.plan_system.models import (PLAN_ID_RE, PLAN_SCHEMA_VERSION,
                                    PLAN_STATUSES, PLAN_STRATEGIES,
                                    STEP_ID_RE, STEP_STATUSES, Plan, PlanStep)


class PlanValidator:
    """计划结构/依赖/工具白名单校验。"""

    @classmethod
    def topological_order(cls, plan: Plan) -> list[PlanStep]:
        """稳定拓扑序:同层按原步骤顺序排列;有环则抛 ValueError。"""
        by_id = {step.id: step for step in plan.steps}
        index = {step.id: pos for pos, step in enumerate(plan.steps)}
        indegree = {step.id: 0 for step in plan.steps}
        edges = {step.id: set() for step in plan.steps}
        for step in plan.steps:
            for dep in step.depends_on:
                if dep not in by_id:
                    raise ValueError(f"step '{step.id}' depends on unknown "
                                     f"step '{dep}'")
                edges[dep].add(step.id)
                indegree[step.id] += 1
        ready = [(index[step_id], step_id)
                 for step_id, count in indegree.items() if count == 0]
        ready.sort()
        order = []
        while ready:
            _, step_id = ready.pop(0)
            order.append(by_id[step_id])
            for child in sorted(edges[step_id], key=lambda item: index[item]):
                indegree[child] -= 1
                if indegree[child] == 0:
                    ready.append((index[child], child))
                    ready.sort()
        if len(order) != len(plan.steps):
            raise ValueError("plan has a dependency cycle")
        return order

    @classmethod
    def validate(cls, plan: Plan, available_tools=None) -> list[str]:
        """返回全部错误;available_tools 为 None 时跳过高名检查。"""
        errors = []
        if not isinstance(plan, Plan):
            return ["plan must be a Plan"]
        if plan.schema_version != PLAN_SCHEMA_VERSION:
            errors.append(f"unsupported schema_version: {plan.schema_version}")
        if plan.id and not PLAN_ID_RE.match(plan.id):
            errors.append(f"invalid plan id: {plan.id!r}")
        if not str(plan.goal or "").strip():
            errors.append("plan goal cannot be empty")
        if plan.strategy not in PLAN_STRATEGIES:
            errors.append(f"invalid strategy: {plan.strategy}")
        if plan.status not in PLAN_STATUSES:
            errors.append(f"invalid plan status: {plan.status}")
        if plan.revision < 1 or plan.revision > Configs.PLAN_MAX_REVISIONS:
            errors.append(f"revision out of range: {plan.revision}")
        if not plan.steps:
            errors.append("plan must have at least one step")
        if len(plan.steps) > Configs.PLAN_MAX_STEPS:
            errors.append(f"too many steps (max {Configs.PLAN_MAX_STEPS})")
        seen = set()
        hint_check = available_tools is not None
        allowed_hints = set(available_tools or ())
        for step in plan.steps:
            if not STEP_ID_RE.match(step.id or ""):
                errors.append(f"invalid step id: {step.id!r}")
            if step.id in seen:
                errors.append(f"duplicate step id: {step.id}")
            seen.add(step.id)
            if step.status not in STEP_STATUSES:
                errors.append(f"invalid status for {step.id}: {step.status}")
            if not str(step.goal or "").strip():
                errors.append(f"step {step.id} goal cannot be empty")
            if not str(step.success_criteria or "").strip():
                errors.append(f"step {step.id} success_criteria is required")
            if len(step.goal) > 2000 or len(step.success_criteria) > 2000:
                errors.append(f"step {step.id} text too long")
            if step.rollback and len(step.rollback) > 4000:
                errors.append(f"step {step.id} rollback too long")
            for dep in step.depends_on:
                if dep == step.id:
                    errors.append(f"step {step.id} depends on itself")
                elif dep not in {item.id for item in plan.steps}:
                    errors.append(f"step {step.id} depends on unknown {dep}")
            for hint in step.tool_hints:
                if hint in PLAN_FORBIDDEN_STEP_TOOLS or \
                        hint.startswith("mcp__"):
                    errors.append(f"step {step.id} forbidden tool hint: {hint}")
                elif hint_check and hint not in allowed_hints:
                    errors.append(f"step {step.id} unknown tool hint: {hint}")
        try:
            cls.topological_order(plan)
        except ValueError as exc:
            errors.append(str(exc))
        return errors
