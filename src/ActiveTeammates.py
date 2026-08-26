"""活跃队友注册表:线程安全地登记正在运行的队友线程。

被 TeammateSystem(登记/注销)与 Memories(注入上下文)共用。
"""
import threading


class ActiveTeammates:
    """活跃队友注册表(线程安全)。"""

    def __init__(self):
        self._teammates: dict[str, bool] = {}
        self._lock = threading.Lock()

    def add(self, name: str):
        with self._lock:
            self._teammates[name] = True

    def remove(self, name: str):
        with self._lock:
            self._teammates.pop(name, None)

    def contains(self, name: str) -> bool:
        with self._lock:
            return name in self._teammates

    def names(self) -> list[str]:
        with self._lock:
            return list(self._teammates.keys())


# 全局单例。
ACTIVE_TEAMMATES = ActiveTeammates()
