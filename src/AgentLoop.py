"""主代理循环:编排 LLM 调用、工具分发、上下文压缩与后台注入。

一个轮次 = 注入定时/后台工作 → 准备上下文 → 调用模型 →
执行 tool_calls → 回传 tool 结果,循环直到模型不再产生工具调用。
"""
import threading
import time

from src.BackgroundTaskSystem import BACKGROUND_TASKS
from src.BasicOper import BASIC_OPER
from src.Configs import Configs
from src.ContextCompact import ContextCompact
from src.CronScheduler import CRON_SCHEDULER
from src.HookSystem import HOOK_SYSTEM
from src.LlmClient import (assistant_message, tool_message, user_message)
from src.Memories import MEMORIES
from src.McpSystem import MCP_SYSTEM
from src.PromptBuilder import PromptBuilder
from src.ProtocolSystem import PROTOCOL
from src.RecoverySystem import (RecoveryState, is_prompt_too_long_error,
                                with_retry)
from src.SkillSystem import SKILL_SYSTEM
from src.SubagentSystem import SubagentSystem
from src.TaskSystem import TASK_SYSTEM
from src.TeammateSystem import TeammateSystem
from src.Terminal import Terminal
from src.ToolRegistry import ToolRegistry
from src.WorktreeSystem import WORKTREE_SYSTEM

TODO_REMINDER_ROUNDS = 3


class AgentLoop:
    """主代理循环:持有全部子系统,提供 run_cycle 与 CLI 入口。"""

    def __init__(self, client):
        self.client = client
        self.basic = BASIC_OPER
        self.hooks = HOOK_SYSTEM
        self.compact = ContextCompact()
        self.background = BACKGROUND_TASKS
        self.cron = CRON_SCHEDULER
        self.mcp = MCP_SYSTEM
        self.protocol = PROTOCOL
        self.memories = MEMORIES
        self.prompt_builder = PromptBuilder()
        self.teammates = TeammateSystem(client)
        self.subagents = SubagentSystem(client)

        # 工具注册表:各系统自持工具定义与处理器,在此聚合。
        self.tool_registry = ToolRegistry(self.mcp)
        # 通过 handlers() 方法返回该字典的副本或直接引用，供外部查询或调用
        for system in (self.basic, TASK_SYSTEM, WORKTREE_SYSTEM, SKILL_SYSTEM,
                       self.cron, self.mcp, self.teammates, self.subagents,
                       self.compact):
            self.tool_registry.register_system(system)

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
    
    def build_tool_results(self, results: list[dict]) -> list[dict]:
        """工具结果 + 后台完成通知,一起作为本轮工具消息回传。"""
        content = list(results)
        for note in self.background.collect_results():
            content.append(user_message(note))
        return content

    def inject_background_notifications(self, messages: list):
        notes = self.background.collect_results()
        if notes:
            messages.extend(user_message(note) for note in notes)

    def call_llm(self, messages: list, context: dict, tools: list,
                 state: RecoveryState, max_tokens: int):
        # 提示词 和 skills 加载
        system = self.prompt_builder.assemble(context)
        # tools llm function call 定义
        return with_retry(
            lambda: self.client.create(messages, system=system, tools=tools,
                                       max_tokens=max_tokens,
                                       model=state.current_model),
            state, client=self.client)

    # ── 主循环 ──

    def run_cycle(self, messages: list, context: dict):
        tools, handlers = self.tool_registry.assemble()
        state = RecoveryState()
        max_tokens = Configs.DEFAULT_MAX_TOKENS

        while True:
            fired = self.cron.consume_queue()
            for job in fired:
                messages.append(user_message(f"[Scheduled] {job.prompt}"))
                Terminal.info("cron inject", job.prompt[:60], Terminal.MAGENTA)

            self.inject_background_notifications(messages)

            if self.rounds_since_todo >= TODO_REMINDER_ROUNDS:
                messages.append(user_message("<reminder>Update your todos.</reminder>"))
                self.rounds_since_todo = 0

            self.prepare_context(messages)
            context = self.memories.update_context(context, messages)
            tools, handlers = self.tool_registry.assemble()

            try:
                print("call_llm args")
                print(messages)
                print(context)
                # print(tools)
                response = self.call_llm(messages, context, tools, state, max_tokens)
                print(response)
            except Exception as e:
                # 应急
                if is_prompt_too_long_error(e) and not state.has_attempted_reactive_compact:
                    messages[:] = self.compact.reactive_compact(messages, self.client)
                    state.has_attempted_reactive_compact = True
                    continue
                messages.append({"role": "assistant",
                                 "content": f"[Error] {type(e).__name__}: {e}"})
                return
            if response.finish_reason == "length":
                if not state.has_escalated:
                    max_tokens = Configs.ESCALATED_MAX_TOKENS
                    state.has_escalated = True
                    Terminal.info("max_tokens", f"retry with {max_tokens}",
                                  Terminal.YELLOW)
                    continue
                messages.append(assistant_message(response))
                if state.recovery_count < Configs.MAX_RECOVERY_RETRIES:
                    messages.append(user_message(Configs.CONTINUATION_PROMPT))
                    state.recovery_count += 1
                    continue
                return

            max_tokens = Configs.DEFAULT_MAX_TOKENS
            state.has_escalated = False
            messages.append(assistant_message(response))
            if not response.has_tool_calls:
                self.hooks.trigger("Stop", messages)
                return

            results = []
            compacted_now = False
            # function call
            for call in response.tool_calls:
                Terminal.print(f"{Terminal.CYAN}> {call.name}{Terminal.RESET}")

                if call.name == "compact":
                    messages[:] = self.compact.compact_history(messages, self.client)
                    messages.append(user_message(
                        "[Compacted. Continue with summarized context.]"))
                    compacted_now = True
                    break

                blocked = self.hooks.trigger("PreToolUse", call)
                if blocked:
                    results.append(tool_message(call.id, str(blocked)))
                    continue

                # 慢操作放后台
                if self.background.should_run_background(call.name, call.arguments):
                    bg_id = self.background.start(call, handlers)
                    output = (f"[Background task {bg_id} started] "
                              "Result will arrive as a task_notification.")
                    results.append(tool_message(call.id, output))
                    continue

                # tool handler 辅助工具
                handler = handlers.get(call.name)
                output = self.basic.call_tool_handler(handler, call.arguments, call.name)
                self.hooks.trigger("PostToolUse", call, output)
                Terminal.print(str(output)[:300])

                if call.name == "todo_write":
                    self.rounds_since_todo = 0
                else:
                    self.rounds_since_todo += 1

                results.append(tool_message(call.id, output))

            if compacted_now:
                continue

            # 通知和工具结果合入同一条 user 消息
            messages.extend(self.build_tool_results(results))

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

    def run_cli(self):
        self.cron.start()
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
