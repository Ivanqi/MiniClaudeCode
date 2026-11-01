"""兼容入口:LoopGuard 实现已拆分到 src/loop_guard/。

保留这个模块是为了不改变 `from src.LoopGuard import ...` 的既有调用。
各功能模块:
- signals.py  —— LoopSignal / ToolObservation 与脱敏工具
- ledger.py   —— ProgressLedger 进度账本
- guard.py    —— LoopGuard 空转/重复/错误检测
- circuit.py  —— ToolCircuitBreaker 工具熔断
- watchdog.py —— LoopWatchdog 阶段超时
"""
from src.loop_guard import (SEVERITY_ORDER, LoopGuard, LoopSignal,
                            LoopWatchdog, ProgressLedger,
                            ToolCircuitBreaker, ToolObservation,
                            digest_text, is_wait_observation, short_digest,
                            summarize_arguments)

__all__ = [
    "LoopGuard",
    "LoopSignal",
    "LoopWatchdog",
    "ProgressLedger",
    "SEVERITY_ORDER",
    "ToolCircuitBreaker",
    "ToolObservation",
    "digest_text",
    "is_wait_observation",
    "short_digest",
    "summarize_arguments",
]
