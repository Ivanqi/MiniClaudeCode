"""TranscriptStore 测试:唯一命名、脱敏、搜索、档案 recall、路径安全。"""
import gzip
import json
import tempfile
import unittest
from pathlib import Path

from src.AuditLog import AuditLog
from src.ContextCompact import ContextCompact
from src.MemoryStore import MemoryStore
from src.TranscriptStore import TranscriptStore


class TestTranscriptStore(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.dir = self.tmp / "transcripts"
        self.memory = MemoryStore(
            root=self.tmp / "memory", audit=AuditLog(directory=self.tmp / "audit"),
            touch_interval=0)
        self.store = TranscriptStore(directory=self.dir, memory=self.memory)

    def messages(self, text="hello world"):
        return [
            {"role": "user", "content": text},
            {"role": "assistant", "content": "answer"},
            {"role": "tool", "tool_call_id": "c1", "content": "tool output"},
        ]

    def test_unique_names_and_redaction(self):
        first = self.store.write(self.messages(
            "my key is sk-abcdefghijklmnopqrst"))
        second = self.store.write(self.messages("second"))
        self.assertNotEqual(first.name, second.name)
        text = first.read_text()
        self.assertNotIn("sk-abcdefghijklmnopqrst", text)
        self.assertIn("[REDACTED]", text)

    def test_writes_episodic_summary(self):
        path = self.store.write(self.messages("remember the project"))
        hits = self.memory.search("remember", namespaces=["episodic"])
        self.assertTrue(hits)
        self.assertEqual(hits[0].item.key,
                         f"transcript:{path.name[:-6]}")
        self.assertIn(str(path), hits[0].item.content)

    def test_search_and_recall_active(self):
        path = self.store.write(self.messages("unique needle"))
        transcript_id = path.name[:-6]
        hits = self.store.search("needle")
        self.assertEqual(hits[0]["transcript_id"], transcript_id)
        recalled = self.store.recall(transcript_id)
        self.assertIn("unique needle", recalled)
        self.assertIn("user:", recalled)

    def test_search_and_recall_archive_gzip(self):
        path = self.store.write(self.messages("archived needle"))
        transcript_id = path.name[:-6]
        archive = self.store.archive_dir / "2026-01-01"
        archive.mkdir(parents=True)
        target = archive / f"{transcript_id}.jsonl.gz"
        with gzip.open(target, "wt", encoding="utf-8") as handle:
            handle.write(path.read_text())
        path.unlink()
        hits = self.store.search("archived")
        self.assertEqual(hits[0]["transcript_id"], transcript_id)
        recalled = self.store.recall(transcript_id)
        self.assertIn("archived needle", recalled)

    def test_recall_validates_id_and_blocks_traversal(self):
        self.assertIn("invalid", self.store.recall("../secret"))
        self.assertIn("invalid", self.store.recall("transcript_bad"))
        self.assertIn("invalid", self.store.recall("transcript_20260101_000000_zzzzzzzz"))
        self.assertIn("not found", self.store.recall(
            "transcript_20260101_000000_abcdef12"))

    def test_symlink_is_not_followed(self):
        outside = self.tmp / "outside.jsonl"
        outside.write_text(json.dumps({"role": "user", "content": "secret"}))
        (self.dir).mkdir(parents=True, exist_ok=True)
        link = self.dir / "transcript_20260101_000000_deadbeef.jsonl"
        link.symlink_to(outside)
        hits = self.store.search("secret")
        self.assertEqual(hits, [])

    def test_context_compact_delegates(self):
        compact = ContextCompact(transcript_store=self.store)
        path = compact.write_transcript(self.messages("compact content"))
        self.assertTrue(path.exists())
        self.assertIn("compact content", path.read_text())

    def test_search_caps_and_empty_query(self):
        self.assertEqual(self.store.search(""), [])
        self.store.write(self.messages("needle"))
        self.assertEqual(len(self.store.search("needle", top_k=1)), 1)


if __name__ == "__main__":
    unittest.main()
