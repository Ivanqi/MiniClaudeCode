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
        stop_reason = getattr(state, "stop_reason", "")
        if state.started_local is None and not error and not stop_reason:
            return None  # 没有任何路由决策(例如空输入),不记录
        if error:
            outcome = "error"
        elif stop_reason == "loop_stopped":
            outcome = "loop_stopped"
        elif state.escalation_reason:
            outcome = "escalated"
        elif state.started_local:
            outcome = "local-ok"
        else:
            outcome = "api"
        record = {
            "messages": redact_obj(messages),
            "metadata": {
                "outcome": outcome, # local-ok/escalated/api/error
                "turn_id": getattr(state, "turn_id", ""), # 唯一编号
                "caller": getattr(state, "caller", "main"),
                "started_local": bool(state.started_local),
                "escalation_reason": state.escalation_reason,
                # 错误信息，但只截前 200 字符,错误堆栈可能几 KB，全存进去档案会臃肿，前 200 字符足够定位问题。
                "error": str(error)[:200],
                "parse_failures": state.parse_failures,
                "tool_errors": state.tool_error_count, # 工具调用报错次数
                "budget": getattr(state, "budget_snapshot", {}), # 这次接待花了多少 token、时间
                # 循环信号，只留最近 20 条（[-20:]）
                # 一个死循环可能产生几千条重复信号，全存没意义，最近 20 条足够看出“它在反复干什么”。
                "loop_signals": list(getattr(state, "loop_signals", []))[-20:], 
                "loop_signal_counts": dict(
                    getattr(state, "loop_signal_counts", {})),
                # 失败尝试，只留最近 10 条
                "failed_attempts": list(
                    getattr(state, "failed_attempts", []))[-10:],
                # 工具调用事件，只留最近 50 条
                "tool_events": list(getattr(state, "tool_events", []))[-50:],
                "loop_stop_reason": getattr(state, "loop_stop_reason", ""),
            },
        }
        try:
            # 归档入库
            # 确认档案柜目录存在，并且权限是私有的（只有当前用户能读写）
            # 训练数据可能含业务对话，不能让同机器其他用户随便翻
            ensure_private_dir(self.directory)
            # 当前秒级时间戳
            stamp = f"{int(time.time())}"
            # 从流水号生成器拿下一个号，比如 1
            seq = next(self._counter)

            path = self.directory / (
                f"turn_{stamp}_{seq:04d}_{uuid.uuid4().hex[:8]}.jsonl")

            # 跨进程文件锁
            with file_lock(self.directory / ".training.lock"):
                atomic_write_text(
                    path, json.dumps(record, default=str,
                                     ensure_ascii=False) + "\n")
            Terminal.info("trajectory",
                          f"{outcome} → {path.name}", Terminal.MAGENTA)
            return str(path)
        except Exception as error: # 兜底
            Terminal.info("trajectory", f"log failed: {error}", Terminal.RED)
            return None
