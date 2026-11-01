"""兼容入口:TaskSystem 实现已拆分到 src/task_system/。

保留这个模块是为了不改变 `from src.TaskSystem import ...` 的既有调用。
各功能模块:
- models.py    —— Task/CreateResult 与去重指纹
- storage.py   —— 文件读写、列表、看板、创建去重
- lifecycle.py —— 认领/完成/失败/释放与租约回收
- steps.py     —— Plan step 任务状态流转
- tools.py     —— lead 侧工具输出与 Schema
"""
from src.task_system import (TASK_SYSTEM, CreateResult, Task, TaskSystem,
                             canonical_fingerprint, normalize_targets,
                             token_set)

__all__ = [
    "CreateResult",
    "TASK_SYSTEM",
    "Task",
    "TaskSystem",
    "canonical_fingerprint",
    "normalize_targets",
    "token_set",
]
