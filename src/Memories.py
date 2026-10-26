"""记忆上下文:语义/实体/情景记忆检索、任务看板摘要与活跃队友。

每轮从最后一条真实用户消息生成检索 query,只注入 top_k 记忆;
不再把 MEMORY.md 前 2000 字符整段塞进 prompt。
"""
from src.ActiveTeammates import ACTIVE_TEAMMATES, ActiveTeammates
from src.Configs import Configs
from src.McpSystem import MCP_SYSTEM, McpSystem
from src.MemoryStore import MEMORY_STORE, MemoryStore
from src.TaskSystem import TASK_SYSTEM, TaskSystem

_SYNTHETIC_PREFIXES = (
    "<reminder>", "<inbox>", "<task_notification>", "<auto-claimed>",
    "<identity>", "<memory", "[Scheduled]", "[Inbox]", "[Compacted",
    "[Reactive compact", "[Budget", "[Error", "[Plan", "[system]",
    "[Background",
)


class Memories:
    """记忆上下文组装器。"""

    TASK_SUMMARY_CHARS = 2000

    def __init__(self, memory: MemoryStore | None = None,
                 tasks: TaskSystem | None = None):
        self.memory = memory or MEMORY_STORE
        self.tasks = tasks or TASK_SYSTEM

    @staticmethod
    def _last_real_user_query(messages: list) -> str:
        for message in reversed(messages or []):
            if message.get("role") != "user":
                continue
            content = message.get("content")
            if not isinstance(content, str):
                continue
            text = content.strip()
            if not text or text.startswith(_SYNTHETIC_PREFIXES):
                continue
            return text[:2000]
        return ""

    def update_context(self, context: dict, messages: list,
                       mcp: McpSystem = MCP_SYSTEM,
                       teammates: ActiveTeammates = ACTIVE_TEAMMATES,
                       tasks: TaskSystem | None = None) -> dict:
        query = self._last_real_user_query(messages)
        try:
            memories = self.memory.format_context(
                query, top_k=Configs.MEMORY_TOP_K)
        except Exception:
            memories = ""
        board = tasks or self.tasks
        try:
            if hasattr(board, "board_summary"):
                task_summary = board.board_summary()
            else:
                task_summary = board.run_list_tasks()
            task_summary = str(task_summary)[:self.TASK_SUMMARY_CHARS]
        except Exception:
            task_summary = ""
        return {
            "memories": memories,
            "connected_mcp": mcp.client_names(),
            "active_teammates": teammates.names(),
            "task_summary": task_summary,
        }


# 全局单例(兼容旧引用)。
MEMORIES = Memories()
