"""CLI、cron 自动执行与启动维护。"""
import threading
import time

from src.Configs import Configs
from src.LlmClient import user_message
from src.Terminal import Terminal


def startup_maintenance(loop):
    """CLI 启动时 best-effort:过期记忆归档、索引重建、retention。"""
    try:
        loop.memory.expire()
        loop.memory.rebuild_index()
    except Exception as exc:
        Terminal.info("memory", f"startup maintenance failed: {exc}",
                      Terminal.RED)
    try:
        loop.retention.run_startup()
    except Exception as exc:
        Terminal.info("retention", f"startup failed: {exc}", Terminal.RED)


def print_turn_assistants(messages: list, turn_start: int):
    for msg in messages[turn_start:]:
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            Terminal.print(content)


def cron_autorun_loop(loop, history: list, context: dict):
    """定时任务自动执行线程:触发后重入主循环。"""
    while True:
        time.sleep(1)
        fired = loop.cron.consume_queue()
        if not fired:
            continue
        with loop._lock:
            turn_start = len(history)
            for job in fired:
                history.append(user_message(f"[Scheduled] {job.prompt}"))
                Terminal.info("cron auto", job.prompt[:60], Terminal.MAGENTA)
            loop.run_cycle(history, context)
            context.update(loop.memories.update_context(context, history))
            loop.print_turn_assistants(history, turn_start)


def run_cli(loop):
    loop._startup_maintenance()
    loop.cron.start()
    loop.lead_monitor.start()
    try:
        loop._run_cli_loop()
    finally:
        loop.lead_monitor.stop()
        loop.metrics.flush()


def run_cli_loop(loop):
    Terminal.print("MiniClaudeCode: coding agent")
    Terminal.print("Enter a question, press Enter to send. Type q to quit.\n")
    history: list[dict] = []
    context = loop.memories.update_context({}, history)
    threading.Thread(target=loop.cron_autorun_loop,
                     args=(history, context), daemon=True).start()
    while True:
        try:
            query = input(Configs.PROMPT)
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        loop.hooks.trigger("UserPromptSubmit", query)
        turn_start = len(history)
        history.append(user_message(query))
        with loop._lock:
            loop.run_cycle(history, context)
            context = loop.memories.update_context(context, history)
            loop.print_turn_assistants(history, turn_start)

        inbox = loop.protocol.consume_lead_inbox(route_protocol=True)
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
