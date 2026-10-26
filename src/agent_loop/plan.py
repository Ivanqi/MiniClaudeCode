"""Plan step 的作用域入口与工具名查询。"""
from src.LlmClient import user_message


def available_tool_names(loop) -> list[str]:
    """Planner 的工具提示来源:当前 registry 的完整工具名(Plan 工具会被过滤)。"""
    tools, _ = loop.tool_registry.assemble()
    return [item["function"]["name"] for item in tools]


def run_plan_step(loop, prompt: str, policy, budget, tool_names,
                  turn_id: str) -> dict:
    """PlanExecutor 的作用域 ReAct 入口:单步消息、无 outer 副作用。"""
    messages = [user_message(prompt)]
    return loop.run_cycle(
        messages, {}, policy=policy, budget=budget,
        tools_allowlist=tool_names, caller="plan_step",
        allow_cron=False, allow_background=False,
        trigger_stop=False, trajectory=False, turn_id=turn_id,
        refresh_context=False, todo_reminders=False)
