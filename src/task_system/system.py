"""TaskSystem facade:组合存储、生命周期、计划步骤与工具四类能力。"""
from src.task_system.lifecycle import TaskLifecycleMixin
from src.task_system.steps import TaskPlanStepMixin
from src.task_system.storage import TaskStorageMixin
from src.task_system.tools import TaskToolsMixin


class TaskSystem(TaskStorageMixin, TaskLifecycleMixin, TaskPlanStepMixin,
                 TaskToolsMixin):
    """任务系统:负责任务文件读写与状态流转。

    mixin 分层:
    - storage   文件读写、列表、看板、创建去重;
    - lifecycle 认领/完成/失败/释放/租约回收;
    - steps     计划步骤专用状态流转;
    - tools     lead 侧工具文本输出与 Schema。
    """


# 全局单例。
TASK_SYSTEM = TaskSystem()
