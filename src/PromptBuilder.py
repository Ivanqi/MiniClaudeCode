"""提示词组装:根据实时上下文重建 system prompt。

每轮对话都从活上下文重建 system prompt,
把记忆、技能目录、MCP 状态、活跃队友等信息注入其中。
"""
from datetime import datetime

from src.Configs import Configs
from src.McpSystem import MCP_SYSTEM
from src.SkillSystem import SKILL_SYSTEM


class PromptBuilder:
    """提示词组装器:组合身份、工具说明、工作区与动态上下文。"""

    PROMPT_SECTIONS = {
        "identity": "You are a coding agent. Act, don't explain.",
        "tools": "Available tools: bash, read_file, write_file, edit_file, glob, "
                 "todo_write, task, load_skill, compact, "
                 "create_task, list_tasks, get_task, claim_task, complete_task, "
                 "schedule_cron, list_crons, cancel_cron, "
                 "spawn_teammate, send_message, check_inbox, "
                 "request_shutdown, request_plan, review_plan, "
                 "create_worktree, remove_worktree, keep_worktree, "
                 "connect_mcp. MCP tools are prefixed mcp__{server}__{tool}.",
        "workspace": f"Working directory: {Configs.WORKDIR}",
        "memory": "Relevant memories are injected below when available.",
    }

    def __init__(self, skills=SKILL_SYSTEM, mcp=MCP_SYSTEM):
        self.skills = skills
        self.mcp = mcp

    def assemble(self, context: dict) -> str:
        sections = [self.PROMPT_SECTIONS["identity"],
                    self.PROMPT_SECTIONS["tools"],
                    self.PROMPT_SECTIONS["workspace"]]
        sections.append(f"Current time: {datetime.now().isoformat(timespec='seconds')}")
        sections.append("Skills catalog:\n" + self.skills.list_skills() +
                        "\nUse load_skill(name) when a skill is relevant.")
        if context.get("memories"):
            sections.append(f"Relevant memories:\n{context['memories']}")
        mcp_names = self.mcp.client_names()
        if mcp_names:
            sections.append(f"Connected MCP servers: {', '.join(mcp_names)}")
        return "\n\n".join(sections)
