"""一次模型回复的 tool_calls 批处理。

Runner 直接更新 CycleRunner 的本轮状态:
- closing_reason: 预算耗尽;
- loop_stop_reason: guard/watchdog 中止;
- pending_signals: 待本批结束后注入的 nudge/reflect;
- artifacts: 成功工具的产物;
- state.tool_error_count / same_tool_streak / last_tool_key;
- messages: 仅 compact 分支会重写历史。
"""
from dataclasses import dataclass

from src.LlmClient import tool_message, user_message
from src.LoopGuard import ToolObservation
from src.Terminal import Terminal
from src.ToolPolicy import (ToolResult, lead_policy, local_policy,
                            skipped_tool_messages)


@dataclass
class BatchResult:
    results: list[dict]
    compacted_now: bool = False


class ToolBatchRunner:
    def __init__(self, cycle, response, decision):
        self.cycle = cycle
        self.loop = cycle.loop
        self.response = response
        self.decision = decision
        self.results: list[dict] = []
        self.compacted_now = False
        if decision.local:
            self.round_handlers = cycle.slim_handlers
            self.round_policy = cycle.policy or local_policy()
        else:
            self.round_handlers = cycle.handlers
            self.round_policy = cycle.policy or lead_policy()
        cycle.mode = ("local" if decision.local
                      else "plan" if cycle.caller == "plan_step" else "api")

    def run(self) -> BatchResult:
        cycle = self.cycle
        for call_index, call in enumerate(self.response.tool_calls):
            Terminal.print(
                f"{Terminal.CYAN}> {call.name}{Terminal.RESET}")

            if call.name == "compact":
                cycle.messages[:] = self.loop.compact.compact_history(
                    cycle.messages, self.loop.client)
                cycle.messages.append(user_message(
                    "[Compacted. Continue with summarized context.]"))
                self.compacted_now = True
                break

            reason = cycle.budget.exhausted_reason()
            if reason:
                self.results.extend(skipped_tool_messages(
                    self.response.tool_calls[call_index:],
                    f"budget exceeded: {reason}"))
                cycle.closing_reason = reason
                break
            if cycle.watchdog.cancelled:
                self.results.extend(skipped_tool_messages(
                    self.response.tool_calls[call_index:],
                    f"watchdog: {cycle.watchdog.reason}"))
                cycle.mark_watchdog_stop()
                break
            cycle.budget.reserve_tool_call()

            metadata = self.loop.tool_registry.metadata(call.name)
            cycle.watchdog.set_phase("approval")
            auth = self.loop.dispatcher.authorize(
                call, self.round_policy, self.round_handlers,
                metadata=metadata, allow_prompt=True, turn_id=cycle.turn_id)
            cycle.watchdog.set_phase("tool")
            if cycle.watchdog.cancelled:
                self.results.extend(skipped_tool_messages(
                    self.response.tool_calls[call_index:],
                    f"watchdog: {cycle.watchdog.reason}"))
                cycle.mark_watchdog_stop()
                break

            tool_result = None
            result_ok = False
            is_background = False
            if not auth.allowed:
                tool_result = auth.denial or ToolResult.denied_result(
                    auth.reason or "Denied")
                output = tool_result.content
            elif cycle.allow_background and \
                    self.loop.background.should_run_background(
                        call.name, call.arguments):
                bg_id = self.loop.background.start(
                    call, self.round_handlers,
                    dispatcher=self.loop.dispatcher,
                    policy=self.round_policy, authorization=auth,
                    turn_id=cycle.turn_id)
                if bg_id:
                    output = (f"[Background task {bg_id} started] "
                              "Result will arrive as a task_notification.")
                    result_ok = True
                    is_background = True
                else:
                    result = self.loop.dispatcher.execute(
                        call, self.round_policy, self.round_handlers,
                        authorization=auth, turn_id=cycle.turn_id,
                        cancel_event=cycle.watchdog.cancel_event)
                    tool_result = result
                    output, result_ok = result.content, result.ok
                    cycle.artifacts.extend(result.artifacts)
            else:
                result = self.loop.dispatcher.execute(
                    call, self.round_policy, self.round_handlers,
                    authorization=auth, turn_id=cycle.turn_id,
                    cancel_event=cycle.watchdog.cancel_event)
                tool_result = result
                output, result_ok = result.content, result.ok
                cycle.artifacts.extend(result.artifacts)
            Terminal.print(str(output)[:300])

            observation = ToolObservation.from_result(
                call, output, result=tool_result, ok=result_ok,
                background=is_background, round_index=cycle.budget.rounds)
            signals = cycle.guard.observe(observation, cycle.mode)
            cycle.handle_tool_signals(signals)

            if cycle.todo_reminders:
                if call.name == "todo_write":
                    self.loop.rounds_since_todo = 0
                else:
                    self.loop.rounds_since_todo += 1

            self.results.append(tool_message(call.id, output))
            if cycle.watchdog.cancelled and not cycle.loop_stop_reason:
                cycle.mark_watchdog_stop()
            if cycle.loop_stop_reason:
                remaining = self.response.tool_calls[call_index + 1:]
                if remaining:
                    self.results.extend(skipped_tool_messages(
                        remaining,
                        f"loop stopped: {cycle.loop_stop_reason}"))
                break

        return BatchResult(self.results, self.compacted_now)
