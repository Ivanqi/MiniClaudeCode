"""混合检索测试:query expansion、RRF、rerank、降级与真实语料回归。"""
import json
import time
import unittest
from datetime import datetime
from pathlib import Path

from Catering.CaseIndex import CaseIndex
from Catering.Cases import load_cases
from Catering.Configs import CateringConfigs
from Catering.Evaluator import Evaluator
from Catering.Retrieval import (
    Bm25CaseRetriever,
    HybridCaseRetriever,
    LexicalCaseRetriever,
    analyze_query,
    build_case_retriever,
    rrf_fuse,
)


def fixed_clock() -> float:
    return datetime.fromisoformat("2026-09-01").timestamp()


def make_case(case_id="CASE-A", store_id="A店", main_factor="traffic",
              summary="暴雨导致客流下降", tags=("暴雨",),
              dimensions=("traffic",), symptoms=("客流下降",),
              actions=("检查排班",), date="2026-06-01", **overrides):
    payload = {
        "case_id": case_id, "store_id": store_id, "date": date,
        "main_factor": main_factor, "dimensions": list(dimensions),
        "summary": summary, "actions": list(actions),
        "tags": list(tags), "symptoms": list(symptoms),
        "effective_from": date, "effective_to": "",
        "deprecated": False, "confidence": 0.8,
    }
    payload.update(overrides)
    return payload


class TestHybridFixtures(unittest.TestCase):
    def setUp(self):
        self.cases = [
            make_case("CASE-A"),
            make_case("CASE-B", store_id="B店", main_factor="ticket",
                      summary="低价套餐拉低客单价",
                      tags=("低价套餐", "客单价下降"),
                      dimensions=("ticket", "dish"),
                      symptoms=("客单价下降",)),
        ]
        self.retriever = HybridCaseRetriever(
            lambda: self.cases, clock=fixed_clock,
            rerank_mode="heuristic")
        self.addCleanup(self.retriever.close)

    def test_contract_and_cross_store(self):
        result = self.retriever.search(
            "暴雨 客流下降", store_id="A店", date="2026-09-01",
            main_factor="traffic", top_k=3)
        required = {
            "store_id", "date", "main_factor", "query", "symptom",
            "match_count", "matches", "candidates", "filters",
            "missing_fields", "retrieval", "degraded", "latency_ms",
        }
        self.assertTrue(required <= set(result))
        self.assertEqual(result["matches"][0]["case_id"], "CASE-A")
        self.assertIn("excluded", result["filters"])
        self.assertEqual(result["retrieval"]["mode"], "hybrid")
        self.assertIn(result["retrieval"]["engine"], ("fts5", "python"))
        self.assertFalse(result["degraded"])

        cross = self.retriever.search(
            "低价套餐 客单价下降", store_id="A店", date="2026-09-01",
            top_k=3)
        self.assertIn("CASE-B",
                      [match["case_id"] for match in cross["matches"]])

    def test_store_only_is_not_recalled(self):
        result = self.retriever.search(
            "S05店 露营 宠物 团建", store_id="S05店",
            date="2026-09-01", top_k=3)
        self.assertEqual(result["match_count"], 0)
        self.assertEqual(result["matches"], [])

    def test_query_expansion_is_conservative(self):
        negative = analyze_query("台风预警 临时闭店")
        self.assertEqual(negative.expanded_terms, ())
        self.assertNotIn("weather", negative.terms)
        self.assertNotIn("external", negative.terms)
        self.assertEqual(analyze_query("POS宕机 收银中断").expanded_terms, ())

        expanded = analyze_query("客流下降 暴雨", "traffic")
        self.assertIn("traffic", expanded.expanded_terms)
        self.assertIn("weather", expanded.expanded_terms)
        self.assertIn("external", expanded.expanded_terms)
        self.assertNotIn("traffic", expanded.original_terms)

        # main_factor 只做结构化信号,不能注入 BM25 文本。
        factor_only = analyze_query("露营 宠物", "traffic")
        self.assertNotIn("traffic", factor_only.terms)

        many = analyze_query(
            "客流 客单价 营业额 暴雨 竞对 促销 评分 排班 菜品 渠道 时段")
        self.assertLessEqual(len(many.expanded_terms), 10)

    def test_rrf_formula_and_route_details(self):
        routes = {
            "bm25": [{"case": {"case_id": "X"}, "rank": 1,
                      "score": 10.0}],
            "structured": [
                {"case": {"case_id": "Y"}, "rank": 1, "score": 5.0},
                {"case": {"case_id": "X"}, "rank": 2, "score": 4.0},
            ],
        }
        fused = rrf_fuse(routes, k=60)
        by_id = {item["case_id"]: item for item in fused}
        self.assertEqual(fused[0]["case_id"], "X")
        self.assertAlmostEqual(
            by_id["X"]["rrf"], 1 / 61 + 1 / 62)
        self.assertEqual(by_id["X"]["routes"]["bm25"]["rank"], 1)
        self.assertEqual(by_id["X"]["routes"]["structured"]["rank"], 2)

    def test_invalid_date(self):
        with self.assertRaises(ValueError):
            self.retriever.search("x", date="bad-date")

    def test_bm25_standalone_contract(self):
        retriever = Bm25CaseRetriever(
            lambda: self.cases, clock=fixed_clock)
        self.addCleanup(retriever.close)
        result = retriever.search(
            "暴雨 客流下降", store_id="A店", date="2026-09-01")
        self.assertEqual(result["retrieval"]["mode"], "bm25")
        self.assertIn("CASE-A",
                      [match["case_id"] for match in result["matches"]])
        self.assertFalse(result["degraded"])

    def test_factory_modes(self):
        self.assertIsInstance(
            build_case_retriever(lambda: self.cases, mode="lexical"),
            LexicalCaseRetriever)
        hybrid = build_case_retriever(lambda: self.cases, mode="hybrid")
        self.addCleanup(hybrid.close)
        self.assertIsInstance(hybrid, HybridCaseRetriever)
        self.assertIsInstance(
            build_case_retriever(lambda: self.cases, mode="bogus"),
            LexicalCaseRetriever)

    def test_index_error_falls_back_to_lexical(self):
        class BrokenIndex:
            engine = "broken"
            version = 0
            doc_count = 0

            def sync(self, cases):
                raise RuntimeError("boom")

            def search(self, *args, **kwargs):
                raise RuntimeError("boom")

        retriever = HybridCaseRetriever(
            lambda: self.cases, index=BrokenIndex(), clock=fixed_clock)
        result = retriever.search(
            "暴雨 客流下降", store_id="A店", date="2026-09-01",
            main_factor="traffic")
        self.assertTrue(result["degraded"])
        self.assertEqual(result["retrieval"]["mode"], "lexical_fallback")
        self.assertIn("index_error", result["retrieval"]["degraded_reason"])
        self.assertIn("CASE-A",
                      [match["case_id"] for match in result["matches"]])

    def test_deadline_skips_rerank(self):
        class SlowIndex(CaseIndex):
            def sync(self, cases):
                time.sleep(0.05)
                return super().sync(cases)

        retriever = HybridCaseRetriever(
            lambda: self.cases, index=SlowIndex(engine="python"),
            clock=fixed_clock, timeout_ms=5)
        self.addCleanup(retriever.close)
        result = retriever.search(
            "暴雨 客流下降", store_id="A店", date="2026-09-01",
            main_factor="traffic")
        self.assertTrue(result["degraded"])
        self.assertIn("rerank_skipped_timeout",
                      result["retrieval"]["degraded_reason"])
        self.assertEqual(
            result["matches"][0]["score_breakdown"]["score_kind"], "rrf")

    def test_provider_changes_are_synced(self):
        cases = [make_case("CASE-A")]
        retriever = HybridCaseRetriever(lambda: cases, clock=fixed_clock)
        self.addCleanup(retriever.close)
        before = retriever.search(
            "露营 宠物", store_id="S01店", date="2026-09-01")
        self.assertEqual(before["match_count"], 0)

        new_case = make_case(
            "CASE-CAMP", store_id="S01店", summary="露营宠物活动引流",
            tags=("露营", "宠物"), dimensions=("traffic",),
            symptoms=("宠物活动",))
        cases.append(new_case)
        after = retriever.search(
            "露营 宠物", store_id="S01店", date="2026-09-01")
        self.assertIn("CASE-CAMP",
                      [match["case_id"] for match in after["matches"]])

        cases.remove(new_case)
        removed = retriever.search(
            "露营 宠物", store_id="S01店", date="2026-09-01")
        self.assertEqual(removed["match_count"], 0)


class TestHybridRealCorpus(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = load_cases(CateringConfigs.CASES_DIR)
        cls.retriever = HybridCaseRetriever(
            lambda: cls.cases, clock=fixed_clock,
            rerank_mode="heuristic")

    @classmethod
    def tearDownClass(cls):
        cls.retriever.close()

    def test_r17_curated_case_is_top1(self):
        """RRF 后 curated 案例可能掉到十几名;lexical 主导重排必须拉回 top1。"""
        result = self.retriever.search(
            "客单价下降 客流上升", store_id="B店", date="2026-09-01",
            main_factor="ticket", top_k=3)
        self.assertEqual(result["matches"][0]["case_id"],
                         "CASE-2026-0712-B")
        self.assertGreater(result["retrieval"]["route_counts"]["bm25"], 0)
        self.assertGreater(
            result["retrieval"]["route_counts"]["structured"], 0)
        self.assertEqual(
            result["matches"][0]["score_breakdown"]["score_kind"], "rerank")

    def test_retrieval_error_is_not_counted_as_no_match(self):
        class FailingHub:
            def search_cases(self, *args, **kwargs):
                raise RuntimeError("boom")

        evaluator = Evaluator()
        pairs = evaluator.load_retrieval_pairs()
        groups = evaluator._retrieval_groups
        negative = next(pair for pair in pairs if pair["id"] == "R21")
        row = evaluator._run_retrieval_pair(
            FailingHub(), negative, groups)
        self.assertFalse(row["hit"])
        self.assertIn("boom", row["error"] or "")

    def test_regression_pairs_pass(self):
        payload = json.loads(
            CateringConfigs.RETRIEVAL_EVAL_PATH.read_text())
        pairs = [pair for pair in payload["pairs"]
                 if pair.get("split", "regression") == "regression"]
        self.assertEqual(len(pairs), 22)
        for pair in pairs:
            with self.subTest(pair=pair["id"]):
                result = self.retriever.search(
                    pair.get("query", ""),
                    store_id=pair.get("store_id", ""),
                    date=pair.get("date", ""),
                    main_factor=pair.get("main_factor", ""),
                    top_k=int(pair.get("top_k", 3)))
                ranked = [match.get("case_id")
                          for match in result.get("matches") or []]
                expected = set(pair.get("expected_case_ids") or [])
                if expected:
                    self.assertTrue(expected & set(ranked),
                                    f"{pair['id']} ranked={ranked}")
                else:
                    self.assertEqual(ranked, [], f"{pair['id']}")
                    self.assertEqual(result["match_count"], 0)


if __name__ == "__main__":
    unittest.main()
