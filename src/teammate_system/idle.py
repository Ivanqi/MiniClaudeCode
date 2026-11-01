"""队友空闲轮询:收件箱消息优先,其次扫描可认领任务。"""
import json
import time

from src.Configs import Configs
from src.LlmClient import user_message
from src.Roles import RoleSpec
from src.Terminal import Terminal


class TeammateIdleMixin:
    """自治代理的空闲行为;由 TeammateSystem 组合使用。"""

    def scan_unclaimed_tasks(self) -> list[dict]:
        """先回收过期租约,再返回 pending/无 owner/依赖就绪的工作项。"""
        reclaim = getattr(self.tasks, "reclaim_expired", None)
        if reclaim is not None:
            try:
                reclaim(live_owners=self.teammates.live_names(),
                        grace_seconds=Configs.TEAM_RECLAIM_GRACE_SECONDS)
            except Exception as exc:
                Terminal.info("task", f"reclaim failed: {exc}", Terminal.RED)
        unclaimed = []
        for f in sorted(self.tasks.tasks_dir.glob("task_*.json")):
            try:
                task = json.loads(f.read_text())
                if (task.get("kind", "work") == "work"
                        and task.get("status") == "pending"
                        and not task.get("owner")
                        and self.tasks.can_start(task["id"])):
                    unclaimed.append(task)
            except (OSError, json.JSONDecodeError, KeyError):
                continue
        return unclaimed

    def idle_poll(self, agent_name: str, messages: list,
                  name: str, role: str,
                  worktree_context: dict | None = None,
                  deadline: float | None = None,
                  claimed: int = 0,
                  spec: RoleSpec | None = None) -> str:
        """空闲轮询:收件箱消息优先于可认领任务。

        返回值:
        - ``work``     有新消息或成功认领任务,继续内层循环;
        - ``idle``     本轮无事可做;
        - ``shutdown`` 收到关机请求;
        - ``lifetime``  超过队友生命周期上限。
        """
        max_tasks = (spec.effective_max_tasks() if spec
                     else Configs.TEAMMATE_MAX_TASKS)
        can_claim = spec.can_claim if spec else True
        for _ in range(Configs.IDLE_TIMEOUT // Configs.IDLE_POLL_INTERVAL):
            time.sleep(Configs.IDLE_POLL_INTERVAL)
            self.teammates.touch(name)
            if deadline is not None and time.monotonic() >= deadline:
                return "lifetime"
            inbox = self.bus.read_inbox(agent_name)
            if inbox:
                for msg in inbox:
                    if msg.get("type") == "shutdown_request":
                        req_id = msg.get("metadata", {}).get("request_id", "")
                        self.bus.send(name, "lead", "Shutting down.",
                                      "shutdown_response",
                                      {"request_id": req_id, "approve": True})
                        return "shutdown"
                messages.append(user_message(
                    "<inbox>" + json.dumps(inbox) + "</inbox>"))
                return "work"
            if not can_claim or claimed >= max_tasks:
                return "idle"
            ok_budget, _ = self.metrics.can_claim()
            if not ok_budget or not self.metrics.reserve_task():
                return "idle"
            unclaimed = self.scan_unclaimed_tasks()
            if not unclaimed:
                self.metrics.rollback_task()
            if unclaimed:
                task_data = unclaimed[0]
                result = self.tasks.claim_task(task_data["id"], agent_name)
                if "Claimed" not in result:
                    self.metrics.rollback_task()
                if "Claimed" in result:
                    wt_info = ""
                    task = self.tasks.load_task(task_data["id"])
                    token = task.claim_token or ""
                    if worktree_context is not None:
                        worktree_context["path"] = None
                    if task.worktree:
                        wt_path = Configs.worktrees_dir() / task.worktree
                        wt_info = f"\nWork directory: {wt_path}"
                        if worktree_context is not None:
                            worktree_context["path"] = str(wt_path)
                    if worktree_context is not None:
                        worktree_context["current_task"] = task_data["id"]
                        worktree_context["claimed"] = claimed + 1
                        worktree_context["claim_token"] = token
                    self.teammates.set_task(name, task_data["id"], token)
                    messages.append(user_message(
                        f"<auto-claimed>Task {task_data['id']}: "
                        f"{task_data['subject']}{wt_info}</auto-claimed>"))
                    return "work"
        return "idle"
