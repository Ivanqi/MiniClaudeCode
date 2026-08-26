"""全局配置:工作目录、路径、阈值、模型常量等。

所有魔法数字集中在此处定义,其余模块只读引用,不重复定义,
方便统一调整参数而不需要改动业务代码。
"""
import os
from pathlib import Path


class Configs:
    """全局配置常量,按职责分组。"""

    # ── 终端交互 ──
    CLI_ACTIVE = False  # CLI 模式下由 main.py 置为 True,决定后台线程是否重绘输入提示符
    PROMPT = "\033[36mivan >> \033[0m"

    # ── 工作目录 ──
    WORKDIR = Path.cwd()
    SKILLS_DIR = WORKDIR / "skills"
    TRANSCRIPT_DIR = WORKDIR / ".transcripts"
    TOOL_RESULTS_DIR = WORKDIR / ".task_outputs" / "tool-results"
    TASKS_DIR = WORKDIR / ".tasks"
    WORKTREES_DIR = WORKDIR / ".worktrees"
    MAILBOX_DIR = WORKDIR / ".mailboxes"
    MEMORY_DIR = WORKDIR / ".memory"
    MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"
    DURABLE_CRON_PATH = WORKDIR / ".scheduled_tasks.json"

    # ── LLM 模型 ──
    MODEL = os.getenv("MODEL_ID") or "deepseek-chat"
    FALLBACK_MODEL = os.getenv("FALLBACK_MODEL_ID")

    # ── Token 预算 ──
    # deepseek-chat 单次输出上限为 8192:默认 4096,遇到截断时升级到 8192。
    DEFAULT_MAX_TOKENS = 4096
    ESCALATED_MAX_TOKENS = 8192
    SUBAGENT_MAX_TOKENS = 8000
    TEAMMATE_MAX_TOKENS = 8000
    SUMMARY_MAX_TOKENS = 2000

    # ── 重试与恢复 ──
    MAX_RETRIES = 3
    MAX_CONSECUTIVE_OVERLOAD = 2
    MAX_RECOVERY_RETRIES = 2
    BASE_DELAY_MS = 500
    CONTINUATION_PROMPT = "Continue from the previous response. Do not repeat completed work."

    # ── 上下文压缩 ──
    CONTEXT_LIMIT = 50000
    KEEP_RECENT_TOOL_RESULTS = 3
    PERSIST_THRESHOLD = 30000
    TOOL_RESULT_BUDGET = 200_000

    # ── 队友自治 ──
    IDLE_POLL_INTERVAL = 5
    IDLE_TIMEOUT = 60
