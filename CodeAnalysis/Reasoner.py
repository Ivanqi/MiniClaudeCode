"""推理环节:把安全分析证据转化为结论与整改建议(LLM 的唯一介入点)。

- LLM 此环节无任何工具:只能阅读 digest(纯数据证据摘要),
  扫描/评估/部署全部由分析引擎确定性完成——"不让 LLM 猜";
- 输出必须符合结构化 schema:JSON 解析 + OutputValidator 校验,
  失败重试 1 次,再失败降级到模板(确定性输出,永不空报告);
- 数据缺失的维度禁止生成数字结论(模板与 system 提示双重约束)。
"""
import json
import re

from src.LlmClient import (assistant_message, user_message)
from src.RecoverySystem import RecoveryState, with_retry
from src.Terminal import Terminal

from CodeAnalysis.Configs import CodeAnalysisConfigs
from CodeAnalysis.Guardrails import OutputValidator
from CodeAnalysis.Models import CodeAsset, Finding

REASONER_SYSTEM = (
    "你是代码安全分析师。基于给定的安全分析证据摘要(digest)撰写结论与整改建议。"
    "规则:\n"
    "1. 只能使用 digest 中的数值与事实,禁止编造任何漏洞、数字或数据来源;\n"
    "2. 数据缺失(missing)的维度禁止生成数字结论,只能说明该维度数据缺失;\n"
    "3. recommendations 每条必须带 evidence_refs,引用 digest 中存在的 step_id;\n"
    "4. 输出严格 JSON,不要输出任何其他文本:\n"
    '{"conclusions": "结论文本", "recommendations": '
    '[{"action": "行动", "reason": "依据", "evidence_refs": ["step_id"]}]}'
)

JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

# 模板建议:每个主风险维度一条确定性行动(与案例动作互补,保证降级可用)
TEMPLATE_ACTIONS = {
    "static_vuln": "按扫描发现的修复建议逐条整改漏洞(参数化查询/强加密/输出编码),"
                   "整改后重新入库扫描复核",
    "env_config": "按环境审查清单逐项整改(关闭调试模式/移除硬编码凭证/升级基础镜像),"
                  "整改后重新评估安全等级",
    "deploy_integrity": "重新构建制品并比对 SHA-256,核查制品仓库写权限与签名密钥,"
                        "确认来源可信后重新部署",
}

DIMENSION_LABELS = {
    "inventory": "资产盘点", "static_scan": "静态扫描", "assess": "安全自评估",
    "env_review": "环境审查", "deploy": "一键部署", "verify": "部署验证",
    "case": "历史案例",
}

RISK_LABELS = {"static_vuln": "静态漏洞风险", "env_config": "运行环境配置风险",
               "deploy_integrity": "部署完整性风险", "none": "未发现显著风险"}

LEVEL_LABELS = {"A": "优秀", "B": "良好", "C": "一般", "D": "较差", "E": "危险"}


class Reasoner:
    """证据 -> 结论/建议:LLM 归纳 + 结构化校验 + 模板降级。"""

    def __init__(self, client=None):
        self.client = client

    # ── 证据摘要 ──

    @staticmethod
    def build_digest(asset: CodeAsset, findings: list[Finding],
                     matched_cases: list[dict], main_risk: str,
                     main_risk_score: float | None, risk_score: float,
                     security_level: str) -> str:
        """把发现压缩成纯数据 JSON 摘要(≤ DIGEST_MAX_CHARS),只含证据不含观点。"""
        digest = {
            "asset": {"project_id": asset.project_id, "name": asset.name,
                      "language": asset.language, "version": asset.version,
                      "ingest_date": asset.ingest_date},
            "main_risk": main_risk, "main_risk_score": main_risk_score,
            "risk_score": risk_score, "security_level": security_level,
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
        if len(text) > CodeAnalysisConfigs.DIGEST_MAX_CHARS:
            # 超预算时先裁案例摘要,再硬截断(证据核心 findings 保留)
            digest["matched_cases"] = [
                {"case_id": c.get("case_id"),
                 "summary": str(c.get("summary"))[:100],
                 "actions": (c.get("actions") or [])[:2]}
                for c in matched_cases]
            text = json.dumps(digest, ensure_ascii=False, indent=1)
        return text[:CodeAnalysisConfigs.DIGEST_MAX_CHARS]

    # ── LLM 归纳(带重试与降级) ──

    def generate(self, asset: CodeAsset, findings: list[Finding],
                 matched_cases: list[dict], main_risk: str,
                 main_risk_score: float | None, risk_score: float,
                 security_level: str) -> dict:
        """返回 {conclusions, recommendations, meta}。client 为空或失败走模板。"""
        valid_refs = {f.step_id for f in findings}
        digest = self.build_digest(asset, findings, matched_cases, main_risk,
                                   main_risk_score, risk_score, security_level)

        if self.client is None:
            conclusions, recommendations = self.template(
                asset, findings, matched_cases, main_risk, main_risk_score,
                risk_score, security_level)
            return {"conclusions": conclusions,
                    "recommendations": recommendations,
                    "meta": {"llm_used": False, "template_fallback": True,
                             "attempts": 0, "llm_error": None}}

        messages = [user_message(digest)]
        attempts = 0
        last_error: str | None = None
        while attempts <= CodeAnalysisConfigs.REASONER_MAX_RETRIES:
            attempts += 1
            try:
                state = RecoveryState()
                response = with_retry(
                    lambda: self.client.create(
                        messages, system=REASONER_SYSTEM,
                        max_tokens=CodeAnalysisConfigs.REASONER_MAX_TOKENS,
                        model=state.current_model),
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
            asset, findings, matched_cases, main_risk, main_risk_score,
            risk_score, security_level)
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
    def template(asset: CodeAsset, findings: list[Finding],
                 matched_cases: list[dict], main_risk: str,
                 main_risk_score: float | None, risk_score: float,
                 security_level: str) -> tuple[str, list[dict]]:
        """确定性结论与建议:按发现逐维度拼装,引用真实 step_id,不编造数字。"""
        lines = [
            f"{asset.name}({asset.project_id}) {asset.ingest_date} "
            f"代码安全分析结论:",
            f"安全等级: {security_level}"
            f"({LEVEL_LABELS.get(security_level, security_level)}),"
            f"风险总分 {risk_score:.2f};",
            f"主风险: {RISK_LABELS.get(main_risk, main_risk)}"
            + (f",风险分 {main_risk_score:.2f}"
               if main_risk_score is not None else ""),
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
        if main_risk in TEMPLATE_ACTIONS:
            recommendations.append({
                "action": TEMPLATE_ACTIONS[main_risk],
                "reason": f"主风险为{RISK_LABELS.get(main_risk, main_risk)},"
                          f"风险分 {main_risk_score:.2f}"
                          if main_risk_score is not None
                          else f"主风险为{RISK_LABELS.get(main_risk, main_risk)}",
                "evidence_refs": [f.step_id for f in findings
                                  if f.verdict == "anomaly"],
                "source": "template"})
        for case in matched_cases:
            for action in (case.get("actions") or []):
                recommendations.append({
                    "action": action,
                    "reason": f"历史案例 {case.get('case_id')} 的处置经验",
                    "evidence_refs": ["case"],
                    "source": "case"})
        if not recommendations:
            lines.append("未发现显著风险,建议保持持续监控,无需干预。")
            recommendations.append({
                "action": "保持定期自动扫描与持续监控",
                "reason": "静态扫描/环境审查/部署验证均通过",
                "evidence_refs": [],
                "source": "template"})
        return "\n".join(lines), recommendations
