"""轮次遥测:guard 信号、RecoveryState 元数据、负记忆与 turn result。"""
import time

from src.Configs import Configs
from src.Terminal import Terminal


def last_assistant_text(messages: list) -> str:
    for message in reversed(messages):
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
    return ""


def record_guard_signals(metrics, audit, state, signals,
                         turn_id: str, caller: str):
    for signal in signals or []:
        entry = signal.to_dict()
        state.loop_signals.append(entry)
        if len(state.loop_signals) > 20:
            del state.loop_signals[:-20]
        key = f"{signal.kind}.{signal.severity}"
        state.loop_signal_counts[key] = (
            state.loop_signal_counts.get(key, 0) + 1)
        metrics.record("loop_signal")
        metrics.record(f"loop_signal.{key}")
        audit.log(
            "loop_signal", turn_id=turn_id, caller=caller,
            kind=signal.kind, severity=signal.severity,
            evidence=dict(signal.evidence or {}))


def sync_state_meta(state, budget, guard, turn_id: str, caller: str):
    state.turn_id = turn_id
    state.caller = caller
    state.budget_snapshot = (budget.runtime_snapshot()
                             if budget is not None else {})
    if guard is not None:
        state.failed_attempts = guard.failed_attempts()
        state.tool_events = guard.tool_events()


def persist_loop_memory(memory, guard, turn_id: str, caller: str):
    """高严重度失败写一条 working 记忆;异常不影响主流程。"""
    try:
        content = guard.negative_memory_text()
        if not content:
            return
        kinds = sorted({entry.get("kind", "")
                        for entry in guard.snapshot().get("signals", [])
                        if entry.get("kind")})
        tags = ["loop_guard", "negative", caller] + kinds
        memory.upsert(
            "working", content,
            key=f"loop_guard:{turn_id}", tags=tags,
            source="loop_guard", source_kind="workflow",
            source_ref=turn_id,
            expires_at=time.time() + Configs.LOOP_MEMORY_TTL_SECONDS)
    except Exception as exc:
        Terminal.info("loop_guard", f"negative memory failed: {exc}",
                      Terminal.RED)


def turn_result(metrics, status: str, messages: list, budget,
                error: str = "", closing_reason: str = "",
                artifacts=None, content: str | None = None,
                state=None) -> dict:
    metrics.record(f"turn.{status}")
    return {
        "status": status,
        "content": (content if content is not None
                    else last_assistant_text(messages)),
        "closing_reason": closing_reason,
        "error": error,
        "budget": budget.snapshot(),
        "messages": messages,
        "artifacts": list(artifacts or []),
        "loop_signals": (list(getattr(state, "loop_signals", []))
                         if state is not None else []),
        "tool_events": (list(getattr(state, "tool_events", []))
                        if state is not None else []),
        "failed_attempts": (list(getattr(state, "failed_attempts", []))
                            if state is not None else []),
        "stop_reason": (getattr(state, "stop_reason", "")
                        if state is not None else ""),
    }
