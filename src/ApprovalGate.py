"""逐次人工确认:能力开关(enable_writes)之外的授权闸门。

设计要点:
- 只在交互式 TTY + 主线程弹确认;后台/队友/cron 线程一律 fail-closed;
- AUTO_APPROVE_TOOLS 是显式逃生舱(精确名或 prefix*);
- 返回 False 由调用方渲染成 "Denied: ..." 工具结果,不抛异常。
"""
import json
import sys
import threading
from dataclasses import dataclass

from src.Configs import Configs
from src.SecurityPolicy import redact
from src.Terminal import Terminal


@dataclass
class ApprovalRequest:
    tool: str
    summary: str
    reason: str = ""
    details: str = ""
    diff: str = ""
    kind: str = "write"


class ApprovalGate:
    """交互式确认门。"""

    def __init__(self, interactive: bool = True,
                 auto_approve: set[str] | None = None,
                 prompt_func=None, tty_check=None,
                 thread_check=None):
        self.interactive = interactive
        self.auto_approve = set(
            Configs.AUTO_APPROVE_TOOLS if auto_approve is None
            else auto_approve)
        print(self.auto_approve)
        self._prompt = prompt_func or input
        self._tty_check = tty_check or (lambda: sys.stdin.isatty())
        self._thread_check = thread_check or (
            lambda: threading.current_thread() is threading.main_thread())

    def _auto_approved(self, tool: str) -> bool:
        if tool in self.auto_approve:
            return True
        return any(pattern.endswith("*") and tool.startswith(pattern[:-1])
                   for pattern in self.auto_approve)

    def can_prompt(self) -> bool:
        return bool(self.interactive and self._tty_check()
                    and self._thread_check())

    def confirm(self, request: ApprovalRequest) -> bool:
        """返回是否批准;任何无法确认的情况都拒绝。"""
        if self._auto_approved(request.tool):
            return True
        if not self.can_prompt():
            return False
        Terminal.print(f"\n{Terminal.YELLOW}[approval] {request.tool}: "
                       f"{request.summary}{Terminal.RESET}")
        if request.reason:
            Terminal.print(f"  reason: {request.reason}")
        if request.details:
            Terminal.print(f"  {redact(request.details)[:1000]}")
        if request.diff:
            Terminal.print(redact(request.diff)[:4000])
        try:
            choice = str(self._prompt("  Allow? [y/N] ")).strip().lower()
        except (EOFError, KeyboardInterrupt, OSError):
            return False
        return choice in ("y", "yes")

    def confirm_tool(self, tool_name: str, arguments: dict,
                     reason: str = "") -> bool:
        """Catering 等外部业务的确认入口。"""
        details = json.dumps(arguments or {}, ensure_ascii=False,
                             default=str)[:1000]
        return self.confirm(ApprovalRequest(
            tool=tool_name,
            summary=f"arguments: {redact(details)}",
            reason=reason or "tool requires human confirmation"))
