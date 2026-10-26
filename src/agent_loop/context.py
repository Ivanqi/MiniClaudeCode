"""每轮上下文管道:压缩、工具结果/后台通知、运行时上下文拼接。"""
from src.Configs import Configs
from src.LlmClient import user_message


def prepare_context(compact, messages: list, client) -> list:
    """每个 LLM 轮次都经过同一上下文预算管道。"""
    messages[:] = compact.tool_result_budget(messages)
    messages[:] = compact.snip_compact(messages)
    messages[:] = compact.micro_compact(messages)
    if compact.estimate_size(messages) > Configs.CONTEXT_LIMIT:
        messages[:] = compact.compact_history(messages, client)
    return messages


def build_tool_results(background, results: list[dict],
                       collect_background: bool = True) -> list[dict]:
    """工具结果 + 后台完成通知,一起作为本轮工具消息回传。"""
    content = list(results)
    if collect_background:
        for note in background.collect_results():
            content.append(user_message(note))
    return content


def inject_background_notifications(background, messages: list):
    notes = background.collect_results()
    if notes:
        messages.extend(user_message(note) for note in notes)


def attach_loop_context(context: dict | None, budget, guard) -> dict:
    """Memories.update_context 会替换 context,运行时信息必须在其后挂载。"""
    merged = dict(context or {})
    if budget is not None:
        merged["runtime"] = budget.runtime_snapshot()
    merged["failed_attempts"] = (guard.failed_attempts()
                                 if guard is not None else [])
    return merged
