"""计划级预算:步骤数、token、墙钟 + 每步轮数/工具调用。

上限在构造时从 Configs 读取(即执行时,而不是 import 时);
每个步骤获得一个子 TurnBudget,执行后把 token 用量吸收回计划总量。
0 表示该类上限关闭,与 TurnBudget 语义一致。
"""
import time

from src.Configs import Configs
from src.ToolPolicy import TurnBudget
from src.plan_system.models import usage_tokens


class PlanBudget:
    """计划级预算与步骤子预算的分配者。"""

    def __init__(self, max_steps: int | None = None,
                 max_total_tokens: int | None = None,
                 max_wall_s: int | None = None,
                 step_max_rounds: int | None = None,
                 step_max_tool_calls: int | None = None,
                 clock=None):
        self.max_steps = (int(Configs.PLAN_MAX_STEPS) if max_steps is None
                          else int(max_steps))
        self.max_total_tokens = (
            int(Configs.PLAN_MAX_TOTAL_TOKENS) if max_total_tokens is None
            else int(max_total_tokens))
        self.max_wall_s = (int(Configs.PLAN_MAX_WALL_SECONDS)
                           if max_wall_s is None else int(max_wall_s))
        self.step_max_rounds = (int(Configs.PLAN_STEP_MAX_ROUNDS)
                                if step_max_rounds is None
                                else int(step_max_rounds))
        self.step_max_tool_calls = (
            int(Configs.PLAN_STEP_MAX_TOOL_CALLS)
            if step_max_tool_calls is None else int(step_max_tool_calls))
        self._clock = clock or time.monotonic
        self.started_at = self._clock()
        self.steps_started = 0
        self.total_tokens = 0

    def elapsed_s(self) -> float:
        return max(0.0, self._clock() - self.started_at)

    def remaining_wall_s(self) -> int:
        if not self.max_wall_s:
            return 0
        return max(1, int(self.max_wall_s - self.elapsed_s()))

    def remaining_tokens(self) -> int:
        if not self.max_total_tokens:
            return 0
        return max(1, self.max_total_tokens - self.total_tokens)

    def exhausted_reason(self) -> str:
        """返回第一个耗尽的预算类别;未耗尽返回空串。"""
        if self.max_steps and self.steps_started >= self.max_steps:
            return f"max plan steps ({self.max_steps})"
        if self.max_wall_s and self.elapsed_s() >= self.max_wall_s:
            return f"max plan wall time ({self.max_wall_s}s)"
        if self.max_total_tokens and \
                self.total_tokens >= self.max_total_tokens:
            return f"max plan tokens ({self.max_total_tokens})"
        return ""

    def begin_step(self):
        self.steps_started += 1

    def record_completed_steps(self, count: int):
        """resume 时把已经完成的步骤计入计划总数。"""
        if count > 0:
            self.steps_started += int(count)

    def record_usage(self, usage):
        self.total_tokens += usage_tokens(usage)

    def absorb(self, budget) -> int:
        """把子 TurnBudget(或 run_cycle 的 budget 快照)吸收进计划总量。"""
        if isinstance(budget, TurnBudget):
            tokens = int(budget.total_tokens)
        elif isinstance(budget, dict):
            try:
                tokens = max(0, int(budget.get("total_tokens") or 0))
            except (TypeError, ValueError):
                tokens = 0
        else:
            tokens = 0
        self.total_tokens += tokens
        return tokens

    def step_budget(self) -> TurnBudget:
        """为下一步生成受计划剩余墙钟/token 约束的子预算。"""
        return TurnBudget(
            max_rounds=self.step_max_rounds,
            max_tool_calls=self.step_max_tool_calls,
            max_wall_s=self.remaining_wall_s(),
            max_total_tokens=self.remaining_tokens(),
        )

    def snapshot(self) -> dict:
        return {
            "max_steps": self.max_steps,
            "max_total_tokens": self.max_total_tokens,
            "max_wall_s": self.max_wall_s,
            "step_max_rounds": self.step_max_rounds,
            "step_max_tool_calls": self.step_max_tool_calls,
            "steps_started": self.steps_started,
            "total_tokens": self.total_tokens,
            "elapsed_s": round(self.elapsed_s(), 3),
        }
