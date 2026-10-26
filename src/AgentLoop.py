"""兼容入口:AgentLoop 实现已拆分到 src/agent_loop/。

保留这个模块是为了不改变 `from src.AgentLoop import AgentLoop` 的既有调用。
"""
from src.agent_loop import AgentLoop

__all__ = ["AgentLoop"]
