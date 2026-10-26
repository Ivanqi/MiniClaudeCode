"""工具策略与预算:统一各 agent 的权限面、单轮预算与结果契约。

ToolPolicy 是纯数据;真正的执行/确认在 ToolDispatcher。
策略工厂按调用时解析 Configs.WORKDIR,便于测试重定向工作目录。
"""
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from src.Configs import Configs

# 与 ToolRegistry.READONLY_TOOL_NAMES 保持一致(本地小模型/只读子代理)
READONLY_TOOL_NAMES = frozenset({
    "read_file", "glob", "list_tasks", "get_task", "list_crons", "load_skill",
    "get_background_task", "recall",
})

SUBAGENT_TOOLS = frozenset({
    "bash", "read_file", "write_file", "edit_file", "glob",
})
SUBAGENT_LOCAL_TOOLS = frozenset({"read_file", "glob"})

TEAMMATE_TOOLS = frozenset({
    "bash", "read_file", "write_file", "send_message", "submit_plan",
    "list_tasks", "claim_task", "complete_task",
})

WRITE_TOOL_NAMES = frozenset({"write_file", "edit_file"})

# 这些工具默认逐次确认;remove_worktree 仅在 discard_changes=true 时确认。
DEFAULT_CONFIRM_TOOLS = frozenset({
    "remove_worktree", "archive_case", "deploy_asset", "register_asset",
    "forget", "seal_worktree", "apply_integration",
})

BASH_FULL = "full"
BASH_READ_ONLY = "read_only"
BASH_DENY = "deny"

# 计划工具只给 lead;步骤 ReAct 与子代理/队友不得递归规划或 spawn。
PLAN_TOOL_NAMES = frozenset({
    "choose_strategy", "create_plan", "execute_plan", "plan_execute",
    "plan_status",
})
PLAN_FORBIDDEN_STEP_TOOLS = PLAN_TOOL_NAMES | {"spawn_teammate"}


@dataclass
class ToolResult:
    """统一工具结果:核心循环只看 ok/exit_code,不再做字符串匹配。"""

    content: str
    ok: bool = True
    denied: bool = False
    exit_code: int | None = None
    truncated: bool = False
    artifacts: list[str] = field(default_factory=list)
    error: str | None = None

    def __str__(self) -> str:
        return self.content

    @classmethod
    def success(cls, content: str, **kwargs) -> "ToolResult":
        return cls(content=content, ok=True, **kwargs)

    @classmethod
    def denied_result(cls, reason: str) -> "ToolResult":
        return cls(content=f"Denied: {reason}", ok=False, denied=True,
                   error=reason)

    @classmethod
    def failure(cls, content: str, error: str | None = None,
                **kwargs) -> "ToolResult":
        return cls(content=content, ok=False, error=error or content, **kwargs)


class TurnBudget:
    """单轮预算:轮数、工具调用数、墙钟、累计 token。"""

    def __init__(self, max_rounds: int | None = None,
                 max_tool_calls: int | None = None,
                 max_wall_s: int | None = None,
                 max_total_tokens: int | None = None):
        self.max_rounds = (Configs.TURN_MAX_ROUNDS
                           if max_rounds is None else max_rounds)
        self.max_tool_calls = (Configs.TURN_MAX_TOOL_CALLS
                               if max_tool_calls is None else max_tool_calls)
        self.max_wall_s = (Configs.TURN_MAX_WALL_SECONDS
                           if max_wall_s is None else max_wall_s)
        self.max_total_tokens = (Configs.TURN_MAX_TOTAL_TOKENS
                                 if max_total_tokens is None
                                 else max_total_tokens)
        self.started_at = time.monotonic()
        self.rounds = 0
        self.tool_calls = 0
        self.total_tokens = 0

    def record_round(self):
        self.rounds += 1

    def reserve_tool_call(self):
        self.tool_calls += 1

    def record_usage(self, usage):
        if not isinstance(usage, dict):
            return
        total = usage.get("total_tokens")
        if total is None:
            total = ((usage.get("prompt_tokens") or 0)
                     + (usage.get("completion_tokens") or 0))
        if isinstance(total, (int, float)) and total > 0:
            self.total_tokens += int(total)

    def elapsed_s(self) -> float:
        return time.monotonic() - self.started_at

    def exhausted_reason(self) -> str:
        if self.max_rounds and self.rounds >= self.max_rounds:
            return f"max rounds ({self.max_rounds})"
        if self.max_tool_calls and self.tool_calls >= self.max_tool_calls:
            return f"max tool calls ({self.max_tool_calls})"
        if self.max_wall_s and self.elapsed_s() >= self.max_wall_s:
            return f"max wall time ({self.max_wall_s}s)"
        if self.max_total_tokens and \
                self.total_tokens >= self.max_total_tokens:
            return f"max tokens ({self.max_total_tokens})"
        return ""

    def snapshot(self) -> dict:
        return {
            "rounds": self.rounds,
            "tool_calls": self.tool_calls,
            "total_tokens": self.total_tokens,
            "elapsed_s": round(self.elapsed_s(), 3),
        }


@dataclass
class ToolPolicy:
    """一个 agent 角色的工具权限面。"""

    role: str
    allowed: frozenset[str] | None = None
    read_only: bool = False
    writable_roots: tuple[Path, ...] = ()
    force_cwd: Path | None = None
    confirm_tools: frozenset[str] = DEFAULT_CONFIRM_TOOLS
    bash_mode: str = BASH_FULL
    allow_mcp: bool = True
    max_calls: int = 0
    requires_sandbox: bool = False

    def root(self) -> Path:
        if self.force_cwd is not None:
            return Path(self.force_cwd)
        if self.writable_roots:
            return Path(self.writable_roots[0])
        return Path(Configs.WORKDIR)

    def allows_tool(self, name: str) -> tuple[bool, str]:
        if name.startswith("mcp__") and not self.allow_mcp:
            return False, f"MCP tool not allowed for {self.role}"
        if self.allowed is not None and name not in self.allowed:
            return False, f"tool '{name}' is not allowed for {self.role}"
        if self.read_only and name in WRITE_TOOL_NAMES:
            return False, f"{self.role} is read-only"
        return True, ""

    def needs_confirmation(self, name: str, arguments: dict,
                           metadata: dict | None = None) -> tuple[bool, str]:
        """返回 (是否需要确认, 原因)。策略层只做静态判断。"""
        if name == "remove_worktree":
            if arguments.get("discard_changes"):
                return True, "discard uncommitted worktree changes"
            return False, ""
        if name.startswith("mcp__"):
            annotations = (metadata or {}).get("annotations") or {}
            if annotations.get("destructiveHint"):
                return True, f"destructive MCP tool: {name}"
            if annotations.get("readOnlyHint"):
                return False, ""
            return True, f"MCP tool has no readOnly annotation: {name}"
        if name in self.confirm_tools:
            return True, f"{name} requires confirmation"
        return False, ""

    def path_is_writable(self, path: Path) -> bool:
        if self.read_only:
            return False
        try:
            resolved = Path(path).resolve()
        except (OSError, RuntimeError, ValueError):
            return False
        return any(resolved.is_relative_to(Path(root).resolve())
                   for root in self.writable_roots)

    def snapshot(self) -> dict:
        return {
            "role": self.role,
            "read_only": self.read_only,
            "writable_roots": [str(root) for root in self.writable_roots],
            "force_cwd": str(self.force_cwd) if self.force_cwd else None,
            "bash_mode": self.bash_mode,
            "allow_mcp": self.allow_mcp,
            "requires_sandbox": self.requires_sandbox,
            "allowed": sorted(self.allowed) if self.allowed is not None else None,
        }


def lead_policy() -> ToolPolicy:
    return ToolPolicy(
        role="lead",
        writable_roots=(Path(Configs.WORKDIR),),
        bash_mode=BASH_FULL,
    )


def local_policy() -> ToolPolicy:
    return ToolPolicy(
        role="local",
        allowed=READONLY_TOOL_NAMES,
        read_only=True,
        bash_mode=BASH_DENY,
        allow_mcp=False,
    )


def subagent_policy() -> ToolPolicy:
    return ToolPolicy(
        role="subagent",
        allowed=SUBAGENT_TOOLS,
        writable_roots=(Path(Configs.WORKDIR),),
        bash_mode=BASH_FULL,
        allow_mcp=False,
    )


def subagent_local_policy() -> ToolPolicy:
    return ToolPolicy(
        role="subagent-local",
        allowed=SUBAGENT_LOCAL_TOOLS,
        read_only=True,
        bash_mode=BASH_DENY,
        allow_mcp=False,
    )


def plan_step_policy(allowed) -> ToolPolicy:
    """Plan step 的作用域策略:lead 类似能力,但工具集被 allowlist 收窄。

    即使调用方误把 Plan 工具/spawn 放进 allowlist,策略层也会剔除;
    MCP 由 allow_mcp=False 拒绝。
    """
    return ToolPolicy(
        role="plan_step",
        allowed=frozenset(set(allowed or ()) - PLAN_FORBIDDEN_STEP_TOOLS),
        writable_roots=(Path(Configs.WORKDIR),),
        bash_mode=BASH_FULL,
        allow_mcp=False,
        confirm_tools=DEFAULT_CONFIRM_TOOLS,
    )


def teammate_policy(worktree: str | Path | None = None) -> ToolPolicy:
    """队友:有 worktree 才能写;没有 worktree 时写拒绝、bash 只读。"""
    if worktree:
        root = Path(worktree)
        return ToolPolicy(
            role="teammate",
            allowed=TEAMMATE_TOOLS,
            writable_roots=(root,),
            force_cwd=root,
            bash_mode=BASH_FULL,
            allow_mcp=False,
            requires_sandbox=True,
        )
    return ToolPolicy(
        role="teammate",
        allowed=TEAMMATE_TOOLS,
        read_only=True,
        writable_roots=(),
        force_cwd=Path(Configs.WORKDIR),
        bash_mode=BASH_READ_ONLY,
        allow_mcp=False,
    )


def skipped_tool_messages(calls, reason: str) -> list[dict]:
    """预算耗尽时,为每个未执行的 tool_call 补一条结果(API 硬要求)。"""
    from src.LlmClient import tool_message
    return [tool_message(call.id, f"[skipped] {reason}") for call in calls]


def tool_names(tool_defs: list[dict] | None) -> list[str]:
    if not tool_defs:
        return []
    return [item["function"]["name"] for item in tool_defs]


def canonical_args(arguments: dict) -> str:
    return json.dumps(arguments or {}, sort_keys=True, ensure_ascii=False)
