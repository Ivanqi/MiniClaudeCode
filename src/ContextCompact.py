"""上下文压缩:预算控制、剪裁与摘要。

压缩是分层的:先收缩超大工具结果,再剪裁旧消息区间,
只有在上下文仍然过大、或模型显式请求 compact 时才调用模型做摘要。
"""
import json
import time
from pathlib import Path

from src.Configs import Configs
from src.LlmClient import make_function_tool, user_message
from src.Terminal import Terminal


class ContextCompact:
    """上下文压缩器:只依赖消息列表与 LLM 客户端,无其他耦合。"""

    def __init__(self, tool_results_dir: Path | None = None):
        self.tool_results_dir = tool_results_dir or Configs.TOOL_RESULTS_DIR

    # ── 消息结构判断 ──

    @staticmethod
    def estimate_size(messages: list) -> int:
        return len(json.dumps(messages, default=str))

    @staticmethod
    def message_has_tool_use(message: dict) -> bool:
        if message.get("role") != "assistant":
            return False
        return bool(message.get("tool_calls"))

    @staticmethod
    def is_tool_result_message(message: dict) -> bool:
        return message.get("role") == "tool"

    def collect_tool_results(self, messages: list) -> list[dict]:
        return [msg for msg in messages if self.is_tool_result_message(msg)]

    def _tail_tool_results(self, messages: list) -> list[dict]:
        """取消息列表尾部连续的工具结果消息(最后一轮工具执行)。"""
        run = []
        for msg in reversed(messages):
            if self.is_tool_result_message(msg):
                run.append(msg)
            else:
                break
        return list(reversed(run))

    # ── 分层压缩 ──

    def persist_large_output(self, tool_call_id: str, output: str) -> str:
        """超大输出落盘,消息中只保留路径与预览。"""
        if len(output) <= Configs.PERSIST_THRESHOLD:
            return output
        self.tool_results_dir.mkdir(parents=True, exist_ok=True)
        path = self.tool_results_dir / f"{tool_call_id}.txt"
        if not path.exists():
            path.write_text(output)
        return (f"<persisted-output>\nFull output: {path}\n"
                f"Preview:\n{output[:2000]}\n</persisted-output>")

    def tool_result_budget(self, messages: list,
                           max_bytes: int = Configs.TOOL_RESULT_BUDGET) -> list:
        """把最近一轮工具结果的总字节数压到预算内(优先压缩最大的)。"""
        if not messages:
            return messages
        run = self._tail_tool_results(messages)
        if not run:
            return messages
        total = sum(len(str(msg.get("content", ""))) for msg in run)
        if total <= max_bytes:
            return messages
        for msg in sorted(run,
                          key=lambda m: len(str(m.get("content", ""))),
                          reverse=True):
            if total <= max_bytes:
                break
            text = str(msg.get("content", ""))
            msg["content"] = self.persist_large_output(
                msg.get("tool_call_id", "unknown"), text)
            total = sum(len(str(m.get("content", ""))) for m in run)
        return messages

    def snip_compact(self, messages: list, max_messages: int = 50) -> list:
        """保留头部与尾部,剪掉中间旧消息,且不拆散 tool_calls 与结果配对。"""
        if len(messages) <= max_messages:
            return messages
        head_end, tail_start = 3, len(messages) - (max_messages - 3)
        if head_end > 0 and self.message_has_tool_use(messages[head_end - 1]):
            while head_end < len(messages) and self.is_tool_result_message(messages[head_end]):
                head_end += 1
        if (tail_start > 0 and tail_start < len(messages)
                and self.is_tool_result_message(messages[tail_start])
                and self.message_has_tool_use(messages[tail_start - 1])):
            tail_start -= 1
        if head_end >= tail_start:
            return messages
        snipped = tail_start - head_end
        return (messages[:head_end]
                + [user_message(f"[snipped {snipped} messages]")]
                + messages[tail_start:])

    def micro_compact(self, messages: list) -> list:
        """只保留最近 N 条工具结果,更早的替换为占位文本。"""
        tool_results = self.collect_tool_results(messages)
        if len(tool_results) <= Configs.KEEP_RECENT_TOOL_RESULTS:
            return messages
        for msg in tool_results[:-Configs.KEEP_RECENT_TOOL_RESULTS]:
            if len(str(msg.get("content", ""))) > 120:
                msg["content"] = "[Earlier tool result compacted. Re-run if needed.]"
        return messages

    # ── 转录与摘要 ──

    def write_transcript(self, messages: list) -> Path:
        transcript_dir = Configs.TRANSCRIPT_DIR
        transcript_dir.mkdir(parents=True, exist_ok=True)
        path = transcript_dir / f"transcript_{int(time.time())}.jsonl"
        with path.open("w") as f:
            for msg in messages:
                f.write(json.dumps(msg, default=str) + "\n")
        return path

    def summarize_history(self, messages: list, client) -> str:
        conversation = json.dumps(messages, default=str)[:80000]
        prompt = ("Summarize this coding-agent conversation so work can continue. "
                  "Preserve current goal, key findings, changed files, remaining work, "
                  "and user constraints.\n\n" + conversation)
        response = client.create(
            [user_message(prompt)], max_tokens=Configs.SUMMARY_MAX_TOKENS)
        return response.content or "(empty summary)"

    def compact_history(self, messages: list, client) -> list:
        transcript = self.write_transcript(messages)
        Terminal.info("compact", f"transcript saved: {transcript}", Terminal.CYAN)
        summary = self.summarize_history(messages, client)
        return [user_message(f"[Compacted]\n\n{summary}")]

    def reactive_compact(self, messages: list, client) -> list:
        """上下文超长时的应急压缩:转录 + 摘要旧历史,保留最近几条。"""
        transcript = self.write_transcript(messages)
        Terminal.info("reactive compact", f"transcript saved: {transcript}", Terminal.RED)
        tail_start = max(0, len(messages) - 5)
        if (tail_start > 0 and tail_start < len(messages)
                and self.is_tool_result_message(messages[tail_start])
                and self.message_has_tool_use(messages[tail_start - 1])):
            tail_start -= 1
        try:
            summary = self.summarize_history(messages[:tail_start], client)
        except Exception:
            summary = "Earlier conversation was trimmed after a prompt-too-long error."
        return [user_message(f"[Reactive compact]\n\n{summary}"), *messages[tail_start:]]

    # ── 工具层:compact 由 AgentLoop 特殊处理,这里只提供定义 ──

    def tool_defs(self) -> list[dict]:
        return [
            make_function_tool("compact",
                "Summarize earlier conversation and continue with compacted context.",
                {"type": "object",
                 "properties": {"focus": {"type": "string"}},
                 "required": []}),
        ]

    def handlers(self) -> dict:
        return {}
