"""策略路由:按复杂度建议 ReAct / Hybrid / Plan。

与 ModelRouter 分离:ModelRouter 只决定本地模型 vs API;
StrategyRouter 只决定控制流,不做模型调用、不创建计划、不改 CLI。
输出是"初始策略 + 重规划触发器";后续可用轨迹 outcome 调阈值。
"""
from dataclasses import dataclass, field

PLAN_MARKER = "/plan"

ACTION_WORDS = (
    "实现", "修复", "重构", "改造", "新增", "开发", "迁移", "部署",
    "写", "改", "建", "删", "优化", "补测试", "诊断", "排查",
    "implement", "fix", "refactor", "migrate", "deploy", "build",
    "create", "write", "edit", "test", "add",
)
MULTI_FILE_WORDS = (
    "多文件", "模块", "整个项目", "架构", "接口", "依赖", "跨文件",
    "module", "architecture", "multi-file", "codebase", "packages",
)
VERIFICATION_WORDS = (
    "测试", "回归", "验收", "验证", "检查", "扫描", "审计",
    "test", "regression", "verify", "validate", "scan", "audit",
)
IRREVERSIBLE_WORDS = (
    "部署", "发布", "删除", "清空", "迁移数据库", "回滚", "覆盖",
    "deploy", "release", "drop", "destroy", "migrate", "rollback",
    "force push",
)
ANALYSIS_WORDS = (
    "诊断", "分析", "排查", "根因", "对比", "报告",
    "diagnose", "analyze", "investigate", "root cause", "compare",
)


@dataclass
class StrategyDecision:
    strategy: str                     # react | hybrid | plan
    reason: str
    complexity: float
    signals: dict = field(default_factory=dict)
    replan_triggers: list[str] = field(default_factory=list)


def _hits(text: str, words) -> list[str]:
    return [word for word in words if word in text]


class StrategyRouter:
    """纯规则策略路由;不读文件、不调模型。"""

    def recommend(self, goal: str, active_tasks: int = 0,
                  active_teammates: bool = False,
                  prior_revision: int = 0,
                  prior_failure: bool = False) -> StrategyDecision:
        text = str(goal or "").strip()
        lower = text.lower()
        explicit_plan = lower.startswith(PLAN_MARKER) or \
            "制定计划" in text or "plan and execute" in lower
        action_hits = _hits(lower, ACTION_WORDS)
        multi_hits = _hits(lower, MULTI_FILE_WORDS)
        verify_hits = _hits(lower, VERIFICATION_WORDS)
        irreversible_hits = _hits(lower, IRREVERSIBLE_WORDS)
        analysis_hits = _hits(lower, ANALYSIS_WORDS)

        score = 0.0
        if len(text) >= 200:
            score += 0.15
        if len(text) >= 500:
            score += 0.15
        if action_hits:
            score += 0.20
        if multi_hits:
            score += 0.25
        if verify_hits:
            score += 0.15
        if irreversible_hits:
            score += 0.20
        if analysis_hits:
            score += 0.10
        if active_tasks >= 3:
            score += 0.10
        if active_teammates:
            score += 0.15
        if prior_failure:
            score += 0.25
        if prior_revision:
            score += 0.15
        score = round(min(1.0, score), 3)

        if explicit_plan or score >= 0.55:
            strategy = "plan"
            reason = ("explicit plan marker" if explicit_plan
                      else f"complexity {score:.2f} >= 0.55")
        elif score >= 0.30 or active_teammates or prior_failure \
                or prior_revision:
            strategy = "hybrid"
            reason = (f"complexity {score:.2f} in hybrid band"
                      if score >= 0.30
                      else "collaboration/prior-failure signal")
        else:
            strategy = "react"
            reason = f"complexity {score:.2f} below planning threshold"

        triggers = []
        if strategy in ("hybrid", "plan"):
            triggers = ["runtime_error", "semantic_failure"]
            if verify_hits or multi_hits:
                triggers.append("verification_failure")
            if irreversible_hits:
                triggers.append("approval_required")
        return StrategyDecision(
            strategy=strategy, reason=reason, complexity=score,
            signals={
                "explicit_plan": explicit_plan,
                "length": len(text),
                "action_hits": action_hits,
                "multi_file_hits": multi_hits,
                "verification_hits": verify_hits,
                "irreversible_hits": irreversible_hits,
                "analysis_hits": analysis_hits,
                "active_tasks": active_tasks,
                "active_teammates": bool(active_teammates),
                "prior_revision": prior_revision,
                "prior_failure": bool(prior_failure),
            },
            replan_triggers=triggers,
        )
