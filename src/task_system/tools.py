"""任务工具的文本输出与 JSON Schema 注册。"""
from src.LlmClient import make_function_tool
from src.Terminal import Terminal


class TaskToolsMixin:
    """供 ToolRegistry/ToolDispatcher 调用的任务工具方法。"""

    def run_create_task(self, subject: str, description: str = "",
                        blockedBy: list[str] | None = None,
                        idempotency_key: str | None = None,
                        scope: str = "", target_files=None,
                        acceptance: str = "") -> str:
        try:
            result = self.create_task_result(
                subject, description, blockedBy, idempotency_key,
                scope=scope, target_files=target_files,
                acceptance=acceptance)
        except (TypeError, ValueError) as exc:
            return f"Error: {exc}"
        task = result.task
        deps = f" (blockedBy: {', '.join(blockedBy)})" if blockedBy else ""
        Terminal.info("create", f"{task.subject}{deps}", Terminal.BLUE)
        prefix = "Reused" if result.reused else "Created"
        message = f"{prefix} {task.id}: {task.subject}{deps}"
        if result.reason == "fingerprint":
            message += " (fingerprint match)"
        if result.similar:
            parts = [f"{task_id} {title} ({score:.2f})"
                     for task_id, title, score in result.similar[:5]]
            message += "\nSimilar: " + "; ".join(parts)
        return message

    def run_list_tasks(self, include_completed: bool = False) -> str:
        tasks = self.list_tasks(include_completed=include_completed)
        if not tasks:
            return "No tasks."
        return "\n".join(
            f"  {t.id}: {t.subject} [{t.status}]"
            + (f" owner:{t.owner}" if t.owner else "")
            + (f" (wt:{t.worktree})" if t.worktree else "")
            for t in tasks)

    def run_get_task(self, task_id: str) -> str:
        try:
            return self.get_task_json(task_id)
        except FileNotFoundError:
            return f"Error: task {task_id} not found"
        except ValueError as exc:
            return f"Error: {exc}"

    def run_claim_task(self, task_id: str) -> str:
        try:
            return self.claim_task(task_id, owner="agent")
        except FileNotFoundError:
            return f"Error: task {task_id} not found"
        except ValueError as exc:
            return f"Error: {exc}"

    def run_complete_task(self, task_id: str) -> str:
        try:
            return self.complete_task(task_id)
        except FileNotFoundError:
            return f"Error: task {task_id} not found"
        except ValueError as exc:
            return f"Error: {exc}"

    def run_reclaim_tasks(self) -> str:
        reclaimed = self.reclaim_expired()
        if not reclaimed:
            return "No expired task leases."
        return "Reclaimed: " + ", ".join(reclaimed)

    def tool_defs(self) -> list[dict]:
        return [
            make_function_tool("create_task", "Create a task.",
                {"type": "object",
                 "properties": {"subject": {"type": "string"},
                                "description": {"type": "string"},
                                "blockedBy": {"type": "array", "items": {"type": "string"}},
                                "idempotency_key": {"type": "string"},
                                "scope": {"type": "string"},
                                "target_files": {"type": "array",
                                                 "items": {"type": "string"}},
                                "acceptance": {"type": "string"}},
                 "required": ["subject"]}),
            make_function_tool("list_tasks", "List tasks.",
                {"type": "object",
                 "properties": {"include_completed": {"type": "boolean"}},
                 "required": []}),
            make_function_tool("get_task", "Get full task details.",
                {"type": "object",
                 "properties": {"task_id": {"type": "string"}},
                 "required": ["task_id"]}),
            make_function_tool("claim_task", "Claim a pending task.",
                {"type": "object",
                 "properties": {"task_id": {"type": "string"}},
                 "required": ["task_id"]}),
            make_function_tool("complete_task", "Complete an in-progress task.",
                {"type": "object",
                 "properties": {"task_id": {"type": "string"}},
                 "required": ["task_id"]}),
            make_function_tool("reclaim_tasks",
                "Release tasks whose lease expired (stuck teammates).",
                {"type": "object", "properties": {}, "required": []}),
        ]

    def handlers(self) -> dict:
        return {
            "create_task": self.run_create_task,
            "list_tasks": self.run_list_tasks,
            "get_task": self.run_get_task,
            "claim_task": self.run_claim_task,
            "complete_task": self.run_complete_task,
            "reclaim_tasks": self.run_reclaim_tasks,
        }
