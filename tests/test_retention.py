"""Retention 测试:gzip/tar 归档、保护、训练默认关闭、失败保留源。"""
import gzip
import json
import os
import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from src.AuditLog import AuditLog
from src.Configs import Configs
from src.Retention import RetentionManager, RetentionPolicy


def old_file(path: Path, age_days: int = 40, content: str = "old"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    stamp = time.time() - age_days * 86400
    os.utime(path, (stamp, stamp))
    return path


class TestRetention(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.audit = AuditLog(directory=self.tmp / "audit")

    def manager(self, policies):
        return RetentionManager(policies=policies, audit=self.audit)

    def archives(self, directory: Path):
        return [path for path in (directory / "archive").rglob("*")
                if path.is_file()]

    def test_transcript_gzip_archive_keeps_new(self):
        directory = self.tmp / "transcripts"
        old = old_file(directory / "transcript_old.jsonl")
        new = directory / "transcript_new.jsonl"
        new.write_text("new")
        policy = RetentionPolicy(
            "transcripts", directory, ("transcript_*.jsonl",),
            keep_days=30, lock_name=".transcripts.lock")
        result = self.manager([policy]).run()
        self.assertEqual(result[0].archived, [str(old)])
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())
        archived = self.archives(directory)
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0].suffix, ".gz")
        with gzip.open(archived[0], "rt") as handle:
            self.assertEqual(handle.read(), "old")
        events = [json.loads(line)["event"] for line in
                  (self.tmp / "audit" / "tool_calls.jsonl").read_text().splitlines()]
        self.assertIn("retention_archive", events)
        # 二次运行 no-op
        self.assertEqual(self.manager([policy]).run()[0].archived, [])

    def test_tool_output_archive(self):
        directory = self.tmp / "tool-results"
        old = old_file(directory / "old.txt")
        (directory / "new.txt").write_text("new")
        policy = RetentionPolicy(
            "tool_outputs", directory, ("*.txt",), keep_days=30,
            lock_name=".tool-results.lock")
        self.manager([policy]).run()
        self.assertFalse(old.exists())
        self.assertTrue((directory / "new.txt").exists())
        self.assertTrue(any(path.name.endswith(".txt.gz")
                            for path in self.archives(directory)))

    def test_backup_directory_tar_archive(self):
        directory = self.tmp / "backups"
        old = directory / "call-1" / "src" / "a.txt"
        old_file(old)
        new = directory / "call-2" / "b.txt"
        new.parent.mkdir(parents=True)
        new.write_text("new")
        policy = RetentionPolicy(
            "backups", directory, keep_days=30, max_entries=10,
            mode="tar", kind="dirs", lock_name=".backups.lock")
        self.manager([policy]).run()
        self.assertFalse((directory / "call-1").exists())
        self.assertTrue((directory / "call-2").exists())
        archives = [path for path in self.archives(directory)
                    if path.name.endswith(".tar.gz")]
        self.assertEqual(len(archives), 1)
        with tarfile.open(archives[0], "r:gz") as archive:
            self.assertIn("call-1/src/a.txt", archive.getnames())

    def test_training_disabled_by_default(self):
        directory = self.tmp / "training"
        old = old_file(directory / "turn_old.jsonl")
        policy = RetentionPolicy(
            "training_data", directory, ("*.jsonl",), keep_days=30,
            lock_name=".training.lock", enabled=False)
        self.manager([policy]).run()
        self.assertTrue(old.exists())

    def test_training_can_be_enabled_explicitly(self):
        directory = self.tmp / "training"
        old = old_file(directory / "turn_old.jsonl")
        policy = RetentionPolicy(
            "training_data", directory, ("*.jsonl",), keep_days=30,
            lock_name=".training.lock", enabled=True)
        self.manager([policy]).run()
        self.assertFalse(old.exists())

    def test_max_entries_keeps_newest(self):
        directory = self.tmp / "tool-results"
        paths = []
        for index in range(3):
            path = directory / f"{index}.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(index))
            stamp = time.time() - (10 - index) * 86400
            os.utime(path, (stamp, stamp))
            paths.append(path)
        policy = RetentionPolicy(
            "tool_outputs", directory, ("*.txt",), keep_days=0,
            max_entries=1, lock_name=".tool-results.lock")
        self.manager([policy]).run()
        self.assertTrue(paths[-1].exists())
        self.assertFalse(paths[0].exists())
        self.assertFalse(paths[1].exists())

    def test_protected_paths_are_kept(self):
        directory = self.tmp / "transcripts"
        protected = old_file(directory / "transcript_protected.jsonl")
        removable = old_file(directory / "transcript_remove.jsonl")
        policy = RetentionPolicy(
            "transcripts", directory, ("transcript_*.jsonl",),
            keep_days=30, lock_name=".transcripts.lock")
        self.manager([policy]).run(protected_paths=[protected])
        self.assertTrue(protected.exists())
        self.assertFalse(removable.exists())

    def test_symlink_is_skipped(self):
        directory = self.tmp / "transcripts"
        directory.mkdir()
        outside = self.tmp / "outside.txt"
        outside.write_text("outside")
        link = directory / "transcript_link.jsonl"
        link.symlink_to(outside)
        policy = RetentionPolicy(
            "transcripts", directory, ("transcript_*.jsonl",),
            keep_days=0, max_entries=0, lock_name=".transcripts.lock")
        self.manager([policy]).run()
        self.assertTrue(link.is_symlink())
        self.assertTrue(outside.exists())

    def test_archive_failure_keeps_source(self):
        directory = self.tmp / "transcripts"
        source = old_file(directory / "transcript_old.jsonl")
        policy = RetentionPolicy(
            "transcripts", directory, ("transcript_*.jsonl",),
            keep_days=30, lock_name=".transcripts.lock")
        manager = self.manager([policy])
        with mock.patch.object(manager, "_archive_file",
                               side_effect=OSError("disk full")):
            result = manager.run()
        self.assertTrue(source.exists())
        self.assertEqual(result[0].failed, [str(source)])
        events = [json.loads(line)["event"] for line in
                  (self.tmp / "audit" / "tool_calls.jsonl").read_text().splitlines()]
        self.assertIn("retention_failure", events)

    def test_global_disable(self):
        directory = self.tmp / "transcripts"
        old = old_file(directory / "transcript_old.jsonl")
        policy = RetentionPolicy(
            "transcripts", directory, ("transcript_*.jsonl",),
            keep_days=30, lock_name=".transcripts.lock")
        with mock.patch.object(Configs, "RETENTION_ENABLED", False):
            self.assertEqual(self.manager([policy]).run(), [])
        self.assertTrue(old.exists())


if __name__ == "__main__":
    unittest.main()
