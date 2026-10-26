"""ProtocolSystem 测试:UUID、重放、发送者/目标/TTL、decide。"""
import tempfile
import unittest
from pathlib import Path

from src.MessageBus import MessageBus
from src.ProtocolSystem import ProtocolError, ProtocolState, ProtocolSystem


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class TestProtocol(unittest.TestCase):
    def setUp(self):
        self.bus = MessageBus(mailbox_dir=Path(tempfile.mkdtemp()))
        self.clock = FakeClock()
        self.protocol = ProtocolSystem(bus=self.bus, ttl=10,
                                       max_pending=5, clock=self.clock)

    def state(self, request_id="req_test", state_type="plan_approval",
              sender="worker", target="lead"):
        return ProtocolState(
            request_id=request_id, type=state_type, sender=sender,
            target=target, status="pending", payload="p",
            created_at=self.clock())

    def test_unique_ids_and_duplicate_track(self):
        self.assertNotEqual(self.protocol.new_request_id(),
                            self.protocol.new_request_id())
        state = self.state()
        self.protocol.track(state)
        with self.assertRaises(ProtocolError):
            self.protocol.track(self.state())

    def test_type_sender_target_and_replay(self):
        state = self.state()
        self.protocol.track(state)
        self.assertIsNone(self.protocol.match_response(
            "shutdown_response", state.request_id, True,
            sender="worker", target="lead"))
        self.assertIsNone(self.protocol.match_response(
            "plan_approval_response", state.request_id, True,
            sender="other", target="worker"))
        self.assertIsNone(self.protocol.match_response(
            "plan_approval_response", state.request_id, True,
            sender="lead", target="someone"))
        matched = self.protocol.match_response(
            "plan_approval_response", state.request_id, True,
            sender="lead", target="worker")
        self.assertEqual(matched.status, "approved")
        # 重放不再生效
        self.assertIsNone(self.protocol.match_response(
            "plan_approval_response", state.request_id, False,
            sender="lead", target="worker"))

    def test_expire_and_pending_count(self):
        state = self.state()
        self.protocol.track(state)
        self.assertEqual(self.protocol.pending_count(), 1)
        self.clock.now += 11
        self.assertEqual(self.protocol.expire(), [state.request_id])
        self.assertEqual(self.protocol.pending_count(), 0)
        self.assertIsNone(self.protocol.match_response(
            "plan_approval_response", state.request_id, True,
            sender="lead", target="worker"))

    def test_decide_sends_and_is_idempotent(self):
        state = self.state()
        self.protocol.track(state)
        ok, message = self.protocol.decide(state.request_id, True, "ok")
        self.assertTrue(ok)
        self.assertIn("approved", message)
        inbox = self.bus.read_inbox("worker")
        self.assertEqual(inbox[0]["type"], "plan_approval_response")
        self.assertTrue(inbox[0]["metadata"]["approve"])
        ok, message = self.protocol.decide(state.request_id, False)
        self.assertFalse(ok)
        self.assertIn("already", message)

    def test_consume_lead_inbox_routes_with_sender(self):
        state = self.state(state_type="shutdown", sender="lead",
                           target="worker")
        self.protocol.track(state)
        self.bus.send("worker", "lead", "ok", "shutdown_response",
                      {"request_id": state.request_id, "approve": True})
        messages = self.protocol.consume_lead_inbox()
        self.assertEqual(len(messages), 1)
        self.assertEqual(self.protocol.get(state.request_id).status,
                         "approved")

    def test_wrong_sender_response_does_not_approve(self):
        state = self.state(state_type="shutdown", sender="lead",
                           target="worker")
        self.protocol.track(state)
        self.bus.send("intruder", "lead", "ok", "shutdown_response",
                      {"request_id": state.request_id, "approve": True})
        self.protocol.consume_lead_inbox()
        self.assertEqual(self.protocol.get(state.request_id).status,
                         "pending")

    def test_cancel(self):
        state = self.state()
        self.protocol.track(state)
        self.protocol.cancel(state.request_id, "timeout")
        self.assertEqual(self.protocol.get(state.request_id).status,
                         "cancelled")


if __name__ == "__main__":
    unittest.main()
