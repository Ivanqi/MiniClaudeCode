"""团队级预算与指标:spawn/claim/token/wall 预算,以及可持久化计数。

- 默认预算全部为 0(不限制),避免影响单 Agent/旧测试;
- 构造不做 I/O;flush() 才创建 .team/metrics.json;
- 指标是报告快照,不是跨重启的预算权威(TeamBudget 只在进程内生效)。
"""
import json
import threading
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from src.Configs import Configs
from src.FileLock import atomic_write_text, ensure_private_dir, file_lock
from src.Terminal import Terminal


def _usage_tokens(usage) -> int:
    if not isinstance(usage, dict):
        return 0
    total = usage.get("total_tokens")
    if total is None:
        total = ((usage.get("prompt_tokens") or 0)
                 + (usage.get("completion_tokens") or 0))
    try:
        return max(0, int(total or 0))
    except (TypeError, ValueError):
        return 0


@dataclass
class TeamBudget:
    max_tokens: int = 0
    max_spawns: int = 0
    max_tasks: int = 0
    max_wall_seconds: int = 0
    started_at: float | None = None
    tokens: int = 0
    spawns: int = 0
    tasks: int = 0

    @classmethod
    def from_configs(cls) -> "TeamBudget":
        return cls(
            max_tokens=Configs.TEAM_MAX_TOKENS,
            max_spawns=Configs.TEAM_MAX_SPAWNS,
            max_tasks=Configs.TEAM_MAX_TASKS,
            max_wall_seconds=Configs.TEAM_MAX_WALL_SECONDS,
        )

    def begin(self, now: float):
        if self.started_at is None:
            self.started_at = now

    def wall_exceeded(self, now: float) -> bool:
        if not self.max_wall_seconds or self.started_at is None:
            return False
        return now - self.started_at >= self.max_wall_seconds

    def exhausted_reason(self, now: float) -> str:
        if self.max_tokens and self.tokens >= self.max_tokens:
            return f"team token budget exhausted ({self.tokens}/{self.max_tokens})"
        if self.max_spawns and self.spawns >= self.max_spawns:
            return f"team spawn budget exhausted ({self.spawns}/{self.max_spawns})"
        if self.max_tasks and self.tasks >= self.max_tasks:
            return f"team task budget exhausted ({self.tasks}/{self.max_tasks})"
        if self.wall_exceeded(now):
            return f"team wall budget exhausted ({self.max_wall_seconds}s)"
        return ""

    def snapshot(self, now: float) -> dict:
        return {
            "max_tokens": self.max_tokens,
            "max_spawns": self.max_spawns,
            "max_tasks": self.max_tasks,
            "max_wall_seconds": self.max_wall_seconds,
            "tokens": self.tokens,
            "spawns": self.spawns,
            "tasks": self.tasks,
            "elapsed_s": (round(now - self.started_at, 3)
                          if self.started_at is not None else 0.0),
        }


class TeamMetrics:
    """线程安全的团队计数器与预算。"""

    def __init__(self, budget: TeamBudget | None = None, clock=None):
        self.budget = budget or TeamBudget.from_configs()
        self._counters: dict[str, int] = defaultdict(int)
        self._lock = threading.RLock()
        self._clock = clock or time.monotonic
        self.session_id = uuid.uuid4().hex[:12]

    # ── 计数 ──

    def record(self, event: str, value: int = 1, **fields):
        with self._lock:
            self._counters[event] += int(value)
            for key, count in fields.items():
                if isinstance(count, bool):
                    continue
                if key.startswith("count_"):
                    self._counters[f"{event}.{key[6:]}"] += int(count)

    def add_usage(self, usage, source: str = ""):
        tokens = _usage_tokens(usage)
        if tokens <= 0:
            return 0
        with self._lock:
            self.budget.begin(self._clock())
            self.budget.tokens += tokens
            self._counters["tokens"] += tokens
            if source:
                self._counters[f"tokens.{source}"] += tokens
        return tokens

    def reserve_spawn(self) -> bool:
        with self._lock:
            now = self._clock()
            self.budget.begin(now)
            if self.budget.exhausted_reason(now):
                return False
            if self.budget.max_spawns and \
                    self.budget.spawns >= self.budget.max_spawns:
                return False
            self.budget.spawns += 1
            return True

    def rollback_spawn(self):
        with self._lock:
            self.budget.spawns = max(0, self.budget.spawns - 1)

    def reserve_task(self) -> bool:
        with self._lock:
            now = self._clock()
            self.budget.begin(now)
            if self.budget.exhausted_reason(now):
                return False
            if self.budget.max_tasks and \
                    self.budget.tasks >= self.budget.max_tasks:
                return False
            self.budget.tasks += 1
            return True

    def rollback_task(self):
        with self._lock:
            self.budget.tasks = max(0, self.budget.tasks - 1)

    def can_spawn(self) -> tuple[bool, str]:
        with self._lock:
            now = self._clock()
            reason = self.budget.exhausted_reason(now)
            return (not reason, reason)

    def can_claim(self) -> tuple[bool, str]:
        with self._lock:
            now = self._clock()
            reason = self.budget.exhausted_reason(now)
            if reason:
                return False, reason
            if self.budget.max_tasks and \
                    self.budget.tasks >= self.budget.max_tasks:
                return False, "team task budget exhausted"
            return True, ""

    # ── 快照/持久化 ──

    def snapshot(self, extra: dict | None = None) -> dict:
        with self._lock:
            data = {
                "session_id": self.session_id,
                "counters": dict(sorted(self._counters.items())),
                "budget": self.budget.snapshot(self._clock()),
            }
        if extra:
            data.update(extra)
        return data

    def reset(self):
        with self._lock:
            self._counters.clear()
            self.budget = TeamBudget.from_configs()
            self.session_id = uuid.uuid4().hex[:12]

    def flush(self, path: Path | None = None) -> Path | None:
        try:
            target = Path(path) if path is not None else Configs.team_metrics_path()
            ensure_private_dir(target.parent)
            payload = json.dumps(self.snapshot(), ensure_ascii=False, indent=2)
            with file_lock(target.parent / ".metrics.lock"):
                atomic_write_text(target, payload)
            return target
        except Exception as exc:
            Terminal.info("metrics", f"flush failed: {exc}", Terminal.RED)
            return None

    def status_text(self, active_teammates=None, tasks=None,
                    protocol=None) -> str:
        snapshot = self.snapshot()
        lines = [f"Team session {snapshot['session_id']}",
                 "Budget: " + json.dumps(snapshot["budget"], ensure_ascii=False)]
        counters = snapshot["counters"]
        if counters:
            lines.append("Metrics:")
            for key in sorted(counters):
                lines.append(f"  {key}: {counters[key]}")
        else:
            lines.append("Metrics: (none)")
        if active_teammates is not None:
            lines.append("Active teammates: "
                         + (", ".join(active_teammates.names()) or "(none)"))
        if tasks is not None:
            try:
                board = tasks.board_summary()
                lines.append("Task board:\n" + board)
            except Exception:
                pass
        if protocol is not None:
            try:
                lines.append(f"Protocol pending: {protocol.pending_count()}")
            except Exception:
                pass
        return "\n".join(lines)


# 默认实例:预算默认 0(不限制),构造无 I/O。
TEAM_METRICS = TeamMetrics()
