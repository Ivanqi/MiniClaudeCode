"""一次 run_cycle 的高层编排。

CycleRunner 持有本轮可变状态,把阶段委托给:
- ToolBatchRunner:一次 response 的 tool_calls 批处理;
- closing.close_turn:预算/loop 收尾;
- telemetry/context:状态同步、信号记录、上下文拼接。
"""
import uuid

from src.Configs import Configs
from src.LlmClient import assistant_message, user_message
from src.LoopGuard import LoopGuard, LoopWatchdog
from src.RecoverySystem import RecoveryState, is_prompt_too_long_error
from src.Terminal import Terminal
from src.ToolPolicy import (PLAN_FORBIDDEN_STEP_TOOLS, TurnBudget,
                            skipped_tool_messages)

from src.agent_loop.closing import close_turn
from src.agent_loop.context import (attach_loop_context, build_tool_results)
from src.agent_loop.telemetry import (persist_loop_memory,
                                      record_guard_signals, sync_state_meta,
                                      turn_result)
from src.agent_loop.tools import BatchResult, ToolBatchRunner

TODO_REMINDER_ROUNDS = 3
_RETRY = object()
_BREAK = object()


class CycleRunner:
    """run_cycle 的编排层;每个 run_cycle 实例只服务一个 turn。"""

    def __init__(self, loop, messages: list, context: dict, *,
                 policy=None, budget=None, tools_allowlist=None,
                 caller: str = "main", flags: dict | None = None,
                 allow_cron: bool = True, allow_background: bool = True,
                 trigger_stop: bool = True, trajectory: bool = True,
                 turn_id: str | None = None,
                 refresh_context: bool = True,
                 todo_reminders: bool = True):
        self.loop = loop
        self.messages = messages
        self.context = context
        self.policy = policy
        self.budget = budget or TurnBudget()
        self.tools_allowlist = tools_allowlist
        self.caller = caller
        self.flags = flags
        self.allow_cron = allow_cron
        self.allow_background = allow_background
        self.trigger_stop = trigger_stop
        self.trajectory = trajectory
        self.turn_id = turn_id or uuid.uuid4().hex[:8]
        self.refresh_context = refresh_context
        self.todo_reminders = todo_reminders

        self.state = RecoveryState()
        self.state.turn_id = self.turn_id
        self.state.caller = caller
        self.guard = LoopGuard(caller=caller)
        self.watchdog = LoopWatchdog(
            budget=self.budget, progress_callback=self.guard.progress_epoch,
            metrics=loop.metrics)
        self.max_tokens = Configs.DEFAULT_MAX_TOKENS
        self.closing_reason = ""
        self.loop_stop_reason = ""
        self.artifacts = []
        self.saved_todo = loop.rounds_since_todo
        self.guard_messages_sent = 0
        self.pending_signals = []
        self.mode = "plan" if caller == "plan_step" else "api"
        self.tools = None
        self.handlers = None
        self.slim_tools = None
        self.slim_handlers = None

    # ── 主流程 ──

    def run(self) -> dict:
        self.watchdog.start()
        self.loop.metrics.record("turn")
        try:
            while True:
                if not self._prepare_round():
                    break
                self.budget.record_round()
                self.guard.begin_round(self.budget.rounds)
                outcome = self._call_model()
                if outcome is _RETRY:
                    continue
                if outcome is _BREAK:
                    break
                if isinstance(outcome, dict):
                    return outcome
                response, decision = outcome
                outcome = self._handle_response(response, decision)
                if outcome is _RETRY:
                    continue
                if isinstance(outcome, dict):
                    return outcome
                batch = ToolBatchRunner(self, response, decision).run()
                result = self._finish_batch(batch)
                if result is not None:
                    return result
            return self._finish_cycle()
        finally:
            self._cleanup()

    def _prepare_round(self) -> bool:
        reason = self.budget.exhausted_reason()
        if reason:
            self.closing_reason = reason
            return False
        if self.watchdog.cancelled:
            self.mark_watchdog_stop()
            return False

        if self.allow_cron:
            fired = self.loop.cron.consume_queue()
            for job in fired:
                self.messages.append(user_message(f"[Scheduled] {job.prompt}"))
                Terminal.info("cron inject", job.prompt[:60],
                              Terminal.MAGENTA)

        if self.allow_background:
            self.loop.inject_background_notifications(self.messages)

        if self.todo_reminders and \
                self.loop.rounds_since_todo >= TODO_REMINDER_ROUNDS:
            self.messages.append(user_message(
                "<reminder>Update your todos.</reminder>"))
            self.loop.rounds_since_todo = 0

        self.loop.prepare_context(self.messages)
        if self.refresh_context:
            self.context = self.loop.memories.update_context(
                self.context, self.messages)
        self.context = attach_loop_context(
            self.context, self.budget, self.guard)

        if self.tools_allowlist is not None:
            allowed = {name for name in
                       set(self.tools_allowlist) - PLAN_FORBIDDEN_STEP_TOOLS
                       if not name.startswith("mcp__")}
            self.tools, self.handlers = self.loop.tool_registry.assemble(
                allowlist=allowed)
            self.slim_tools, self.slim_handlers = \
                self.loop.tool_registry.assemble(
                    slim=True, allowlist=allowed)
        else:
            self.tools, self.handlers = self.loop.tool_registry.assemble()
            self.slim_tools, self.slim_handlers = \
                self.loop.tool_registry.assemble(slim=True)
        return True

    def _call_model(self):
        self.watchdog.set_phase("llm")
        try:
            response, decision = self.loop.call_llm(
                self.messages, self.context, self.tools, self.slim_tools,
                self.state, self.max_tokens, caller=self.caller,
                flags=self.flags)
        except Exception as e:
            if is_prompt_too_long_error(e) and \
                    not self.state.has_attempted_reactive_compact:
                self.messages[:] = self.loop.compact.reactive_compact(
                    self.messages, self.loop.client)
                self.state.has_attempted_reactive_compact = True
                return _RETRY
            self.messages.append({"role": "assistant",
                                  "content": f"[Error] {type(e).__name__}: {e}"})
            return self._final_result("error", error=str(e))
        self.budget.record_usage(getattr(response, "usage", None))
        self.loop.metrics.add_usage(
            getattr(response, "usage", None),
            "lead" if self.caller == "main" else self.caller)

        if self.watchdog.cancelled and response.has_tool_calls:
            self.messages.append(assistant_message(response))
            self.messages.extend(build_tool_results(
                self.loop.background,
                skipped_tool_messages(
                    response.tool_calls,
                    f"watchdog: {self.watchdog.reason}"),
                collect_background=self.allow_background))
            self.mark_watchdog_stop()
            return _BREAK
        return response, decision

    def _handle_response(self, response, decision):
        if response.has_parse_error:
            response.tool_calls = [c for c in response.tool_calls
                                   if not c.parse_error]
            self.state.parse_failures += 1
            Terminal.info(
                "parse-error",
                f"dropped malformed call ({self.state.parse_failures})",
                Terminal.RED)
            if decision.local:
                if self.state.parse_failures >= \
                        Configs.MAX_PARSE_ERRORS_BEFORE_ESCALATE:
                    self.state.turn_escalated = True
                    self.state.escalation_reason = "parse-errors"
                    Terminal.info("escalate", "parse errors → API",
                                  Terminal.RED)
                return _RETRY

        if (not response.tool_calls
                and not (response.content or "").strip()):
            if decision.local:
                self.state.turn_escalated = True
                self.state.escalation_reason = "empty-reply"
                Terminal.info("escalate", "empty reply → API", Terminal.RED)
                return _RETRY
            # API 空回复:按收尾处理(原有语义)

        if response.finish_reason == "length":
            if decision.local:
                self.state.turn_escalated = True
                self.state.escalation_reason = "length"
                Terminal.info("escalate", "length → API", Terminal.RED)
                return _RETRY
            if not self.state.has_escalated:
                self.max_tokens = Configs.ESCALATED_MAX_TOKENS
                self.state.has_escalated = True
                Terminal.info("max_tokens",
                              f"retry with {self.max_tokens}",
                              Terminal.YELLOW)
                return _RETRY
            self.messages.append(assistant_message(response))
            if self.state.recovery_count < Configs.MAX_RECOVERY_RETRIES:
                self.messages.append(user_message(
                    Configs.CONTINUATION_PROMPT))
                self.state.recovery_count += 1
                return _RETRY
            return self._final_result("length")

        self.max_tokens = Configs.DEFAULT_MAX_TOKENS
        self.state.has_escalated = False
        self.messages.append(assistant_message(response))
        if not response.has_tool_calls:
            if self.trigger_stop:
                self.loop.hooks.trigger("Stop", self.messages)
            return self._final_result("completed")
        return None

    def _finish_batch(self, batch: BatchResult):
        if batch.compacted_now:
            self.pending_signals.clear()
            return None

        self.messages.extend(self.loop.build_tool_results(
            batch.results, collect_background=self.allow_background))
        if self.closing_reason:
            return self._close("budget_exhausted", self.closing_reason,
                               "budget")
        if self.loop_stop_reason:
            return self._close("loop_stopped", self.loop_stop_reason, "loop")

        end_signals = self.guard.end_round(self.mode)
        record_guard_signals(
            self.loop.metrics, self.loop.audit, self.state, end_signals,
            self.turn_id, self.caller)
        action, signal = self.guard.resolve(
            self.pending_signals + end_signals, self.mode,
            self.guard_messages_sent)
        self.pending_signals.clear()
        if action == "escalate" and self.mode == "local":
            self.apply_escalation(signal)
        elif action in ("stop", "replan"):
            self.loop_stop_reason = (
                f"loop guard: {self.describe_stop(signal)}")
            return self._close(
                "loop_stopped", self.loop_stop_reason, "loop")
        elif action in ("nudge", "reflect"):
            self.messages.append(user_message(
                self.guard.format_message(action, signal)))
            self.guard_messages_sent += 1
        return None

    def _finish_cycle(self) -> dict:
        if self.loop_stop_reason:
            return self._close("loop_stopped", self.loop_stop_reason, "loop")
        return self._close("budget_exhausted", self.closing_reason, "budget")

    def _close(self, status: str, reason: str, stop_kind: str) -> dict:
        text = close_turn(
            self.loop, self.messages, self.context, self.state, reason,
            self.turn_id, budget=self.budget,
            allow_background=self.allow_background,
            trigger_stop=self.trigger_stop, trajectory=self.trajectory,
            caller=self.caller, guard=self.guard, watchdog=self.watchdog,
            stop_kind=stop_kind)
        closing_reason = (f"loop stopped: {reason}"
                          if stop_kind == "loop" else reason)
        return turn_result(
            self.loop.metrics, status, self.messages, self.budget,
            closing_reason=closing_reason, artifacts=self.artifacts,
            content=text, state=self.state)

    def _final_result(self, status: str, error: str = "",
                      closing_reason: str = "", content=None) -> dict:
        sync_state_meta(
            self.state, self.budget, self.guard, self.turn_id, self.caller)
        if self.trajectory:
            self.loop.trajectory.log_turn(
                self.messages, self.state, error=error)
        return turn_result(
            self.loop.metrics, status, self.messages, self.budget,
            error=error, closing_reason=closing_reason,
            artifacts=self.artifacts, content=content, state=self.state)

    def _cleanup(self):
        self.watchdog.stop()
        if self.caller == "main" and self.guard.should_persist_memory():
            persist_loop_memory(
                self.loop.memory, self.guard, self.turn_id, self.caller)
        if not self.todo_reminders:
            self.loop.rounds_since_todo = self.saved_todo

    # ── guard/watchdog 动作 ──

    def handle_tool_signals(self, signals):
        record_guard_signals(
            self.loop.metrics, self.loop.audit, self.state, signals,
            self.turn_id, self.caller)
        self.state.tool_error_count = self.guard.total_errors
        self.state.same_tool_streak = self.guard.exact_streak
        self.state.last_tool_key = self.guard.last_key
        action, signal = self.guard.resolve(
            signals, self.mode, self.guard_messages_sent)
        if action == "escalate" and self.mode == "local":
            self.apply_escalation(signal)
        elif action in ("nudge", "reflect"):
            self.pending_signals.append(signal)
        elif action in ("stop", "replan"):
            self.loop_stop_reason = (
                f"loop guard: {self.describe_stop(signal)}")

    def apply_escalation(self, signal):
        self.state.turn_escalated = True
        kind = getattr(signal, "kind", "") if signal else ""
        self.state.escalation_reason = (
            "tool-errors" if kind == "error_streak"
            else "no-progress" if kind == "no_progress"
            else "tool-loop")
        Terminal.info("escalate", f"{self.state.escalation_reason} → API",
                      Terminal.RED)

    def describe_stop(self, signal) -> str:
        if signal is None:
            return "loop guard"
        evidence = signal.evidence or {}
        detail = (evidence.get("tool") or evidence.get("pattern")
                  or evidence.get("error_code") or "")
        return (f"{signal.kind} ({detail})" if detail
                else signal.kind)

    def mark_watchdog_stop(self):
        if self.loop_stop_reason:
            return
        reason = self.watchdog.reason or "stalled"
        self.loop_stop_reason = f"watchdog: {reason}"
        signal = self.guard.record_external(
            "watchdog", "stop", {"reason": reason})
        record_guard_signals(
            self.loop.metrics, self.loop.audit, self.state, [signal],
            self.turn_id, self.caller)
