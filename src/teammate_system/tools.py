"""lead 侧团队工具的 JSON Schema 与 handler 映射。"""
from src.LlmClient import make_function_tool


class TeammateToolsMixin:
    """spawn/send/check/status/shutdown/request_plan/review_plan 工具。"""

    def tool_defs(self) -> list[dict]:
        try:
            roles = self.role_registry().spawnable_names()
        except Exception:
            roles = ["developer"]
        roles = roles or ["developer"]
        return [
            make_function_tool("spawn_teammate", "Spawn an autonomous teammate.",
                {"type": "object",
                 "properties": {"name": {"type": "string"},
                                "role": {"type": "string", "enum": roles},
                                "prompt": {"type": "string"}},
                 "required": ["name", "role", "prompt"]}),
            make_function_tool("send_message", "Send message to a teammate.",
                {"type": "object",
                 "properties": {"to": {"type": "string"},
                                "content": {"type": "string"}},
                 "required": ["to", "content"]}),
            make_function_tool("check_inbox",
                "Check inbox for messages and protocol responses.",
                {"type": "object", "properties": {}, "required": []}),
            make_function_tool("team_status",
                "Show team metrics, budget, active teammates, task board and "
                "pending protocol requests.",
                {"type": "object", "properties": {}, "required": []}),
            make_function_tool("request_shutdown",
                "Request a teammate to shut down.",
                {"type": "object",
                 "properties": {"teammate": {"type": "string"}},
                 "required": ["teammate"]}),
            make_function_tool("request_plan",
                "Ask a teammate to submit a plan.",
                {"type": "object",
                 "properties": {"teammate": {"type": "string"},
                                "task": {"type": "string"}},
                 "required": ["teammate", "task"]}),
            make_function_tool("review_plan",
                "Approve or reject a submitted plan.",
                {"type": "object",
                 "properties": {"request_id": {"type": "string"},
                                "approve": {"type": "boolean"},
                                "feedback": {"type": "string"}},
                 "required": ["request_id", "approve"]}),
        ]

    def handlers(self) -> dict:
        return {
            "spawn_teammate": self.spawn,
            "send_message": self.run_send_message,
            "check_inbox": self.run_check_inbox,
            "team_status": self.run_team_status,
            "request_shutdown": self.run_request_shutdown,
            "request_plan": self.run_request_plan,
            "review_plan": self.run_review_plan,
        }
