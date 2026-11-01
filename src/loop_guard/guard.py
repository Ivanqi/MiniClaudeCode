"""LoopGuard 检测器:空转、重复调用、重复观察、错误连击、乒乓与无进展。

设计边界:
- 每个 run_cycle 新建,不持有跨 turn/跨 plan-step 状态;
- 不直接改历史消息,只返回信号/动作,由 AgentLoop 在 tool 结果之后应用;
- 信号去重键包含 kind/severity/threshold/签名,同一证据只提醒一次;
- 仅保存计数、哈希与脱敏摘要,负记忆不泄漏工具原始输出。
"""
import json
import time
from collections import deque

from src.Configs import Configs
from src.loop_guard.ledger import ProgressLedger
from src.loop_guard.signals import (_MUTATION_TOOLS, _WRITE_TOOLS,
                                    SEVERITY_ORDER, LoopSignal, ToolObservation,
                                    is_wait_observation, short_digest)


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
        """记录一次工具结果,返回本轮新产生的全部信号。"""
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
        """轮次结束检查“无进展”阈值;local 模式直接 escalate。"""
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
        """同名同参调用连续出现;等待类轮询不算重复。"""
        if is_wait_observation(obs):
            return
        key = (obs.tool, obs.args_key)
        if key == self._exact_key:
            self._exact_streak += 1
        else:
            self._exact_key = key
            self._exact_streak = 1
        evidence = {
            "tool": obs.tool,
            "args_hash": short_digest(obs.args_key),
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
        """工具返回内容连续相同;写操作/任务状态模板输出交给 exact_repeat。"""
        if is_wait_observation(obs):
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
            "args_hash": short_digest(obs.args_key),
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
        """错误码连续重复;熔断器自身拒绝不算错误连击。"""
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
            "args_hash": short_digest(obs.args_key),
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
        """检测 A/B 或 A/B/C 周期交替调用。"""
        if is_wait_observation(obs):
            return
        self._recent_keys.append((obs.tool, short_digest(obs.args_key)))
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
        """返回 (重复次数, A/B 形式的模式串);不足两轮重复则忽略。"""
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
        """在多条信号中选最高严重度,并按模式/提醒次数降级或升级。"""
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
        """生成给模型的提醒文本;长度受 LOOP_GUARD_MESSAGE_CHARS 限制。"""
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
        """最近失败记录(按发生顺序),供 prompt 与负记忆使用。"""
        limit = max(0, int(Configs.LOOP_FAILED_ATTEMPTS_MAX))
        if not limit:
            return []
        entries = list(self._failed.values())[-limit:]
        return [dict(entry) for entry in entries]

    def tool_events(self) -> list[dict]:
        """本 turn 的工具事件流(已脱敏),供轨迹记录。"""
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
        """是否值得把失败经验写入负记忆。"""
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
        """最近一次真实进展的单调时钟时间;watchdog 消费它。"""
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
        """轨迹事件只保留哈希与状态,不保留原始参数/输出。"""
        self._events.append({
            "tool": obs.tool,
            "args_hash": short_digest(obs.args_key),
            "result_hash": obs.content_hash[:16],
            "ok": bool(obs.ok),
            "denied": bool(obs.denied),
            "error_code": obs.error_code,
            "exit_code": obs.exit_code,
            "background": bool(obs.background),
            "round": obs.round_index,
        })

    def _add_failed(self, obs: ToolObservation, code: str):
        """按 (tool, args 哈希, error_code) 聚合失败次数。"""
        key = (obs.tool, short_digest(obs.args_key), code)
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
        """按 (kind, severity, threshold, 签名) 去重后生成信号。"""
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
