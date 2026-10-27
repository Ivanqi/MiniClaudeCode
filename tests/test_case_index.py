"""CaseIndex 测试:tokenizer、硬过滤、FTS5/纯 Python 双引擎、增量同步。"""
import unittest

from Catering.CaseIndex import (
    CaseIndex,
    _fts5_available,
    exclusion_reason,
    search_tokens,
)


def case(case_id="CASE-1", store_id="A店", date="2026-06-01",
         main_factor="traffic", **overrides):
    payload = {
        "case_id": case_id, "store_id": store_id, "date": date,
        "main_factor": main_factor, "dimensions": ["traffic"],
        "summary": "暴雨导致客流下降", "actions": ["检查排班"],
        "tags": ["暴雨"], "symptoms": ["客流下降"],
        "effective_from": date, "effective_to": "",
        "deprecated": False, "confidence": 0.8,
    }
    payload.update(overrides)
    return payload


class TestTokenizer(unittest.TestCase):
    def test_ascii_cjk_and_noise(self):
        self.assertEqual(
            search_tokens("dimension:traffic POS宕机 客流下降 12%"),
            ["traffic", "pos", "宕机", "客流", "流下", "下降"])
        self.assertEqual(search_tokens("S05店"), ["s05"])
        self.assertEqual(search_tokens("A店"), [])
        self.assertEqual(search_tokens("露营 宠物 团建"),
                         ["露营", "宠物", "团建"])
        self.assertEqual(search_tokens("雨"), [])

    def test_exclusion_reason_order(self):
        base = case()
        self.assertIsNone(exclusion_reason(base, "2026-09-01"))
        self.assertEqual(
            exclusion_reason(case(deprecated=True), "2026-09-01"),
            "deprecated")
        self.assertEqual(
            exclusion_reason(case(date=""), "2026-09-01"), "undated")
        self.assertEqual(
            exclusion_reason(case(date="2026-12-01"), "2026-09-01"),
            "future")
        self.assertEqual(
            exclusion_reason(case(effective_to="2026-07-01"),
                             "2026-09-01"),
            "expired")


class TestCaseIndex(unittest.TestCase):
    def _engines(self):
        engines = ["python"]
        if _fts5_available():
            engines.append("fts5")
        return engines

    def test_hard_filters_match_lexical_semantics(self):
        cases = [
            case("CASE-OK"),
            case("CASE-FUTURE", date="2026-12-01"),
            case("CASE-DEP", deprecated=True),
            case("CASE-EXPIRED", effective_to="2026-07-01"),
            case("CASE-UNDATED", date="", effective_from=""),
        ]
        for engine in self._engines():
            with self.subTest(engine=engine):
                index = CaseIndex(engine=engine)
                try:
                    index.sync(cases)
                    hits = index.search(
                        ["客流", "下降"], date="2026-09-01", limit=10)
                    self.assertEqual([hit.case_id for hit in hits],
                                     ["CASE-OK"])
                    with_deprecated = index.search(
                        ["客流", "下降"], date="2026-09-01",
                        include_deprecated=True, limit=10)
                    self.assertEqual(
                        {hit.case_id for hit in with_deprecated},
                        {"CASE-OK", "CASE-DEP"})
                    earlier = index.search(
                        ["客流", "下降"], date="2026-05-01", limit=10)
                    self.assertEqual(earlier, [])
                finally:
                    index.close()

    def test_sync_add_update_remove_and_version(self):
        for engine in self._engines():
            with self.subTest(engine=engine):
                index = CaseIndex(engine=engine)
                try:
                    cases = [case("CASE-1")]
                    stats = index.sync(cases)
                    self.assertEqual(stats.doc_count, 1)
                    self.assertEqual(index.version, 1)
                    self.assertEqual(
                        [hit.case_id for hit in index.search(
                            ["暴雨"], date="2026-09-01")],
                        ["CASE-1"])

                    noop = index.sync(cases)
                    self.assertEqual(
                        noop.added + noop.changed + noop.removed, 0)
                    self.assertEqual(index.version, 1)

                    cases[0]["tags"] = ["露营"]
                    cases[0]["summary"] = "露营活动引流"
                    index.sync(cases)
                    self.assertEqual(index.version, 2)
                    self.assertEqual(
                        index.search(["暴雨"], date="2026-09-01"), [])
                    self.assertEqual(
                        [hit.case_id for hit in index.search(
                            ["露营"], date="2026-09-01")],
                        ["CASE-1"])

                    cases.append(case("CASE-2", store_id="B店",
                                      tags=["宠物"]))
                    index.sync(cases)
                    self.assertEqual(
                        {hit.case_id for hit in index.search(
                            ["宠物"], date="2026-09-01")},
                        {"CASE-2"})

                    index.sync([case("CASE-2", store_id="B店",
                                     tags=["宠物"])])
                    self.assertEqual(
                        [hit.case_id for hit in index.search(
                            ["露营"], date="2026-09-01")],
                        [])
                    self.assertEqual(index.doc_count, 1)
                finally:
                    index.close()

    def test_empty_and_unsafe_terms(self):
        for engine in self._engines():
            with self.subTest(engine=engine):
                index = CaseIndex(engine=engine)
                try:
                    index.sync([case()])
                    self.assertEqual(
                        index.search([], date="2026-09-01"), [])
                    self.assertEqual(index.search(["客流"], limit=0), [])
                    # tokenizer 不会产出这些字符;索引层仍需兜底,
                    # 不能抛 FTS 语法错误。
                    self.assertEqual(
                        index.search(['"', "*", "NEAR", 'foo"bar'],
                                     date="2026-09-01"), [])
                finally:
                    index.close()

    def test_auto_engine_and_once_sync_cost(self):
        index = CaseIndex()
        try:
            self.assertIn(index.engine, ("fts5", "python"))
            index.sync([case()])
            stats = index.sync([case()])
            self.assertEqual(stats.build_ms, 0.0)
        finally:
            index.close()


if __name__ == "__main__":
    unittest.main()
