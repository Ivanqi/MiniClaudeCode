"""审计日志测试:惰性创建、脱敏、正文哈希、轮转。"""
import json
import tempfile
import unittest
from pathlib import Path

from src.AuditLog import AuditLog, summarize_arguments


class TestAuditLog(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp()) / "audit"

    def test_lazy_creation_and_redaction(self):
        audit = AuditLog(directory=self.dir)
        self.assertFalse(self.dir.exists())
        secret = "sk-abcdefghijklmnopqrst"
        audit.tool_decision(
            agent="lead", tool="write_file",
            arguments={"path": "x.txt", "content": secret},
            decision="confirm", reason="test")
        path = self.dir / "tool_calls.jsonl"
        self.assertTrue(path.exists())
        text = path.read_text()
        self.assertNotIn(secret, text)
        record = json.loads(text.splitlines()[-1])
        self.assertEqual(record["event"], "tool_decision")
        content = record["arguments"]["content"]
        self.assertIn("sha256", content)
        self.assertEqual(content["chars"], len(secret))

    def test_result_excerpt_redacted(self):
        audit = AuditLog(directory=self.dir)
        audit.log("tool_result", output_excerpt="Bearer abc.def.ghi")
        text = (self.dir / "tool_calls.jsonl").read_text()
        self.assertNotIn("abc.def.ghi", text)
        self.assertIn("[REDACTED]", text)

    def test_rotation(self):
        audit = AuditLog(directory=self.dir, max_bytes=400, backup_count=2)
        for index in range(20):
            audit.log("tick", index=index, payload="x" * 40)
        base = self.dir / "tool_calls.jsonl"
        self.assertTrue(base.exists())
        self.assertTrue((self.dir / "tool_calls.jsonl.1").exists())
        self.assertFalse((self.dir / "tool_calls.jsonl.4").exists())

    def test_summarize_arguments_truncates_and_hashes(self):
        summary = summarize_arguments({
            "path": "a.txt", "content": "x" * 5000, "old_text": "old",
        })
        self.assertEqual(summary["path"], "a.txt")
        self.assertEqual(summary["content"]["chars"], 5000)
        self.assertEqual(summary["old_text"]["chars"], 3)


if __name__ == "__main__":
    unittest.main()
