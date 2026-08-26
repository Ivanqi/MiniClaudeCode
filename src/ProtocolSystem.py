"""协议状态机:lead 与队友之间带 request_id 的请求/响应匹配。

响应按 request_id 与挂起的请求匹配,保证一个协议回复
不会误审批另一个挂起请求(shutdown / plan_approval)。
"""
import random
import time
from dataclasses import dataclass, field

from src.MessageBus import BUS, MessageBus


@dataclass
class ProtocolState:
    request_id: str
    type: str
    sender: str
    target: str
    status: str
    payload: str
    created_at: float = field(default_factory=time.time)


class ProtocolSystem:
    """协议状态机:管理挂起请求并路由响应。"""

    def __init__(self, bus: MessageBus = BUS):
        self.bus = bus
        self.pending_requests: dict[str, ProtocolState] = {}

    @staticmethod
    def new_request_id() -> str:
        return f"req_{random.randint(0, 999999):06d}"

    def track(self, state: ProtocolState):
        self.pending_requests[state.request_id] = state

    def match_response(self, response_type: str, request_id: str, approve: bool):
        state = self.pending_requests.get(request_id)
        if not state:
            return
        if state.type == "shutdown" and response_type != "shutdown_response":
            return
        if state.type == "plan_approval" and response_type != "plan_approval_response":
            return
        state.status = "approved" if approve else "rejected"

    def consume_lead_inbox(self, route_protocol: bool = True) -> list[dict]:
        """读取 lead 邮箱;协议响应先路由到挂起请求,再返回给调用方。"""
        msgs = self.bus.read_inbox("lead")
        if route_protocol:
            for msg in msgs:
                meta = msg.get("metadata", {})
                req_id = meta.get("request_id", "")
                msg_type = msg.get("type", "")
                if req_id and msg_type.endswith("_response"):
                    self.match_response(msg_type, req_id, meta.get("approve", False))
        return msgs


# 全局单例,复用全局消息总线。
PROTOCOL = ProtocolSystem()
