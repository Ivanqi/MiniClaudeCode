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

'''
团队级资源预算与运行指标的记账中心，解决两个问题：
    1. 预算闸门：在 spawn（派生队友）、claim（认领任务）等动作发生前，判断团队是否还有额度——token、spawn 次数、任务数、墙钟时间四种维度。
    2. 指标计数与持久化：线程安全地累计各类事件（protocol_tracked、protocol_expired、token 用量等），并能快照落盘成 metrics.json。

add_usage(usage) ──┐
record(event)   ──┤
reserve_spawn()  ──┼──> TeamMetrics（RLock 保护）──> _counters + budget
reserve_task()   ──┤                                      │
rollback_*()    ──┘                                      ▼
                                                  snapshot() / flush() → .team/metrics.json

四大设计支柱
    1.  即不限制：max_tokens/max_spawns/max_tasks/max_wall_seconds 为 0 时，对应检查直接跳过。这决定了所有 if self.max_xxx 的判断。
    2. 预算检查统一走 exhausted_reason：四种维度的耗尽判断集中在一个方法里，reserve_*、can_*、rollback 都复用它，避免各处判断口径漂移。
    3. "先 begin 再检查"：reserve_spawn/reserve_task/can_* 都先 budget.begin(now) 启动墙钟计时，再检查是否耗尽。begin 自身是幂等的（started_at is None 才写），保证第一次预算检查就是墙钟起点。
    4. 锁纪律 + 可注入时钟：RLock 保护 _counters 和 budget；clock 默认 time.monotonic（单调递增，不受系统时间回拨影响），测试时可注入假时钟。
'''
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
    # 四种预算上限，0 = 不限制
    max_tokens: int = 0
    max_spawns: int = 0
    max_tasks: int = 0
    max_wall_seconds: int = 0
    # 墙钟起点，None 表示团队尚未开始计时
    started_at: float | None = None
    # 已用量
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
        '''
        按固定优先级检查四种预算,返回首个命中原因;全未命中返回空串
        优先级:token > spawn > task > wall。
        token 排第一是因为它通常是最终约束。
        '''
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
        '''
        可序列化快照;elapsed_s 保留毫秒,未开始时为 0.0 保证类型稳定。
        '''
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
        """累计事件计数;`count_` 前缀字段会累加到 `event.<后缀>` 子计数。"""
        with self._lock:
            self._counters[event] += int(value)
            for key, count in fields.items():
                # bool 是 int 子类,必须跳过,否则 True/False 会被当计数累加
                if isinstance(count, bool):
                    continue
                if key.startswith("count_"):
                    self._counters[f"{event}.{key[6:]}"] += int(count)

    '''
    add_usage 记录一次 LLM 调用的 token 用量：
        1. _usage_tokens 解析，<= 0 直接返回 0（不记零值，避免污染计数器）。
        2. 锁内：budget.begin 启动墙钟 → budget.tokens += tokens → 总计数 "tokens" 累加 → 若给了 source，再加 "tokens.{source}" 分来源计数
        3. 返回本次 tokens，便于调用方日志或二次统计。
    
    只记账不拦截
        token 已经花出去，拦不住
        真正的闸门在下一次 reserve_spawn/reserve_task/can_* 时由 exhausted_reason 统一拦截。
        理解这个"先消费后拦截"模型，才能解释为什么 add_usage 没有失败路径。
    '''
    def add_usage(self, usage, source: str = ""):
        """记录一次 LLM 调用的 token 用量;只记账,不做预算拦截。"""
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
        """原子占用一次 spawn 额度;任一预算耗尽则拒绝。"""
        with self._lock:
            now = self._clock()
            # 通用检查:任一维度耗尽都拒绝
            self.budget.begin(now) # 惰性启动计时
            if self.budget.exhausted_reason(now): # 任一预算耗尽 → 拒绝
                return False
            # 显式二次检查 spawn 维度:防御性冗余,意图明确
            if self.budget.max_spawns and \
                    self.budget.spawns >= self.budget.max_spawns:
                return False
            self.budget.spawns += 1
            return True

    def rollback_spawn(self):
        """spawn 成功但派生失败时的补偿;max(0,...) 防负值,可安全重复调用。"""
        with self._lock:
            self.budget.spawns = max(0, self.budget.spawns - 1)

    '''
    与 spawn 完全对称：reserve_task 做 begin → exhausted_reason → 显式 task 检查 → tasks += 1
    rollback_task 同样 max(0, ...) 回退。
    '''
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

    '''
    只读检查，返回 (bool, reason)
    '''
    def can_spawn(self) -> tuple[bool, str]:
        with self._lock:
            now = self._clock()
            reason = self.budget.exhausted_reason(now)
            return (not reason, reason)

    '''
    认领任务的预检，比 can_spawn 多一步：
        1. exhausted_reason 非空 → False。
        2. 再显式检查 max_tasks and tasks >= max_tasks → False，reason 为 "team task budget exhausted"（注意这里没有带数字，与 exhausted_reason 的格式不同）。
    '''
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

    '''
    生成人类可读的状态报告字符串，供 CLI/日志展示
        1. 会话 ID + 预算 JSON。
        2. 指标列表（排序）；为空则 "(none)"。
        3. 可选：活跃队友名（active_teammates.names()）。
        4. 可选：任务板摘要（tasks.board_summary()），包在 try/except 里，失败静默跳过。
        5. 可选：协议挂起数（protocol.pending_count()），同样 try/except。
    '''
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
