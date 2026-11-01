"""阶段感知的协作式 watchdog:超时只设置 cancel_event,不强行杀线程。

bash 等长任务会消费 cancel_event;其他同步调用只能返回后收尾。
`poll()` 可用假时钟单独测试,不依赖后台线程。
"""
import threading
import time

from src.Configs import Configs


class LoopWatchdog:
    """监控阶段超时、turn 墙钟与“工具无进展”时间。"""

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
        """启动后台轮询线程;重复 start 幂等。"""
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
        """进入新阶段并重置阶段计时;timeout=None 使用阶段默认值。"""
        with self._lock:
            self._phase = phase
            self._phase_started = self._clock()
            if timeout is None:
                timeout = self._default_timeout(phase)
            self._phase_timeout = float(timeout or 0.0)

    def poll(self) -> str:
        """检查一次超时条件;命中后设置 cancel_event 并固化原因。"""
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
