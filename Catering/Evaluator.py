"""Agent 评测体系(需求 4):离线场景集 + 指标评分。

每个场景断言:
- root_cause_hit:  报告主因与场景预期一致(客流/客单价/无异常);
- contrib_close:   主因贡献额与期望值(fixture 现算,独立实现)误差 ≤ 0.05 元;
- evidence_coverage: 场景要求的证据步骤全部覆盖;
- structure_ok:    报告通过 OutputValidator 结构化校验;
- efficiency:      执行步骤数 ≤ 场景上限。

运行全程离线(client=None → 模板降级),不读 .env、不碰网络。
"""
import json
from pathlib import Path

from src.Terminal import Terminal

from Catering.Configs import CateringConfigs
from Catering.Guardrails import OutputValidator
from Catering.Models import AnomalyAlert


class Evaluator:
    """离线评测器:跑场景集,汇总评分报告。"""

    def __init__(self, scenarios_path: Path | None = None,
                 expected_path: Path | None = None):
        self.scenarios_path = scenarios_path or CateringConfigs.EVAL_SCENARIOS_PATH
        self.expected_path = expected_path or (
                CateringConfigs.DATA_DIR / "expected_contributions.json")

    def load_scenarios(self) -> list[dict]:
        payload = json.loads(self.scenarios_path.read_text())
        if payload.get("schema_version") != CateringConfigs.SCHEMA_VERSION:
            raise ValueError("评测场景 schema_version 不支持")
        return payload.get("scenarios") or []

    def run_suite(self, agent) -> dict:
        """在 agent 上跑全部场景(agent 必须离线可用)。"""
        expected = json.loads(self.expected_path.read_text())
        results = []
        for scenario in self.load_scenarios():
            alert = AnomalyAlert(store_id=scenario["store_id"],
                                 date=scenario["date"], source="detector")
            report = agent.run_alert(alert)
            results.append(self._score(scenario, report, expected))
        return self._summary(results)

    @staticmethod
    def _score(scenario: dict, report, expected: dict) -> dict:
        errors: list[str] = []
        structure = OutputValidator.validate_report(report.to_dict())
        if structure:
            errors.append("结构校验失败: " + "; ".join(structure))

        main_hit = report.main_factor == scenario["expected_main_factor"]
        if not main_hit:
            errors.append(f"主因不符: 期望 {scenario['expected_main_factor']},"
                          f"实际 {report.main_factor}")

        contrib_close = True
        if scenario["expected_main_factor"] != "none":
            want = expected.get(scenario["store_id"], {})
            field = {"traffic": "traffic_contrib",
                     "ticket": "ticket_contrib"}[scenario["expected_main_factor"]]
            want_value = want.get(field)
            got = report.main_factor_contrib
            contrib_close = (isinstance(want_value, (int, float))
                             and isinstance(got, (int, float))
                             and abs(got - want_value) <= 0.05)
            if not contrib_close:
                errors.append(f"主因贡献偏差过大: 期望 {want_value},"
                              f"实际 {got}")

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

        return {
            "id": scenario["id"], "store_id": scenario["store_id"],
            "main_factor": report.main_factor,
            "main_factor_contrib": report.main_factor_contrib,
            "ran_steps": ran,
            "root_cause_hit": main_hit, "contrib_close": contrib_close,
            "evidence_coverage": coverage, "structure_ok": not structure,
            "efficiency": efficient,
            "passed": not errors, "errors": errors,
        }

    @staticmethod
    def _summary(results: list[dict]) -> dict:
        total = len(results)
        passed = sum(1 for r in results if r["passed"])
        return {
            "total": total, "passed": passed,
            "metrics": {
                "root_cause_hit": f"{sum(r['root_cause_hit'] for r in results)}/{total}",
                "contrib_close": f"{sum(r['contrib_close'] for r in results)}/{total}",
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
                           f"({r['store_id']}) 主因={r['main_factor']} "
                           f"贡献={r['main_factor_contrib']}")
            for err in r["errors"]:
                Terminal.print(f"      {Terminal.RED}- {err}{Terminal.RESET}")
