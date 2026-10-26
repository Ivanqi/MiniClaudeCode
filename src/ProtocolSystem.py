"""协议状态机:lead 与队友之间带 request_id 的请求/响应匹配。

- request_id = req_<uuid12>,track 拒绝重复;
- 只有 pending 状态能接受一次响应,发送者/目标/类型必须匹配,重放无效;
- 请求带 TTL,expire() 标记过期;lead 可通过 decide() 原子批准/拒绝;
- pending_requests 有锁且有上限,不再无限增长。
"""
import threading
import time
import uuid
from dataclasses import dataclass, field

from src.Configs import Configs
from src.MessageBus import BUS, MessageBus
from src.TeamMetrics import TEAM_METRICS


class ProtocolError(RuntimeError):
    """协议请求非法/过多。"""


@dataclass
class ProtocolState:
    request_id: str
    type: str
    sender: str
    target: str
    status: str
    payload: str
    created_at: float = field(default_factory=time.time)
    expires_at: float | None = None
    decided_at: float | None = None
    consumed_at: float | None = None


class ProtocolSystem:
    """协议状态机:管理挂起请求并路由响应。"""

    def __init__(self, bus: MessageBus = BUS, ttl: float | None = None,
                 max_pending: int | None = None, clock=None):
        self.bus = bus
        self.ttl = (Configs.PROTOCOL_REQUEST_TTL_SECONDS
                    if ttl is None else ttl)
        self.max_pending = (Configs.PROTOCOL_MAX_PENDING
                            if max_pending is None else max_pending)
        self.pending_requests: dict[str, ProtocolState] = {}
        self._lock = threading.RLock()
        self._clock = clock or time.time

    @staticmethod
    def new_request_id() -> str:
        return f"req_{uuid.uuid4().hex[:12]}"

    def track(self, state: ProtocolState):
        with self._lock:
            now = self._clock()
            if state.request_id in self.pending_requests:
                raise ProtocolError(
                    f"duplicate request_id: {state.request_id}")
            if state.expires_at is None:
                state.expires_at = state.created_at + self.ttl
            self._prune_locked(now)
            active = sum(1 for item in self.pending_requests.values()
                         if item.status == "pending")
            if self.max_pending and active >= self.max_pending:
                raise ProtocolError(
                    f"too many pending protocol requests (max {self.max_pending})")
            self.pending_requests[state.request_id] = state
            TEAM_METRICS.record("protocol_tracked")

    def get(self, request_id: str) -> ProtocolState | None:
        with self._lock:
            return self.pending_requests.get(request_id)

    def pending_count(self) -> int:
        with self._lock:
            return sum(1 for state in self.pending_requests.values()
                       if state.status == "pending")

    @staticmethod
    def _response_type_matches(state_type: str, response_type: str) -> bool:
        return ((state_type == "shutdown"
                 and response_type == "shutdown_response")
                or (state_type == "plan_approval"
                    and response_type == "plan_approval_response"))

    def match_response(self, response_type: str, request_id: str,
                       approve: bool, sender: str = "",
                       target: str = "") -> ProtocolState | None:
        with self._lock:
            state = self.pending_requests.get(request_id)
            if state is None:
                return None
            if state.status != "pending":
                return None
            if state.expires_at is not None and \
                    self._clock() >= state.expires_at:
                state.status = "expired"
                state.decided_at = self._clock()
                return None
            if not self._response_type_matches(state.type, response_type):
                return None
            if state.type == "plan_approval":
                expected_sender, expected_target = "lead", state.sender
            else:
                expected_sender, expected_target = state.target, "lead"
            if sender and sender != expected_sender:
                return None
            if target and target != expected_target:
                return None
            state.status = "approved" if approve else "rejected"
            state.decided_at = self._clock()
            state.consumed_at = state.decided_at
            return state

    def decide(self, request_id: str, approve: bool,
               feedback: str = "") -> tuple[bool, str]:
        """lead 原子批准/拒绝 plan_approval;返回 (ok, message)。"""
        with self._lock:
            state = self.pending_requests.get(request_id)
            if state is None:
                return False, f"Request {request_id} not found"
            if state.type != "plan_approval":
                return False, f"Request {request_id} is not a plan approval"
            if state.status != "pending":
                return False, f"Request {request_id} is already {state.status}"
            if state.expires_at is not None and \
                    self._clock() >= state.expires_at:
                state.status = "expired"
                state.decided_at = self._clock()
                return False, f"Request {request_id} expired"
            state.status = "approved" if approve else "rejected"
            state.decided_at = self._clock()
            sender = state.sender
        # 不在协议锁内发消息
        self.bus.send("lead", sender,
                      feedback or ("Approved" if approve else "Rejected"),
                      "plan_approval_response",
                      {"request_id": request_id, "approve": approve})
        return True, f"Plan {'approved' if approve else 'rejected'}"

    def cancel(self, request_id: str, reason: str = "cancelled") -> ProtocolState | None:
        with self._lock:
            state = self.pending_requests.get(request_id)
            if state is None:
                return None
            if state.status == "pending":
                state.status = "cancelled"
                state.decided_at = self._clock()
            return state

    def expire(self, now: float | None = None) -> list[str]:
        now = self._clock() if now is None else now
        expired = []
        with self._lock:
            for state in self.pending_requests.values():
                if (state.status == "pending" and state.expires_at is not None
                        and state.expires_at <= now):
                    state.status = "expired"
                    state.decided_at = now
                    expired.append(state.request_id)
                    TEAM_METRICS.record("protocol_expired")
            self._prune_locked(now)
        return expired

    def _prune_locked(self, now: float):
        if self.max_pending and len(self.pending_requests) > self.max_pending:
            oldest = sorted(
                (state for state in self.pending_requests.values()
                 if state.status != "pending"),
                key=lambda state: state.created_at)
            for state in oldest[:len(self.pending_requests) - self.max_pending]:
                self.pending_requests.pop(state.request_id, None)
        # 清理超过 2 个 TTL 的终态请求
        cutoff = now - max(self.ttl, 1) * 2
        for request_id, state in list(self.pending_requests.items()):
            if state.status != "pending" and state.created_at < cutoff:
                self.pending_requests.pop(request_id, None)

    def consume_lead_inbox(self, route_protocol: bool = True) -> list[dict]:
        """读取 lead 邮箱;协议响应先路由到挂起请求,再返回给调用方。"""
        msgs = self.bus.read_inbox("lead")
        if route_protocol:
            for msg in msgs:
                meta = msg.get("metadata", {})
                req_id = meta.get("request_id", "")
                msg_type = msg.get("type", "")
                if req_id and msg_type.endswith("_response"):
                    self.match_response(
                        msg_type, req_id, meta.get("approve", False),
                        sender=msg.get("from", ""),
                        target=msg.get("to", ""))
        return msgs


# 全局单例,复用全局消息总线。
PROTOCOL = ProtocolSystem()
