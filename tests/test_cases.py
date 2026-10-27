"""Catering 案例记忆测试:rglob、upsert/version、去重、时间衰减。"""
import json
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

from Catering.Cases import CaseStore, load_cases, validate_case_id
from Catering.Configs import CateringConfigs
from Catering.DataHub import DataHub
from Catering.Retrieval import LexicalCaseRetriever


def case(case_id="CASE-2026-09-01-A店", summary="客流下降", **overrides):
    payload = {
        "case_id": case_id, "store_id": "A店", "date": "2026-09-01",
        "main_factor": "traffic", "dimensions": ["traffic"],
        "summary": summary, "actions": ["检查排班"],
        "tags": ["暴雨", "客流下降"],
    }
    payload.update(overrides)
    return payload


class TestCases(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cases_dir = self.tmp / "cases"
        self.data_dir = self.tmp / "data"
        (self.data_dir / "cases").mkdir(parents=True)
        self._old_cases = CateringConfigs.CASES_DIR
        CateringConfigs.CASES_DIR = self.cases_dir
        self.cases_dir.mkdir(parents=True)

    def tearDown(self):
        CateringConfigs.CASES_DIR = self._old_cases

    def test_legacy_envelope_is_loaded(self):
        (self.cases_dir / "history_cases.json").write_text(json.dumps({
            "cases": [case(case_id="CASE-LEGACY-1"),
                      case(case_id="CASE-LEGACY-2")]}))
        loaded = load_cases(self.cases_dir)
        self.assertEqual({item["case_id"] for item in loaded},
                         {"CASE-LEGACY-1", "CASE-LEGACY-2"})
        self.assertEqual(loaded[0]["source"], "legacy")
        self.assertIn("version", loaded[0])

    def test_upsert_version_and_envelope(self):
        store = CaseStore()
        first = store.upsert(case(), source="tool:archive_case",
                             confidence=0.8)
        self.assertTrue(first.created)
        self.assertEqual(first.record["version"], 1)
        path = self.cases_dir / "archive" / "CASE-2026-09-01-A店.json"
        self.assertTrue(path.exists())
        payload = json.loads(path.read_text())
        self.assertIn("cases", payload)
        self.assertEqual(payload["cases"][0]["case_id"],
                         "CASE-2026-09-01-A店")

        same = store.upsert(case(), source="tool:archive_case",
                            confidence=0.8)
        self.assertFalse(same.updated)
        self.assertEqual(same.record["version"], 1)

        changed = store.upsert(case(summary="客流继续下降"),
                               source="tool:archive_case", confidence=0.9)
        self.assertTrue(changed.updated)
        self.assertEqual(changed.record["version"], 2)
        self.assertEqual(changed.record["summary"], "客流继续下降")
        self.assertEqual(changed.record["confidence"], 0.9)
        self.assertEqual(len(list((self.cases_dir / "archive").glob("*.json"))), 1)

    def test_load_sees_archive_and_dedupes(self):
        store = CaseStore()
        store.upsert(case())
        store.upsert(case(summary="新版"))
        loaded = load_cases(self.cases_dir)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["version"], 2)
        self.assertEqual(loaded[0]["summary"], "新版")

    def test_search_dedupes_and_projects_metadata(self):
        (self.cases_dir / "history_cases.json").write_text(json.dumps({
            "cases": [case(case_id="CASE-DUP", summary="old")]}))
        store = CaseStore()
        store.upsert(case(case_id="CASE-DUP", summary="new"),
                     source="tool:archive_case", confidence=0.9)
        hub = DataHub(data_dir=self.data_dir)
        result = hub.search_cases("A店", "客流下降", top_k=5)
        self.assertEqual(result["match_count"], 1)
        match = result["matches"][0]
        self.assertEqual(match["case_id"], "CASE-DUP")
        self.assertEqual(match["version"], 2)
        self.assertEqual(match["summary"], "new")
        self.assertIn("source", match)
        self.assertIn("confidence", match)

    def test_time_decay_prefers_recent_case(self):
        old = case(case_id="CASE-OLD", summary="old")
        new = case(case_id="CASE-NEW", summary="new")
        (self.cases_dir / "history_cases.json").write_text(json.dumps({
            "cases": [old, new]}))
        old_ts = time.time() - 365 * 86400
        new_ts = time.time()
        for path in [self.cases_dir / "history_cases.json"]:
            payload = json.loads(path.read_text())
            payload["cases"][0].update({"updated_at": old_ts,
                                        "confidence": 0.9})
            payload["cases"][1].update({"updated_at": new_ts,
                                        "confidence": 0.9})
            path.write_text(json.dumps(payload))
        hub = DataHub(data_dir=self.data_dir)
        result = hub.search_cases("A店", "客流下降", top_k=2)
        self.assertEqual(result["matches"][0]["case_id"], "CASE-NEW")

    def test_invalid_case_id_rejected(self):
        for value in ("../escape", "a/b", "", "x" * 201):
            with self.assertRaises(ValueError):
                validate_case_id(value)
        self.assertEqual(validate_case_id("CASE-2026-09-01-A店"),
                         "CASE-2026-09-01-A店")

    def test_extended_fields_persist_and_bump_version(self):
        """新增 schema 字段参与 hash/版本:症状变化必须产生新版本。"""
        store = CaseStore()
        first = store.upsert(case(
            symptoms=["暴雨", "客流下降"],
            metrics_snapshot={"traffic.pct_change": -25.0},
            outcome="次日恢复", effective_from="2026-06-22",
            effective_to="2026-07-31"), source="tool:archive_case",
            confidence=0.8)
        self.assertEqual(first.record["effective_from"], "2026-06-22")
        self.assertEqual(set(first.record["symptoms"]), {"暴雨", "客流下降"})
        self.assertEqual(
            first.record["metrics_snapshot"]["traffic.pct_change"], -25.0)
        self.assertEqual(first.record["outcome"], "次日恢复")
        self.assertFalse(first.record["deprecated"])

        changed = store.upsert(case(
            symptoms=["暴雨", "客流下降", "外卖上升"],
            metrics_snapshot={"traffic.pct_change": -25.0},
            outcome="次日恢复", effective_from="2026-06-22",
            effective_to="2026-07-31"), source="tool:archive_case",
            confidence=0.8)
        self.assertTrue(changed.updated)
        self.assertEqual(changed.record["version"], 2)
        loaded = load_cases(self.cases_dir)
        self.assertEqual(loaded[0]["version"], 2)
        self.assertEqual(len(loaded[0]["symptoms"]), 3)

    def test_retrieval_date_and_deprecated_filters(self):
        """告警日之前/生效期内/未废弃才可召回;同店只是加权不是硬过滤。"""
        payload = {"cases": [
            case(case_id="CASE-OK", date="2026-06-01"),
            case(case_id="CASE-FUTURE", date="2026-12-01"),
            case(case_id="CASE-DEP", date="2026-06-01", deprecated=True),
            case(case_id="CASE-EXPIRED", date="2026-06-01",
                 effective_to="2026-07-01"),
            case(case_id="CASE-UNDATED", date=""),
        ]}
        (self.cases_dir / "history_cases.json").write_text(
            json.dumps(payload, ensure_ascii=False))
        hub = DataHub(data_dir=self.data_dir)
        result = hub.search_cases("A店", "客流下降", top_k=10,
                                  date="2026-09-01")
        ids = {match["case_id"] for match in result["matches"]}
        self.assertEqual(ids, {"CASE-OK"})
        self.assertEqual(result["filters"]["excluded"],
                         {"deprecated": 1, "future": 1, "expired": 1,
                          "undated": 1})

        with_deprecated = hub.search_cases(
            "A店", "客流下降", top_k=10, date="2026-09-01",
            include_deprecated=True)
        self.assertEqual(
            {m["case_id"] for m in with_deprecated["matches"]},
            {"CASE-OK", "CASE-DEP"})

        future_alert = hub.search_cases("A店", "客流下降", top_k=10,
                                        date="2026-05-01")
        self.assertEqual(future_alert["match_count"], 0)

    def test_synthetic_corpus_has_1000_plus_valid_cases(self):
        """案例库测试语料 >=1000 条,字段完整、ID 唯一、过滤边界可识别。"""
        corpus_path = (CateringConfigs.CATERING_DIR / "data" / "cases"
                       / "synthetic_cases.json")
        payload = json.loads(corpus_path.read_text())
        cases = payload["cases"]
        self.assertGreaterEqual(len(cases), 1000)
        ids = [case["case_id"] for case in cases]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertGreaterEqual(
            len({case["store_id"] for case in cases}), 50)
        self.assertEqual(
            {case["main_factor"] for case in cases},
            {"traffic", "ticket", "none"})
        required = ("case_id", "store_id", "date", "main_factor",
                    "dimensions", "summary", "actions", "tags", "symptoms",
                    "metrics_snapshot", "outcome", "effective_from",
                    "effective_to", "deprecated", "confidence")
        for case in cases:
            for key in required:
                self.assertIn(key, case, f"{case.get('case_id')} 缺少 {key}")
            self.assertIn(case["main_factor"], ("traffic", "ticket", "none"))
            self.assertRegex(case["date"], r"^\d{4}-\d{2}-\d{2}$")
        by_id = {case["case_id"]: case for case in cases}
        self.assertIn("CASE-SYN-FUTURE-1", by_id)
        self.assertIn("CASE-SYN-DEPRECATED-1", by_id)
        self.assertTrue(by_id["CASE-SYN-DEPRECATED-1"]["deprecated"])
        self.assertIn("CASE-SYN-EXPIRED-1", by_id)

        # 真实语料上的过滤:未来/废弃/过期案例在告警日视角不可召回。
        retriever = LexicalCaseRetriever(lambda: cases)
        result = retriever.search("台风预警 临时闭店", store_id="C店",
                                  date="2026-09-01")
        self.assertEqual(result["match_count"], 0)
        self.assertGreaterEqual(result["filters"]["excluded"]["future"], 1)
        self.assertGreaterEqual(result["filters"]["excluded"]["deprecated"], 1)
        self.assertGreaterEqual(result["filters"]["excluded"]["expired"], 2)

    def test_lexical_scoring_golden_and_store_id_not_topic(self):
        """锁定 _score_case 抽取前后的分数;门店 ID 不能当作文本主题。"""
        cases = [case(case_id="CASE-G", date="2026-09-01",
                      main_factor="traffic", tags=["暴雨"])]
        clock = lambda: datetime.fromisoformat("2026-09-01").timestamp()
        retriever = LexicalCaseRetriever(lambda: cases, clock=clock)
        result = retriever.search(
            "dimension:traffic 暴雨", store_id="A店",
            date="2026-09-01", main_factor="traffic")
        match = result["matches"][0]
        breakdown = match["score_breakdown"]
        self.assertEqual(breakdown["store"], 1.0)
        self.assertEqual(breakdown["main_factor"], 2.0)
        self.assertEqual(breakdown["dimensions"], 1.0)
        self.assertEqual(breakdown["tags"], 1.5)
        self.assertAlmostEqual(breakdown["text_overlap"], 1.2)
        self.assertAlmostEqual(breakdown["base"], 6.7)
        self.assertAlmostEqual(breakdown["recency"], 1.0)
        self.assertAlmostEqual(breakdown["confidence"], 0.5)
        self.assertAlmostEqual(match["score"], 6.825)

        # query 只出现门店 ID 时,不能靠同店 + 文本重叠误召回。
        store_only = retriever.search(
            "A店", store_id="A店", date="2026-09-01")
        self.assertEqual(store_only["match_count"], 0)

    def test_lexical_retriever_breakdown_and_same_store_boost(self):
        cases = [
            case(case_id="CASE-T", main_factor="traffic", tags=["暴雨"]),
            case(case_id="CASE-K", store_id="B店", main_factor="ticket",
                 tags=["低价套餐"]),
        ]
        retriever = LexicalCaseRetriever(lambda: cases)
        result = retriever.search("dimension:traffic 暴雨",
                                  store_id="A店", date="2026-09-01",
                                  main_factor="traffic")
        self.assertEqual(result["matches"][0]["case_id"], "CASE-T")
        breakdown = result["matches"][0]["score_breakdown"]
        self.assertEqual(breakdown["main_factor"], 2.0)
        self.assertEqual(breakdown["dimensions"], 1.0)
        self.assertGreater(result["matches"][0]["score"], 0)
        self.assertTrue(result["candidates"])
        self.assertEqual(result["filters"]["total_cases"], 2)

        # 跨店案例仍可召回:门店只加分,不做硬过滤。
        cross_store = retriever.search("低价套餐", store_id="A店",
                                       date="2026-09-01")
        self.assertIn("CASE-K",
                      [m["case_id"] for m in cross_store["matches"]])
        # 纯同店、无主题信号不召回。
        store_only = retriever.search("露营 宠物 团建", store_id="A店",
                                      date="2026-09-01")
        self.assertEqual(store_only["match_count"], 0)
        with self.assertRaises(ValueError):
            retriever.search("x", date="bad-date")


if __name__ == "__main__":
    unittest.main()
