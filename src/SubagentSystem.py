"""子代理系统:task 工具背后的聚焦式子代理。

子代理只暴露最小工具集(bash/文件操作),循环执行直到
不再产生工具调用,最后只返回文本摘要,不向外发送过程消息。
所有工具调用经过统一 ToolDispatcher:
- 本地路由时强制只读(只有 read_file/glob);
- API 路由允许 bash/写/编辑,但写仍受工作区/敏感路径策略约束;
- 轮数、工具调用数、墙钟都有预算,超限做一次无工具总结。
"""
from src.BasicOper import BASIC_OPER, BasicOper
from src.Configs import Configs
from src.LlmClient import (assistant_message, make_function_tool, tool_message,
                           user_message)
from src.Roles import RoleSpec
from src.TaskSystem import TASK_SYSTEM, TaskSystem
from src.TeamMetrics import TEAM_METRICS, TeamMetrics
from src.Terminal import Terminal
from src.ToolDispatcher import ToolDispatcher
from src.ToolPolicy import (BASH_DENY, SUBAGENT_LOCAL_TOOLS, SUBAGENT_TOOLS,
                            ToolPolicy, TurnBudget, skipped_tool_messages,
                            subagent_local_policy, subagent_policy)

SUB_ROUND_LIMIT = 30


class SubagentSystem:
    """子代理系统:spawn_subagent 是 task 工具的实现。"""

    SUB_SYSTEM = (
        f"You are a coding subagent at {Configs.WORKDIR}. "
        "Complete the task, then return a concise final summary. "
        "Do not spawn more agents."
    )

    def __init__(self, client, router=None,
                 hooks=None,
                 basic: BasicOper = BASIC_OPER,
                 dispatcher: ToolDispatcher | None = None,
                 metrics: TeamMetrics | None = None,
                 tasks: TaskSystem = TASK_SYSTEM):
        self.client = client
        self.router = router  # None 表示不路由,直接用 self.client(向后兼容)
        self.hooks = hooks
        self.basic = basic
        self.metrics = metrics or TEAM_METRICS
        self.tasks = tasks
        self.dispatcher = dispatcher or ToolDispatcher(
            basic=self.basic, hooks=self.hooks)

    # ── 子代理工具集 ──

    def sub_tools(self) -> list[dict]:
        return [
            make_function_tool("bash", "Run a shell command.",
                {"type": "object",
                 "properties": {"command": {"type": "string"},
                                "timeout": {"type": "integer", "minimum": 1,
                                            "maximum": Configs.BASH_TIMEOUT_MAX}},
                 "required": ["command"]}),
            make_function_tool("read_file", "Read file contents.",
                {"type": "object",
                 "properties": {"path": {"type": "string"},
                                "limit": {"type": "integer", "minimum": 1,
                                          "maximum": Configs.READ_FILE_MAX_LINES},
                                "offset": {"type": "integer", "minimum": 0}},
                 "required": ["path"]}),
            make_function_tool("write_file", "Write content to a file.",
                {"type": "object",
                 "properties": {"path": {"type": "string"},
                                "content": {"type": "string"}},
                 "required": ["path", "content"]}),
            make_function_tool("edit_file", "Replace exact text in a file once.",
                {"type": "object",
                 "properties": {"path": {"type": "string"},
                                "old_text": {"type": "string"},
                                "new_text": {"type": "string"}},
                 "required": ["path", "old_text", "new_text"]}),
            make_function_tool("glob", "Find files matching a glob pattern.",
                {"type": "object",
                 "properties": {"pattern": {"type": "string"}},
                 "required": ["pattern"]}),
            make_function_tool("list_tasks", "List tasks (read-only).",
                {"type": "object", "properties": {}, "required": []}),
        ]

    def sub_handlers(self) -> dict:
        return {
            "bash": self.basic.run_bash, "read_file": self.basic.run_read,
            "write_file": self.basic.run_write, "edit_file": self.basic.run_edit,
            "glob": self.basic.run_glob,
            "list_tasks": lambda: self.tasks.run_list_tasks(),
        }

    def tools_for(self, local: bool, role_spec: RoleSpec | None = None,
                  tools_override=None) -> list[dict]:
        tools = self.sub_tools()
        if role_spec is not None:
            names = set(role_spec.allowed_tools)
        elif local:
            names = set(SUBAGENT_LOCAL_TOOLS)
        else:
            names = set(SUBAGENT_TOOLS)
        if tools_override is not None:
            names &= set(tools_override)
        if local:
            names &= set(SUBAGENT_LOCAL_TOOLS)
        return [tool for tool in tools
                if tool["function"]["name"] in names]

    def policy_for(self, local: bool, role_spec: RoleSpec | None = None,
                   base_dir=None):
        if role_spec is None:
            return subagent_local_policy() if local else subagent_policy()
        if local:
            return ToolPolicy(
                role=role_spec.name,
                allowed=frozenset(set(role_spec.allowed_tools)
                                  & set(SUBAGENT_LOCAL_TOOLS)),
                read_only=True,
                writable_roots=(),
                force_cwd=base_dir or None,
                bash_mode=BASH_DENY,
                allow_mcp=False,
            )
        return role_spec.to_policy(base_dir)

    # ── 子代理循环 ──

    def spawn_subagent(self, description: str,
                       role_spec: RoleSpec | None = None,
                       base_dir=None, tools_override=None,
                       usage_sink=None, prefer_api: bool = False) -> str:
        messages = [user_message(description)]
        all_handlers = self.sub_handlers()
        system = ((role_spec.prompt() + f"\nWork directory: {base_dir}")
                  if role_spec is not None and base_dir
                  else role_spec.prompt() if role_spec is not None
                  else self.SUB_SYSTEM)
        budget = TurnBudget(
            max_rounds=SUB_ROUND_LIMIT,
            max_tool_calls=Configs.SUBAGENT_MAX_TOOL_CALLS,
            max_wall_s=Configs.SUBAGENT_MAX_WALL_SECONDS,
            max_total_tokens=0)
        escalated = False
        closing_reason = ""
        client, model = self.client, None
        decision = None

        while True:
            closing_reason = budget.exhausted_reason()
            if closing_reason:
                break

            local = False
            use_router = self.router is not None and not prefer_api
            if use_router:
                decision = self.router.route(
                    messages, caller="subagent", flags={"escalated": escalated})
                client, model, max_tokens = self.router.apply(
                    decision, Configs.SUBAGENT_MAX_TOKENS)
                local = decision.local
            else:
                max_tokens = Configs.SUBAGENT_MAX_TOKENS

            tools = self.tools_for(local, role_spec, tools_override)
            tool_names = {tool["function"]["name"] for tool in tools}
            handlers = {name: handler for name, handler in all_handlers.items()
                        if name in tool_names}
            policy = self.policy_for(local, role_spec, base_dir)
            budget.record_round()
            try:
                response = client.create(
                    messages, system=system, tools=tools,
                    max_tokens=max_tokens, model=model)
            except Exception as exc:
                return (f"Error: Subagent failed: "
                        f"{type(exc).__name__}: {exc}")
            budget.record_usage(getattr(response, "usage", None))
            if usage_sink is not None:
                try:
                    usage_sink(getattr(response, "usage", None))
                except Exception:
                    pass
            else:
                self.metrics.add_usage(getattr(response, "usage", None),
                                       "subagent")

            if response.has_parse_error:
                # 丢弃坏调用;本地模型直接升级,不追加坏消息
                response.tool_calls = [c for c in response.tool_calls
                                       if not c.parse_error]
                Terminal.info("parse-error", "subagent dropped malformed call",
                              Terminal.RED)
                if use_router and decision is not None and decision.local:
                    escalated = True
                    Terminal.info("escalate", "subagent parse error → API",
                                  Terminal.RED)
                    continue
            messages.append(assistant_message(response))
            if not response.has_tool_calls:
                break

            results = []
            for call_index, call in enumerate(response.tool_calls):
                reason = budget.exhausted_reason()
                if reason:
                    results.extend(skipped_tool_messages(
                        response.tool_calls[call_index:],
                        f"subagent budget exceeded: {reason}"))
                    closing_reason = reason
                    break
                budget.reserve_tool_call()
                auth = self.dispatcher.authorize(
                    call, policy, handlers, allow_prompt=True,
                    agent=policy.role)
                if not auth.allowed:
                    output = auth.denial.content if auth.denial else "Denied"
                else:
                    output = self.dispatcher.execute(
                        call, policy, handlers, authorization=auth).content
                results.append(tool_message(call.id, output))
            messages.extend(results)
            if closing_reason:
                break

        summary = self._last_text(messages)
        if closing_reason:
            final = self._final_summary(messages, client, model,
                                        closing_reason, system=system)
            summary = final or summary
            note = f"[subagent budget reached: {closing_reason}]"
            return f"{summary}\n{note}" if summary else note
        return summary or "Subagent finished without a text summary."

    @staticmethod
    def _last_text(messages: list) -> str:
        for msg in reversed(messages):
            if msg.get("role") == "assistant":
                text = msg.get("content")
                if isinstance(text, str) and text.strip():
                    return text.strip()
        return ""

    def _final_summary(self, messages: list, client, model,
                       reason: str, system: str | None = None) -> str:
        """预算耗尽时做一次无工具总结(不追加副作用消息到返回历史)。"""
        prompt = (f"[Subagent budget reached: {reason}. Based on the work so "
                  "far, return a concise final summary. Do not call tools.]")
        try:
            response = client.create(
                messages + [user_message(prompt)],
                system=system or self.SUB_SYSTEM,
                tools=None, max_tokens=Configs.SUBAGENT_MAX_TOKENS,
                model=model)
            return (response.content or "").strip()
        except Exception as exc:
            Terminal.info("subagent", f"final summary failed: {exc}",
                          Terminal.RED)
            return ""

    # ── 工具层:task ──

    def run_task(self, description: str) -> str:
        return self.spawn_subagent(description)

    def tool_defs(self) -> list[dict]:
        return [
            make_function_tool("task",
                "Launch a focused subagent. Returns only its final summary.",
                {"type": "object",
                 "properties": {"description": {"type": "string"}},
                 "required": ["description"]}),
        ]

    def handlers(self) -> dict:
        return {"task": self.run_task}
