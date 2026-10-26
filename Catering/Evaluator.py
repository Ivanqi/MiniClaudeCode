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
                 expected_path: Path | None = None,
                 retrieval_path: Path | None = None):
        self.scenarios_path = scenarios_path or CateringConfigs.EVAL_SCENARIOS_PATH
        self.expected_path = expected_path or (
                CateringConfigs.DATA_DIR / "expected_contributions.json")
        self.retrieval_path = (retrieval_path
                               or CateringConfigs.RETRIEVAL_EVAL_PATH)

    def load_scenarios(self) -> list[dict]:
        payload = json.loads(self.scenarios_path.read_text())
        if payload.get("schema_version") != CateringConfigs.SCHEMA_VERSION:
            raise ValueError("评测场景 schema_version 不支持")
        return payload.get("scenarios") or []

    def load_retrieval_pairs(self) -> list[dict]:
        """加载检索标注对;文件缺失时返回空(不阻断诊断评测)。"""
        if not self.retrieval_path.exists():
            return []
        payload = json.loads(self.retrieval_path.read_text())
        if payload.get("schema_version") != CateringConfigs.SCHEMA_VERSION:
            raise ValueError("检索评测 schema_version 不支持")
        return payload.get("pairs") or []

    def run_retrieval_suite(self, hub) -> dict:
        """检索评测:recall@k / MRR / 无匹配准确率,全程离线。"""
        results = []
        for pair in self.load_retrieval_pairs():
            top_k = int(pair.get("top_k", 3))
            result = hub.search_cases(
                pair.get("store_id", ""), pair.get("query", ""),
                top_k=top_k, date=pair.get("date", ""),
                main_factor=pair.get("main_factor", ""))
            ranked = [match.get("case_id")
                      for match in (result.get("matches") or [])
                      if match.get("case_id")]
            expected = list(dict.fromkeys(pair.get("expected_case_ids") or []))
            expected_set = set(expected)
            ranks = [i + 1 for i, case_id in enumerate(ranked)
                     if case_id in expected_set]
            if expected:
                hit = bool(ranks)
                relevant_in_top = len(expected_set & set(ranked))
                recall = relevant_in_top / len(expected_set)
                precision = (relevant_in_top / len(ranked)
                             if ranked else 0.0)
                precision_at_1 = (1.0 if ranked
                                  and ranked[0] in expected_set else 0.0)
                reciprocal_rank = 1.0 / ranks[0] if ranks else 0.0
            else:
                hit = not ranked
                recall = 1.0 if not ranked else 0.0
                precision = 1.0 if not ranked else 0.0
                precision_at_1 = 1.0 if not ranked else 0.0
                reciprocal_rank = None
            results.append({
                "id": pair.get("id"), "query": pair.get("query"),
                "store_id": pair.get("store_id"),
                "date": pair.get("date", ""),
                "main_factor": pair.get("main_factor", ""),
                "top_k": top_k, "expected": expected, "ranked": ranked,
                "hit": hit, "recall": recall, "precision": precision,
                "precision_at_1": precision_at_1,
                "reciprocal_rank": reciprocal_rank,
            })
        positive = [r for r in results if r["expected"]]
        negative = [r for r in results if not r["expected"]]
        metrics = {
            "recall_at_k": (f"{sum(r['recall'] for r in positive) / len(positive):.3f}"
                            if positive else "n/a"),
            "precision_at_k": (f"{sum(r['precision'] for r in positive) / len(positive):.3f}"
                               if positive else "n/a"),
            "precision_at_1": (f"{sum(r['precision_at_1'] for r in positive) / len(positive):.3f}"
                               if positive else "n/a"),
            "mrr": (f"{sum(r['reciprocal_rank'] for r in positive) / len(positive):.3f}"
                    if positive else "n/a"),
            "no_match_accuracy": (f"{sum(1 for r in negative if r['hit']) / len(negative):.3f}"
                                  if negative else "n/a"),
            "pair_pass_rate": (f"{sum(1 for r in results if r['hit']) / len(results):.3f}"
                               if results else "n/a"),
        }
        return {"total": len(results),
                "passed": sum(1 for r in results if r["hit"]),
                "metrics": metrics, "pairs": results}

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

        matched_case_ids = {case.get("case_id")
                            for case in report.matched_cases
                            if case.get("case_id")}
        invalid_case_refs = [
            ref for rec in report.recommendations
            for ref in (rec.get("case_refs") or [])
            if ref not in matched_case_ids]
        citation_valid = not invalid_case_refs
        recommendations = report.recommendations or []
        unsupported = [
            rec for rec in recommendations
            if rec.get("source") in ("llm", "case")
            and not (rec.get("evidence_refs") or [])
            and not (rec.get("case_refs") or [])]

        return {
            "id": scenario["id"], "store_id": scenario["store_id"],
            "main_factor": report.main_factor,
            "main_factor_contrib": report.main_factor_contrib,
            "ran_steps": ran,
            "root_cause_hit": main_hit, "contrib_close": contrib_close,
            "evidence_coverage": coverage, "structure_ok": not structure,
            "efficiency": efficient,
            "case_citation_valid": citation_valid,
            "invalid_case_refs": invalid_case_refs,
            "unsupported_recommendations": len(unsupported),
            "unsupported_rate": (len(unsupported) / len(recommendations)
                                 if recommendations else 0.0),
            "passed": not errors, "errors": errors,
        }

    @staticmethod
    def _summary(results: list[dict]) -> dict:
        total = len(results)
        passed = sum(1 for r in results if r["passed"])
        unsupported = sum(r.get("unsupported_recommendations", 0)
                          for r in results)
        return {
            "total": total, "passed": passed,
            "metrics": {
                "root_cause_hit": f"{sum(r['root_cause_hit'] for r in results)}/{total}",
                "contrib_close": f"{sum(r['contrib_close'] for r in results)}/{total}",
                "evidence_coverage": f"{sum(r['evidence_coverage'] for r in results)}/{total}",
                "structure_ok": f"{sum(r['structure_ok'] for r in results)}/{total}",
                "efficiency": f"{sum(r['efficiency'] for r in results)}/{total}",
                "case_citation_valid": f"{sum(r['case_citation_valid'] for r in results)}/{total}",
                "unsupported_recommendations": unsupported,
                "unsupported_recommendation_rate":
                    (f"{sum(r.get('unsupported_rate', 0.0) for r in results) / total:.3f}"
                     if total else "n/a"),
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

    @staticmethod
    def print_retrieval_report(summary: dict):
        """终端输出检索评测汇总(recall@k/MRR/无匹配准确率)。"""
        if not summary.get("total"):
            Terminal.print("\n未配置检索评测集,跳过。")
            return
        passed = summary["passed"]
        total = summary["total"]
        color = Terminal.GREEN if passed == total else Terminal.YELLOW
        Terminal.print(f"\n{color}=== 检索评测: {passed}/{total} pair 命中 "
                       f"{Terminal.RESET}")
        for key, value in summary["metrics"].items():
            Terminal.print(f"  {key}: {value}")
        for pair in summary["pairs"]:
            mark = Terminal.GREEN + "PASS" if pair["hit"] else Terminal.RED + "FAIL"
            Terminal.print(f"  [{mark}{Terminal.RESET}] {pair['id']} "
                           f"expected={pair['expected']} ranked={pair['ranked']}")
