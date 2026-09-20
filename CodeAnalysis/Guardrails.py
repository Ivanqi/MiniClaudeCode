"""可靠性护栏:规则约束求值、输出结构化校验、工具权限(需求 4 的落地)。

- RuleEngine: 分析链规则的 op 白名单求值(无任意代码执行)、
  summary 模板占位符解析、when 条件评估(链分支 + 阶段标记);
- OutputValidator: 分析报告与 LLM 推理输出的结构化校验,
  证据引用必须闭合(每个结论可回溯到工具结果);
- ToolGuard: 工具白名单 + 项目范围 + 写工具闸门,
  工作流/探索双模式共用同一权限模型。

相对 Catering 的白名单增量(全部文档化):
- op 新增 in(threshold 必须为 list,validate_chain 校验);
- when 新增 deploy_done(部署步骤产出)与 level_is:X(自评估等级分支),
  stage_flags 由引擎在 assess/deploy 步骤后写入 {level, deploy_done}。
"""
import re
from datetime import date as date_type

from CodeAnalysis.Configs import CodeAnalysisConfigs
from CodeAnalysis.Models import (MAIN_RISKS, SEVERITIES, SOURCE_CATEGORIES,
                                 STEP_STATUSES)

# ── 规则约束求值 ──

RULE_OPS = {
    "le": lambda a, b: a <= b,
    "ge": lambda a, b: a >= b,
    "lt": lambda a, b: a < b,
    "gt": lambda a, b: a > b,
    "eq": lambda a, b: a == b,
    "abs_ge": lambda a, b: abs(a) >= b,
    "in": lambda a, b: a in b,
}

WHEN_OPS = ("always", "never", "any_anomaly", "no_anomaly", "deploy_done")

_TOKEN_RE = re.compile(r"\{([^{}]+)\}")


def dot_get(data: dict, path: str):
    """按点路径取嵌套值:a.b.0.c(数字段按列表下标)。"""
    current = data
    for segment in str(path).split("."):
        if isinstance(current, list):
            current = current[int(segment)]
        else:
            current = current[segment]
    return current


def eval_rule(rule: dict, data: dict) -> dict | None:
    """对工具结果求值一条规则;命中返回命中记录,否则 None。

    指标缺失/不可比时返回 None(不命中,不猜数);
    op 必须命中白名单,保证分析链文件无法注入任意代码。
    """
    op = RULE_OPS.get(rule.get("op", ""))
    if op is None:
        raise ValueError(f"分析链含非法规则操作符: {rule.get('op')}")
    try:
        value = dot_get(data, rule["metric"])
    except (KeyError, TypeError, IndexError, ValueError):
        return None
    threshold = rule["threshold"]
    try:
        # 布尔/数值类型不可比时视为不命中(防静默错误)
        if isinstance(value, bool) != isinstance(threshold, bool):
            return None
        hit = op(value, threshold)
    except (TypeError, ZeroDivisionError):
        return None
    if not hit:
        return None
    return {"metric": rule["metric"], "op": rule["op"],
            "threshold": rule["threshold"], "verdict": rule["verdict"],
            "summary": format_summary(rule.get("summary", ""), value, data,
                                      threshold=rule["threshold"]),
            "value": value}


def _fmt_value(value) -> str:
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def format_summary(template: str, value, data: dict,
                   threshold=None) -> str:
    """解析 summary 模板:{value} / {value:.1f} / {threshold} / {path.to.field}。"""
    def repl(match):
        raw = match.group(1)
        path, _, spec = raw.partition(":")
        try:
            if path == "value":
                val = value
            elif path == "threshold":
                val = threshold
            else:
                val = dot_get(data, path)
        except (KeyError, TypeError, IndexError, ValueError):
            return match.group(0)
        if spec and isinstance(val, (int, float)):
            return format(val, spec)
        return _fmt_value(val)

    return _TOKEN_RE.sub(repl, template)


def eval_when(when: str, anomaly_seen: bool, stage_flags: dict | None = None) -> bool:
    """评估分析链步骤的 when 条件(白名单语义,链分支机制)。

    stage_flags 由引擎在关键步骤后写入:
    - assess 步骤 → {"level": "A"~"E"}(供 level_is:X 分支);
    - deploy 步骤 → {"deploy_done": True/False}(供 verify 步骤分支,
      仅在部署状态为 deployed 时为 True,blocked/failed 均为 False)。
    """
    if when == "always":
        return True
    if when == "never":
        return False
    if when == "any_anomaly":
        return anomaly_seen
    if when == "no_anomaly":
        return not anomaly_seen
    if when == "deploy_done":
        return bool(stage_flags and stage_flags.get("deploy_done"))
    if when.startswith("level_is:"):
        return bool(stage_flags and stage_flags.get("level")
                    == when.split(":", 1)[1])
    raise ValueError(f"分析链含非法 when 条件: {when}")


# ── 输出结构化校验 ──

REPORT_REQUIRED = ("report_id", "schema_version", "asset", "main_risk",
                   "main_risk_score", "risk_score", "security_level",
                   "findings", "evidence", "matched_cases", "conclusions",
                   "recommendations", "completeness", "meta", "created_at")

SECURITY_LEVELS = ("A", "B", "C", "D", "E")


class OutputValidator:
    """分析报告与 LLM 推理输出的结构化校验(手写规则,零第三方依赖)。"""

    @staticmethod
    def validate_report(report: dict) -> list[str]:
        """校验报告 dict;返回错误列表(空 = 通过)。"""
        errors: list[str] = []
        for key in REPORT_REQUIRED:
            if key not in report:
                errors.append(f"缺少字段: {key}")
        if errors:
            return errors
        if report["schema_version"] != CodeAnalysisConfigs.SCHEMA_VERSION:
            errors.append(f"schema_version 不支持: {report['schema_version']}")
        if report["main_risk"] not in MAIN_RISKS:
            errors.append(f"main_risk 非法: {report['main_risk']}")
        if not isinstance(report.get("risk_score"), (int, float)):
            errors.append("risk_score 必须为数值")
        main_score = report.get("main_risk_score")
        # main_risk=none 时主风险分为 None(无主风险);其余必须为数值
        if report["main_risk"] != "none" and \
                not isinstance(main_score, (int, float)):
            errors.append("main_risk_score 必须为数值")
        if report["security_level"] not in SECURITY_LEVELS:
            errors.append(f"security_level 非法: {report['security_level']}")

        asset = report.get("asset") or {}
        if not str(asset.get("project_id") or "").strip():
            errors.append("asset.project_id 不能为空")
        if not re.match(r"^\d{4}-\d{2}-\d{2}$",
                        str(asset.get("ingest_date") or "")):
            errors.append(f"asset.ingest_date 非法: {asset.get('ingest_date')}")
        if asset.get("source") not in ("detector", "manual"):
            errors.append(f"asset.source 非法: {asset.get('source')}")

        evidence_ids = set()
        for i, ev in enumerate(report.get("evidence") or []):
            step = str(ev.get("step_id") or "")
            if not step:
                errors.append(f"evidence[{i}] 缺少 step_id")
            evidence_ids.add(step)
            if ev.get("source") not in SOURCE_CATEGORIES:
                errors.append(f"evidence[{i}].source 非法: {ev.get('source')}")
            if not isinstance(ev.get("data"), dict):
                errors.append(f"evidence[{i}].data 必须为对象")

        for i, f in enumerate(report.get("findings") or []):
            verdict = f.get("verdict")
            if verdict not in STEP_STATUSES and verdict != "info":
                errors.append(f"findings[{i}].verdict 非法: {verdict}")
            if not str(f.get("summary") or "").strip():
                errors.append(f"findings[{i}].summary 不能为空")
            for ref in f.get("evidence_refs") or []:
                if ref not in evidence_ids:
                    errors.append(f"findings[{i}] 引用不存在的证据: {ref}")

        for i, rec in enumerate(report.get("recommendations") or []):
            if not str(rec.get("action") or "").strip():
                errors.append(f"recommendations[{i}].action 不能为空")
            for ref in rec.get("evidence_refs") or []:
                if ref not in evidence_ids:
                    errors.append(f"recommendations[{i}] 引用不存在的证据: {ref}")

        completeness = report.get("completeness") or {}
        for key in ("ran_steps", "excluded_steps", "no_data_steps", "errors"):
            if key not in completeness:
                errors.append(f"completeness 缺少键: {key}")
        meta = report.get("meta") or {}
        for key in ("llm_used", "template_fallback"):
            if not isinstance(meta.get(key), bool):
                errors.append(f"meta.{key} 必须为布尔值")
        return errors

    @staticmethod
    def validate_reasoner_output(obj: dict, valid_refs: set) -> list[str]:
        """校验 LLM 推理输出 {conclusions, recommendations}。"""
        errors: list[str] = []
        if not isinstance(obj.get("conclusions"), str) \
                or not obj["conclusions"].strip():
            errors.append("conclusions 必须为非空字符串")
        recs = obj.get("recommendations")
        if not isinstance(recs, list):
            errors.append("recommendations 必须为数组")
            return errors
        for i, rec in enumerate(recs):
            if not isinstance(rec, dict):
                errors.append(f"recommendations[{i}] 必须为对象")
                continue
            if not str(rec.get("action") or "").strip():
                errors.append(f"recommendations[{i}].action 不能为空")
            if not str(rec.get("reason") or "").strip():
                errors.append(f"recommendations[{i}].reason 不能为空")
            refs = rec.get("evidence_refs")
            if not isinstance(refs, list):
                errors.append(f"recommendations[{i}].evidence_refs 必须为数组")
            elif any(ref not in valid_refs for ref in refs):
                errors.append(f"recommendations[{i}] 引用不在证据内的 step_id")
        return errors


# ── 工具权限 ──

class ToolGuard:
    """工具权限:白名单 + 项目范围 + 写工具闸门。

    双模式共用:探索模式在分发前拦截;工作流模式的写操作同样经此检查。
    拒绝返回可读原因文本,由调用方回传 LLM 自适应,而不是抛异常。

    与 Catering 的差异: MCP 含 deploy/trigger 的工具不再一律拒绝——
    部署是本包的正规业务能力,仅当 CODEANALYSIS_ENABLE_WRITES=1 时放行,
    否则仍按破坏性工具拒绝(默认安全)。
    """

    WRITE_TOOLS = {"deploy_asset", "register_asset"}
    # MCP 工具名中包含这些词视为可能破坏性(与 src.HookSystem 策略一致)
    MCP_DANGEROUS_WORDS = ("deploy", "trigger")

    def __init__(self, read_tools: list[str], scope: tuple | None = None,
                 allow_writes: bool = False):
        self.read_tools = set(read_tools)
        self.scope = set(scope or ())       # 空 = 全部放开
        self.allow_writes = allow_writes

    def can_call(self, tool_name: str, arguments: dict) -> tuple[bool, str]:
        """权限判定:返回 (是否放行, 拒绝原因)。"""
        if tool_name.startswith("mcp__"):
            # 动态晚绑定的 MCP 工具:只读放行;deploy/trigger 类仅写模式放行
            if any(w in tool_name for w in self.MCP_DANGEROUS_WORDS):
                if self.allow_writes:
                    return True, ""
                return False, (f"MCP 工具 {tool_name} 可能带破坏性,"
                               "未开启写模式时拒绝(需 CODEANALYSIS_ENABLE_WRITES=1)")
            return True, ""
        if tool_name in self.WRITE_TOOLS:
            if not self.allow_writes:
                return False, (f"写工具 {tool_name} 默认禁用"
                               "(需开启 CODEANALYSIS_ENABLE_WRITES)")
        elif tool_name not in self.read_tools:
            return False, f"工具 {tool_name} 不在白名单内"
        project_id = str(arguments.get("project_id") or "")
        if project_id and self.scope and project_id not in self.scope:
            return False, (f"项目 {project_id} 超出授权范围 "
                           f"{sorted(self.scope)}")
        return True, ""
