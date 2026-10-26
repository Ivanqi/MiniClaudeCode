"""记忆上下文:把记忆索引、MCP 连接与活跃队友组装为轮次上下文。

上下文以普通 dict 在 AgentLoop 中流转,由 PromptBuilder 注入 system prompt。
"""
from src.ActiveTeammates import ACTIVE_TEAMMATES, ActiveTeammates
from src.Configs import Configs
from src.McpSystem import MCP_SYSTEM, McpSystem
from src.TaskSystem import TASK_SYSTEM, TaskSystem


class Memories:
    """记忆上下文组装器。"""

    MEMORY_PREVIEW_CHARS = 2000
    TASK_SUMMARY_CHARS = 2000

    def update_context(self, context: dict, messages: list,
                       mcp: McpSystem = MCP_SYSTEM,
                       teammates: ActiveTeammates = ACTIVE_TEAMMATES,
                       tasks: TaskSystem = TASK_SYSTEM) -> dict:
        memories = ""
        if Configs.MEMORY_INDEX.exists():
            memories = Configs.MEMORY_INDEX.read_text()[:self.MEMORY_PREVIEW_CHARS]
        try:
            task_summary = tasks.run_list_tasks()[:self.TASK_SUMMARY_CHARS]
        except Exception:
            task_summary = ""
        return {
            "memories": memories,
            "connected_mcp": mcp.client_names(),
            "active_teammates": teammates.names(),
            "task_summary": task_summary,
        }


# 全局单例。
MEMORIES = Memories()
