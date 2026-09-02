"""推理环节:把诊断证据转化为结论与行动建议(LLM 的唯一介入点)。

- LLM 此环节无任何工具:只能阅读 digest(纯数据证据摘要),
  工具调用全部由诊断引擎确定性完成——"不让 LLM 猜原因";
- 输出必须符合结构化 schema:JSON 解析 + OutputValidator 校验,
  失败重试 1 次,再失败降级到模板(确定性输出,永不空报告);
- 数据缺失的维度禁止生成数字结论(模板与 system 提示双重约束)。
"""
import json
import re

from src.LlmClient import (assistant_message, user_message)
from src.RecoverySystem import RecoveryState, with_retry
from src.Terminal import Terminal

from Catering.Configs import CateringConfigs
from Catering.Guardrails import OutputValidator
from Catering.Models import AnomalyAlert, Finding

REASONER_SYSTEM = (
    "你是餐饮运营数据分析师。基于给定的诊断证据摘要(digest)撰写结论与行动建议。"
    "规则:\n"
    "1. 只能使用 digest 中的数值与事实,禁止编造任何数字、事实或数据来源;\n"
    "2. 数据缺失(missing)的维度禁止生成数字结论,只能说明该维度数据缺失;\n"
    "3. recommendations 每条必须带 evidence_refs,引用 digest 中存在的 step_id;\n"
    "4. 输出严格 JSON,不要输出任何其他文本:\n"
    '{"conclusions": "结论文本", "recommendations": '
    '[{"action": "行动", "reason": "依据", "evidence_refs": ["step_id"]}]}'
)

JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

# 模板建议:每个异常维度一条确定性行动(与 case 动作互补,保证降级可用)
TEMPLATE_ACTIONS = {
    "traffic": "核对次日客流是否恢复;若持续下降,排查引流渠道投放与周边竞对动态",
    "ticket": "复盘促销活动对客单价的冲击,测算毛利影响后决定活动力度",
    "periods": "对降幅集中时段加强营销投放或调整排班资源",
    "channels": "按堂食/外卖降幅重新配置渠道资源与投放预算",
    "dish": "优化菜品结构,控制低价品类销售占比,补充高毛利主推品",
    "promotion": "评估促销活动 ROI 与新客转化质量,防止低价蚕食毛利",
    "rating": "跟进新增差评原因并制定整改计划",
    "staff": "排查排班缺口,保障高峰时段人力配置",
    "external": "关注天气与竞对市场动态,制定雨天/竞对活动应对预案",
}

DIMENSION_LABELS = {
    "decompose": "贡献度拆解", "traffic": "客流", "ticket": "客单价",
    "periods": "时段", "channels": "渠道", "dish": "菜品结构",
    "promotion": "活动", "rating": "评分", "staff": "人员",
    "external": "外部因素", "case": "历史案例",
}

FACTOR_LABELS = {"traffic": "客流下降", "ticket": "客单价下降",
                 "none": "未发现显著异常"}


class Reasoner:
    """证据 -> 结论/建议:LLM 归纳 + 结构化校验 + 模板降级。"""

    def __init__(self, client=None):
        self.client = client

    # ── 证据摘要 ──

    @staticmethod
    def build_digest(alert: AnomalyAlert, findings: list[Finding],
                     matched_cases: list[dict], main_factor: str,
                     main_factor_contrib: float | None) -> str:
        """把发现压缩成纯数据 JSON 摘要(≤ DIGEST_MAX_CHARS),只含证据不含观点。"""
        digest = {
            "alert": {"store_id": alert.store_id, "date": alert.date,
                      "change_pct": alert.change_pct},
            "main_factor": main_factor,
            "main_factor_contrib": main_factor_contrib,
            "valid_step_ids": [f.step_id for f in findings],
            "findings": [
                {"step_id": f.step_id, "dimension": f.dimension,
                 "verdict": f.verdict, "summary": f.summary,
                 "metrics": f.metrics}
                for f in findings],
            "matched_cases": [
                {"case_id": c.get("case_id"), "summary": c.get("summary"),
                 "actions": c.get("actions")}
                for c in matched_cases],
        }
        text = json.dumps(digest, ensure_ascii=False, indent=1)
        if len(text) > CateringConfigs.DIGEST_MAX_CHARS:
            # 超预算时先裁案例摘要,再硬截断(证据核心 findings 保留)
            digest["matched_cases"] = [
                {"case_id": c.get("case_id"),
                 "summary": str(c.get("summary"))[:100],
                 "actions": (c.get("actions") or [])[:2]}
                for c in matched_cases]
            text = json.dumps(digest, ensure_ascii=False, indent=1)
        return text[:CateringConfigs.DIGEST_MAX_CHARS]

    # ── LLM 归纳(带重试与降级) ──

    def generate(self, alert: AnomalyAlert, findings: list[Finding],
                 matched_cases: list[dict], main_factor: str,
                 main_factor_contrib: float | None) -> dict:
        """返回 {conclusions, recommendations, meta}。client 为空或失败走模板。"""
        valid_refs = {f.step_id for f in findings}
        digest = self.build_digest(alert, findings, matched_cases,
                                   main_factor, main_factor_contrib)
        if self.client is None:
            conclusions, recommendations = self.template(
                alert, findings, matched_cases, main_factor,
                main_factor_contrib)
            return {"conclusions": conclusions,
                    "recommendations": recommendations,
                    "meta": {"llm_used": False, "template_fallback": True,
                             "attempts": 0, "llm_error": None}}

        messages = [user_message(digest)]
        attempts = 0
        last_error: str | None = None
        while attempts <= CateringConfigs.REASONER_MAX_RETRIES:
            attempts += 1
            try:
                state = RecoveryState()
                response = with_retry(
                    lambda: self.client.create(
                        messages, system=REASONER_SYSTEM,
                        max_tokens=CateringConfigs.REASONER_MAX_TOKENS),
                    state, client=self.client)
            except Exception as e:
                last_error = f"{type(e).__name__}: {e}"
                Terminal.info("reasoner", f"LLM 调用失败: {last_error[:80]}",
                              Terminal.RED)
                break
            parsed, err = self._parse_output(response.content)
            if parsed is not None and not OutputValidator.validate_reasoner_output(
                    parsed, valid_refs):
                return self._finish_llm(parsed, attempts)
            last_error = err or "schema 校验失败"
            messages.append(assistant_message(response))
            messages.append(user_message(
                f"输出不符合要求: {last_error}。请只输出符合 schema 的严格 JSON。"))

        conclusions, recommendations = self.template(
            alert, findings, matched_cases, main_factor, main_factor_contrib)
        Terminal.info("reasoner", "LLM 输出不可用,已降级为模板结论",
                      Terminal.YELLOW)
        return {"conclusions": conclusions,
                "recommendations": recommendations,
                "meta": {"llm_used": False, "template_fallback": True,
                         "attempts": attempts, "llm_error": last_error}}

    def _finish_llm(self, parsed: dict, attempts: int) -> dict:
        return {"conclusions": parsed["conclusions"],
                "recommendations": [
                    {"action": r["action"], "reason": r["reason"],
                     "evidence_refs": r.get("evidence_refs") or [],
                     "source": "llm"}
                    for r in parsed.get("recommendations") or []],
                "meta": {"llm_used": True, "template_fallback": False,
                         "attempts": attempts, "llm_error": None}}

    @staticmethod
    def _parse_output(content: str) -> tuple[dict | None, str | None]:
        """从 LLM 文本中提取 JSON(容忍 markdown 代码块)。"""
        if not content:
            return None, "空输出"
        match = JSON_BLOCK_RE.search(content)
        text = match.group(1) if match else content
        try:
            return json.loads(text), None
        except json.JSONDecodeError as e:
            return None, f"JSON 解析失败: {e}"

    # ── 模板降级(确定性,与 LLM 输出同 schema) ──

    @staticmethod
    def template(alert: AnomalyAlert, findings: list[Finding],
                 matched_cases: list[dict], main_factor: str,
                 main_factor_contrib: float | None) -> tuple[str, list[dict]]:
        """确定性结论与建议:按发现逐维度拼装,引用真实 step_id,不编造数字。"""
        lines = [
            f"{alert.store_id} {alert.date} 经营异常诊断结论:",
            f"主因判定: {FACTOR_LABELS.get(main_factor, main_factor)}"
            + (f",贡献 {main_factor_contrib:+.2f} 元"
               if main_factor_contrib is not None else ""),
        ]
        recommendations: list[dict] = []
        for f in findings:
            label = DIMENSION_LABELS.get(f.dimension, f.dimension)
            if f.verdict == "anomaly":
                lines.append(f"- [异常] {label}: {f.summary}")
            elif f.verdict == "info":
                lines.append(f"- [参考] {label}: {f.summary}")
            elif f.verdict == "no_data":
                lines.append(f"- [数据缺失] {label}: 无法判断,建议补录数据")
            if f.verdict == "anomaly" and f.dimension in TEMPLATE_ACTIONS:
                recommendations.append({
                    "action": TEMPLATE_ACTIONS[f.dimension],
                    "reason": f.summary,
                    "evidence_refs": [f.step_id],
                    "source": "template"})
        for case in matched_cases:
            for action in (case.get("actions") or []):
                recommendations.append({
                    "action": action,
                    "reason": f"历史案例 {case.get('case_id')} 的处置经验",
                    "evidence_refs": ["case"],
                    "source": "case"})
        if not recommendations:
            lines.append("未发现显著异常,建议持续观察,无需干预。")
            recommendations.append({
                "action": "保持日常监控,关注后续 2-3 天数据",
                "reason": "各维度均在正常范围",
                "evidence_refs": [],
                "source": "template"})
        return "\n".join(lines), recommendations
