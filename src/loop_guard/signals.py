"""循环保护信号模型与工具结果归一化。

- ``LoopSignal`` 是检测器对外输出的最小事件,evidence 只放计数/哈希;
- ``ToolObservation`` 把 dispatcher 的 denial/background/同步执行三种结果
  形态归一,供进度账本与各条信号规则共用;
- 工具参数中的正文只保留长度与哈希,其余字段脱敏截断,避免敏感内容
  进入轨迹或负记忆。
"""
import hashlib
import json
from dataclasses import dataclass, field

from src.SecurityPolicy import redact
from src.ToolPolicy import ToolResult, canonical_args, infer_error_code

# 信号严重度排序:resolve() 取最高优先级的一条执行。
SEVERITY_ORDER = {
    "none": 0,
    "nudge": 1,
    "reflect": 2,
    "replan": 3,
    "escalate": 4,
    "stop": 5,
}

# 这些参数是正文内容,摘要里只保留长度与哈希。
_BODY_KEYS = {"content", "old_text", "new_text", "plan", "prompt",
              "description", "feedback"}
_WRITE_TOOLS = {"write_file", "edit_file"}
# 会产生外部副作用的工具;同名同参重复调用不算“新进展”。
_MUTATION_TOOLS = {
    "create_task", "claim_task", "complete_task", "fail_task", "release_task",
    "create_worktree", "remove_worktree", "seal_worktree", "apply_integration",
    "remember", "forget", "create_plan", "execute_plan", "plan_execute",
    "archive_case", "deploy_asset", "register_asset",
    "send_message", "submit_plan", "spawn_teammate",
    "schedule_cron", "cancel_cron", "keep_worktree", "reclaim_tasks",
    "request_shutdown", "request_plan", "review_plan",
}
# 轮询后台任务是合法的等待行为:重复同一状态不应触发空转/无进展。
_WAIT_TOOLS = {"get_background_task"}


def digest_text(text: str) -> str:
    """完整 sha256;用于内容寻址与去重。"""
    return hashlib.sha256(str(text or "").encode("utf-8", "replace")).hexdigest()


def short_digest(text: str) -> str:
    """16 位短哈希;写入 evidence/事件流时足够定位且不冗长。"""
    return digest_text(text)[:16]


def is_wait_observation(obs: "ToolObservation") -> bool:
    """后台轮询仍在 running/queued 时是合法等待;终态轮询回归普通信号。"""
    if obs.tool not in _WAIT_TOOLS:
        return False
    text = str(obs.content_excerpt or "").lower()
    first_line = text.split("\n", 1)[0]
    terminal = ("completed", "failed", "cancelled", "timed_out", "timed out")
    return not any(token in first_line for token in terminal)


def summarize_arguments(arguments: dict | None) -> str:
    """参数摘要:敏感正文只留长度/哈希,其余脱敏并截断。"""
    summary = {}
    for key, value in (arguments or {}).items():
        if key in _BODY_KEYS:
            text = value if isinstance(value, str) else str(value)
            summary[key] = f"<{len(text)} chars sha256:{short_digest(text)}>"
        elif isinstance(value, (dict, list)):
            summary[key] = redact(
                json.dumps(value, ensure_ascii=False, default=str))[:120]
        else:
            summary[key] = redact(str(value))[:120]
    return json.dumps(summary, ensure_ascii=False, default=str)[:240]


@dataclass(frozen=True)
class LoopSignal:
    """一次循环保护信号;evidence 只放计数/哈希,不放原始工具输出。"""

    kind: str
    severity: str
    evidence: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "severity": self.severity,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True)
class ToolObservation:
    """归一化后的工具结果,屏蔽 denial/background/同步三种分支差异。"""

    tool: str
    args_key: str
    content_hash: str
    ok: bool
    error_code: str = ""
    exit_code: int | None = None
    denied: bool = False
    background: bool = False
    artifacts: tuple[str, ...] = ()
    args_summary: str = ""
    content_excerpt: str = ""
    round_index: int = 0

    @classmethod
    def from_result(cls, call, output, result: ToolResult | None = None,
                    ok: bool | None = None, error_code: str | None = None,
                    background: bool = False,
                    round_index: int = 0) -> "ToolObservation":
        """从 ToolCall + 输出 + 可选 ToolResult 构造观察。"""
        content = str(output if output is not None else "")
        arguments = dict(getattr(call, "arguments", {}) or {})
        if result is not None:
            ok = bool(result.ok) if ok is None else bool(ok)
            code = (error_code or result.error_code
                    or ("" if result.ok else infer_error_code(call.name, result)))
            exit_code = result.exit_code
            denied = bool(result.denied)
            artifacts = tuple(result.artifacts or ())
        else:
            ok = bool(ok)
            code = error_code or ("" if ok else "tool_error")
            exit_code = None
            denied = False
            artifacts = ()
        return cls(
            tool=str(getattr(call, "name", "")),
            args_key=canonical_args(arguments),
            content_hash=digest_text(content),
            ok=ok,
            error_code=code or "",
            exit_code=exit_code,
            denied=denied,
            background=background,
            artifacts=artifacts,
            args_summary=summarize_arguments(arguments),
            content_excerpt=redact(content.strip())[:200],
            round_index=round_index,
        )
