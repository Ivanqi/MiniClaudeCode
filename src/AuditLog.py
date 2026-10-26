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


def _digest(value) -> str:
    text = value if isinstance(value, str) else json.dumps(
        value, ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


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

    def _append(self, line: str):
        directory = ensure_private_dir(self.directory())
        path = directory / "tool_calls.jsonl"
        with file_lock(directory / ".audit.lock"):
            self._rotate(path, len(line.encode("utf-8")))
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())

    def _rotate(self, path: Path, incoming_bytes: int):
        if not path.exists():
            return
        if path.stat().st_size + incoming_bytes <= self.max_bytes:
            return
        if self.backup_count <= 0:
            path.unlink(missing_ok=True)
            return
        for index in range(self.backup_count - 1, 0, -1):
            source = path.with_name(f"{path.name}.{index}")
            target = path.with_name(f"{path.name}.{index + 1}")
            if source.exists():
                os.replace(source, target)
        os.replace(path, path.with_name(f"{path.name}.1"))
        oldest = path.with_name(f"{path.name}.{self.backup_count + 1}")
        oldest.unlink(missing_ok=True)
