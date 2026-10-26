"""活跃队友注册表:线程安全地登记队友状态与心跳。

保留旧接口 add(name)/remove/contains/names;新增 role/task/claim_token/
last_seen/thread 元数据,touch/set_task/stale/prune_dead 供 LeadMonitor 使用。
lead 不应加入此表,names() 只包含队友。
"""
import threading
import time
from dataclasses import dataclass, field


@dataclass
class TeammateInfo:
    name: str
    role: str = ""
    task_id: str | None = None
    claim_token: str = ""
    status: str = "starting"
    started_at: float = field(default_factory=time.monotonic)
    last_seen: float = field(default_factory=time.monotonic)
    thread: object | None = None

    def snapshot(self) -> dict:
        return {
            "name": self.name,
            "role": self.role,
            "task_id": self.task_id,
            "status": self.status,
            "started_at": self.started_at,
            "last_seen": self.last_seen,
        }


class ActiveTeammates:
    """活跃队友注册表(线程安全)。"""

    def __init__(self):
        self._teammates: dict[str, TeammateInfo] = {}
        self._lock = threading.RLock()

    def add(self, name: str, role: str = "", task_id: str | None = None,
            thread=None) -> TeammateInfo:
        with self._lock:
            info = self._teammates.get(name)
            if info is None:
                info = TeammateInfo(name=name, role=role, task_id=task_id,
                                    thread=thread)
                self._teammates[name] = info
            else:
                info.role = role or info.role
                info.task_id = task_id if task_id is not None else info.task_id
                info.thread = thread or info.thread
                info.last_seen = time.monotonic()
            return info

    def remove(self, name: str):
        with self._lock:
            self._teammates.pop(name, None)

    def contains(self, name: str) -> bool:
        with self._lock:
            return name in self._teammates

    def names(self) -> list[str]:
        with self._lock:
            return list(self._teammates.keys())

    def live_names(self) -> list[str]:
        with self._lock:
            return [name for name, info in self._teammates.items()
                    if info.thread is None or info.thread.is_alive()]

    def touch(self, name: str):
        with self._lock:
            info = self._teammates.get(name)
            if info is not None:
                info.last_seen = time.monotonic()
                info.status = "running"

    def set_task(self, name: str, task_id: str | None,
                 claim_token: str = ""):
        with self._lock:
            info = self._teammates.get(name)
            if info is not None:
                info.task_id = task_id
                info.claim_token = claim_token
                info.last_seen = time.monotonic()

    def clear_task(self, name: str):
        self.set_task(name, None, "")

    def set_status(self, name: str, status: str):
        with self._lock:
            info = self._teammates.get(name)
            if info is not None:
                info.status = status
                info.last_seen = time.monotonic()

    def get(self, name: str) -> TeammateInfo | None:
        with self._lock:
            return self._teammates.get(name)

    def snapshot(self) -> list[dict]:
        with self._lock:
            return [info.snapshot() for info in self._teammates.values()]

    def stale(self, ttl_seconds: float, now: float | None = None) -> list[str]:
        now = time.monotonic() if now is None else now
        with self._lock:
            return [name for name, info in self._teammates.items()
                    if now - info.last_seen >= ttl_seconds]

    def prune_dead(self) -> list[str]:
        removed = []
        with self._lock:
            for name, info in list(self._teammates.items()):
                if info.thread is not None and not info.thread.is_alive():
                    self._teammates.pop(name, None)
                    removed.append(name)
        return removed


# 全局单例。
ACTIVE_TEAMMATES = ActiveTeammates()
