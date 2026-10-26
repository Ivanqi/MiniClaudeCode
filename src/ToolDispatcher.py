"""统一工具分发:所有 agent 循环的 authorize → execute 唯一收口。

- authorize:策略白名单、只读、路径/命令分析、MCP 元数据、逐次确认、PreToolUse;
- execute:内置文件/bash 走协议级执行(不经过各系统的闭包 handler),
  其他工具统一异常捕获;写前备份、输出脱敏、PostToolUse、审计;
- execute_pre_authorized:后台线程使用,校验指纹与策略快照,绝不弹确认。
"""
import difflib
import json
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from src.ApprovalGate import ApprovalGate, ApprovalRequest
from src.AuditLog import AuditLog
from src.BasicOper import BASIC_OPER, BasicOper
from src.FileLock import ensure_private_dir, file_lock
from src.Sandbox import Sandbox, SandboxUnavailable
from src.SecurityPolicy import (CONFIRM, DENY, analyze_bash, child_env,
                                classify_path, redact)
from src.ToolPolicy import (BASH_DENY, BASH_READ_ONLY, ToolPolicy, ToolResult,
                            canonical_args)
from src.Terminal import Terminal

BUILTIN_FILE_TOOLS = frozenset({"bash", "read_file", "write_file",
                                "edit_file", "glob"})
WRITE_TOOLS = frozenset({"write_file", "edit_file"})
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9_-]")


@dataclass
class Authorization:
    """一次已完成的授权决策;可安全传给后台线程。"""

    tool: str
    fingerprint: str
    allowed: bool
    denial: ToolResult | None = None
    reason: str = ""
    needs_network: bool = False
    root: str = ""
    writable_roots: tuple[str, ...] = ()
    policy_snapshot: dict = field(default_factory=dict)
    call_id: str = ""


class ToolDispatcher:
    """策略 + 确认 + 执行 + 审计的统一入口。"""

    def __init__(self, basic: BasicOper | None = None, hooks=None,
                 audit: AuditLog | None = None,
                 approval: ApprovalGate | None = None,
                 sandbox: Sandbox | None = None):
        self.basic = basic or BASIC_OPER
        self.hooks = hooks
        self.audit = audit or AuditLog()
        self.approval = approval or ApprovalGate()
        self.sandbox = sandbox or Sandbox()

    # ── 授权 ──

    @staticmethod
    def fingerprint(name: str, arguments: dict) -> str:
        return f"{name}\0{canonical_args(arguments)}"

    def _deny(self, tool: str, reason: str, agent: str, arguments: dict,
              turn_id: str = "", bg_id: str = "",
              content: str | None = None) -> Authorization:
        result = ToolResult.denied_result(reason)
        if content is not None:
            result.content = content
            result.error = reason
        self.audit.tool_decision(
            agent=agent, tool=tool, arguments=arguments, decision="deny",
            reason=reason, turn_id=turn_id, bg_id=bg_id)
        return Authorization(
            tool=tool, fingerprint=self.fingerprint(tool, arguments),
            allowed=False, denial=result, reason=reason,
            call_id="")

    def authorize(self, call, policy: ToolPolicy, handlers: dict,
                  metadata: dict | None = None, allow_prompt: bool = True,
                  turn_id: str = "", bg_id: str = "",
                  agent: str | None = None) -> Authorization:
        """做完整策略与确认;返回的 Authorization 可在主线程/后台复用。"""
        name = call.name
        arguments = dict(call.arguments or {})
        agent = agent or policy.role
        fingerprint = self.fingerprint(name, arguments)

        allowed, reason = policy.allows_tool(name)
        if not allowed:
            return self._deny(name, reason, agent, arguments, turn_id, bg_id)
        if name not in handlers and name not in BUILTIN_FILE_TOOLS:
            return self._deny(
                name, f"unknown tool '{name}'", agent, arguments,
                turn_id, bg_id, content=f"Unknown: {name}")

        needs_confirm = False
        confirm_reasons = []
        needs_network = False
        control_plane_write = False
        root = policy.root()

        if name in WRITE_TOOLS:
            raw_path = arguments.get("path")
            decision = classify_path(raw_path, root, write=True)
            if decision.verdict == DENY:
                return self._deny(name, decision.reason, agent, arguments,
                                  turn_id, bg_id)
            if not policy.path_is_writable(self._resolve(raw_path, root)):
                return self._deny(
                    name,
                    f"{policy.role} cannot write '{raw_path}'"
                    + ("" if policy.writable_roots else
                       "; teammate has no worktree assigned"),
                    agent, arguments, turn_id, bg_id)
            if decision.verdict == CONFIRM:
                needs_confirm = True
                control_plane_write = True
                confirm_reasons.append(decision.reason)
        elif name == "read_file":
            raw_path = arguments.get("path")
            decision = classify_path(raw_path, root, write=False)
            if decision.verdict == DENY:
                return self._deny(name, decision.reason, agent, arguments,
                                  turn_id, bg_id)
        elif name == "bash":
            if policy.bash_mode == BASH_DENY:
                return self._deny(name, f"bash is not allowed for {policy.role}",
                                  agent, arguments, turn_id, bg_id)
            command = arguments.get("command")
            if not isinstance(command, str) or not command.strip():
                return self._deny(name, "missing command", agent, arguments,
                                  turn_id, bg_id)
            decision = analyze_bash(
                command, root,
                restrict_paths=policy.bash_mode == BASH_READ_ONLY)
            if decision.verdict == DENY:
                return self._deny(name, decision.reason, agent, arguments,
                                  turn_id, bg_id)
            if policy.bash_mode == BASH_READ_ONLY and not decision.read_only:
                return self._deny(
                    name,
                    "teammate without a worktree may only run read-only bash",
                    agent, arguments, turn_id, bg_id)
            if decision.verdict == CONFIRM:
                needs_confirm = True
                confirm_reasons.append(decision.reason)
            needs_network = decision.needs_network

        mcp_confirm, mcp_reason = policy.needs_confirmation(
            name, arguments, metadata)
        if mcp_confirm:
            needs_confirm = True
            confirm_reasons.append(mcp_reason)
        if needs_confirm:
            reason_text = "; ".join(dict.fromkeys(
                item for item in confirm_reasons if item))
            if not allow_prompt:
                return self._deny(
                    name, f"{reason_text} (confirmation unavailable)",
                    agent, arguments, turn_id, bg_id)
            diff = (self._preview_diff(name, arguments, root)
                    if control_plane_write else "")
            request = ApprovalRequest(
                tool=name,
                summary=self._approval_summary(name, arguments),
                reason=reason_text,
                details=json.dumps(arguments, ensure_ascii=False,
                                   default=str)[:1500],
                diff=diff)
            if not self.approval.confirm(request):
                return self._deny(
                    name, f"{reason_text} (approval not granted)",
                    agent, arguments, turn_id, bg_id)

        if self.hooks is not None:
            blocked = self.hooks.trigger("PreToolUse", call)
            if blocked:
                return self._deny(name, str(blocked), agent, arguments,
                                  turn_id, bg_id)

        auth = Authorization(
            tool=name, fingerprint=fingerprint, allowed=True,
            needs_network=needs_network,
            root=str(root.resolve()),
            writable_roots=tuple(str(Path(item).resolve())
                                 for item in policy.writable_roots),
            policy_snapshot=policy.snapshot(),
            call_id=getattr(call, "id", "") or uuid.uuid4().hex[:8])
        self.audit.tool_decision(
            agent=agent, tool=name, arguments=arguments,
            decision="confirm" if needs_confirm else "allow",
            reason="; ".join(dict.fromkeys(
                item for item in confirm_reasons if item)),
            turn_id=turn_id, bg_id=bg_id)
        return auth

    # ── 执行 ──

    def execute(self, call, policy: ToolPolicy, handlers: dict,
                authorization: Authorization | None = None,
                allow_prompt: bool = True, turn_id: str = "",
                bg_id: str = "", cancel_event=None,
                metadata: dict | None = None) -> ToolResult:
        name = call.name
        was_pre_authorized = authorization is not None
        if authorization is None:
            authorization = self.authorize(
                call, policy, handlers, metadata=metadata,
                allow_prompt=allow_prompt, turn_id=turn_id, bg_id=bg_id)
        if not authorization.allowed:
            return authorization.denial or ToolResult.denied_result(
                authorization.reason)
        # 后台/复用授权:校验参数、策略根目录没有被换掉
        mismatch = self._authorization_mismatch(
            call, policy, authorization)
        if mismatch:
            result = ToolResult.denied_result(mismatch)
            self.audit.tool_result(
                agent=policy.role, tool=name, result=result, duration_ms=0,
                turn_id=turn_id, bg_id=bg_id, pre_authorized=True)
            return result

        started = time.perf_counter()
        try:
            result = self._execute_checked(
                call, policy, handlers, authorization, cancel_event)
        except Exception as exc:  # 分发器是最后一道异常边界
            Terminal.info("tool-error", f"{name}: {type(exc).__name__}: "
                          f"{redact(str(exc))}", Terminal.RED)
            result = ToolResult.failure(
                f"Error: {type(exc).__name__}: {exc}", error=str(exc))
        result.content = redact(result.content)
        if result.error:
            result.error = redact(result.error)
        duration_ms = int((time.perf_counter() - started) * 1000)

        if self.hooks is not None:
            try:
                self.hooks.trigger("PostToolUse", call, result.content)
            except Exception:
                pass
        self.audit.tool_result(
            agent=policy.role, tool=name, result=result,
            duration_ms=duration_ms, turn_id=turn_id, bg_id=bg_id,
            pre_authorized=was_pre_authorized)
        return result

    def execute_pre_authorized(self, call, policy: ToolPolicy, handlers: dict,
                               authorization: Authorization,
                               turn_id: str = "", bg_id: str = "",
                               cancel_event=None,
                               metadata: dict | None = None) -> ToolResult:
        """后台线程入口:绝不弹确认,只校验指纹与策略快照。"""
        return self.execute(
            call, policy, handlers, authorization=authorization,
            allow_prompt=False, turn_id=turn_id, bg_id=bg_id,
            cancel_event=cancel_event, metadata=metadata)

    def _authorization_mismatch(self, call, policy: ToolPolicy,
                                authorization: Authorization) -> str:
        if authorization.fingerprint != self.fingerprint(
                call.name, dict(call.arguments or {})):
            return "authorization fingerprint changed"
        if authorization.root != str(policy.root().resolve()):
            return "workspace root changed after authorization"
        current_roots = tuple(str(Path(item).resolve())
                              for item in policy.writable_roots)
        if current_roots != authorization.writable_roots:
            return "writable roots changed after authorization"
        allowed, reason = policy.allows_tool(call.name)
        if not allowed:
            return reason
        arguments = dict(call.arguments or {})
        if call.name == "bash":
            decision = analyze_bash(
                arguments.get("command", ""), policy.root(),
                restrict_paths=policy.bash_mode == BASH_READ_ONLY)
            if decision.verdict == DENY:
                return decision.reason
            if policy.bash_mode == BASH_READ_ONLY and not decision.read_only:
                return "read-only bash policy changed"
        elif call.name in WRITE_TOOLS:
            decision = classify_path(arguments.get("path"), policy.root(),
                                     write=True)
            if decision.verdict == DENY:
                return decision.reason
        return ""

    def _execute_checked(self, call, policy: ToolPolicy, handlers: dict,
                         authorization: Authorization,
                         cancel_event) -> ToolResult:
        name = call.name
        arguments = dict(call.arguments or {})
        if name == "bash":
            return self._run_bash(arguments, policy, authorization,
                                  cancel_event)
        if name in WRITE_TOOLS:
            return self._run_write(name, call, arguments, policy)
        if name == "read_file":
            text = self.basic.run_read(
                arguments.get("path", ""), limit=arguments.get("limit"),
                offset=arguments.get("offset", 0), cwd=policy.root())
            return ToolResult(content=text, ok=not text.startswith("Error:"),
                              error=None if not text.startswith("Error:")
                              else text)
        if name == "glob":
            text = self.basic.run_glob(
                arguments.get("pattern", ""), cwd=policy.root())
            return ToolResult(content=text, ok=not text.startswith("Error:"),
                              error=None if not text.startswith("Error:")
                              else text)

        handler = handlers.get(name)
        if handler is None:
            return ToolResult.failure(f"Unknown: {name}",
                                      error=f"unknown tool '{name}'")
        try:
            raw = handler(**arguments)
        except Exception as exc:
            Terminal.info("tool-error", f"{name}: {type(exc).__name__}: "
                          f"{redact(str(exc))}", Terminal.RED)
            return ToolResult.failure(
                f"Error: {type(exc).__name__}: {exc}", error=str(exc))
        return self._normalize_result(raw)

    @staticmethod
    def _normalize_result(raw) -> ToolResult:
        if isinstance(raw, ToolResult):
            return raw
        if raw is None:
            content, ok = "(no output)", True
        elif isinstance(raw, (dict, list)):
            content = json.dumps(raw, ensure_ascii=False, default=str)
            ok = not (isinstance(raw, dict) and raw.get("error"))
        else:
            content = str(raw)
            ok = not content.lstrip().startswith(
                ("Error:", "Unknown:", "MCP error:"))
        return ToolResult(content=content, ok=ok,
                          error=None if ok else content)

    def _run_bash(self, arguments: dict, policy: ToolPolicy,
                  authorization: Authorization,
                  cancel_event) -> ToolResult:
        command = arguments.get("command", "")
        # 队友的完整 bash 依赖沙箱把写操作限制在 worktree 内;
        # auto 模式无 bwrap 时 fail-closed,只有显式 SANDBOX_MODE=off 才放行。
        if (policy.role == "teammate" and policy.bash_mode == "full"
                and self.sandbox.mode != "off"
                and not self.sandbox.sandboxed):
            return ToolResult.denied_result(
                "teammate bash requires an available sandbox")
        writable = () if policy.read_only else tuple(policy.writable_roots)
        try:
            argv = self.sandbox.wrap(
                command, policy.root(), writable,
                network=authorization.needs_network)
        except SandboxUnavailable as exc:
            return ToolResult.failure(f"Error: {exc}")
        text, code, truncated = self.basic.run_bash_process(
            command, cwd=policy.root(),
            timeout=arguments.get("timeout"), env=child_env(),
            argv=argv, cancel_event=cancel_event)
        if code not in (0, None) and not text.startswith("Error:"):
            text = (f"Exit code {code}" if text == "(no output)"
                    else f"Exit code {code}\n{text}")
        return ToolResult(
            content=text, ok=code == 0, exit_code=code,
            truncated=truncated,
            error=None if code == 0 else text)

    def _run_write(self, name: str, call, arguments: dict,
                   policy: ToolPolicy) -> ToolResult:
        backup = self._backup_before_write(call, arguments, policy)
        artifacts = [backup] if backup else []
        if name == "write_file":
            text = self.basic.run_write(
                arguments.get("path", ""), arguments.get("content", ""),
                cwd=policy.root())
        else:
            text = self.basic.run_edit(
                arguments.get("path", ""), arguments.get("old_text", ""),
                arguments.get("new_text", ""), cwd=policy.root())
        ok = not text.startswith("Error:")
        return ToolResult(content=text, ok=ok, artifacts=artifacts,
                          error=None if ok else text)

    # ── 辅助 ──

    @staticmethod
    def _resolve(raw_path, root: Path) -> Path:
        path = Path(raw_path or "")
        if not path.is_absolute():
            path = Path(root) / path
        return path.resolve()

    def _backup_before_write(self, call, arguments: dict,
                             policy: ToolPolicy) -> str | None:
        try:
            root = policy.root().resolve()
            path = self._resolve(arguments.get("path"), root)
            if not path.is_file():
                return None
            raw_id = str(getattr(call, "id", "") or uuid.uuid4().hex[:8])
            safe_id = _UNSAFE_NAME.sub("_", raw_id)[:64] or uuid.uuid4().hex[:8]
            backup_root = ensure_private_dir(root / ".backups" / safe_id)
            target = backup_root / path.relative_to(root)
            with file_lock(root / ".backups" / ".backups.lock"):
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
            return str(target)
        except (OSError, ValueError):
            return None

    def _approval_summary(self, name: str, arguments: dict) -> str:
        if name == "bash":
            return f"run: {arguments.get('command', '')[:200]}"
        if name in WRITE_TOOLS:
            return f"write {arguments.get('path', '')}"
        return f"call {name}"

    def _preview_diff(self, name: str, arguments: dict, root: Path) -> str:
        """控制面写入的人工确认 diff(限制行数/字符数)。"""
        try:
            path = self._resolve(arguments.get("path"), root)
            old = path.read_text(errors="replace") if path.exists() else ""
            if name == "edit_file":
                old_text = arguments.get("old_text", "")
                new = (old.replace(old_text, arguments.get("new_text", ""), 1)
                       if old_text and old_text in old else old)
            else:
                new = arguments.get("content", "")
            diff = list(difflib.unified_diff(
                old.splitlines(), new.splitlines(),
                fromfile=str(arguments.get("path")),
                tofile=str(arguments.get("path")), lineterm=""))
            return "\n".join(diff[:200])[:6000]
        except (OSError, ValueError):
            return ""
