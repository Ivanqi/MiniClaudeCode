"""AgentLoop 构造装配:持有全部子系统并注册工具。"""
import threading

from src.ApprovalGate import ApprovalGate
from src.AuditLog import AuditLog
from src.BackgroundTaskSystem import BACKGROUND_TASKS
from src.BasicOper import BASIC_OPER
from src.ContextCompact import ContextCompact
from src.CronScheduler import CRON_SCHEDULER
from src.HookSystem import HOOK_SYSTEM
from src.IntegrationSystem import IntegrationSystem
from src.LeadMonitor import LeadMonitor
from src.LoopGuard import ToolCircuitBreaker
from src.McpSystem import MCP_SYSTEM
from src.Memories import Memories
from src.MemoryStore import MemoryStore
from src.ModelRouter import ModelRouter
from src.PlanSystem import PlanStore, PlanSystem
from src.PromptBuilder import PromptBuilder
from src.ProtocolSystem import PROTOCOL
from src.Retention import RetentionManager
from src.ReviewSystem import ReviewSystem
from src.Sandbox import Sandbox
from src.SkillSystem import SKILL_SYSTEM
from src.SubagentSystem import SubagentSystem
from src.TaskSystem import TASK_SYSTEM
from src.TeamMetrics import TEAM_METRICS
from src.TeammateSystem import TeammateSystem
from src.ToolDispatcher import ToolDispatcher
from src.ToolRegistry import ToolRegistry
from src.TrajectoryLogger import TrajectoryLogger
from src.TranscriptStore import TranscriptStore
from src.WorktreeSystem import WORKTREE_SYSTEM

'''
wire 是 AgentLoop 的“总装配线”
它把散落在各处的子系统（模型路由、工具调度、审计、记忆、任务、团队协作等）
逐个实例化并挂载到 owner（即 AgentLoop 实例）上
同时通过依赖注入把这些组件互相连接起来
让后续代码可以直接用 owner.xxx 的方式访问
'''
def wire(owner, client, router: ModelRouter | None,
         dispatcher: ToolDispatcher | None, step_runner,
         tool_names_provider):
    """把全部子系统挂到 owner 上;保持原 AgentLoop 属性访问方式。"""
    # 基础设施（底层组件）
    # 这些都是全局单例或轻量对象，直接挂载即可。
    owner.client = client
    owner.router = router or ModelRouter(client)
    owner.basic = BASIC_OPER
    owner.hooks = HOOK_SYSTEM
    owner.background = BACKGROUND_TASKS
    owner.cron = CRON_SCHEDULER
    owner.mcp = MCP_SYSTEM
    owner.protocol = PROTOCOL
    owner.prompt_builder = PromptBuilder()
    owner.trajectory = TrajectoryLogger()

    # 存储与审计（有依赖链）
    owner.audit = AuditLog()
    owner.memory = MemoryStore(audit=owner.audit)
    owner.transcripts = TranscriptStore(memory=owner.memory)
    owner.retention = RetentionManager(audit=owner.audit)
    owner.compact = ContextCompact(
        router=owner.router, transcript_store=owner.transcripts,
        memory_store=owner.memory)
    owner.memories = Memories(memory=owner.memory, tasks=TASK_SYSTEM)

    # 安全与工具调度
    owner.approval = ApprovalGate()
    owner.sandbox = Sandbox()
    owner.metrics = TEAM_METRICS
    owner.circuit_breaker = ToolCircuitBreaker(
        metrics=owner.metrics, audit=owner.audit)
    if dispatcher is not None:
        owner.dispatcher = dispatcher
        owner.circuit_breaker = (
            getattr(dispatcher, "circuit_breaker", None)
            or owner.circuit_breaker)
    else:
        owner.dispatcher = ToolDispatcher(
            basic=owner.basic, hooks=owner.hooks, audit=owner.audit,
            approval=owner.approval, sandbox=owner.sandbox,
            circuit_breaker=owner.circuit_breaker)

    # 团队协作与子代理（高层编排）
    # LeadMonitor 依赖 owner.teammates 的内部组件（protocol、tasks、teammates、bus）
    # 所以 teammates 必须先于 lead_monitor 创建
    owner.teammates = TeammateSystem(
        client, router=owner.router, dispatcher=owner.dispatcher,
        metrics=owner.metrics)
    owner.subagents = SubagentSystem(
        client, router=owner.router, dispatcher=owner.dispatcher,
        metrics=owner.metrics)
    owner.review = ReviewSystem(
        owner.subagents, tasks=TASK_SYSTEM, worktrees=WORKTREE_SYSTEM,
        metrics=owner.metrics)
    owner.integration = IntegrationSystem(
        tasks=TASK_SYSTEM, worktrees=WORKTREE_SYSTEM,
        dispatcher=owner.dispatcher, metrics=owner.metrics)
    owner.lead_monitor = LeadMonitor(
        protocol=owner.teammates.protocol, tasks=owner.teammates.tasks,
        teammates=owner.teammates.teammates, bus=owner.teammates.bus,
        metrics=owner.metrics)

    # 工具注册表
    owner.tool_registry = ToolRegistry(owner.mcp)
    for system in (owner.basic, TASK_SYSTEM, WORKTREE_SYSTEM, SKILL_SYSTEM,
                   owner.cron, owner.mcp, owner.teammates, owner.subagents,
                   owner.background, owner.memory, owner.transcripts,
                   owner.review, owner.integration, owner.compact):
        owner.tool_registry.register_system(system)

    # 计划系统与初始化
    owner.plans = PlanSystem(
        client, router=owner.router, tasks=TASK_SYSTEM,
        memory=owner.memory, store=PlanStore(), approval=owner.approval,
        metrics=owner.metrics, step_runner=step_runner,
        tool_names_provider=tool_names_provider)
    owner.tool_registry.register_system(owner.plans)

    owner.rounds_since_todo = 0
    owner._lock = threading.Lock()
