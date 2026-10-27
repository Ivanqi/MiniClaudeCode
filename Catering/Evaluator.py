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
import math
import time
from datetime import datetime, timezone
from pathlib import Path

from src.Terminal import Terminal

from Catering.Configs import CateringConfigs
from Catering.Guardrails import OutputValidator
from Catering.Models import AnomalyAlert
from Catering.Retrieval import HybridCaseRetriever, LexicalCaseRetriever


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
        self._retrieval_groups: dict = {}

    def load_scenarios(self) -> list[dict]:
        payload = json.loads(self.scenarios_path.read_text())
        if payload.get("schema_version") != CateringConfigs.SCHEMA_VERSION:
            raise ValueError("评测场景 schema_version 不支持")
        return payload.get("scenarios") or []

    def load_retrieval_pairs(self) -> list[dict]:
        """加载检索标注对;文件缺失时返回空(不阻断诊断评测)。"""
        self._retrieval_groups = {}
        if not self.retrieval_path.exists():
            return []
        payload = json.loads(self.retrieval_path.read_text())
        if payload.get("schema_version") != CateringConfigs.SCHEMA_VERSION:
            raise ValueError("检索评测 schema_version 不支持")
        self._retrieval_groups = payload.get("groups") or {}
        return payload.get("pairs") or []

    @staticmethod
    def _expected_ids(pair: dict, groups: dict) -> list[str]:
        expected = list(dict.fromkeys(pair.get("expected_case_ids") or []))
        group = pair.get("group")
        if group and group in groups:
            expected.extend(groups[group])
        return list(dict.fromkeys(expected))

    @staticmethod
    def _ndcg_at_k(ranked: list[str], relevant: set[str], k: int = 3) -> float:
        gains = [1.0 if case_id in relevant else 0.0
                 for case_id in ranked[:k]]
        dcg = sum(gain / math.log2(index + 2)
                  for index, gain in enumerate(gains))
        ideal = sum(1.0 / math.log2(index + 2)
                    for index in range(min(len(relevant), k)))
        return dcg / ideal if ideal else 0.0

    @staticmethod
    def _percentile(values: list[float], q: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        index = max(0, min(len(ordered) - 1,
                           int(math.ceil(q * len(ordered))) - 1))
        return ordered[index]

    def _run_retrieval_pair(self, hub, pair: dict, groups: dict) -> dict:
        top_k = int(pair.get("top_k", 3))
        split = str(pair.get("split") or "regression")
        expected = self._expected_ids(pair, groups)
        relevance = pair.get("relevance") or (
            "group" if split in ("group", "probe")
            else ("exact" if expected else "none"))
        must_exclude = [str(item) for item in
                        (pair.get("must_exclude") or [])]
        started = time.perf_counter()
        error = None
        try:
            result = hub.search_cases(
                pair.get("store_id", ""), pair.get("query", ""),
                top_k=top_k, date=pair.get("date", ""),
                main_factor=pair.get("main_factor", ""))
        except Exception as exc:  # 单个 pair 失败不阻断整套评测
            result = {}
            error = str(exc)
        latency_ms = (time.perf_counter() - started) * 1000.0
        ranked = [match.get("case_id")
                  for match in (result.get("matches") or [])
                  if match.get("case_id")]
        candidate_ids = [match.get("case_id")
                         for match in (result.get("candidates") or [])
                         if match.get("case_id")]
        excluded_set = set(must_exclude)
        leaks = [case_id for case_id in ranked
                 if case_id in excluded_set]
        candidate_leaks = [case_id for case_id in candidate_ids
                           if case_id in excluded_set]
        expected_set = set(expected)
        ranks = [index + 1 for index, case_id in enumerate(ranked)
                 if case_id in expected_set]

        row = {
            "id": pair.get("id"), "query": pair.get("query"),
            "store_id": pair.get("store_id"),
            "date": pair.get("date", ""),
            "main_factor": pair.get("main_factor", ""),
            "top_k": top_k, "split": split, "relevance": relevance,
            "expected": expected, "ranked": ranked,
            "must_exclude": must_exclude, "leaks": leaks,
            "candidate_leaks": candidate_leaks,
            "latency_ms": latency_ms,
            "degraded": bool(result.get("degraded")),
            "retrieval_mode": (result.get("retrieval") or {}).get("mode"),
            "cold_build_ms": ((result.get("retrieval") or {})
                              .get("cold_build_ms") or 0.0),
            "error": error,
            "hit": False, "recall": None, "recall_at_3": None,
            "precision": None, "precision_at_1": None,
            "reciprocal_rank": None, "ndcg_at_3": None,
            "group_hit_at_1": None, "group_hit_at_3": None,
            "group_mrr": None, "group_ndcg_at_3": None,
        }

        if relevance == "group" and expected:
            row["group_hit_at_1"] = (
                1.0 if ranked and ranked[0] in expected_set else 0.0)
            row["group_hit_at_3"] = (
                1.0 if any(case_id in expected_set
                           for case_id in ranked[:3]) else 0.0)
            row["group_mrr"] = 1.0 / ranks[0] if ranks else 0.0
            row["group_ndcg_at_3"] = self._ndcg_at_k(
                ranked, expected_set, 3)
            row["hit"] = (bool(row["group_hit_at_3"])
                          and not leaks and not candidate_leaks)
        elif expected:
            relevant_in_top = len(expected_set & set(ranked))
            row["recall"] = relevant_in_top / len(expected_set)
            row["recall_at_3"] = (
                len(expected_set & set(ranked[:3])) / len(expected_set))
            row["precision"] = (relevant_in_top / len(ranked)
                                if ranked else 0.0)
            row["precision_at_1"] = (
                1.0 if ranked and ranked[0] in expected_set else 0.0)
            row["reciprocal_rank"] = 1.0 / ranks[0] if ranks else 0.0
            row["ndcg_at_3"] = self._ndcg_at_k(ranked, expected_set, 3)
            row["hit"] = (bool(ranks) and not leaks
                          and not candidate_leaks)
        elif split == "filter":
            # 允许命中 live 案例,只断言过滤边界案例不泄漏。
            row["hit"] = not leaks and not candidate_leaks
        else:
            row["hit"] = (not ranked and not leaks
                          and not candidate_leaks)
            row["recall"] = 1.0 if row["hit"] else 0.0
            row["recall_at_3"] = row["recall"]
            row["precision"] = row["recall"]
            row["precision_at_1"] = row["recall"]
        if error:
            # 检索异常不能被误判成“无匹配正确”。
            row["hit"] = False
        return row

    def run_retrieval_suite(self, hub, retriever=None) -> dict:
        """检索评测:strict/group/filter/probe 分层指标 + 延迟分位。"""
        pairs = self.load_retrieval_pairs()
        groups = self._retrieval_groups or {}
        source = None
        old_retriever = None
        if retriever is not None:
            source = (hub.sources.get("knowledge_base")
                      if hasattr(hub, "sources") else None)
            if source is not None:
                old_retriever = getattr(source, "retriever", None)
                source.retriever = retriever
        try:
            results = [self._run_retrieval_pair(hub, pair, groups)
                       for pair in pairs]
        finally:
            if source is not None:
                source.retriever = old_retriever

        regression = [r for r in results if r["split"] == "regression"]
        positive = [r for r in regression if r["expected"]]
        negative = [r for r in regression if not r["expected"]]
        group_rows = [r for r in results if r["split"] == "group"]
        probe_rows = [r for r in results if r["split"] == "probe"]
        filter_rows = [r for r in results if r["split"] == "filter"]
        latencies = [r["latency_ms"] for r in results]
        cold_build = max((r.get("cold_build_ms") or 0.0)
                         for r in results) if results else 0.0

        def average(rows: list[dict], key: str) -> str:
            values = [row[key] for row in rows
                      if row.get(key) is not None]
            return f"{sum(values) / len(values):.3f}" if values else "n/a"

        def pass_rate(rows: list[dict]) -> str:
            return (f"{sum(1 for row in rows if row['hit']) / len(rows):.3f}"
                    if rows else "n/a")

        metrics = {
            "recall_at_k": average(positive, "recall"),
            "recall_at_3": average(positive, "recall_at_3"),
            "precision_at_k": average(positive, "precision"),
            "precision_at_1": average(positive, "precision_at_1"),
            "mrr": average(positive, "reciprocal_rank"),
            "ndcg_at_3": average(positive, "ndcg_at_3"),
            "no_match_accuracy": pass_rate(negative),
            "pair_pass_rate": pass_rate(results),
            "group_hit_at_1": average(group_rows, "group_hit_at_1"),
            "group_hit_at_3": average(group_rows, "group_hit_at_3"),
            "group_mrr": average(group_rows, "group_mrr"),
            "group_ndcg_at_3": average(group_rows, "group_ndcg_at_3"),
            "probe_hit_at_1": average(probe_rows, "group_hit_at_1"),
            "probe_hit_at_3": average(probe_rows, "group_hit_at_3"),
            "probe_mrr": average(probe_rows, "group_mrr"),
            "probe_ndcg_at_3": average(probe_rows, "group_ndcg_at_3"),
            "filter_pass_rate": pass_rate(filter_rows),
            "excluded_leak_matches": str(
                sum(len(row["leaks"]) for row in results)),
            "excluded_leak_candidates": str(
                sum(len(row["candidate_leaks"]) for row in results)),
            "p50_latency_ms": f"{self._percentile(latencies, 0.50):.1f}",
            "p95_latency_ms": f"{self._percentile(latencies, 0.95):.1f}",
            "max_latency_ms": f"{max(latencies):.1f}" if latencies else "n/a",
            "cold_build_ms": f"{cold_build:.1f}",
            "degraded_count": str(
                sum(1 for row in results if row["degraded"])),
        }
        return {
            "total": len(results),
            "passed": sum(1 for row in results if row["hit"]),
            "regression_total": len(regression),
            "regression_passed": sum(1 for row in regression
                                     if row["hit"]),
            "group_total": len(group_rows),
            "group_passed": sum(1 for row in group_rows if row["hit"]),
            "filter_total": len(filter_rows),
            "filter_passed": sum(1 for row in filter_rows if row["hit"]),
            "probe_total": len(probe_rows),
            "probe_passed": sum(1 for row in probe_rows if row["hit"]),
            "metrics": metrics, "pairs": results,
        }

    def compare_retrieval_modes(self, hub) -> dict:
        """同一评测集上对比 lexical / hybrid / hybrid+heuristic。"""
        source = (hub.sources.get("knowledge_base")
                  if hasattr(hub, "sources") else None)
        if source is None:
            return {}
        provider = source.all_cases
        fixed_clock = lambda: datetime(
            2026, 9, 1, tzinfo=timezone.utc).timestamp()
        retrievers = {
            "lexical": LexicalCaseRetriever(
                provider, clock=fixed_clock),
            "hybrid": HybridCaseRetriever(
                provider, clock=fixed_clock, rerank_mode="off"),
            "hybrid_rerank": HybridCaseRetriever(
                provider, clock=fixed_clock, rerank_mode="heuristic"),
        }
        pairs = self.load_retrieval_pairs()
        comparison = {}
        for name, retriever in retrievers.items():
            if pairs:
                first = pairs[0]
                try:
                    retriever.search(
                        first.get("query", ""),
                        store_id=first.get("store_id", ""),
                        date=first.get("date", ""),
                        main_factor=first.get("main_factor", ""),
                        top_k=int(first.get("top_k", 3)))
                except Exception:
                    pass
            try:
                comparison[name] = self.run_retrieval_suite(
                    hub, retriever=retriever)
            finally:
                close = getattr(retriever, "close", None)
                if close is not None:
                    close()
        return comparison

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
        """终端输出检索评测汇总(strict/group/filter/probe + 延迟)。"""
        if not summary.get("total"):
            Terminal.print("\n未配置检索评测集,跳过。")
            return
        passed = summary["passed"]
        total = summary["total"]
        regression_ok = (summary.get("regression_passed", 0)
                         == summary.get("regression_total", 0))
        color = Terminal.GREEN if regression_ok else Terminal.RED
        Terminal.print(
            f"\n{color}=== 检索评测: {passed}/{total} pair 命中 "
            f"(regression {summary.get('regression_passed', 0)}"
            f"/{summary.get('regression_total', 0)}, "
            f"group {summary.get('group_passed', 0)}"
            f"/{summary.get('group_total', 0)}, "
            f"filter {summary.get('filter_passed', 0)}"
            f"/{summary.get('filter_total', 0)}, "
            f"probe {summary.get('probe_passed', 0)}"
            f"/{summary.get('probe_total', 0)}){Terminal.RESET}")
        for key, value in summary["metrics"].items():
            Terminal.print(f"  {key}: {value}")
        for pair in summary["pairs"]:
            if pair["hit"] and pair["split"] != "probe":
                continue
            expected = pair["expected"]
            expected_text = (f"{len(expected)} group cases"
                             if len(expected) > 5 else expected)
            mark = (Terminal.GREEN + "PASS" if pair["hit"]
                    else Terminal.RED + "FAIL")
            Terminal.print(
                f"  [{mark}{Terminal.RESET}] {pair['id']} "
                f"({pair['split']}) expected={expected_text} "
                f"ranked={pair['ranked']}")

    @staticmethod
    def print_retrieval_comparison(comparison: dict):
        """终端输出 lexical / hybrid / hybrid+heuristic 对比。"""
        if not comparison:
            Terminal.print("\n暂无可对比的检索模式。")
            return
        Terminal.print("\n=== 检索模式对比 ===")
        Terminal.print(f"{'mode':<16} {'reg':>7} {'R@3':>6} {'MRR':>6} "
                       f"{'group@3':>8} {'probe@3':>8} {'p95ms':>7} "
                       f"{'deg':>4}")
        for name, summary in comparison.items():
            metrics = summary.get("metrics") or {}
            regression = (f"{summary.get('regression_passed', 0)}"
                          f"/{summary.get('regression_total', 0)}")
            Terminal.print(
                f"{name:<16} {regression:>7} "
                f"{metrics.get('recall_at_3', 'n/a'):>6} "
                f"{metrics.get('mrr', 'n/a'):>6} "
                f"{metrics.get('group_hit_at_3', 'n/a'):>8} "
                f"{metrics.get('probe_hit_at_3', 'n/a'):>8} "
                f"{metrics.get('p95_latency_ms', 'n/a'):>7} "
                f"{metrics.get('degraded_count', '0'):>4}")
