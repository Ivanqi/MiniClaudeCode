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
    "4. matched_cases 是历史类比,不是当前事实;当前 findings/alert 数据优先,"
    "不得用案例数值推断本期经营结果,案例只能作为建议参考;\n"
    "5. 引用案例时必须在 case_refs 中给出 matched_cases 中真实存在的 case_id;"
    "未引用案例则输出空数组;\n"
    "6. 输出严格 JSON,不要输出任何其他文本:\n"
    '{"conclusions": "结论文本", "recommendations": '
    '[{"action": "行动", "reason": "依据", "evidence_refs": ["step_id"], '
    '"case_refs": ["case_id"]}]}'
)

CASE_POLICY = (
    "matched_cases 仅为历史类比,不是当前事实;当前 alert/findings 数据优先;"
    "案例不得用于推断本期数值,引用时必须在 case_refs 给出真实 case_id。"
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
    def _dumps(digest: dict) -> str:
        return json.dumps(digest, ensure_ascii=False, indent=1)

    @staticmethod
    def _case_context(case: dict) -> dict:
        """注入 LLM 的案例上下文:保留可追溯元数据,不携带无关大字段。"""
        fields = ("case_id", "date", "main_factor", "dimensions", "summary",
                  "actions", "tags", "symptoms", "outcome", "confidence",
                  "version", "source", "score", "effective_from",
                  "effective_to", "deprecated")
        return {key: case.get(key) for key in fields
                if case.get(key) is not None}

    @staticmethod
    def _compact_case(case: dict) -> dict:
        compact = dict(case)
        compact["summary"] = str(case.get("summary") or "")[:100]
        compact["actions"] = [str(item)[:120]
                              for item in (case.get("actions") or [])[:2]]
        compact["symptoms"] = [str(item)[:80]
                               for item in (case.get("symptoms") or [])[:3]]
        return compact

    @staticmethod
    def _shrink_digest(digest: dict, max_chars: int) -> dict:
        """预算兜底:始终返回合法 JSON,绝不因硬截断产生半截对象。"""
        def fits() -> bool:
            return len(Reasoner._dumps(digest)) <= max_chars

        if fits():
            return digest
        for summary_limit in (200, 120, 80, 40):
            trimmed = False
            for finding in digest.get("findings") or []:
                summary = str(finding.get("summary") or "")
                if len(summary) > summary_limit:
                    finding["summary"] = summary[:summary_limit]
                    trimmed = True
            if trimmed:
                digest["findings_truncated"] = True
            if fits():
                return digest
        metrics_dropped = False
        for finding in digest.get("findings") or []:
            if "metrics" in finding:
                finding.pop("metrics", None)
                metrics_dropped = True
        if metrics_dropped:
            digest["findings_truncated"] = True
        if fits():
            return digest
        while digest.get("findings") and not fits():
            digest["findings"].pop()
            digest["findings_truncated"] = True
        digest["valid_step_ids"] = [
            finding.get("step_id")
            for finding in digest.get("findings") or []
            if finding.get("step_id")]
        if fits():
            return digest
        # 病态输入的最后兜底:只保留告警与主因,保持结构合法。
        return {
            "alert": digest.get("alert"),
            "main_factor": digest.get("main_factor"),
            "main_factor_contrib": digest.get("main_factor_contrib"),
            "valid_step_ids": [],
            "findings": [], "findings_truncated": True,
            "matched_cases": [], "cases_omitted": 0,
            "case_policy": CASE_POLICY,
        }

    @staticmethod
    def _compose_digest(alert: AnomalyAlert, findings: list[Finding],
                        matched_cases: list[dict], main_factor: str,
                        main_factor_contrib: float | None
                        ) -> tuple[str, set[str], set[str]]:
        """构造 digest,并返回其中实际可引用的 step_id / case_id 集合。

        案例使用独立预算:先保证 findings 完整,再按预算装案例;
        装不下就截断案例,绝不硬截断 JSON 或挤掉当前证据。
        """
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
            "case_policy": CASE_POLICY,
            "matched_cases": [],
        }
        max_chars = CateringConfigs.DIGEST_MAX_CHARS
        base_len = len(Reasoner._dumps(digest))
        budget = min(CateringConfigs.CASE_DIGEST_MAX_CHARS,
                     max(0, max_chars - base_len - 120))
        selected: list[dict] = []
        omitted = 0
        for case in matched_cases:
            context = Reasoner._case_context(case)
            if not context.get("case_id"):
                omitted += 1
                continue
            chosen = None
            for candidate in (context, Reasoner._compact_case(context)):
                trial = selected + [candidate]
                if (len(Reasoner._dumps(trial)) <= budget
                        and len(Reasoner._dumps(
                            {**digest, "matched_cases": trial})) <= max_chars):
                    chosen = trial
                    break
            if chosen is None:
                omitted += 1
            else:
                selected = chosen
        digest["matched_cases"] = selected
        if omitted:
            digest["cases_omitted"] = omitted
        if len(Reasoner._dumps(digest)) > max_chars:
            # 案例再重要也不能挤掉当前证据;退回无案例并重试预算收缩。
            digest["matched_cases"] = []
            digest["cases_omitted"] = len(matched_cases)
        final = Reasoner._shrink_digest(digest, max_chars)
        text = Reasoner._dumps(final)
        valid_steps = {str(finding.get("step_id"))
                       for finding in final.get("findings") or []
                       if finding.get("step_id")}
        valid_cases = {str(case.get("case_id"))
                       for case in final.get("matched_cases") or []
                       if case.get("case_id")}
        return text, valid_steps, valid_cases

    @staticmethod
    def build_digest(alert: AnomalyAlert, findings: list[Finding],
                     matched_cases: list[dict], main_factor: str,
                     main_factor_contrib: float | None) -> str:
        """把发现压缩成纯数据 JSON 摘要(≤ DIGEST_MAX_CHARS),只含证据不含观点。"""
        return Reasoner._compose_digest(
            alert, findings, matched_cases, main_factor,
            main_factor_contrib)[0]

    # ── LLM 归纳(带重试与降级) ──

    def generate(self, alert: AnomalyAlert, findings: list[Finding],
                 matched_cases: list[dict], main_factor: str,
                 main_factor_contrib: float | None,
                 allow_case_actions: bool = True) -> dict:
        """返回 {conclusions, recommendations, meta}。client 为空或失败走模板。"""
        digest, valid_refs, valid_case_ids = self._compose_digest(
            alert, findings, matched_cases, main_factor, main_factor_contrib)

        if self.client is None:
            conclusions, recommendations = self.template(
                alert, findings, matched_cases, main_factor,
                main_factor_contrib, allow_case_actions=allow_case_actions)
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
                    parsed, valid_refs, valid_case_ids):
                return self._finish_llm(parsed, attempts)
            last_error = err or "schema 校验失败"
            messages.append(assistant_message(response))
            messages.append(user_message(
                f"输出不符合要求: {last_error}。请只输出符合 schema 的严格 JSON。"))

        conclusions, recommendations = self.template(
            alert, findings, matched_cases, main_factor, main_factor_contrib,
            allow_case_actions=allow_case_actions)
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
                     "case_refs": r.get("case_refs") or [],
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
                 main_factor_contrib: float | None,
                 allow_case_actions: bool = True
                 ) -> tuple[str, list[dict]]:
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
                    "case_refs": [],
                    "source": "template"})
        if allow_case_actions:
            # 模板采用案例动作时保守取一条:优先主因一致,避免弱相关案例
            # (例如 B 店客单价问题召回 A 店暴雨案例)被机械抄进报告。
            same_factor = [c for c in matched_cases
                           if c.get("main_factor") == main_factor]
            usable = same_factor or matched_cases[:1]
            for case in usable[:1]:
                case_id = case.get("case_id")
                for action in (case.get("actions") or []):
                    recommendations.append({
                        "action": action,
                        "reason": f"历史案例 {case_id} 的处置经验",
                        "evidence_refs": [],
                        "case_refs": [case_id] if case_id else [],
                        "source": "case"})
        if not recommendations:
            anomalies = [f for f in findings if f.verdict == "anomaly"]
            if anomalies:
                lines.append("检出异常但暂无模板化动作,建议人工复核。")
                recommendations.append({
                    "action": "人工复核异常维度并制定处置方案",
                    "reason": "异常维度未配置模板动作",
                    "evidence_refs": [f.step_id for f in anomalies],
                    "case_refs": [],
                    "source": "template"})
            else:
                lines.append("未发现显著异常,建议持续观察,无需干预。")
                recommendations.append({
                    "action": "保持日常监控,关注后续 2-3 天数据",
                    "reason": "各维度均在正常范围",
                    "evidence_refs": [],
                    "case_refs": [],
                    "source": "template"})
        return "\n".join(lines), recommendations
