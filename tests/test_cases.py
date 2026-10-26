"""Catering 案例记忆测试:rglob、upsert/version、去重、时间衰减。"""
import json
import tempfile
import time
import unittest
from pathlib import Path

from Catering.Cases import CaseStore, load_cases, validate_case_id
from Catering.Configs import CateringConfigs
from Catering.DataHub import DataHub


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


if __name__ == "__main__":
    unittest.main()
