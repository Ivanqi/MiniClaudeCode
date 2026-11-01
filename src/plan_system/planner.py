"""Planner:用 API 模型生成/修订 Plan,严格 JSON → validator。"""
import json
import re
import time
import uuid

from src.Configs import Configs
from src.LlmClient import user_message
from src.RecoverySystem import RecoveryState, with_retry
from src.StrategyRouter import StrategyRouter
from src.TeamMetrics import TEAM_METRICS, TeamMetrics
from src.ToolPolicy import PLAN_FORBIDDEN_STEP_TOOLS
from src.plan_system.models import (PLAN_SCHEMA_VERSION, PLANNER_SYSTEM, Plan,
                                    PlanStep)
from src.plan_system.validator import PlanValidator


class Planner:
    """用 API 模型生成/修订 Plan;严格 JSON → validator。"""

    def __init__(self, client, available_tools,
                 metrics: TeamMetrics | None = None,
                 max_tokens: int | None = None):
        self.client = client
        self.available_tools = available_tools
        self.metrics = metrics or TEAM_METRICS
        self.max_tokens = (Configs.PLANNER_MAX_TOKENS
                           if max_tokens is None else max_tokens)
        self.validator = PlanValidator()
        self.router = StrategyRouter()
        self.last_usage: dict = {}

    def safe_tools(self) -> list[str]:
        """可出现在步骤中的安全工具;排除 plan 工具与 MCP 工具。"""
        provider = self.available_tools
        names = provider() if callable(provider) else provider
        return sorted(name for name in set(names or ())
                      if name not in PLAN_FORBIDDEN_STEP_TOOLS
                      and not name.startswith("mcp__"))

    def _validation_tools(self):
        # 未提供工具来源时跳过高名检查;仍会拒绝 Plan/spawn/MCP hint。
        return self.safe_tools() if self.available_tools is not None else None

    def _call(self, prompt: str, state: RecoveryState) -> str:
        """带重试的规划调用;usage 记入 metrics 并保留 last_usage。"""
        response = with_retry(
            lambda: self.client.create(
                [user_message(prompt)], system=PLANNER_SYSTEM,
                tools=None, max_tokens=self.max_tokens),
            state, client=self.client)
        self.last_usage = getattr(response, "usage", None) or {}
        self.metrics.add_usage(self.last_usage, "planner")
        return response.content or ""

    @staticmethod
    def _extract_json(text: str) -> dict | None:
        """从模型输出中截取第一个 JSON 对象;失败返回 None。"""
        match = re.search(r"\{.*\}", text or "", re.S)
        if not match:
            return None
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    def _schema_prompt(self, goal: str, constraints: str) -> str:
        safe = self.safe_tools()
        return (
            "Create a plan for the goal below. Return JSON only with fields: "
            "goal, strategy (plan_execute|hybrid), steps. Each step: id, goal, "
            "tool_hints (array), depends_on (array), success_criteria, rollback "
            "(string|null), requires_approval (bool). "
            f"Maximum steps: {Configs.PLAN_MAX_STEPS}. "
            f"Allowed tools: {', '.join(safe) or '(none)'}. "
            "Use a DAG; each step must be small and independently verifiable. "
            "Do not include plan tools or spawn_teammate.\n"
            f"Goal: {goal}\nConstraints: {constraints or '(none)'}"
        )

    def create(self, goal: str, constraints: str = "") -> Plan:
        """生成新计划并校验;任何结构错误都抛 ValueError。"""
        prompt = self._schema_prompt(goal, constraints)
        payload = self._extract_json(self._call(prompt, RecoveryState()))
        if payload is None:
            raise ValueError("planner returned invalid JSON")
        plan = Plan.from_dict(payload)
        plan.goal = plan.goal or goal
        plan.id = f"plan_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        plan.revision = 1
        plan.schema_version = PLAN_SCHEMA_VERSION
        plan.status = "draft"
        plan.created_at = plan.updated_at = time.time()
        plan.signals = self.router.recommend(goal).signals
        errors = self.validator.validate(plan, self._validation_tools())
        if errors:
            raise ValueError("invalid plan: " + "; ".join(errors))
        return plan

    def replan(self, previous: Plan, failed_step: PlanStep,
               observation: str) -> Plan:
        """基于失败观察生成下一修订;只覆盖剩余/新增工作。"""
        completed = [step for step in previous.steps
                     if step.status == "completed"]
        remaining = [step for step in previous.steps
                     if step.status not in ("completed", "failed")]
        prompt = (
            "Revise the plan after a step failure. Return JSON only with "
            "goal, strategy, steps. New steps must cover only remaining/new "
            "work; do not repeat completed steps. Keep the original goal.\n"
            f"Original goal: {previous.goal}\n"
            f"Previous revision: {previous.revision}\n"
            f"Failed step: {failed_step.id} {failed_step.goal}\n"
            f"Failure observation: {str(observation)[:2000]}\n"
            f"Completed steps: {', '.join(step.id for step in completed) or '(none)'}\n"
            f"Remaining steps: {', '.join(step.id for step in remaining) or '(none)'}\n"
            f"Allowed tools: {', '.join(self.safe_tools()) or '(none)'}\n"
            f"Maximum steps: {Configs.PLAN_MAX_STEPS}."
        )
        payload = self._extract_json(self._call(prompt, RecoveryState()))
        if payload is None:
            raise ValueError("replanner returned invalid JSON")
        plan = Plan.from_dict(payload)
        plan.goal = previous.goal
        plan.id = previous.id
        plan.revision = previous.revision + 1
        plan.schema_version = PLAN_SCHEMA_VERSION
        plan.status = "draft"
        plan.parent_plan_id = ""
        plan.created_at = previous.created_at or time.time()
        plan.updated_at = time.time()
        errors = self.validator.validate(plan, self._validation_tools())
        if errors:
            raise ValueError("invalid replan: " + "; ".join(errors))
        return plan
