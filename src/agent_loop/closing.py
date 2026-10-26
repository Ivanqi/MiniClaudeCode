"""预算/循环保护后的强制收敛。"""
from src.Configs import Configs
from src.LlmClient import user_message

from src.agent_loop.context import (attach_loop_context,
                                    inject_background_notifications)
from src.agent_loop.telemetry import sync_state_meta


def close_turn(loop, messages: list, context: dict, state, reason: str,
               turn_id: str, budget=None, allow_background: bool = True,
               trigger_stop: bool = True, trajectory: bool = True,
               caller: str = "main", guard=None, watchdog=None,
               stop_kind: str = "budget") -> str:
    """一次无工具调用收尾,不允许悬挂 tool_call。"""
    if stop_kind == "loop":
        state.stop_reason = "loop_stopped"
        state.loop_stop_reason = reason
        prompt = (f"[Loop stopped: {reason}. Based on existing results, "
                  "give a final answer now. Do not call any tools.]")
        failed_prefix = f"[Loop stopped: {reason}."
    else:
        state.budget_exhausted_reason = reason
        prompt = (f"[Budget reached: {reason}. Based on existing results, "
                  "give a final answer now. Do not call any tools.]")
        failed_prefix = f"[Budget exhausted: {reason}."
    if allow_background:
        inject_background_notifications(loop.background, messages)
    messages.append(user_message(prompt))
    context = attach_loop_context(context, budget, guard)
    system = loop.prompt_builder.assemble(context, None)
    if watchdog is not None:
        watchdog.set_phase("close")
    try:
        response = loop.client.create(
            messages, system=system, tools=None,
            max_tokens=Configs.SUMMARY_MAX_TOKENS)
        if budget is not None:
            budget.record_usage(getattr(response, "usage", None))
        loop.metrics.add_usage(
            getattr(response, "usage", None),
            "lead" if caller == "main" else caller)
        text = (response.content or "").strip()
    except Exception as exc:
        text = (f"{failed_prefix} "
                f"Summary failed: {type(exc).__name__}: {exc}]")
    if not text:
        text = f"{failed_prefix} No summary produced.]"
    messages.append({"role": "assistant", "content": text})
    if stop_kind == "loop":
        loop.metrics.record("loop_stopped")
        loop.audit.log(
            "loop_stopped", turn_id=turn_id, caller=caller,
            reason=reason,
            signals=list(getattr(state, "loop_signals", []))[-10:])
    if trigger_stop:
        loop.hooks.trigger("Stop", messages)
    sync_state_meta(state, budget, guard, turn_id, caller)
    if trajectory:
        loop.trajectory.log_turn(messages, state)
    return text
