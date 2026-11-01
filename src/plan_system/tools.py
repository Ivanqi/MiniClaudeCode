"""plan 工具的 JSON Schema 与 handler 映射。"""
from src.LlmClient import make_function_tool


class PlanToolsMixin:
    """choose_strategy/create_plan/execute_plan/plan_execute/plan_status。"""

    def tool_defs(self) -> list[dict]:
        return [
            make_function_tool(
                "choose_strategy",
                "Recommend react, hybrid, or plan for a goal. Lead-only; "
                "does not create or execute a plan.",
                {"type": "object",
                 "properties": {
                     "goal": {"type": "string"},
                     "active_tasks": {"type": "integer", "minimum": 0},
                     "active_teammates": {"type": "boolean"},
                     "prior_failure": {"type": "boolean"},
                     "prior_revision": {"type": "integer", "minimum": 0}},
                 "required": ["goal"]}),
            make_function_tool(
                "create_plan",
                "Create and persist a multi-step plan for a goal. Lead-only; "
                "planning model output is validated before saving.",
                {"type": "object",
                 "properties": {"goal": {"type": "string"},
                                "constraints": {"type": "string"}},
                 "required": ["goal"]}),
            make_function_tool(
                "execute_plan",
                "Execute a saved plan by id, resuming completed steps. "
                "Lead-only; approval-required steps prompt the human.",
                {"type": "object",
                 "properties": {"plan_id": {"type": "string"}},
                 "required": ["plan_id"]}),
            make_function_tool(
                "plan_execute",
                "Create a plan for a goal and execute it in one call. "
                "Lead-only; use only when the user explicitly wants a plan.",
                {"type": "object",
                 "properties": {"goal": {"type": "string"},
                                "constraints": {"type": "string"}},
                 "required": ["goal"]}),
            make_function_tool(
                "plan_status",
                "Show one plan (with step results/evidence) or recent plans.",
                {"type": "object",
                 "properties": {"plan_id": {"type": "string"}},
                 "required": []}),
        ]

    def handlers(self) -> dict:
        return {
            "choose_strategy": self.run_choose_strategy,
            "create_plan": self.run_create_plan,
            "execute_plan": self.run_execute_plan,
            "plan_execute": self.run_plan_execute,
            "plan_status": self.run_plan_status,
        }
