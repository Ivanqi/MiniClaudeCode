"""兼容入口:TeammateSystem 实现已拆分到 src/teammate_system/。

保留这个模块是为了不改变 `from src.TeammateSystem import ...` 的既有调用。
各功能模块:
- idle.py   —— 空闲轮询与自动认领
- agent.py  —— spawn、ReAct 循环与退出收尾
- lead.py   —— 关机/计划审批/消息/看板
- tools.py  —— lead 侧工具 Schema 与 handler
- system.py —— TeammateSystem facade
"""
from src.teammate_system import TeammateSystem

__all__ = ["TeammateSystem"]
