"""工具熔断器:按工具熔断基础设施类失败;policy deny/业务 exit_code 不算。"""
import threading
import time

from src.Configs import Configs
from src.ToolPolicy import INFRA_ERROR_CODES, ToolResult, infer_error_code


class ToolCircuitBreaker:
    """挂在 ToolDispatcher 上,主循环/子代理/队友/后台共享同一份状态。

    状态机:closed → (失败计数达到阈值) → open → (冷却结束) → half_open
    → 探测成功 close / 探测失败重新 open(冷却时间指数增长,有上限)。
    """

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
        """惰性为每个工具创建状态;调用方必须已持有锁。"""
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
        """返回给模型的拒绝原因,提示它换方法而不是重试。"""
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
        """打开熔断并翻倍冷却时间;同时记录 metrics/audit。"""
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
