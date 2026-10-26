"""通用循环保护:空转信号、进度账本、工具熔断与主循环 watchdog。

设计边界:
- LoopGuard 每个 run_cycle 新建,不持有跨 turn/跨 plan-step 状态;
- 不直接改历史消息,只返回信号/动作,由 AgentLoop 在 tool 结果之后应用;
- 熔断器挂在 ToolDispatcher,主循环/子代理/队友/后台自动共享;
- watchdog 只做协作式取消:bash 消费 cancel_event,其他同步调用只能返回后收尾。
"""
import hashlib
import json
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from src.Configs import Configs
from src.SecurityPolicy import redact
from src.ToolPolicy import (INFRA_ERROR_CODES, ToolResult, canonical_args,
                            infer_error_code)

SEVERITY_ORDER = {
    "none": 0,
    "nudge": 1,
    "reflect": 2,
    "replan": 3,
    "escalate": 4,
    "stop": 5,
}

_BODY_KEYS = {"content", "old_text", "new_text", "plan", "prompt",
              "description", "feedback"}
_WRITE_TOOLS = {"write_file", "edit_file"}
_MUTATION_TOOLS = {
    "create_task", "claim_task", "complete_task", "fail_task", "release_task",
    "create_worktree", "remove_worktree", "seal_worktree", "apply_integration",
    "remember", "forget", "create_plan", "execute_plan", "plan_execute",
    "archive_case", "deploy_asset", "register_asset",
    "send_message", "submit_plan", "spawn_teammate",
    "schedule_cron", "cancel_cron", "keep_worktree", "reclaim_tasks",
    "request_shutdown", "request_plan", "review_plan",
}
# 轮询后台任务是合法的等待行为:重复同一状态不应触发空转/无进展。
_WAIT_TOOLS = {"get_background_task"}


def _digest(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8", "replace")).hexdigest()


def _short_digest(text: str) -> str:
    return _digest(text)[:16]


def _is_wait_observation(obs: "ToolObservation") -> bool:
    """后台轮询仍在 running/queued 时是合法等待;终态轮询回归普通信号。"""
    if obs.tool not in _WAIT_TOOLS:
        return False
    text = str(obs.content_excerpt or "").lower()
    first_line = text.split("\n", 1)[0]
    terminal = ("completed", "failed", "cancelled", "timed_out", "timed out")
    return not any(token in first_line for token in terminal)


def _summarize_arguments(arguments: dict | None) -> str:
    """参数摘要:敏感正文只留长度/哈希,其余脱敏并截断。"""
    summary = {}
    for key, value in (arguments or {}).items():
        if key in _BODY_KEYS:
            text = value if isinstance(value, str) else str(value)
            summary[key] = f"<{len(text)} chars sha256:{_short_digest(text)}>"
        elif isinstance(value, (dict, list)):
            summary[key] = redact(
                json.dumps(value, ensure_ascii=False, default=str))[:120]
        else:
            summary[key] = redact(str(value))[:120]
    return json.dumps(summary, ensure_ascii=False, default=str)[:240]


@dataclass(frozen=True)
class LoopSignal:
    """一次循环保护信号;evidence 只放计数/哈希,不放原始工具输出。"""

    kind: str
    severity: str
    evidence: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "severity": self.severity,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True)
class ToolObservation:
    """归一化后的工具结果,屏蔽 denial/background/同步三种分支差异。"""

    tool: str
    args_key: str
    content_hash: str
    ok: bool
    error_code: str = ""
    exit_code: int | None = None
    denied: bool = False
    background: bool = False
    artifacts: tuple[str, ...] = ()
    args_summary: str = ""
    content_excerpt: str = ""
    round_index: int = 0

    @classmethod
    def from_result(cls, call, output, result: ToolResult | None = None,
                    ok: bool | None = None, error_code: str | None = None,
                    background: bool = False,
                    round_index: int = 0) -> "ToolObservation":
        content = str(output if output is not None else "")
        arguments = dict(getattr(call, "arguments", {}) or {})
        if result is not None:
            ok = bool(result.ok) if ok is None else bool(ok)
            code = (error_code or result.error_code
                    or ("" if result.ok else infer_error_code(call.name, result)))
            exit_code = result.exit_code
            denied = bool(result.denied)
            artifacts = tuple(result.artifacts or ())
        else:
            ok = bool(ok)
            code = error_code or ("" if ok else "tool_error")
            exit_code = None
            denied = False
            artifacts = ()
        return cls(
            tool=str(getattr(call, "name", "")),
            args_key=canonical_args(arguments),
            content_hash=_digest(content),
            ok=ok,
            error_code=code or "",
            exit_code=exit_code,
            denied=denied,
            background=background,
            artifacts=artifacts,
            args_summary=_summarize_arguments(arguments),
            content_excerpt=redact(content.strip())[:200],
            round_index=round_index,
        )


class ProgressLedger:
    """按“产出增量”判断是否真的前进了,而不是按工具调用次数。"""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self.round = 0
        self.last_progress_round = 0
        self.last_progress_at = clock()
        self.seen_outputs: set[str] = set()
        self.seen_artifacts: set[str] = set()
        self.seen_writes: set[str] = set()
        self.seen_mutations: set[str] = set()
        self.last_todo: str | None = None

    def begin_round(self, round_index: int):
        self.round = max(self.round, int(round_index or 0))

    def observe(self, obs: ToolObservation) -> bool:
        if obs.background:
            return False
        progress = False
        args_hash = _short_digest(obs.args_key)
        if obs.ok and _is_wait_observation(obs):
            self.last_progress_round = max(self.round,
                                           self.last_progress_round)
            self.last_progress_at = self._clock()
            return True
        if obs.ok and not obs.denied:
            if obs.content_excerpt and obs.content_excerpt != "(no output)" \
                    and obs.content_hash not in self.seen_outputs:
                self.seen_outputs.add(obs.content_hash)
                progress = True
            for artifact in obs.artifacts:
                if artifact and artifact not in self.seen_artifacts:
                    self.seen_artifacts.add(artifact)
                    progress = True
            if obs.tool in _WRITE_TOOLS and args_hash not in self.seen_writes:
                self.seen_writes.add(args_hash)
                progress = True
            if obs.tool == "todo_write" and args_hash != self.last_todo:
                self.last_todo = args_hash
                progress = True
            mutation_key = f"{obs.tool}\0{args_hash}"
            if obs.tool in _MUTATION_TOOLS \
                    and mutation_key not in self.seen_mutations:
                self.seen_mutations.add(mutation_key)
                progress = True
        if progress:
            self.last_progress_round = max(self.round, self.last_progress_round)
            self.last_progress_at = self._clock()
        return progress

    def stale_rounds(self) -> int:
        return max(0, self.round - self.last_progress_round)


class LoopGuard:
    """一次 run_cycle 的空转/无进展检测器。"""

    def __init__(self, caller: str = "main", clock=time.monotonic,
                 enabled: bool | None = None):
        self.caller = caller or "main"
        self.enabled = (Configs.LOOP_GUARD_ENABLED if enabled is None
                        else bool(enabled))
        self._clock = clock
        self._ledger = ProgressLedger(clock=clock)
        self._exact_key = None
        self._exact_streak = 0
        self._last_obs_hash = ""
        self._obs_streak = 0
        self._last_error_code = ""
        self._error_streak = 0
        self._total_errors = 0
        self._recent_keys: deque = deque(maxlen=8)
        self._emitted: set[tuple[str, str, int, str]] = set()
        self._signals: deque = deque(maxlen=20)
        self._high_severity_seen = False
        self._failed: dict[tuple[str, str, str], dict] = {}
        self._events: deque = deque(maxlen=Configs.LOOP_EVENTS_MAX)

    # ── 观察 ──

    def begin_round(self, round_index: int):
        self._ledger.begin_round(round_index)

    def observe(self, obs: ToolObservation, mode: str) -> list[LoopSignal]:
        if not self.enabled:
            return []
        self._record_event(obs)
        self._ledger.observe(obs)
        if obs.background:
            return []

        signals: list[LoopSignal] = []
        self._track_exact(obs, mode, signals)
        self._track_observation(obs, mode, signals)
        self._track_error(obs, mode, signals)
        self._track_pingpong(obs, mode, signals)
        return signals

    def end_round(self, mode: str) -> list[LoopSignal]:
        if not self.enabled:
            return []
        stale = self._ledger.stale_rounds()
        epoch = self._ledger.last_progress_round
        signals: list[LoopSignal] = []
        if mode == "local":
            threshold = Configs.LOOP_LOCAL_NO_PROGRESS_ESCALATE
            if stale >= threshold:
                signal = self._emit(
                    "no_progress", "escalate", threshold,
                    {"rounds": stale, "threshold": threshold,
                     "epoch": epoch})
                if signal:
                    signals.append(signal)
        else:
            reflect = Configs.LOOP_NO_PROGRESS_REFLECT
            stop = Configs.LOOP_NO_PROGRESS_STOP
            if stale >= reflect:
                signal = self._emit(
                    "no_progress", "reflect", reflect,
                    {"rounds": stale, "threshold": reflect,
                     "epoch": epoch})
                if signal:
                    signals.append(signal)
            if stale >= stop:
                severity = "replan" if mode == "plan" else "stop"
                signal = self._emit(
                    "no_progress", severity, stop,
                    {"rounds": stale, "threshold": stop,
                     "epoch": epoch})
                if signal:
                    signals.append(signal)
        return signals

    # ── 信号规则 ──

    def _track_exact(self, obs: ToolObservation, mode: str,
                     signals: list[LoopSignal]):
        if _is_wait_observation(obs):
            return
        key = (obs.tool, obs.args_key)
        if key == self._exact_key:
            self._exact_streak += 1
        else:
            self._exact_key = key
            self._exact_streak = 1
        evidence = {
            "tool": obs.tool,
            "args_hash": _short_digest(obs.args_key),
            "count": self._exact_streak,
            "round": obs.round_index,
        }
        if mode == "local":
            threshold = Configs.MAX_IDENTICAL_TOOL_CALLS
            if self._exact_streak >= threshold:
                signal = self._emit("exact_repeat", "escalate", threshold,
                                    evidence)
                if signal:
                    signals.append(signal)
            return
        if self._exact_streak >= Configs.LOOP_EXACT_REPEAT_NUDGE:
            signal = self._emit(
                "exact_repeat", "nudge", Configs.LOOP_EXACT_REPEAT_NUDGE,
                evidence)
            if signal:
                signals.append(signal)
        if self._exact_streak >= Configs.LOOP_EXACT_REPEAT_STOP:
            signal = self._emit(
                "exact_repeat", "stop", Configs.LOOP_EXACT_REPEAT_STOP,
                evidence)
            if signal:
                signals.append(signal)

    def _track_observation(self, obs: ToolObservation, mode: str,
                           signals: list[LoopSignal]):
        if _is_wait_observation(obs):
            return
        # 写操作/todo/任务状态工具的返回文本通常是固定模板,
        # 重复不代表观察停滞;这类空转交给 exact_repeat 处理。
        if obs.ok and (obs.tool in _WRITE_TOOLS or obs.tool == "todo_write"
                       or obs.tool in _MUTATION_TOOLS):
            self._last_obs_hash = ""
            self._obs_streak = 0
            return
        if obs.content_hash == self._last_obs_hash:
            self._obs_streak += 1
        else:
            self._last_obs_hash = obs.content_hash
            self._obs_streak = 1
        evidence = {
            "tool": obs.tool,
            "args_hash": _short_digest(obs.args_key),
            "result_hash": obs.content_hash[:16],
            "count": self._obs_streak,
            "round": obs.round_index,
        }
        if mode == "local":
            threshold = Configs.MAX_IDENTICAL_TOOL_CALLS
            if self._obs_streak >= threshold:
                signal = self._emit("same_observation", "escalate", threshold,
                                    evidence)
                if signal:
                    signals.append(signal)
            return
        if self._obs_streak >= Configs.LOOP_OBSERVATION_NUDGE:
            signal = self._emit(
                "same_observation", "nudge", Configs.LOOP_OBSERVATION_NUDGE,
                evidence)
            if signal:
                signals.append(signal)
        if self._obs_streak >= Configs.LOOP_OBSERVATION_STOP:
            signal = self._emit(
                "same_observation", "stop", Configs.LOOP_OBSERVATION_STOP,
                evidence)
            if signal:
                signals.append(signal)

    def _track_error(self, obs: ToolObservation, mode: str,
                     signals: list[LoopSignal]):
        if obs.ok:
            self._last_error_code = ""
            self._error_streak = 0
            return
        self._total_errors += 1
        code = obs.error_code or "tool_error"
        if not obs.background:
            self._add_failed(obs, code)
        if code == "circuit_open":
            return
        if code == self._last_error_code:
            self._error_streak += 1
        else:
            self._last_error_code = code
            self._error_streak = 1
        evidence = {
            "tool": obs.tool,
            "args_hash": _short_digest(obs.args_key),
            "error_code": code,
            "exit_code": obs.exit_code,
            "count": self._error_streak,
            "round": obs.round_index,
        }
        if mode == "local":
            threshold = Configs.MAX_TOOL_ERRORS_BEFORE_ESCALATE
            if self._total_errors >= threshold:
                signal = self._emit("error_streak", "escalate", threshold,
                                    evidence)
                if signal:
                    signals.append(signal)
            return
        if self._error_streak >= Configs.LOOP_ERROR_REFLECT:
            signal = self._emit(
                "error_streak", "reflect", Configs.LOOP_ERROR_REFLECT,
                evidence)
            if signal:
                signals.append(signal)
        if self._error_streak >= Configs.LOOP_ERROR_STOP:
            signal = self._emit(
                "error_streak", "stop", Configs.LOOP_ERROR_STOP, evidence)
            if signal:
                signals.append(signal)

    def _track_pingpong(self, obs: ToolObservation, mode: str,
                        signals: list[LoopSignal]):
        if _is_wait_observation(obs):
            return
        self._recent_keys.append((obs.tool, _short_digest(obs.args_key)))
        keys = list(self._recent_keys)
        best_len = 0
        best_pattern = ""
        for period in (2, 3):
            repeats, pattern = self._pattern_repeats(keys, period)
            if repeats >= 2 and repeats * period > best_len:
                best_len = repeats * period
                best_pattern = pattern
        if not best_len:
            return
        evidence = {
            "tool": obs.tool,
            "calls": best_len,
            "pattern": best_pattern,
            "round": obs.round_index,
        }
        if mode == "local":
            threshold = Configs.LOOP_PINGPONG_STOP
            if best_len >= threshold:
                signal = self._emit("ping_pong", "escalate", threshold,
                                    evidence)
                if signal:
                    signals.append(signal)
            return
        if best_len >= Configs.LOOP_PINGPONG_REFLECT:
            signal = self._emit(
                "ping_pong", "reflect", Configs.LOOP_PINGPONG_REFLECT,
                evidence)
            if signal:
                signals.append(signal)
        if best_len >= Configs.LOOP_PINGPONG_STOP:
            signal = self._emit(
                "ping_pong", "stop", Configs.LOOP_PINGPONG_STOP, evidence)
            if signal:
                signals.append(signal)

    @staticmethod
    def _pattern_repeats(keys: list, period: int) -> tuple[int, str]:
        if len(keys) < period * 2:
            return 0, ""
        block = keys[-period:]
        if len(set(block)) <= 1:
            return 0, ""
        repeats = 1
        index = len(keys) - period
        while index - period >= 0 and keys[index - period:index] == block:
            repeats += 1
            index -= period
        if repeats < 2:
            return 0, ""
        names = {}
        letters = []
        for key in block:
            if key not in names:
                names[key] = chr(ord("A") + len(names))
            letters.append(names[key])
        return repeats, "".join(letters) * repeats

    # ── 动作解析 ──

    def resolve(self, signals: list[LoopSignal], mode: str,
                messages_sent: int = 0) -> tuple[str, LoopSignal | None]:
        valid = [signal for signal in (signals or []) if signal]
        if not valid:
            return "none", None
        chosen = max(valid, key=lambda item: SEVERITY_ORDER.get(
            item.severity, 0))
        action = chosen.severity
        if action == "escalate" and mode != "local":
            action = "nudge"
        if action == "replan" and mode != "plan":
            action = "stop"
        if action in ("nudge", "reflect") and \
                messages_sent >= Configs.LOOP_MAX_GUARD_MESSAGES:
            action = "stop"
        return action, chosen

    def format_message(self, action: str, signal: LoopSignal) -> str:
        evidence = dict(signal.evidence or {})
        kind = signal.kind
        if kind == "exact_repeat":
            body = (
                f"Repeated the same tool call {evidence.get('count', '?')} "
                f"times (tool={evidence.get('tool', '?')}, "
                f"args={evidence.get('args_hash', '?')}). "
                "Do not repeat it; change the arguments, use another tool, "
                "or stop and summarize the blocker.")
        elif kind == "same_observation":
            body = (
                f"Tool {evidence.get('tool', '?')} returned the same output "
                f"{evidence.get('count', '?')} times "
                f"(result={evidence.get('result_hash', '?')}). "
                "The observation is not changing; try a different approach.")
        elif kind == "error_streak":
            body = (
                f"Tool failures are repeating (tool={evidence.get('tool', '?')}, "
                f"error={evidence.get('error_code', '?')}, "
                f"count={evidence.get('count', '?')}). "
                "Do not retry the same failing action; inspect the error, "
                "change conditions, or use another tool.")
        elif kind == "ping_pong":
            body = (
                "Tools are alternating without progress "
                f"(pattern={evidence.get('pattern', '?')}, "
                f"calls={evidence.get('calls', '?')}). "
                "Break the cycle: pick one path, gather the missing fact, "
                "or summarize what is blocked.")
        elif kind == "no_progress":
            body = (
                f"No new files, evidence, task state, memory, or plan progress "
                f"for {evidence.get('rounds', '?')} rounds. "
                "Stop exploring the same area; state what is blocked and "
                "choose a different action.")
        elif kind == "watchdog":
            body = f"The turn watchdog stopped a stalled phase: {evidence}."
        elif kind == "plan_loop":
            body = f"The plan is repeating a failed step: {evidence}."
        else:
            body = f"Loop signal {kind}: {evidence}."
        if action in ("reflect", "replan", "stop"):
            body += (" Before your next action, state what you tried, why it "
                     "failed, and the new approach.")
        text = f"[LoopGuard] {body}"
        return text[:Configs.LOOP_GUARD_MESSAGE_CHARS]

    # ── 供 Prompt/轨迹/负记忆 ──

    def failed_attempts(self) -> list[dict]:
        limit = max(0, int(Configs.LOOP_FAILED_ATTEMPTS_MAX))
        if not limit:
            return []
        entries = list(self._failed.values())[-limit:]
        return [dict(entry) for entry in entries]

    def tool_events(self) -> list[dict]:
        return list(self._events)

    def snapshot(self) -> dict:
        return {
            "enabled": self.enabled,
            "caller": self.caller,
            "total_errors": self._total_errors,
            "exact_streak": self._exact_streak,
            "observation_streak": self._obs_streak,
            "error_streak": self._error_streak,
            "last_error_code": self._last_error_code,
            "stale_rounds": self._ledger.stale_rounds(),
            "signals": [signal.to_dict() for signal in self._signals],
            "failed_attempts": self.failed_attempts(),
        }

    def should_persist_memory(self) -> bool:
        if self._high_severity_seen:
            return True
        if any(signal.severity in ("escalate", "stop", "replan")
               for signal in self._signals):
            return True
        return any(entry.get("count", 0) >= Configs.LOOP_MEMORY_REPEAT_FAILURES
                   for entry in self._failed.values())

    def negative_memory_text(self) -> str:
        lines = ["LoopGuard negative memory"]
        for signal in list(self._signals)[-10:]:
            evidence = json.dumps(signal.evidence, ensure_ascii=False,
                                  default=str)
            lines.append(f"- signal {signal.kind}/{signal.severity}: {evidence}")
        for entry in self.failed_attempts():
            lines.append(
                f"- failed {entry.get('tool')} [{entry.get('error_code')}] "
                f"x{entry.get('count')} args={entry.get('args_summary')} "
                f"last={entry.get('excerpt')}")
        return "\n".join(lines)[:Configs.LOOP_MEMORY_MAX_CHARS]

    def record_external(self, kind: str, severity: str,
                        evidence: dict | None = None) -> LoopSignal:
        """记录 AgentLoop/watchdog 产生的外部信号(不参与阈值去重)。"""
        signal = LoopSignal(kind=kind, severity=severity,
                            evidence=dict(evidence or {}))
        self._signals.append(signal)
        if severity in ("escalate", "stop", "replan"):
            self._high_severity_seen = True
        return signal

    def progress_epoch(self) -> float:
        return self._ledger.last_progress_at

    @property
    def total_errors(self) -> int:
        return self._total_errors

    @property
    def exact_streak(self) -> int:
        return self._exact_streak

    @property
    def last_key(self):
        return self._exact_key

    # ── 内部 ──

    def _record_event(self, obs: ToolObservation):
        self._events.append({
            "tool": obs.tool,
            "args_hash": _short_digest(obs.args_key),
            "result_hash": obs.content_hash[:16],
            "ok": bool(obs.ok),
            "denied": bool(obs.denied),
            "error_code": obs.error_code,
            "exit_code": obs.exit_code,
            "background": bool(obs.background),
            "round": obs.round_index,
        })

    def _add_failed(self, obs: ToolObservation, code: str):
        key = (obs.tool, _short_digest(obs.args_key), code)
        entry = self._failed.pop(key, None) or {
            "tool": obs.tool,
            "error_code": code,
            "count": 0,
            "args_summary": obs.args_summary,
            "excerpt": obs.content_excerpt,
        }
        entry["count"] += 1
        entry["excerpt"] = obs.content_excerpt
        entry["round"] = obs.round_index
        self._failed[key] = entry
        while len(self._failed) > max(1, Configs.LOOP_FAILED_ATTEMPTS_MAX * 2):
            self._failed.pop(next(iter(self._failed)))

    def _emit(self, kind: str, severity: str, threshold: int,
              evidence: dict) -> LoopSignal | None:
        signature_keys = {
            "exact_repeat": ("tool", "args_hash"),
            "same_observation": ("tool", "result_hash"),
            "error_streak": ("tool", "error_code"),
            "ping_pong": ("pattern",),
            "no_progress": ("epoch",),
        }.get(kind, ("tool", "error_code", "pattern", "epoch"))
        signature = "|".join(str(evidence.get(key, ""))
                             for key in signature_keys)
        key = (kind, severity, int(threshold), signature)
        if key in self._emitted:
            return None
        self._emitted.add(key)
        if severity in ("escalate", "stop", "replan"):
            self._high_severity_seen = True
        payload = dict(evidence)
        payload.setdefault("threshold", threshold)
        signal = LoopSignal(kind=kind, severity=severity, evidence=payload)
        self._signals.append(signal)
        return signal


class ToolCircuitBreaker:
    """按工具熔断基础设施类失败;policy deny/业务 exit_code 不算。"""

    def __init__(self, failure_threshold: int | None = None,
                 cooldown_s: int | None = None,
                 max_cooldown_s: int | None = None,
                 clock=time.monotonic, metrics=None, audit=None):
        self.failure_threshold = (Configs.CIRCUIT_FAILURE_THRESHOLD
                                  if failure_threshold is None
                                  else int(failure_threshold))
        self.base_cooldown = (Configs.CIRCUIT_COOLDOWN_SECONDS
                              if cooldown_s is None else int(cooldown_s))
        self.max_cooldown = (Configs.CIRCUIT_MAX_COOLDOWN_SECONDS
                             if max_cooldown_s is None
                             else int(max_cooldown_s))
        self._clock = clock
        self.metrics = metrics
        self.audit = audit
        self._lock = threading.RLock()
        self._states: dict[str, dict] = {}

    def _state(self, tool: str) -> dict:
        state = self._states.get(tool)
        if state is None:
            state = {
                "failures": 0,
                "opened_at": 0.0,
                "open_until": 0.0,
                "cooldown": float(self.base_cooldown),
                "probe_in_flight": False,
                "last_code": "",
                "opens": 0,
            }
            self._states[tool] = state
        return state

    def allow(self, tool: str) -> tuple[bool, str, float]:
        """授权阶段 peek;不占用半开探测名额。"""
        with self._lock:
            state = self._state(tool)
            now = self._clock()
            if state["probe_in_flight"]:
                return False, "circuit probe already in flight", 0.0
            if state["open_until"] and now < state["open_until"]:
                remaining = max(0.0, state["open_until"] - now)
                return False, self._block_reason(tool, state, remaining), remaining
            return True, "", 0.0

    def acquire(self, tool: str) -> tuple[bool, str]:
        """执行阶段原子 claim;冷却结束后的第一次调用成为半开探测。"""
        with self._lock:
            state = self._state(tool)
            now = self._clock()
            if state["open_until"] and now < state["open_until"]:
                remaining = max(0.0, state["open_until"] - now)
                return False, self._block_reason(tool, state, remaining)
            if state["open_until"] and now >= state["open_until"]:
                if state["probe_in_flight"]:
                    return False, "circuit probe already in flight"
                state["probe_in_flight"] = True
            return True, ""

    def record(self, tool: str, result: ToolResult):
        """记录一次真实执行结果;任何非基础设施失败都视为“工具有响应”。"""
        with self._lock:
            state = self._state(tool)
            if result.ok:
                self._close(state, tool)
                return
            code = infer_error_code(tool, result)
            state["last_code"] = code
            if code not in INFRA_ERROR_CODES:
                # 业务失败/策略拒绝说明工具本身可用,不应熔断。
                self._close(state, tool)
                return
            state["failures"] += 1
            half_open = bool(state["probe_in_flight"])
            state["probe_in_flight"] = False
            if state["failures"] >= self.failure_threshold or half_open:
                self._open(state, tool, code)

    def record_failure(self, tool: str, error_code: str):
        """给 dispatcher 外的包装层(如后台超时)使用。"""
        with self._lock:
            if error_code not in INFRA_ERROR_CODES:
                return
            state = self._state(tool)
            state["failures"] += 1
            state["last_code"] = error_code
            half_open = bool(state["probe_in_flight"])
            state["probe_in_flight"] = False
            if state["failures"] >= self.failure_threshold or half_open:
                self._open(state, tool, error_code)

    def record_blocked(self, tool: str):
        if self.metrics is not None:
            self.metrics.record("tool_circuit_blocked")
            self.metrics.record(f"tool_circuit_blocked.{tool}")

    def state(self, tool: str) -> str:
        with self._lock:
            state = self._state(tool)
            now = self._clock()
            if state["open_until"] and now < state["open_until"]:
                return "open"
            if state["open_until"]:
                return "half_open"
            return "closed"

    def snapshot(self) -> dict:
        with self._lock:
            return {
                tool: {
                    "state": self.state(tool),
                    "failures": state["failures"],
                    "last_code": state["last_code"],
                    "opens": state["opens"],
                    "open_until": state["open_until"],
                }
                for tool, state in self._states.items()
            }

    def reset(self, tool: str | None = None):
        with self._lock:
            if tool is None:
                self._states.clear()
            else:
                self._states.pop(tool, None)

    def _block_reason(self, tool: str, state: dict,
                      remaining: float) -> str:
        return (f"tool '{tool}' temporarily unavailable after "
                f"{state['failures']} consecutive failures "
                f"({state['last_code'] or 'unknown'}); "
                f"retry in {remaining:.0f}s. Choose another approach or tool.")

    def _close(self, state: dict, tool: str):
        state["failures"] = 0
        state["opened_at"] = 0.0
        state["open_until"] = 0.0
        state["cooldown"] = float(self.base_cooldown)
        state["probe_in_flight"] = False
        state["last_code"] = ""

    def _open(self, state: dict, tool: str, code: str):
        now = self._clock()
        cooldown = max(1.0, state["cooldown"])
        state["opened_at"] = now
        state["open_until"] = now + cooldown
        state["cooldown"] = min(self.max_cooldown,
                                max(self.base_cooldown,
                                    int(cooldown * 2)))
        state["opens"] += 1
        state["probe_in_flight"] = False
        state["last_code"] = code
        if self.metrics is not None:
            self.metrics.record("tool_circuit_open")
            self.metrics.record(f"tool_circuit_open.{tool}")
        if self.audit is not None:
            try:
                self.audit.log(
                    "tool_circuit_open", tool=tool, error_code=code,
                    failures=state["failures"], cooldown_s=cooldown,
                    opens=state["opens"])
            except Exception:
                pass


class LoopWatchdog:
    """阶段感知的协作式 watchdog;poll() 可单独用假时钟测试。"""

    def __init__(self, budget=None, clock=time.monotonic, cancel_event=None,
                 poll_interval: float | None = None,
                 no_progress_timeout: float | None = None,
                 progress_callback=None, metrics=None):
        self.budget = budget
        self._clock = clock
        self.cancel_event = cancel_event or threading.Event()
        self.poll_interval = (Configs.LOOP_WATCHDOG_POLL_SECONDS
                              if poll_interval is None else poll_interval)
        self.no_progress_timeout = (
            Configs.LOOP_WATCHDOG_NO_PROGRESS_SECONDS
            if no_progress_timeout is None else no_progress_timeout)
        self.progress_callback = progress_callback
        self.metrics = metrics
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._phase = "idle"
        self._phase_started = clock()
        self._phase_timeout = 0.0
        self._reason = ""

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=1.0)

    def set_phase(self, phase: str, timeout: float | None = None):
        with self._lock:
            self._phase = phase
            self._phase_started = self._clock()
            if timeout is None:
                timeout = self._default_timeout(phase)
            self._phase_timeout = float(timeout or 0.0)

    def poll(self) -> str:
        with self._lock:
            if self._reason:
                return self._reason
            now = self._clock()
            reason = ""
            if self.budget is not None and self.budget.max_wall_s:
                if now - self.budget.started_at >= self.budget.max_wall_s:
                    reason = (f"turn wall time exceeded "
                              f"({self.budget.max_wall_s}s)")
            if not reason and self._phase_timeout and \
                    now - self._phase_started >= self._phase_timeout:
                reason = (f"phase '{self._phase}' exceeded "
                          f"{self._phase_timeout:.0f}s")
            if not reason and self._phase == "tool" and \
                    self.no_progress_timeout and self.progress_callback:
                try:
                    progressed_at = float(self.progress_callback())
                except (TypeError, ValueError):
                    progressed_at = self._phase_started
                last = max(progressed_at, self._phase_started)
                if now - last >= self.no_progress_timeout:
                    reason = (f"no tool progress for "
                              f"{self.no_progress_timeout:.0f}s")
            if reason:
                self._reason = reason
                self.cancel_event.set()
                if self.metrics is not None:
                    self.metrics.record("watchdog_trigger")
            return self._reason

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return bool(self._reason)

    @property
    def reason(self) -> str:
        with self._lock:
            return self._reason

    def _run(self):
        while not self._stop.is_set():
            reason = self.poll()
            if reason:
                return
            self._stop.wait(max(0.05, self.poll_interval))

    @staticmethod
    def _default_timeout(phase: str) -> float:
        if phase == "llm":
            return Configs.LOOP_WATCHDOG_LLM_SECONDS
        if phase == "tool":
            return Configs.LOOP_WATCHDOG_TOOL_SECONDS
        if phase == "approval":
            return Configs.LOOP_WATCHDOG_APPROVAL_SECONDS
        if phase == "close":
            return Configs.LOOP_WATCHDOG_CLOSE_SECONDS
        return 0.0
