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

    # ── 模型路由(混合路由:本地小模型 / API) ──
    # auto=全量路由;api-only=全走 API(逃生舱);local-only=全走本地(仅测试);shadow=只记录不生效(调参)
    ROUTER_MODE = os.getenv("ROUTER_MODE", "auto")
    API_MODEL_ID = os.getenv("API_MODEL_ID")  # 默认取客户端模型
    LOCAL_MODEL_ID = os.getenv("LOCAL_MODEL_ID")  # 未配置则退化为纯 API 行为
    LOCAL_BASE_URL = os.getenv("LOCAL_BASE_URL")  # 例: http://<gpu-box>:8000/v1
    LOCAL_API_KEY = os.getenv("LOCAL_API_KEY", "local")  # vLLM 通常不需要鉴权,占位即可
    LOCAL_LLM_TIMEOUT = float(os.getenv("LOCAL_LLM_TIMEOUT", "300"))  # 本地推理慢,超时放宽
    SIMPLE_TASK_MAX_CHARS = 200  # 简单任务判定:用户消息长度上限
    VERY_SHORT_CHARS = 40  # 极短消息(问候/闲聊)视作纯问答
    LOCAL_MAX_CONTEXT_CHARS = 15000  # 本地小模型能承受的上下文上限(字符)
    LOCAL_MAX_TOKENS = 1024  # 本地调用的 max_tokens 上限:失败要便宜
    HEALTH_CHECK_TTL = 30  # 本地服务健康检查缓存秒数
    HEALTH_CHECK_TIMEOUT = 3.0  # 健康探测超时秒数
    MAX_PARSE_ERRORS_BEFORE_ESCALATE = 2  # 工具参数解析失败 N 次后升级 API
    MAX_IDENTICAL_TOOL_CALLS = 3  # 同一工具+参数连续调用 N 次视为空转
    MAX_TOOL_ERRORS_BEFORE_ESCALATE = 2  # 工具执行报错 N 次后升级 API

    # ── Phase 3:轨迹收集(路由日志 → 微调训练数据) ──
    COLLECT_TRAINING_DATA = os.getenv("COLLECT_TRAINING_DATA", "0") == "1"
    TRAINING_DATA_DIR = WORKDIR / ".training_data"

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

    # ── 预算与配额(ToolDispatcher / AgentLoop / 子代理 / 队友 / 后台 / cron) ──
    TURN_MAX_ROUNDS = int(os.getenv("TURN_MAX_ROUNDS", "40"))
    TURN_MAX_TOOL_CALLS = int(os.getenv("TURN_MAX_TOOL_CALLS", "100"))
    TURN_MAX_WALL_SECONDS = int(os.getenv("TURN_MAX_WALL_SECONDS", "600"))
    # 0 = 不按 token 限额(部分兼容接口不返回 usage)
    TURN_MAX_TOTAL_TOKENS = int(os.getenv("TURN_MAX_TOTAL_TOKENS", "200000"))
    SUBAGENT_MAX_TOOL_CALLS = int(os.getenv("SUBAGENT_MAX_TOOL_CALLS", "60"))
    SUBAGENT_MAX_WALL_SECONDS = int(os.getenv("SUBAGENT_MAX_WALL_SECONDS", "600"))
    TEAMMATE_MAX_LIFETIME_SECONDS = int(
        os.getenv("TEAMMATE_MAX_LIFETIME_SECONDS", "1800"))
    TEAMMATE_MAX_API_CALLS = int(os.getenv("TEAMMATE_MAX_API_CALLS", "200"))
    TEAMMATE_MAX_TASKS = int(os.getenv("TEAMMATE_MAX_TASKS", "20"))
    MAX_TEAMMATES = int(os.getenv("MAX_TEAMMATES", "5"))

    BACKGROUND_MAX_CONCURRENT = int(os.getenv("BACKGROUND_MAX_CONCURRENT", "4"))
    BACKGROUND_MAX_QUEUED = int(os.getenv("BACKGROUND_MAX_QUEUED", "16"))
    BACKGROUND_TASK_TIMEOUT_SECONDS = int(
        os.getenv("BACKGROUND_TASK_TIMEOUT_SECONDS", "600"))
    BACKGROUND_MAX_OUTPUT_CHARS = 100_000

    TASK_LEASE_SECONDS = int(os.getenv("TASK_LEASE_SECONDS", "300"))
    TASK_MAX_ATTEMPTS = int(os.getenv("TASK_MAX_ATTEMPTS", "3"))

    BASH_TIMEOUT_DEFAULT = int(os.getenv("BASH_TIMEOUT_DEFAULT", "120"))
    BASH_TIMEOUT_MAX = int(os.getenv("BASH_TIMEOUT_MAX", "600"))
    READ_FILE_MAX_LINES = int(os.getenv("READ_FILE_MAX_LINES", "2000"))
    MCP_CALL_TIMEOUT = int(os.getenv("MCP_CALL_TIMEOUT", "60"))
    MCP_MAX_OUTPUT_CHARS = int(os.getenv("MCP_MAX_OUTPUT_CHARS", "100000"))

    # ── 审计与备份 ──
    AUDIT_MAX_BYTES = int(os.getenv("AUDIT_MAX_BYTES", "5000000"))
    AUDIT_BACKUP_COUNT = int(os.getenv("AUDIT_BACKUP_COUNT", "3"))

    # ── 沙箱(bwrap:只读根 + 可写工作区 + 默认无网络) ──
    SANDBOX_MODE = os.getenv("SANDBOX_MODE", "auto")  # auto / require / off
    SANDBOX_MEMORY_MB = int(os.getenv("SANDBOX_MEMORY_MB", "4096"))
    SANDBOX_CPU_SECONDS = int(os.getenv("SANDBOX_CPU_SECONDS", "600"))

    # ── 定时任务上限 ──
    CRON_MAX_JOBS = int(os.getenv("CRON_MAX_JOBS", "50"))
    CRON_MAX_PROMPT_CHARS = int(os.getenv("CRON_MAX_PROMPT_CHARS", "2000"))
    CRON_MIN_INTERVAL_MINUTES = int(
        os.getenv("CRON_MIN_INTERVAL_MINUTES", "5"))

    # ── 逐次确认 ──
    # 显式逃生舱:非交互模式下仍允许这些工具(格式: name 或 prefix*)
    AUTO_APPROVE_TOOLS = tuple(
        item.strip() for item in os.getenv("AUTO_APPROVE_TOOLS", "").split(",")
        if item.strip())
    print("\n --- b ---")
    print(AUTO_APPROVE_TOOLS)

    @classmethod
    def audit_dir(cls) -> Path:
        """审计目录(惰性解析 WORKDIR,便于测试重定向)。"""
        return cls.WORKDIR / ".audit"

    @classmethod
    def backup_dir(cls) -> Path:
        """写前备份目录(惰性解析 WORKDIR)。"""
        return cls.WORKDIR / ".backups"
