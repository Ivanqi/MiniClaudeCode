"""消息总线测试:并发发送/消费、坏行跳过、draining 恢复。"""
import json
import tempfile
import threading
import unittest
from pathlib import Path

from src.MessageBus import MessageBus


class TestMessageBus(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.bus = MessageBus(mailbox_dir=self.dir)

    def test_concurrent_send_loses_nothing(self):
        threads = [threading.Thread(
            target=self.bus.send, args=("a", "b", f"m{i}")) for i in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        messages = self.bus.read_inbox("b")
        self.assertEqual(len(messages), 20)
        self.assertEqual({m["content"] for m in messages},
                         {f"m{i}" for i in range(20)})

    def test_two_readers_consume_each_message_once(self):
        self.bus.send("a", "b", "only")
        barrier = threading.Barrier(2)
        seen = []

        def reader():
            barrier.wait()
            seen.extend(self.bus.read_inbox("b"))

        threads = [threading.Thread(target=reader) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["content"], "only")

    def test_malformed_lines_are_skipped(self):
        inbox = self.dir / "b.jsonl"
        valid = {"from": "a", "to": "b", "content": "ok", "type": "message",
                 "ts": 1, "metadata": {}}
        inbox.write_text("not json\n" + json.dumps(valid) + "\n")
        messages = self.bus.read_inbox("b")
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["content"], "ok")

    def test_orphan_draining_file_is_recovered(self):
        valid = {"from": "a", "to": "b", "content": "orphan", "type": "message",
                 "ts": 1, "metadata": {}}
        drain = self.dir / "b.jsonl.draining-deadbeef"
        drain.write_text(json.dumps(valid) + "\n")
        messages = self.bus.read_inbox("b")
        self.assertEqual([m["content"] for m in messages], ["orphan"])
        self.assertFalse(drain.exists())

    def test_invalid_agent_names(self):
        with self.assertRaises(ValueError):
            self.bus.send("../evil", "b", "x")
        self.assertEqual(self.bus.read_inbox("../evil"), [])

    def test_send_returns_message_id(self):
        message_id = self.bus.send("a", "b", "hello")
        self.assertTrue(message_id)
        messages = self.bus.read_inbox("b")
        self.assertEqual(messages[0]["message_id"], message_id)

    def test_explicit_duplicate_send_is_skipped(self):
        self.bus.send("a", "b", "once", message_id="fixed-id")
        self.bus.send("a", "b", "twice", message_id="fixed-id")
        messages = self.bus.read_inbox("b")
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["content"], "once")

    def test_delivered_id_is_not_redelivered(self):
        self.bus.send("a", "b", "once", message_id="delivered-id")
        self.assertEqual(len(self.bus.read_inbox("b")), 1)
        # 模拟崩溃/重放:同 ID 再次落到 inbox
        record = {"message_id": "delivered-id", "from": "a", "to": "b",
                  "content": "replay", "type": "message", "ts": 2,
                  "metadata": {}}
        (self.dir / "b.jsonl").write_text(json.dumps(record) + "\n")
        self.assertEqual(self.bus.read_inbox("b"), [])

    def test_legacy_message_gets_synthetic_id_and_dedupes(self):
        record = {"from": "a", "to": "b", "content": "legacy",
                  "type": "message", "ts": 1, "metadata": {}}
        (self.dir / "b.jsonl").write_text(json.dumps(record) + "\n")
        first = self.bus.read_inbox("b")
        self.assertEqual(len(first), 1)
        self.assertTrue(first[0]["message_id"])
        (self.dir / "b.jsonl").write_text(json.dumps(record) + "\n")
        self.assertEqual(self.bus.read_inbox("b"), [])

    def test_ledger_is_bounded(self):
        from src.Configs import Configs
        from unittest import mock
        with mock.patch.object(Configs, "MESSAGE_DEDUPE_LEDGER_MAX", 2):
            for index in range(5):
                self.bus.send("a", "b", f"m{index}",
                              message_id=f"id-{index}")
        ledger = (self.dir / "b.sent.jsonl").read_text().splitlines()
        self.assertEqual(len(ledger), 2)


if __name__ == "__main__":
    unittest.main()
