"""Lead 监控:心跳、协议过期、孤儿任务回收、stale 队友关机、指标落盘。

硬约束:monitor 绝不读取/消费 lead 邮箱,否则会偷走计划审批响应。
只由 AgentLoop.run_cli 启动,不在构造/import 时启动线程。
"""
import threading
import time

from src.ActiveTeammates import ACTIVE_TEAMMATES, ActiveTeammates
from src.Configs import Configs
from src.MessageBus import BUS, MessageBus
from src.ProtocolSystem import PROTOCOL, ProtocolState, ProtocolSystem
from src.TaskSystem import TASK_SYSTEM, TaskSystem
from src.TeamMetrics import TEAM_METRICS, TeamMetrics
from src.Terminal import Terminal


class LeadMonitor:
    """周期性维护协作者健康;只发 shutdown,不杀线程、不读邮箱。"""

    def __init__(self, protocol: ProtocolSystem = PROTOCOL,
                 tasks: TaskSystem = TASK_SYSTEM,
                 teammates: ActiveTeammates = ACTIVE_TEAMMATES,
                 bus: MessageBus = BUS,
                 metrics: TeamMetrics = TEAM_METRICS,
                 interval: float | None = None):
        self.protocol = protocol
        self.tasks = tasks
        self.teammates = teammates
        self.bus = bus
        self.metrics = metrics
        self.interval = (Configs.TEAM_MONITOR_INTERVAL_SECONDS
                         if interval is None else interval)
        self.last_tick: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._shutdown_cooldown: dict[str, float] = {}

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="lead-monitor")
        self._thread.start()

    def stop(self, timeout: float = 2.0):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._thread = None
        self.metrics.flush()

    def _loop(self):
        while not self._stop.wait(self.interval):
            try:
                self.tick_once()
            except Exception as exc:
                Terminal.info("monitor", f"tick failed: {exc}", Terminal.RED)

    def tick_once(self) -> dict:
        now = time.monotonic()
        self.last_tick = now
        expired = self.protocol.expire()
        live = self.teammates.live_names()
        reclaimed = self.tasks.reclaim_expired(
            live_owners=live,
            grace_seconds=Configs.TEAM_RECLAIM_GRACE_SECONDS)
        stale = self.teammates.stale(Configs.TEAMMATE_STALE_SECONDS, now=now)
        shutdowns = []
        for name in stale:
            last = self._shutdown_cooldown.get(name, 0.0)
            if now - last < max(self.interval, 30):
                continue
            req_id = self.protocol.new_request_id()
            try:
                self.protocol.track(ProtocolState(
                    request_id=req_id, type="shutdown", sender="lead",
                    target=name, status="pending", payload="stale teammate"))
            except Exception:
                continue
            self.bus.send("lead", name,
                          "No heartbeat received; shutting down.",
                          "shutdown_request", {"request_id": req_id})
            self._shutdown_cooldown[name] = now
            shutdowns.append(name)
            self.metrics.record("stale_teammate_shutdown")
        self.teammates.prune_dead()
        self.metrics.flush()
        return {"expired": expired, "reclaimed": reclaimed,
                "stale_shutdowns": shutdowns}
