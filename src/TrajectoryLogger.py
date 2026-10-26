"""轨迹收集器(Phase 3):把每个 turn 的对话与路由结果沉淀为微调训练数据。

每个 run_cycle 结束(成功收尾/升级/出错)落一条 JSONL 记录,
格式与 ShareGPT 对齐(字段名 messages),后续 Llama-Factory / Unsloth
可直接读取。按 outcome 区分样本类型:

- local-ok: 整 turn 由本地小模型完成 → 小模型正样本;
- escalated: 先 local 后升级 API → 困难样本(带升级原因,用于困难例挖掘);
- api:      直接走 API 的复杂任务 → 强模型示范,小模型 SFT 模仿目标;
- error:    异常终止 → 诊断用。

开关:COLLECT_TRAINING_DATA=1(默认关闭,避免正常使用产生磁盘垃圾)。
真正的 QLoRA 训练在 24GB 机器上执行,本模块只负责持续产出数据。
"""
import itertools
import json
import time
import uuid

from src.Configs import Configs
from src.FileLock import atomic_write_text, ensure_private_dir, file_lock
from src.SecurityPolicy import redact_obj
from src.Terminal import Terminal


class TrajectoryLogger:
    """把 run_cycle 的对话轨迹写入 .training_data/ 目录。"""

    def __init__(self, directory=None, enabled: bool | None = None):
        self.directory = directory or Configs.training_data_dir()
        self.enabled = (enabled if enabled is not None
                        else Configs.COLLECT_TRAINING_DATA)
        self._counter = itertools.count(1)  # 文件名去重:同一秒内多条不互相覆盖

    def log_turn(self, messages: list, state, error: str = "") -> str | None:
        """记录一个 turn。outcome 由 state 推导,返回文件路径或 None。"""
        if not self.enabled:
            return None
        if state.started_local is None and not error:
            return None  # 没有任何路由决策(例如空输入),不记录
        if error:
            outcome = "error"
        elif state.escalation_reason:
            outcome = "escalated"
        elif state.started_local:
            outcome = "local-ok"
        else:
            outcome = "api"
        record = {
            "messages": redact_obj(messages),
            "metadata": {
                "outcome": outcome,
                "started_local": bool(state.started_local),
                "escalation_reason": state.escalation_reason,
                "error": str(error)[:200],
                "parse_failures": state.parse_failures,
                "tool_errors": state.tool_error_count,
            },
        }
        try:
            ensure_private_dir(self.directory)
            stamp = f"{int(time.time())}"
            seq = next(self._counter)
            path = self.directory / (
                f"turn_{stamp}_{seq:04d}_{uuid.uuid4().hex[:8]}.jsonl")
            with file_lock(self.directory / ".training.lock"):
                atomic_write_text(
                    path, json.dumps(record, default=str,
                                     ensure_ascii=False) + "\n")
            Terminal.info("trajectory",
                          f"{outcome} → {path.name}", Terminal.MAGENTA)
            return str(path)
        except Exception as error:
            Terminal.info("trajectory", f"log failed: {error}", Terminal.RED)
            return None
