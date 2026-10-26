"""LoopGuard 纯逻辑:信号、动作阶梯、进度账本、失败记忆。"""
import json
import unittest
from unittest import mock

from src.Configs import Configs
from src.LoopGuard import LoopGuard, ToolObservation
from src.ToolPolicy import ToolResult


class FakeCall:
    def __init__(self, name="glob", arguments=None):
        self.name = name
        self.arguments = dict(arguments or {"pattern": "*.py"})


def observation(index=1, *, tool="glob", args=None, output="out",
                ok=True, error_code="", background=False):
    call = FakeCall(tool, args if args is not None else {"i": index})
    result = None if background else ToolResult(
        content=output, ok=ok, error_code=error_code)
    return ToolObservation.from_result(
        call, output, result=result, ok=ok, error_code=error_code,
        background=background, round_index=1)


class TestLoopGuardSignals(unittest.TestCase):
    def test_exact_repeat_api_nudge_then_stop(self):
        guard = LoopGuard()
        self.assertEqual(guard.observe(observation(1), "api"), [])
        signals = guard.observe(observation(1), "api")
        action, signal = guard.resolve(signals, "api")
        self.assertEqual(action, "nudge")
        self.assertEqual(signal.kind, "exact_repeat")

        signals = guard.observe(observation(1), "api")
        action, signal = guard.resolve(signals, "api")
        self.assertEqual(action, "stop")
        self.assertEqual(signal.kind, "exact_repeat")

    def test_local_still_only_escalates_at_three(self):
        guard = LoopGuard()
        for _ in range(2):
            self.assertEqual(guard.observe(observation(1), "local"), [])
        signals = guard.observe(observation(1), "local")
        action, signal = guard.resolve(signals, "local")
        self.assertEqual(action, "escalate")
        self.assertEqual(signal.kind, "exact_repeat")

    def test_local_no_progress_waits_for_threshold(self):
        guard = LoopGuard()
        guard.begin_round(1)
        guard.observe(observation(1), "local")
        self.assertEqual(guard.end_round("local"), [])

    def test_same_observation_with_different_args(self):
        guard = LoopGuard()
        self.assertEqual(
            guard.observe(observation(1, args={"i": 1}, output="same"), "api"),
            [])
        signals = guard.observe(
            observation(2, args={"i": 2}, output="same"), "api")
        action, signal = guard.resolve(signals, "api")
        self.assertEqual(action, "nudge")
        self.assertEqual(signal.kind, "same_observation")

    def test_error_streak_is_per_error_code(self):
        guard = LoopGuard()
        for index in range(1, 3):
            signals = guard.observe(
                observation(index, ok=False, output=f"err{index}",
                            error_code="timeout"), "api")
        action, signal = guard.resolve(signals, "api")
        self.assertEqual(action, "reflect")
        self.assertEqual(signal.evidence["error_code"], "timeout")

        # 换错误码后连续计数重置,不应直接 stop。
        signals = []
        for index in range(3, 5):
            signals = guard.observe(
                observation(index, ok=False, output=f"missing{index}",
                            error_code="not_found"), "api")
        action, _ = guard.resolve(signals, "api")
        self.assertNotEqual(action, "stop")

    def test_error_streak_stops_at_four(self):
        guard = LoopGuard()
        signals = []
        for index in range(1, 5):
            signals = guard.observe(
                observation(index, ok=False, output=f"timeout{index}",
                            error_code="timeout"), "api")
        action, signal = guard.resolve(signals, "api")
        self.assertEqual(action, "stop")
        self.assertEqual(signal.kind, "error_streak")

    def test_ping_pong_reflect_then_stop(self):
        guard = LoopGuard()
        signals = []
        for index in range(1, 7):
            signals = guard.observe(
                observation(index, args={"side": index % 2},
                            output=f"o{index}"), "api")
            if index == 4:
                self.assertEqual(guard.resolve(signals, "api")[0], "reflect")
        action, signal = guard.resolve(signals, "api")
        self.assertEqual(action, "stop")
        self.assertEqual(signal.kind, "ping_pong")

    def test_no_progress_api_and_plan(self):
        guard = LoopGuard()
        guard.begin_round(1)
        guard.observe(observation(1), "api")
        actions = []
        for round_index in range(2, 8):
            guard.begin_round(round_index)
            signals = guard.end_round("api")
            action, _ = guard.resolve(signals, "api")
            actions.append((round_index, action))
        self.assertIn((4, "reflect"), actions)
        self.assertIn((7, "stop"), actions)

        # 恢复进展后,no_progress 阈值应重新武装。
        resumed = LoopGuard()
        resumed.begin_round(1)
        resumed.observe(observation(1), "api")
        for round_index in range(2, 5):
            resumed.begin_round(round_index)
            resumed.end_round("api")
        resumed.begin_round(5)
        resumed.observe(observation(5, output="new progress"), "api")
        rearmed = []
        for round_index in range(6, 9):
            resumed.begin_round(round_index)
            rearmed = resumed.end_round("api")
        self.assertEqual(resumed.resolve(rearmed, "api")[0], "reflect")

        plan = LoopGuard()
        plan.begin_round(1)
        plan.observe(observation(1), "plan")
        for round_index in range(2, 8):
            plan.begin_round(round_index)
            signals = plan.end_round("plan")
            action, signal = plan.resolve(signals, "plan")
            if round_index == 7:
                self.assertEqual(action, "replan")
                self.assertEqual(signal.kind, "no_progress")

    def test_message_cap_converts_nudge_to_stop(self):
        guard = LoopGuard()
        signals = guard.observe(observation(1), "api")
        signals += guard.observe(observation(1), "api")
        action, _ = guard.resolve(
            signals, "api", Configs.LOOP_MAX_GUARD_MESSAGES)
        self.assertEqual(action, "stop")

    def test_background_start_is_not_progress_or_error(self):
        guard = LoopGuard()
        guard.begin_round(1)
        obs = observation(1, background=True)
        self.assertEqual(guard.observe(obs, "api"), [])
        self.assertEqual(guard.total_errors, 0)

    def test_background_polling_is_a_legitimate_wait(self):
        guard = LoopGuard()
        for round_index in range(1, 5):
            guard.begin_round(round_index)
            obs = observation(
                1, tool="get_background_task", args={"task_id": "bg_1"},
                output="running")
            self.assertEqual(guard.observe(obs, "api"), [])
            self.assertEqual(guard.end_round("api"), [])

        terminal = LoopGuard()
        signals = []
        for _ in range(3):
            signals = terminal.observe(observation(
                1, tool="get_background_task", args={"task_id": "bg_1"},
                output="bg_1: failed"), "api")
        self.assertEqual(terminal.resolve(signals, "api")[0], "stop")


class TestLoopGuardMemory(unittest.TestCase):
    def test_failed_attempt_redacts_body_and_dedupes(self):
        guard = LoopGuard()
        call = FakeCall("write_file", {
            "path": "x.txt", "content": "TOP-SECRET-VALUE"})
        result = ToolResult.failure("Error: no such file",
                                    error_code="not_found")
        obs = ToolObservation.from_result(
            call, result.content, result=result, round_index=1)
        guard.observe(obs, "api")
        guard.observe(obs, "api")
        entries = guard.failed_attempts()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["count"], 2)
        text = json.dumps(entries, ensure_ascii=False)
        self.assertNotIn("TOP-SECRET-VALUE", text)
        self.assertIn("sha256:", text)

    def test_persist_memory_only_on_high_severity_or_repeat(self):
        nudge = LoopGuard()
        nudge.observe(observation(1), "api")
        nudge.observe(observation(1), "api")
        self.assertFalse(nudge.should_persist_memory())

        repeat = LoopGuard()
        with mock.patch.object(Configs, "LOOP_EXACT_REPEAT_STOP", 99), \
                mock.patch.object(Configs, "LOOP_OBSERVATION_STOP", 99):
            for _ in range(3):
                repeat.observe(observation(
                    1, ok=False, output="same error",
                    error_code="timeout"), "api")
        self.assertTrue(repeat.should_persist_memory())

        stop = LoopGuard()
        stop.observe(observation(1), "api")
        stop.observe(observation(1), "api")
        stop.observe(observation(1), "api")
        self.assertTrue(stop.should_persist_memory())


if __name__ == "__main__":
    unittest.main()
