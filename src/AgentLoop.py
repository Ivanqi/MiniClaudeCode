"""主代理循环:编排 LLM 调用、工具分发、上下文压缩与后台注入。

一个轮次 = 注入定时/后台工作 → 准备上下文 → 调用模型 →
执行 tool_calls → 回传 tool 结果,循环直到模型不再产生工具调用。
"""
import json
import threading
import time
import uuid

from src.ActiveTeammates import ACTIVE_TEAMMATES
from src.ApprovalGate import ApprovalGate
from src.AuditLog import AuditLog
from src.BackgroundTaskSystem import BACKGROUND_TASKS
from src.BasicOper import BASIC_OPER
from src.Configs import Configs
from src.ContextCompact import ContextCompact
from src.CronScheduler import CRON_SCHEDULER
from src.HookSystem import HOOK_SYSTEM
from src.LlmClient import (assistant_message, tool_message, user_message)
from src.IntegrationSystem import IntegrationSystem
from src.LeadMonitor import LeadMonitor
from src.Memories import Memories
from src.MemoryStore import MemoryStore
from src.ModelRouter import ModelRouter, RouterDecision
from src.McpSystem import MCP_SYSTEM
from src.PlanSystem import PlanStore, PlanSystem
from src.ReviewSystem import ReviewSystem
from src.PromptBuilder import PromptBuilder
from src.ProtocolSystem import PROTOCOL
from src.RecoverySystem import (RecoveryState, is_prompt_too_long_error,
                                with_retry)
from src.Retention import RetentionManager
from src.Sandbox import Sandbox
from src.SkillSystem import SKILL_SYSTEM
from src.SubagentSystem import SubagentSystem
from src.TaskSystem import TASK_SYSTEM
from src.TeamMetrics import TEAM_METRICS
from src.TeammateSystem import TeammateSystem
from src.Terminal import Terminal
from src.ToolDispatcher import ToolDispatcher
from src.ToolPolicy import (PLAN_FORBIDDEN_STEP_TOOLS, TurnBudget,
                            lead_policy, local_policy, skipped_tool_messages)
from src.ToolRegistry import ToolRegistry
from src.TrajectoryLogger import TrajectoryLogger
from src.TranscriptStore import TranscriptStore
from src.WorktreeSystem import WORKTREE_SYSTEM

TODO_REMINDER_ROUNDS = 3


class AgentLoop:
    """主代理循环:持有全部子系统,提供 run_cycle 与 CLI 入口。"""

    def __init__(self, client, router: ModelRouter | None = None,
                 dispatcher: ToolDispatcher | None = None):
        self.client = client
        # 未提供路由器时退化为纯 API 行为(向后兼容测试与简单部署)
        self.router = router or ModelRouter(client)
        self.basic = BASIC_OPER
        self.hooks = HOOK_SYSTEM
        self.background = BACKGROUND_TASKS
        self.cron = CRON_SCHEDULER
        self.mcp = MCP_SYSTEM
        self.protocol = PROTOCOL
        self.prompt_builder = PromptBuilder()
        self.trajectory = TrajectoryLogger()  # Phase 3:路由轨迹 → 微调数据

        # 统一分发:权限、确认、沙箱、审计、备份都收口在这里。
        # 构造过程不做文件 I/O / 沙箱探测,便于测试注入。
        self.audit = AuditLog()
        self.memory = MemoryStore(audit=self.audit)
        self.transcripts = TranscriptStore(memory=self.memory)
        self.retention = RetentionManager(audit=self.audit)
        self.compact = ContextCompact(
            router=self.router, transcript_store=self.transcripts,
            memory_store=self.memory)
        self.memories = Memories(memory=self.memory, tasks=TASK_SYSTEM)
        self.approval = ApprovalGate()
        self.sandbox = Sandbox()
        self.dispatcher = dispatcher or ToolDispatcher(
            basic=self.basic, hooks=self.hooks, audit=self.audit,
            approval=self.approval, sandbox=self.sandbox)
        self.metrics = TEAM_METRICS
        self.teammates = TeammateSystem(
            client, router=self.router, dispatcher=self.dispatcher,
            metrics=self.metrics)
        self.subagents = SubagentSystem(
            client, router=self.router, dispatcher=self.dispatcher,
            metrics=self.metrics)
        self.review = ReviewSystem(
            self.subagents, tasks=TASK_SYSTEM, worktrees=WORKTREE_SYSTEM,
            metrics=self.metrics)
        self.integration = IntegrationSystem(
            tasks=TASK_SYSTEM, worktrees=WORKTREE_SYSTEM,
            dispatcher=self.dispatcher, metrics=self.metrics)
        self.lead_monitor = LeadMonitor(
            protocol=self.teammates.protocol, tasks=self.teammates.tasks,
            teammates=self.teammates.teammates, bus=self.teammates.bus,
            metrics=self.metrics)

        # 工具注册表:各系统自持工具定义与处理器,在此聚合。
        self.tool_registry = ToolRegistry(self.mcp)
        # 通过 handlers() 方法返回该字典的副本或直接引用，供外部查询或调用
        for system in (self.basic, TASK_SYSTEM, WORKTREE_SYSTEM, SKILL_SYSTEM,
                       self.cron, self.mcp, self.teammates, self.subagents,
                       self.background, self.memory, self.transcripts,
                       self.review, self.integration, self.compact):
            self.tool_registry.register_system(system)

        # 通用 Plan 只给 lead;最后注册,构造零 I/O / 零 LLM / 零线程。
        self.plans = PlanSystem(
            client, router=self.router, tasks=TASK_SYSTEM,
            memory=self.memory, store=PlanStore(), approval=self.approval,
            metrics=self.metrics, step_runner=self._run_plan_step,
            tool_names_provider=self.available_tool_names)
        self.tool_registry.register_system(self.plans)

        self.rounds_since_todo = 0
        self._lock = threading.Lock()

    # ── 上下文管道 ──
    # message 预处理
    # 1. tool message 大结果落盘
    def prepare_context(self, messages: list) -> list:
        """每个 LLM 轮次都经过同一上下文预算管道。"""
        # tool message 大结果落盘
        messages[:] = self.compact.tool_result_budget(messages)
        # 裁掉无关的旧对话:消息数超过 50 条 → 保留头部 3 条（初始上下文）和尾部 47 条（当前工作），中间裁掉
        messages[:] = self.compact.snip_compact(messages)
        # 只保留最近 3 条 tool_result 的完整内容
        messages[:] = self.compact.micro_compact(messages)
        if self.compact.estimate_size(messages) > Configs.CONTEXT_LIMIT:
            # 替换消息列表：所有旧消息被替换为一条摘要
            messages[:] = self.compact.compact_history(messages, self.client)
        return messages
    
    def build_tool_results(self, results: list[dict],
                           collect_background: bool = True) -> list[dict]:
        """工具结果 + 后台完成通知,一起作为本轮工具消息回传。"""
        content = list(results)
        if collect_background:
            for note in self.background.collect_results():
                content.append(user_message(note))
        return content

    def inject_background_notifications(self, messages: list):
        notes = self.background.collect_results()
        if notes:
            messages.extend(user_message(note) for note in notes)

    def call_llm(self, messages: list, context: dict, tools: list,
                 slim_tools: list | None, state: RecoveryState,
                 max_tokens: int, caller: str = "main",
                 flags: dict | None = None):
        """路由决策 + 带重试的模型调用,返回 (response, decision)。

        - 本地小模型:使用精简只读工具集(slim_tools);重试耗尽(RuntimeError)后升级 API;
        - 纯问答任务剥离工具集(tools=None);
        - API:沿用 with_retry 的 429/503 处理与 FALLBACK_MODEL_ID 降级;
        - system prompt 的工具清单来自本轮实际路由后的工具集。
        """
        route_flags = dict(flags or {})
        route_flags.setdefault("escalated", state.turn_escalated)
        route_flags.setdefault("active_teammates",
                               bool(ACTIVE_TEAMMATES.names()))
        decision = self.router.route(messages, caller=caller,
                                     flags=route_flags)
        if state.started_local is None:
            state.started_local = decision.local  # 记录 turn 起点(轨迹数据用)
        client, model, routed_max = self.router.apply(
            decision, max_tokens,
            model_override=None if decision.local else state.current_model)
        # tools llm function call 定义:纯问答剥离,本地调用用只读子集
        if decision.strip_tools:
            routed_tools = None
        elif decision.local:
            routed_tools = slim_tools
        else:
            routed_tools = tools
        system = self.prompt_builder.assemble(context, routed_tools)
        return (self._create_with_retry(client, messages, system, routed_tools,
                                        routed_max, model, state, decision,
                                        context=context, full_tools=tools,
                                        caller=caller, flags=route_flags),
                decision)

    def _create_with_retry(self, client, messages: list, system: str,
                           tools: list | None, max_tokens: int, model: str,
                           state: RecoveryState, decision: RouterDecision,
                           context: dict | None = None,
                           full_tools: list | None = None,
                           caller: str = "main",
                           flags: dict | None = None):
        """with_retry 封装;本地模型重试耗尽时升级 API 再试一次。

        升级时恢复完整工具集:strip_tools 只是本地小模型的优化,
        API 侧不需要。
        """
        try:
            return with_retry(
                lambda: client.create(messages, system=system, tools=tools,
                                      max_tokens=max_tokens, model=model),
                state, client=client)
        except RuntimeError:
            if decision.local:
                state.turn_escalated = True
                state.escalation_reason = "retries-exhausted"
                Terminal.info("escalate", "local retries exhausted → API",
                              Terminal.RED)
                upgraded_flags = dict(flags or {})
                upgraded_flags["escalated"] = True
                upgraded = self.router.route(
                    messages, caller=caller, flags=upgraded_flags)
                api_client, api_model, api_max = self.router.apply(
                    upgraded, max_tokens, model_override=state.current_model)
                full_system = self.prompt_builder.assemble(context or {},
                                                           full_tools)
                return with_retry(
                    lambda: api_client.create(messages, system=full_system,
                                              tools=full_tools,
                                              max_tokens=api_max,
                                              model=api_model),
                    state, client=api_client)
            raise

    # ── 主循环 ──

    def run_cycle(self, messages: list, context: dict, *,
                  policy=None, budget=None, tools_allowlist=None,
                  caller: str = "main", flags: dict | None = None,
                  allow_cron: bool = True, allow_background: bool = True,
                  trigger_stop: bool = True, trajectory: bool = True,
                  turn_id: str | None = None,
                  refresh_context: bool = True,
                  todo_reminders: bool = True) -> dict:
        budget = budget or TurnBudget()
        state = RecoveryState()
        max_tokens = Configs.DEFAULT_MAX_TOKENS
        turn_id = turn_id or uuid.uuid4().hex[:8]
        closing_reason = ""
        artifacts = []
        saved_todo = self.rounds_since_todo

        try:
            while True:
                reason = budget.exhausted_reason()
                if reason:
                    closing_reason = reason
                    break

                if allow_cron:
                    fired = self.cron.consume_queue()
                    for job in fired:
                        messages.append(user_message(
                            f"[Scheduled] {job.prompt}"))
                        Terminal.info("cron inject", job.prompt[:60],
                                      Terminal.MAGENTA)

                if allow_background:
                    self.inject_background_notifications(messages)

                if todo_reminders and \
                        self.rounds_since_todo >= TODO_REMINDER_ROUNDS:
                    messages.append(user_message(
                        "<reminder>Update your todos.</reminder>"))
                    self.rounds_since_todo = 0

                self.prepare_context(messages)
                if refresh_context:
                    context = self.memories.update_context(context, messages)
                if tools_allowlist is not None:
                    allowed = {name for name in
                               set(tools_allowlist)
                               - PLAN_FORBIDDEN_STEP_TOOLS
                               if not name.startswith("mcp__")}
                    tools, handlers = self.tool_registry.assemble(
                        allowlist=allowed)
                    slim_tools, slim_handlers = self.tool_registry.assemble(
                        slim=True, allowlist=allowed)
                else:
                    tools, handlers = self.tool_registry.assemble()
                    slim_tools, slim_handlers = self.tool_registry.assemble(
                        slim=True)

                budget.record_round()  # 失败的 LLM 尝试也计入轮数
                try:
                    response, decision = self.call_llm(
                        messages, context, tools, slim_tools, state,
                        max_tokens, caller=caller, flags=flags)
                except Exception as e:
                    # 应急
                    if is_prompt_too_long_error(e) and \
                            not state.has_attempted_reactive_compact:
                        messages[:] = self.compact.reactive_compact(
                            messages, self.client)
                        state.has_attempted_reactive_compact = True
                        continue
                    messages.append({"role": "assistant",
                                     "content": f"[Error] {type(e).__name__}: {e}"})
                    if trajectory:
                        self.trajectory.log_turn(messages, state,
                                                 error=str(e))
                    return self._turn_result(
                        "error", messages, budget, error=str(e),
                        artifacts=artifacts)
                budget.record_usage(getattr(response, "usage", None))
                self.metrics.add_usage(
                    getattr(response, "usage", None),
                    "lead" if caller == "main" else caller)

                # ── 质量信号:本地小模型失败 → 升级 API 重试本轮(不追加坏消息) ──
                if response.has_parse_error:
                    response.tool_calls = [c for c in response.tool_calls
                                           if not c.parse_error]
                    state.parse_failures += 1
                    Terminal.info(
                        "parse-error",
                        f"dropped malformed call ({state.parse_failures})",
                        Terminal.RED)
                    if decision.local:
                        if state.parse_failures >= \
                                Configs.MAX_PARSE_ERRORS_BEFORE_ESCALATE:
                            state.turn_escalated = True
                            state.escalation_reason = "parse-errors"
                            Terminal.info("escalate", "parse errors → API",
                                          Terminal.RED)
                        continue  # 不追加解析失败的回复,直接重试本轮
                if (not response.tool_calls
                        and not (response.content or "").strip()):
                    if decision.local:
                        state.turn_escalated = True
                        state.escalation_reason = "empty-reply"
                        Terminal.info("escalate", "empty reply → API",
                                      Terminal.RED)
                        continue
                    # API 空回复:按收尾处理(原有语义)
                if response.finish_reason == "length":
                    if decision.local:
                        # 本地模型生成慢,不升级 max_tokens,直接切 API 重试
                        state.turn_escalated = True
                        state.escalation_reason = "length"
                        Terminal.info("escalate", "length → API", Terminal.RED)
                        continue
                    if not state.has_escalated:
                        max_tokens = Configs.ESCALATED_MAX_TOKENS
                        state.has_escalated = True
                        Terminal.info("max_tokens",
                                      f"retry with {max_tokens}",
                                      Terminal.YELLOW)
                        continue
                    messages.append(assistant_message(response))
                    if state.recovery_count < Configs.MAX_RECOVERY_RETRIES:
                        messages.append(user_message(
                            Configs.CONTINUATION_PROMPT))
                        state.recovery_count += 1
                        continue
                    if trajectory:
                        self.trajectory.log_turn(messages, state)
                    return self._turn_result(
                        "length", messages, budget, artifacts=artifacts)

                max_tokens = Configs.DEFAULT_MAX_TOKENS
                state.has_escalated = False
                messages.append(assistant_message(response))
                if not response.has_tool_calls:
                    if trigger_stop:
                        self.hooks.trigger("Stop", messages)
                    if trajectory:
                        self.trajectory.log_turn(messages, state)
                    return self._turn_result(
                        "completed", messages, budget, artifacts=artifacts)

                # 本地轮次使用只读策略 + 精简 handler;幻觉工具名会被策略/分发器拒绝,
                # 进而触发质量信号升级 API。
                round_handlers = slim_handlers if decision.local else handlers
                round_policy = policy or (
                    local_policy() if decision.local else lead_policy())

                results = []
                compacted_now = False
                # function call
                for call_index, call in enumerate(response.tool_calls):
                    Terminal.print(
                        f"{Terminal.CYAN}> {call.name}{Terminal.RESET}")

                    if call.name == "compact":
                        messages[:] = self.compact.compact_history(
                            messages, self.client)
                        messages.append(user_message(
                            "[Compacted. Continue with summarized context.]"))
                        compacted_now = True
                        break

                    reason = budget.exhausted_reason()
                    if reason:
                        results.extend(skipped_tool_messages(
                            response.tool_calls[call_index:],
                            f"budget exceeded: {reason}"))
                        closing_reason = reason
                        break
                    budget.reserve_tool_call()

                    metadata = self.tool_registry.metadata(call.name)
                    auth = self.dispatcher.authorize(
                        call, round_policy, round_handlers, metadata=metadata,
                        allow_prompt=True, turn_id=turn_id)
                    result_ok = False
                    if not auth.allowed:
                        output = (auth.denial.content
                                  if auth.denial else "Denied")
                    elif allow_background and self.background.should_run_background(
                            call.name, call.arguments):
                        bg_id = self.background.start(
                            call, round_handlers, dispatcher=self.dispatcher,
                            policy=round_policy, authorization=auth,
                            turn_id=turn_id)
                        if bg_id:
                            output = (f"[Background task {bg_id} started] "
                                      "Result will arrive as a task_notification.")
                            result_ok = True
                        else:
                            result = self.dispatcher.execute(
                                call, round_policy, round_handlers,
                                authorization=auth, turn_id=turn_id)
                            output, result_ok = result.content, result.ok
                            artifacts.extend(result.artifacts)
                    else:
                        result = self.dispatcher.execute(
                            call, round_policy, round_handlers,
                            authorization=auth, turn_id=turn_id)
                        output, result_ok = result.content, result.ok
                        artifacts.extend(result.artifacts)
                    Terminal.print(str(output)[:300])

                    # ── 质量信号:工具报错 / 空转检测 → 下一轮升级 API ──
                    if decision.local:
                        if not result_ok:
                            state.tool_error_count += 1
                            if state.tool_error_count >= \
                                    Configs.MAX_TOOL_ERRORS_BEFORE_ESCALATE:
                                state.turn_escalated = True
                                state.escalation_reason = "tool-errors"
                                Terminal.info("escalate", "tool errors → API",
                                              Terminal.RED)
                        tool_key = (call.name,
                                    json.dumps(call.arguments, sort_keys=True,
                                               ensure_ascii=False))
                        if tool_key == state.last_tool_key:
                            state.same_tool_streak += 1
                        else:
                            state.last_tool_key = tool_key
                            state.same_tool_streak = 1  # 首次调用记为 1
                        if state.same_tool_streak >= \
                                Configs.MAX_IDENTICAL_TOOL_CALLS:
                            state.turn_escalated = True
                            state.escalation_reason = "tool-loop"
                            Terminal.info("escalate", "tool loop → API",
                                          Terminal.RED)

                    if todo_reminders:
                        if call.name == "todo_write":
                            self.rounds_since_todo = 0
                        else:
                            self.rounds_since_todo += 1

                    results.append(tool_message(call.id, output))

                if compacted_now:
                    continue

                # 通知和工具结果合入同一条 user 消息
                messages.extend(self.build_tool_results(
                    results, collect_background=allow_background))
                if closing_reason:
                    text = self._close_turn(
                        messages, context, state, closing_reason, turn_id,
                        budget=budget, allow_background=allow_background,
                        trigger_stop=trigger_stop, trajectory=trajectory,
                        caller=caller)
                    return self._turn_result(
                        "budget_exhausted", messages, budget,
                        closing_reason=closing_reason, artifacts=artifacts,
                        content=text)

            # 轮次开始前预算已耗尽
            text = self._close_turn(
                messages, context, state, closing_reason, turn_id,
                budget=budget, allow_background=allow_background,
                trigger_stop=trigger_stop, trajectory=trajectory,
                caller=caller)
            return self._turn_result(
                "budget_exhausted", messages, budget,
                closing_reason=closing_reason, artifacts=artifacts,
                content=text)
        finally:
            if not todo_reminders:
                self.rounds_since_todo = saved_todo

    @staticmethod
    def _last_assistant_text(messages: list) -> str:
        for message in reversed(messages):
            if message.get("role") != "assistant":
                continue
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                return content.strip()
        return ""

    def _turn_result(self, status: str, messages: list, budget: TurnBudget,
                     error: str = "", closing_reason: str = "",
                     artifacts=None, content: str | None = None) -> dict:
        return {
            "status": status,
            "content": (content if content is not None
                        else self._last_assistant_text(messages)),
            "closing_reason": closing_reason,
            "error": error,
            "budget": budget.snapshot(),
            "messages": messages,
            "artifacts": list(artifacts or []),
        }

    def _close_turn(self, messages: list, context: dict, state: RecoveryState,
                    reason: str, turn_id: str, budget: TurnBudget | None = None,
                    allow_background: bool = True,
                    trigger_stop: bool = True, trajectory: bool = True,
                    caller: str = "main") -> str:
        """预算耗尽后的强制收敛:一次无工具调用,不允许悬挂 tool_call。"""
        state.budget_exhausted_reason = reason
        if allow_background:
            self.inject_background_notifications(messages)
        messages.append(user_message(
            f"[Budget reached: {reason}. Based on existing results, give a "
            "final answer now. Do not call any tools.]"))
        system = self.prompt_builder.assemble(context, None)
        try:
            response = self.client.create(
                messages, system=system, tools=None,
                max_tokens=Configs.SUMMARY_MAX_TOKENS)
            if budget is not None:
                budget.record_usage(getattr(response, "usage", None))
            self.metrics.add_usage(
                getattr(response, "usage", None),
                "lead" if caller == "main" else caller)
            text = (response.content or "").strip()
        except Exception as exc:
            text = (f"[Budget exhausted: {reason}. "
                    f"Summary failed: {type(exc).__name__}: {exc}]")
        if not text:
            text = f"[Budget exhausted: {reason}. No summary produced.]"
        messages.append({"role": "assistant", "content": text})
        if trigger_stop:
            self.hooks.trigger("Stop", messages)
        if trajectory:
            self.trajectory.log_turn(messages, state)
        return text

    def available_tool_names(self) -> list[str]:
        """Planner 的工具提示来源:当前 registry 的完整工具名(Plan 工具会被过滤)。"""
        tools, _ = self.tool_registry.assemble()
        return [item["function"]["name"] for item in tools]

    def _run_plan_step(self, prompt: str, policy, budget: TurnBudget,
                       tool_names, turn_id: str) -> dict:
        """PlanExecutor 的作用域 ReAct 入口:单步消息、无 outer 副作用。"""
        messages = [user_message(prompt)]
        return self.run_cycle(
            messages, {}, policy=policy, budget=budget,
            tools_allowlist=tool_names, caller="plan_step",
            allow_cron=False, allow_background=False,
            trigger_stop=False, trajectory=False, turn_id=turn_id,
            refresh_context=False, todo_reminders=False)

    # ── 输出与定时 ──

    def print_turn_assistants(self, messages: list, turn_start: int):
        for msg in messages[turn_start:]:
            if msg.get("role") != "assistant":
                continue
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                Terminal.print(content)

    def cron_autorun_loop(self, history: list, context: dict):
        """定时任务自动执行线程:触发后重入主循环。"""
        while True:
            time.sleep(1)
            fired = self.cron.consume_queue()
            if not fired:
                continue
            with self._lock:
                turn_start = len(history)
                for job in fired:
                    history.append(user_message(f"[Scheduled] {job.prompt}"))
                    Terminal.info("cron auto", job.prompt[:60], Terminal.MAGENTA)
                self.run_cycle(history, context)
                context.update(self.memories.update_context(context, history))
                self.print_turn_assistants(history, turn_start)

    # ── CLI ──

    def _startup_maintenance(self):
        """CLI 启动时 best-effort:过期记忆归档、索引重建、retention。"""
        try:
            self.memory.expire()
            self.memory.rebuild_index()
        except Exception as exc:
            Terminal.info("memory", f"startup maintenance failed: {exc}",
                          Terminal.RED)
        try:
            self.retention.run_startup()
        except Exception as exc:
            Terminal.info("retention", f"startup failed: {exc}", Terminal.RED)

    def run_cli(self):
        self._startup_maintenance()
        self.cron.start()
        self.lead_monitor.start()
        try:
            self._run_cli_loop()
        finally:
            self.lead_monitor.stop()
            self.metrics.flush()

    def _run_cli_loop(self):
        Terminal.print("MiniClaudeCode: coding agent")
        Terminal.print("Enter a question, press Enter to send. Type q to quit.\n")
        history: list[dict] = []
        context = self.memories.update_context({}, history)
        threading.Thread(target=self.cron_autorun_loop,
                         args=(history, context), daemon=True).start()
        while True:
            try:
                query = input(Configs.PROMPT)
            except (EOFError, KeyboardInterrupt):
                break
            if query.strip().lower() in ("q", "exit", ""):
                break
            self.hooks.trigger("UserPromptSubmit", query)
            turn_start = len(history)
            history.append(user_message(query))
            with self._lock:
                self.run_cycle(history, context)
                context = self.memories.update_context(context, history)
                self.print_turn_assistants(history, turn_start)

            inbox = self.protocol.consume_lead_inbox(route_protocol=True)
            if inbox:
                def inbox_label(msg):
                    req_id = msg.get("metadata", {}).get("request_id", "")
                    suffix = f" req:{req_id}" if req_id else ""
                    return f"{msg.get('type', 'message')}{suffix}"

                inbox_text = "\n".join(
                    f"From {m['from']} [{inbox_label(m)}]: "
                    f"{m['content'][:200]}" for m in inbox)
                history.append(user_message(f"[Inbox]\n{inbox_text}"))
            Terminal.print("")
