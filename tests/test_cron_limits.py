"""cron 限额与持久化测试。"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.Configs import Configs
from src.CronScheduler import CronScheduler


class TestCronLimits(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.path = self.dir / "cron.json"

    def test_max_jobs(self):
        scheduler = CronScheduler(durable_path=self.path)
        with mock.patch.object(Configs, "CRON_MAX_JOBS", 2):
            self.assertIsNotNone_type(scheduler.schedule_job("0 9 * * *", "a"))
            self.assertIsNotNone_type(scheduler.schedule_job("0 10 * * *", "b"))
            result = scheduler.schedule_job("0 11 * * *", "c")
            self.assertIsInstance(result, str)
            self.assertIn("too many", result)

    def test_prompt_length(self):
        scheduler = CronScheduler(durable_path=self.path)
        with mock.patch.object(Configs, "CRON_MAX_PROMPT_CHARS", 5):
            self.assertIn("prompt too long",
                          scheduler.schedule_job("0 9 * * *", "toolong"))

    def test_frequency_limit(self):
        scheduler = CronScheduler(durable_path=self.path)
        self.assertIn("too frequent",
                      scheduler.schedule_job("*/2 * * * *", "x"))
        self.assertIn("too frequent",
                      scheduler.schedule_job("* * * * *", "x"))
        job = scheduler.schedule_job("*/5 * * * *", "x")
        self.assertNotIsInstance(job, str)

    def test_malformed_durable_file_tolerated(self):
        self.path.write_text("not json")
        scheduler = CronScheduler(durable_path=self.path)
        self.assertEqual(scheduler.scheduled_jobs, {})
        self.assertNotIsInstance(
            scheduler.schedule_job("0 9 * * *", "x"), str)

    def test_bad_records_are_skipped_individually(self):
        good = {"id": "cron_1", "cron": "0 9 * * *", "prompt": "x",
                "recurring": True, "durable": True}
        self.path.write_text(json.dumps([good, "bad", {"id": "x"}]))
        scheduler = CronScheduler(durable_path=self.path)
        self.assertIn("cron_1", scheduler.scheduled_jobs)

    def test_atomic_save_is_valid_json(self):
        scheduler = CronScheduler(durable_path=self.path)
        scheduler.schedule_job("0 9 * * *", "x")
        data = json.loads(self.path.read_text())
        self.assertEqual(len(data), 1)
        self.assertFalse(list(self.dir.glob("*.tmp")))

    def assertIsNotNone_type(self, value):
        self.assertNotIsInstance(value, str)


if __name__ == "__main__":
    unittest.main()
