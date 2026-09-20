"""Agent 评测体系(需求 4):离线场景集 + 指标评分。

每个场景断言:
- main_risk_hit:    报告主风险与场景预期一致(静态漏洞/环境配置/部署完整性/无);
- level_hit:        报告安全等级与期望值一致(风险模型交叉验证);
- deploy_status_hit: 部署步骤状态与期望一致(deployed/blocked/failed);
- failed_checks_hit: 部署验证失败项与期望一致(如 C 场景 integrity);
- excluded_hit:     分支排除的步骤与期望一致(如 C 场景 verify);
- evidence_coverage: 场景要求的证据步骤全部覆盖;
- structure_ok:     报告通过 OutputValidator 结构化校验;
- efficiency:       执行步骤数 ≤ 场景上限。

期望值由 gen_fixtures 内联独立重实现写入 expected_assessments.json,
与 Metrics 生产实现交叉验证(同 Catering 的 LMDI 模式)。
运行全程离线(client=None → 模板降级),不读 .env、不碰网络。
"""
import json
from pathlib import Path

from src.Terminal import Terminal

from CodeAnalysis.Configs import CodeAnalysisConfigs
from CodeAnalysis.Guardrails import OutputValidator
from CodeAnalysis.Models import CodeAsset


class Evaluator:
    """离线评测器:跑场景集,汇总评分报告。"""

    def __init__(self, scenarios_path: Path | None = None,
                 expected_path: Path | None = None):
        self.scenarios_path = (scenarios_path
                               or CodeAnalysisConfigs.EVAL_SCENARIOS_PATH)
        self.expected_path = expected_path or (
                CodeAnalysisConfigs.DATA_DIR / "expected_assessments.json")

    def load_scenarios(self) -> list[dict]:
        payload = json.loads(self.scenarios_path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != CodeAnalysisConfigs.SCHEMA_VERSION:
            raise ValueError("评测场景 schema_version 不支持")
        return payload.get("scenarios") or []

    def run_suite(self, agent) -> dict:
        """在 agent 上跑全部场景(agent 必须离线可用)。"""
        expected = json.loads(self.expected_path.read_text(encoding="utf-8"))
        projects = expected.get("projects") or {}
        results = []
        for scenario in self.load_scenarios():
            asset = CodeAsset(project_id=scenario["project_id"],
                              name=scenario.get("name", ""),
                              language="", version="",
                              ingest_date=scenario.get("ingest_date",
                                                      "2026-09-18"),
                              source="detector")
            report = agent.run_analysis(asset)
            results.append(self._score(scenario, report,
                                       projects.get(scenario["project_id"], {})))
        return self._summary(results)

    @staticmethod
    def _score(scenario: dict, report, expected: dict) -> dict:
        errors: list[str] = []
        structure = OutputValidator.validate_report(report.to_dict())
        if structure:
            errors.append("结构校验失败: " + "; ".join(structure))

        main_hit = report.main_risk == scenario["expected_main_risk"]
        if not main_hit:
            errors.append(f"主风险不符: 期望 {scenario['expected_main_risk']},"
                          f"实际 {report.main_risk}")

        level_hit = report.security_level == scenario["expected_level"]
        if not level_hit:
            errors.append(f"安全等级不符: 期望 {scenario['expected_level']},"
                          f"实际 {report.security_level}")

        deploy_data = Evaluator._deploy_data(report)
        deploy_hit = (deploy_data.get("status")
                      == scenario["expected_deploy_status"])
        if not deploy_hit:
            errors.append(f"部署状态不符: 期望 {scenario['expected_deploy_status']},"
                          f"实际 {deploy_data.get('status')}")

        failed_checks = [c for c, v in (deploy_data.get("verification")
                                        or {}).items()
                         if isinstance(v, dict) and v.get("passed") is False]
        want_failed = scenario.get("expected_failed_checks") or []
        checks_hit = sorted(failed_checks) == sorted(want_failed)
        if not checks_hit:
            errors.append(f"验证失败项不符: 期望 {want_failed},"
                          f"实际 {failed_checks}")

        excluded = set(report.completeness.get("excluded_steps") or [])
        want_excluded = set(scenario.get("expected_excluded_steps") or [])
        excluded_hit = want_excluded.issubset(excluded)
        if not excluded_hit:
            errors.append(f"分支排除不符: 期望 {sorted(want_excluded)} ⊆ "
                          f"{sorted(excluded)}")

        evidence_ids = {e.step_id for e in report.evidence}
        missing_tags = [tag for tag in scenario["evidence_tags"]
                        if tag not in evidence_ids]
        coverage = not missing_tags
        if missing_tags:
            errors.append(f"证据缺失: {missing_tags}")

        ran = len(report.completeness.get("ran_steps") or [])
        efficient = ran <= scenario["max_steps"]
        if not efficient:
            errors.append(f"步骤数超限: {ran} > {scenario['max_steps']}")

        # 期望值交叉验证:报告风险分/等级与独立重实现一致
        expected_close = True
        if expected:
            want_score = expected.get("risk_score")
            got = report.risk_score
            expected_close = (isinstance(want_score, (int, float))
                              and abs(got - want_score) <= 1e-4)
            if not expected_close:
                errors.append(f"风险分偏差过大: 期望 {want_score},实际 {got}")

        return {
            "id": scenario["id"], "project_id": scenario["project_id"],
            "main_risk": report.main_risk,
            "security_level": report.security_level,
            "risk_score": report.risk_score,
            "deploy_status": deploy_data.get("status"),
            "ran_steps": ran,
            "main_risk_hit": main_hit, "level_hit": level_hit,
            "deploy_status_hit": deploy_hit, "failed_checks_hit": checks_hit,
            "excluded_hit": excluded_hit,
            "evidence_coverage": coverage, "structure_ok": not structure,
            "efficiency": efficient, "expected_close": expected_close,
            "passed": not errors, "errors": errors,
        }

    @staticmethod
    def _deploy_data(report) -> dict:
        """从报告证据中取 deploy 步骤的工具结果。"""
        for ev in report.evidence:
            if ev.step_id == "deploy":
                return ev.data or {}
        return {}

    @staticmethod
    def _summary(results: list[dict]) -> dict:
        total = len(results)
        passed = sum(1 for r in results if r["passed"])
        return {
            "total": total, "passed": passed,
            "metrics": {
                "main_risk_hit": f"{sum(r['main_risk_hit'] for r in results)}/{total}",
                "level_hit": f"{sum(r['level_hit'] for r in results)}/{total}",
                "deploy_status_hit": f"{sum(r['deploy_status_hit'] for r in results)}/{total}",
                "failed_checks_hit": f"{sum(r['failed_checks_hit'] for r in results)}/{total}",
                "excluded_hit": f"{sum(r['excluded_hit'] for r in results)}/{total}",
                "evidence_coverage": f"{sum(r['evidence_coverage'] for r in results)}/{total}",
                "structure_ok": f"{sum(r['structure_ok'] for r in results)}/{total}",
                "efficiency": f"{sum(r['efficiency'] for r in results)}/{total}",
            },
            "scenarios": results,
        }

    @staticmethod
    def print_report(summary: dict):
        """终端输出评测汇总。"""
        passed = summary["passed"]
        total = summary["total"]
        color = Terminal.GREEN if passed == total else Terminal.RED
        Terminal.print(f"\n{color}=== 评测结果: {passed}/{total} 场景通过 "
                       f"{Terminal.RESET}")
        for key, value in summary["metrics"].items():
            Terminal.print(f"  {key}: {value}")
        for r in summary["scenarios"]:
            mark = Terminal.GREEN + "PASS" if r["passed"] else Terminal.RED + "FAIL"
            Terminal.print(f"  [{mark}{Terminal.RESET}] {r['id']} "
                           f"({r['project_id']}) 主风险={r['main_risk']} "
                           f"等级={r['security_level']} "
                           f"部署={r['deploy_status']}")
            for err in r["errors"]:
                Terminal.print(f"      {Terminal.RED}- {err}{Terminal.RESET}")
