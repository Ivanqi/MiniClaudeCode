"""后台任务测试:完成/失败/取消/超时、get 非消费、并发上限。"""
import tempfile
import time
import unittest
from pathlib import Path

from src.ApprovalGate import ApprovalGate
from src.AuditLog import AuditLog
from src.BackgroundTaskSystem import BackgroundTaskSystem
from src.Configs import Configs
from src.LlmClient import ToolCall
from src.Sandbox import Sandbox
from src.ToolDispatcher import ToolDispatcher
from src.ToolPolicy import lead_policy


class TestBackgroundTasks(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()).resolve()
        self._old = Configs.WORKDIR
        Configs.WORKDIR = self.root
        self.dispatcher = ToolDispatcher(
            hooks=None, audit=AuditLog(directory=self.root / "audit"),
            approval=ApprovalGate(interactive=False, auto_approve=set()),
            sandbox=Sandbox(mode="off"))
        self.handlers = {
            "quick": lambda: "done",
            "boom": self._boom,
            "slow": lambda: (time.sleep(0.5), "slow-done")[1],
        }

    def tearDown(self):
        Configs.WORKDIR = self._old

    @staticmethod
    def _boom():
        raise ValueError("boom")

    def _start(self, background, name, **arguments):
        call = ToolCall(id=f"call_{name}", name=name, arguments=arguments)
        policy = lead_policy()
        auth = self.dispatcher.authorize(call, policy, self.handlers)
        self.assertTrue(auth.allowed, auth.reason)
        return background.start(
            call, self.handlers, dispatcher=self.dispatcher,
            policy=policy, authorization=auth)

    def _wait(self, background, limit=3.0):
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            notes = background.collect_results()
            if notes:
                return notes
            time.sleep(0.01)
        return []

    def test_completed_notification(self):
        background = BackgroundTaskSystem()
        bg_id = self._start(background, "quick")
        notes = self._wait(background)
        self.assertEqual(len(notes), 1)
        self.assertIn("<status>completed</status>", notes[0])
        self.assertIn("done", notes[0])
        self.assertIn(bg_id, notes[0])

    def test_failed_handler_is_visible(self):
        background = BackgroundTaskSystem()
        self._start(background, "boom")
        notes = self._wait(background)
        self.assertEqual(len(notes), 1)
        self.assertIn("<status>failed</status>", notes[0])
        self.assertIn("ValueError", notes[0])
        self.assertIn("<error_code>exception</error_code>", notes[0])

    def test_cancel_is_visible(self):
        background = BackgroundTaskSystem()
        bg_id = self._start(background, "slow")
        self.assertIn("Cancelled", background.cancel(bg_id))
        notes = self._wait(background)
        self.assertIn("<status>cancelled</status>", notes[0])
        self.assertIn("<error_code>cancelled</error_code>", notes[0])

    def test_timeout_is_visible(self):
        background = BackgroundTaskSystem(timeout=0.1)
        self._start(background, "slow")
        notes = self._wait(background)
        self.assertIn("<status>timed_out</status>", notes[0])
        self.assertIn("<error_code>timeout</error_code>", notes[0])

    def test_get_does_not_consume(self):
        background = BackgroundTaskSystem()
        bg_id = self._start(background, "quick")
        notes = self._wait(background)
        self.assertEqual(len(notes), 1)
        self.assertIn("completed", background.get(bg_id))
        self.assertIn("completed", background.get(bg_id))
        self.assertEqual(background.collect_results(), [])

    def test_concurrency_cap_returns_none(self):
        background = BackgroundTaskSystem(max_concurrent=1, max_queued=0)
        first = self._start(background, "slow")
        self.assertIsNotNone(first)
        second = self._start(background, "slow")
        self.assertIsNone(second)
        self._wait(background, limit=3.0)

    def test_unknown_task(self):
        background = BackgroundTaskSystem()
        self.assertIn("not found", background.get("bg_9999"))
        self.assertIn("not found", background.cancel("bg_9999"))


if __name__ == "__main__":
    unittest.main()
