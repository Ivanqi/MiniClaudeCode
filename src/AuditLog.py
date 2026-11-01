"""常开工具审计:JSONL 落盘、参数脱敏、按大小轮转。

审计是安全可追溯性的一部分,与训练轨迹(TrajectoryLogger)分开:
- 默认开启,不依赖 COLLECT_TRAINING_DATA;
- 写文件正文(content/old_text/new_text)只记哈希与长度,不落内容;
- 所有字符串经过 SecurityPolicy.redact;
- 审计失败只告警,绝不阻断工具执行。
"""
import hashlib
import json
import os
import time
import uuid
from pathlib import Path

from src.Configs import Configs
from src.FileLock import ensure_private_dir, file_lock
from src.SecurityPolicy import redact
from src.Terminal import Terminal

# 这些参数不落正文,只记哈希/长度
_BODY_KEYS = {"content", "old_text", "new_text", "plan", "prompt",
              "description", "feedback"}
_MAX_FIELD_CHARS = 800

'''
计算输入值的 SHA256 哈希，并截取前 16 个字符。
    1. 数据流：dict/list 先被 json.dumps 转为字符串，然后进行 UTF-8 编码（errors="replace" 防止特殊字符导致编码崩溃），最后哈希。
    2. 目的：对于敏感正文，我们不需要知道它是什么，只需要知道两次调用的内容是否一致（通过比对哈希值），或者排查是否存在特定的数据。
'''
def _digest(value) -> str:
    text = value if isinstance(value, str) else json.dumps(
        value, ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]

'''
对工具调用的参数进行摘要化处理。
    1. 敏感字段处理：如果 key 在 _BODY_KEYS 中，返回 {"sha256": "a1b2...", "chars": 1024}。仅记录长度和哈希。
    2. 复杂类型处理：如果是 dict/list，先序列化为 JSON 字符串，截断至 800 字符，然后调用 redact()（来自外部安全模块，通常用于抹除身份证号、手机号、API Key 等正则匹配的 PII）。
    3. 字符串处理：直接截断至 800 字符，然后 redact()。
    4. 基础类型：直接保留原值（如 int、bool）。
'''
def summarize_arguments(arguments: dict | None) -> dict:
    """审计用参数摘要:敏感正文只留指纹,其余截断并脱敏。"""
    summary = {}
    for key, value in (arguments or {}).items():
        if key in _BODY_KEYS:
            text = value if isinstance(value, str) else str(value)
            summary[key] = {"sha256": _digest(value), "chars": len(text)}
            continue
        if isinstance(value, (dict, list)):
            text = json.dumps(value, ensure_ascii=False, default=str)
            summary[key] = redact(text[:_MAX_FIELD_CHARS])
        elif isinstance(value, str):
            summary[key] = redact(value[:_MAX_FIELD_CHARS])
        else:
            summary[key] = value
    return summary

'''
这段代码实现了一个生产级的、非阻塞的、线程/进程安全的审计日志系统。

它专门用于记录工具（Tool）的调用决策和结果，其核心工程挑战在于
- 如何在不泄露敏感数据、不影响主流程性能、且磁盘空间有限的前提下，持久化高并发的审计事件

核心设计原则
- Fail-Safe（失败导向安全）：try/except 包住了写盘逻辑，审计失败绝不阻断主工具运行。
- 脱敏隔离：正文只留 Hash，外围字符串截断并正则脱敏（redact），双重保障。
- 跨进程一致性：依赖 file_lock 处理轮转和追加，避免多进程并发写乱序或把文件写烂。
- 零依赖注入：路径和大小限制均支持构造时注入（默认回退到 Configs），方便测试。

'''
class AuditLog:
    """写 JSONL 审计日志;目录与文件惰性创建。"""

    def __init__(self, directory: Path | None = None,
                 max_bytes: int | None = None,
                 backup_count: int | None = None):
        self._directory = Path(directory) if directory else None
        self.max_bytes = max_bytes or Configs.AUDIT_MAX_BYTES
        self.backup_count = (Configs.AUDIT_BACKUP_COUNT
                             if backup_count is None else backup_count)

    def directory(self) -> Path:
        return self._directory or Configs.audit_dir()

    def path(self) -> Path:
        return self.directory() / "tool_calls.jsonl"

    '''
    主写入入口
        1. 数据组装：注入时间戳 ts、唯一的 event_id、事件类型 event，合并传入的业务字段 fields。
        2. 双重脱敏：虽然 fields 可能在外部已经被处理，但这里在序列化后再次对整个 JSON 字符串调用 redact()，作为兜底安全策略。
        3. 异常吞噬：捕获所有异常（如磁盘满、序列化失败），仅打印红色终端日志，绝不向上抛出。审计系统是旁路功能，不能因为审计写入失败导致主业务（工具执行）中断。
    '''
    def log(self, event: str, **fields):
        """追加一条审计事件;任何异常都只告警。"""
        record = {
            "ts": time.time(),
            "event_id": uuid.uuid4().hex[:12],
            "event": event,
        }
        record.update(fields)
        try:
            line = redact(json.dumps(record, ensure_ascii=False, default=str))
            self._append(line + "\n")
        except Exception as exc:  # 审计不能影响主流程
            Terminal.info("audit", f"write failed: {exc}", Terminal.RED)
        return record

    '''
    tool_decision 与 tool_result：业务语义封装
        1. tool_decision：记录工具被调用时的元数据（谁调用的、调用了啥、参数摘要、决策结果如 allow/deny）。
        2. tool_result：记录工具执行结果。使用 getattr(result, 'content', str(result)) 兼容不同类型的结果对象。提取 ok、denied、error_code 等结构化属性
        3. 对于结果正文，只记录 output_sha256、output_chars 以及前 300 字符的脱敏摘要 output_excerpt。
    '''
    def tool_decision(self, *, agent: str, tool: str, arguments: dict,
                      decision: str, reason: str = "", turn_id: str = "",
                      bg_id: str = ""):
        return self.log(
            "tool_decision", agent=agent, tool=tool,
            arguments=summarize_arguments(arguments), decision=decision,
            reason=reason, turn_id=turn_id, bg_id=bg_id)

    def tool_result(self, *, agent: str, tool: str, result, duration_ms: int,
                    turn_id: str = "", bg_id: str = "",
                    pre_authorized: bool = False):
        content = getattr(result, "content", str(result))
        return self.log(
            "tool_result", agent=agent, tool=tool,
            ok=bool(getattr(result, "ok", True)),
            denied=bool(getattr(result, "denied", False)),
            error_code=str(getattr(result, "error_code", "") or ""),
            exit_code=getattr(result, "exit_code", None),
            truncated=bool(getattr(result, "truncated", False)),
            duration_ms=duration_ms,
            output_sha256=_digest(content),
            output_chars=len(content),
            output_excerpt=redact(content[:300]),
            artifacts=list(getattr(result, "artifacts", []) or []),
            turn_id=turn_id, bg_id=bg_id,
            pre_authorized=pre_authorized)

    '''
    写入与轮转
        1.ensure_private_dir：确保目录存在且权限为 0700（仅当前用户可读写）。
        2. file_lock：获取跨进程的文件锁（通常基于 fcntl 或 msvcrt）。因为可能多个进程/线程同时写审计日志，必须在轮转判断和写入期间互斥。
        3. os.fsync：调用 flush() 将 Python 缓冲区数据推给 OS，再调用 fsync(fileno()) 强制操作系统将数据刷入物理磁盘。审计日志必须防掉电丢失，哪怕牺牲一点 I/O 性能。
    '''
    def _append(self, line: str):
        directory = ensure_private_dir(self.directory())
        path = directory / "tool_calls.jsonl"
        with file_lock(directory / ".audit.lock"):
            self._rotate(path, len(line.encode("utf-8")))
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())

    '''
    文件轮转
        1. 触发条件：当前文件大小 + 新行字节数 > max_bytes。
        2. 如果 backup_count == 0：直接 path.unlink() 删除当前文件，相当于不保留历史。
        3. 如果 backup_count > 0（倒序重命名）：
            1. 假设 backup_count = 3，当前状态：tool_calls.jsonl, .1, .2, .3。
            2. index 从 2 迭代到 1：
                1. index=2：把 .2 重命名为 .3（覆盖旧的 .3）。
                2. index=1：把 .1 重命名为 .2。
            3. 循环结束后，把主文件 tool_calls.jsonl 重命名为 .1。
            4. 删除超限的 .4 文件（self.backup_count + 1）。
    '''
    def _rotate(self, path: Path, incoming_bytes: int):
        if not path.exists():
            return
        if path.stat().st_size + incoming_bytes <= self.max_bytes:
            return
        if self.backup_count <= 0:
            path.unlink(missing_ok=True)
            return
        # ... 处理 backup_count <= 0 的情况 ...
        for index in range(self.backup_count - 1, 0, -1):
            source = path.with_name(f"{path.name}.{index}")
            target = path.with_name(f"{path.name}.{index + 1}")
            if source.exists():
                os.replace(source, target)
        os.replace(path, path.with_name(f"{path.name}.1"))
        oldest = path.with_name(f"{path.name}.{self.backup_count + 1}")
        oldest.unlink(missing_ok=True)
