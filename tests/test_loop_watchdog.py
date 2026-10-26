"""LoopWatchdog:阶段超时、整体墙钟、无进展与线程唤醒。"""
import threading
import time
import unittest
from types import SimpleNamespace

from src.LoopGuard import LoopWatchdog


class FakeClock:
    def __init__(self, now=100.0):
        self.now = float(now)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)


class TestLoopWatchdog(unittest.TestCase):
    def test_llm_phase_timeout(self):
        clock = FakeClock()
        event = threading.Event()
        watchdog = LoopWatchdog(clock=clock, cancel_event=event,
                                no_progress_timeout=0)
        watchdog.set_phase("llm", 10)
        clock.advance(10)
        reason = watchdog.poll()
        self.assertIn("phase 'llm'", reason)
        self.assertTrue(watchdog.cancelled)
        self.assertTrue(event.is_set())

    def test_overall_wall_timeout(self):
        clock = FakeClock()
        budget = SimpleNamespace(started_at=100.0, max_wall_s=5)
        watchdog = LoopWatchdog(budget=budget, clock=clock,
                                no_progress_timeout=0)
        watchdog.set_phase("llm", 1000)
        clock.advance(5)
        self.assertIn("turn wall time", watchdog.poll())

    def test_tool_no_progress(self):
        clock = FakeClock()
        watchdog = LoopWatchdog(
            clock=clock, no_progress_timeout=5,
            progress_callback=lambda: 100.0)
        watchdog.set_phase("tool", 100)
        clock.advance(6)
        self.assertIn("no tool progress", watchdog.poll())
        self.assertTrue(watchdog.cancel_event.is_set())

    def test_approval_phase_has_no_timeout(self):
        clock = FakeClock()
        watchdog = LoopWatchdog(clock=clock, no_progress_timeout=5)
        watchdog.set_phase("approval")
        clock.advance(1000)
        self.assertEqual(watchdog.poll(), "")

    def test_first_reason_wins(self):
        clock = FakeClock()
        watchdog = LoopWatchdog(clock=clock, no_progress_timeout=0)
        watchdog.set_phase("llm", 1)
        clock.advance(1)
        first = watchdog.poll()
        clock.advance(100)
        self.assertEqual(watchdog.poll(), first)

    def test_stop_wakes_thread_without_triggering(self):
        watchdog = LoopWatchdog(poll_interval=0.01, no_progress_timeout=0)
        watchdog.start()
        time.sleep(0.03)
        watchdog.stop()
        self.assertFalse(watchdog.cancelled)
        self.assertFalse(watchdog._thread.is_alive())


if __name__ == "__main__":
    unittest.main()
