"""AgentLoop facade:对外保留原有构造/方法,内部委托给各阶段模块。"""
from src.ModelRouter import ModelRouter
from src.ToolDispatcher import ToolDispatcher

from src.agent_loop.cli import cron_autorun_loop as cli_cron_autorun_loop
from src.agent_loop.cli import print_turn_assistants as cli_print_assistants
from src.agent_loop.cli import run_cli as cli_run
from src.agent_loop.cli import run_cli_loop as cli_run_loop
from src.agent_loop.cli import startup_maintenance as cli_startup_maintenance
from src.agent_loop.context import (build_tool_results as context_build_results)
from src.agent_loop.context import (
    inject_background_notifications as context_inject_background)
from src.agent_loop.context import prepare_context as context_prepare
from src.agent_loop.cycle import CycleRunner
from src.agent_loop.llm import call_llm as call_llm_for_loop
from src.agent_loop.plan import available_tool_names as get_tool_names
from src.agent_loop.plan import run_plan_step
from src.agent_loop.wiring import wire


class AgentLoop:
    """主代理循环:持有全部子系统,提供 run_cycle 与 CLI 入口。"""

    def __init__(self, client, router: ModelRouter | None = None,
                 dispatcher: ToolDispatcher | None = None):
        wire(self, client, router, dispatcher,
             step_runner=self._run_plan_step,
             tool_names_provider=self.available_tool_names)

    # ── 上下文管道 ──

    def prepare_context(self, messages: list) -> list:
        return context_prepare(self.compact, messages, self.client)

    def build_tool_results(self, results: list[dict],
                           collect_background: bool = True) -> list[dict]:
        return context_build_results(
            self.background, results, collect_background)

    def inject_background_notifications(self, messages: list):
        context_inject_background(self.background, messages)

    # ── LLM 调用 ──

    def call_llm(self, messages: list, context: dict, tools: list,
                 slim_tools: list | None, state, max_tokens: int,
                 caller: str = "main", flags: dict | None = None):
        return call_llm_for_loop(
            self, messages, context, tools, slim_tools, state,
            max_tokens, caller=caller, flags=flags)

    # ── 主循环 ──

    def run_cycle(self, messages: list, context: dict, *,
                  policy=None, budget=None, tools_allowlist=None,
                  caller: str = "main", flags: dict | None = None,
                  allow_cron: bool = True, allow_background: bool = True,
                  trigger_stop: bool = True, trajectory: bool = True,
                  turn_id: str | None = None,
                  refresh_context: bool = True,
                  todo_reminders: bool = True) -> dict:
        return CycleRunner(
            self, messages, context, policy=policy, budget=budget,
            tools_allowlist=tools_allowlist, caller=caller, flags=flags,
            allow_cron=allow_cron, allow_background=allow_background,
            trigger_stop=trigger_stop, trajectory=trajectory,
            turn_id=turn_id, refresh_context=refresh_context,
            todo_reminders=todo_reminders).run()

    # ── Plan step ──

    def available_tool_names(self) -> list[str]:
        return get_tool_names(self)

    def _run_plan_step(self, prompt: str, policy, budget,
                       tool_names, turn_id: str) -> dict:
        return run_plan_step(self, prompt, policy, budget, tool_names, turn_id)

    # ── CLI / cron ──

    def print_turn_assistants(self, messages: list, turn_start: int):
        cli_print_assistants(messages, turn_start)

    def cron_autorun_loop(self, history: list, context: dict):
        cli_cron_autorun_loop(self, history, context)

    def _startup_maintenance(self):
        cli_startup_maintenance(self)

    def run_cli(self):
        cli_run(self)

    def _run_cli_loop(self):
        cli_run_loop(self)
