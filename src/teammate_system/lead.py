"""lead 侧团队协议与通信:关机/要计划/审批计划/收发消息/看板。"""
from src.ProtocolSystem import ProtocolState


class TeammateLeadMixin:
    """lead 侧操作队友协议的方法。"""

    # ── 队友侧:提交计划 ──

    def submit_plan(self, from_name: str, plan: str) -> str:
        req_id = self.protocol.new_request_id()
        self.protocol.track(ProtocolState(
            request_id=req_id, type="plan_approval",
            sender=from_name, target="lead",
            status="pending", payload=plan))
        self.bus.send(from_name, "lead", plan,
                      "plan_approval_request", {"request_id": req_id})
        return f"Plan submitted ({req_id})"

    # ── lead 侧:协议与通信工具 ──

    def run_request_shutdown(self, teammate: str) -> str:
        req_id = self.protocol.new_request_id()
        self.protocol.track(ProtocolState(
            request_id=req_id, type="shutdown",
            sender="lead", target=teammate,
            status="pending", payload=""))
        self.bus.send("lead", teammate, "Shut down.", "shutdown_request",
                      {"request_id": req_id})
        return f"Shutdown request sent to {teammate}"

    def run_request_plan(self, teammate: str, task: str) -> str:
        self.bus.send("lead", teammate, f"Submit plan for: {task}", "message")
        return f"Asked {teammate} to submit a plan"

    def run_review_plan(self, request_id: str, approve: bool,
                        feedback: str = "") -> str:
        ok, message = self.protocol.decide(request_id, approve, feedback)
        return message if ok else f"Error: {message}"

    def run_send_message(self, to: str, content: str) -> str:
        self.bus.send("lead", to, content)
        return f"Sent to {to}"

    def run_team_status(self) -> str:
        """团队指标 + 预算 + 活跃队友 + 任务看板 + 待审批协议。"""
        return self.metrics.status_text(
            active_teammates=self.teammates, tasks=self.tasks,
            protocol=self.protocol)

    def run_check_inbox(self) -> str:
        """读取 lead 收件箱,并顺带把协议响应路由到协议表。"""
        msgs = self.protocol.consume_lead_inbox(route_protocol=True)
        if not msgs:
            return "(inbox empty)"
        lines = []
        for m in msgs:
            meta = m.get("metadata", {})
            req_id = meta.get("request_id", "")
            tag = f" [{m['type']} req:{req_id}]" if req_id else f" [{m['type']}]"
            lines.append(f"  [{m['from']}]{tag} {m['content'][:200]}")
        return "\n".join(lines)
