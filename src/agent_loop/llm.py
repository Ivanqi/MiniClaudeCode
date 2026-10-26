"""LLM 路由调用与本地→API 重试升级。"""
from src.ActiveTeammates import ACTIVE_TEAMMATES
from src.RecoverySystem import with_retry
from src.Terminal import Terminal


def call_llm(loop, messages: list, context: dict, tools: list,
             slim_tools: list | None, state, max_tokens: int,
             caller: str = "main", flags: dict | None = None):
    """路由决策 + 带重试的模型调用,返回 (response, decision)。"""
    route_flags = dict(flags or {})
    route_flags.setdefault("escalated", state.turn_escalated)
    route_flags.setdefault("active_teammates",
                           bool(ACTIVE_TEAMMATES.names()))
    decision = loop.router.route(messages, caller=caller,
                                flags=route_flags)
    if state.started_local is None:
        state.started_local = decision.local  # 记录 turn 起点(轨迹数据用)
    client, model, routed_max = loop.router.apply(
        decision, max_tokens,
        model_override=None if decision.local else state.current_model)
    if decision.strip_tools:
        routed_tools = None
    elif decision.local:
        routed_tools = slim_tools
    else:
        routed_tools = tools
    system = loop.prompt_builder.assemble(context, routed_tools)
    return (create_with_retry(
        loop, client, messages, system, routed_tools, routed_max, model,
        state, decision, context=context, full_tools=tools,
        caller=caller, flags=route_flags),
        decision)


def create_with_retry(loop, client, messages: list, system: str,
                      tools: list | None, max_tokens: int, model: str,
                      state, decision, context: dict | None = None,
                      full_tools: list | None = None,
                      caller: str = "main", flags: dict | None = None):
    """with_retry 封装;本地模型重试耗尽时升级 API 再试一次。"""
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
            upgraded = loop.router.route(
                messages, caller=caller, flags=upgraded_flags)
            api_client, api_model, api_max = loop.router.apply(
                upgraded, max_tokens, model_override=state.current_model)
            full_system = loop.prompt_builder.assemble(context or {},
                                                       full_tools)
            return with_retry(
                lambda: api_client.create(messages, system=full_system,
                                          tools=full_tools,
                                          max_tokens=api_max,
                                          model=api_model),
                state, client=api_client)
        raise
