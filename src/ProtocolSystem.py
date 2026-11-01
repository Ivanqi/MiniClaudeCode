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

'''
这是一个带 request_id 的请求/响应协议状态机，解决的是多 Agent 协作中一个具体问题.
一方发出请求后，怎么确认收到的响应确实对应自己发出的那个请求，且只被处理一次。
它不负责业务逻辑，只负责"请求生命周期"的管理。

        track()
           │
           ▼
       ┌─────────┐
       │ pending │──── expire()/超时 ──▶ expired
       └─────────┘──── decide() ───────▶ approved / rejected
           │
           └──── cancel() ──────────────▶ cancelled

关键约束：只有 pending 能接受一次响应；一旦离开 pending，任何重放/重复响应都被 match_response 静默丢弃。这是防重放的核心机制。

数据流分两条：
1. 主动决策路径：lead 调 decide() 原子地批准/拒绝 plan_approval，并在锁外发响应消息。
2. 被动接收路径：lead 调 consume_lead_inbox() 读出邮箱，把 *_response 类型消息按 request_id 路由回 match_response()，完成 shutdown 类请求的响应匹配。

核心设计约束
1. 一次性消费：match_response 和 decide 都要求 status == "pending"，处理成功后立刻改状态。同一请求第二次响应会被 status != "pending" 拦截，返回 None/False。
2. 三重匹配：类型（_response_type_matches）、发送者（sender）、目标（target）都要对上，防止"A 的响应被错误归到 B 的请求"。
3. TTL 兜底：expires_at 在 track 时设定，match_response/decide/expire 三处都会检查过期并落 expired 状态，防止请求永远挂起。
4. 锁纪律：RLock 保护 pending_requests；_prune_locked 约定只在锁内调用；decide 不在锁内发消息（锁内只取 sender，出锁再 bus.send），避免消息总线回调造成死锁或长时间持锁。
'''
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

    '''
    生成 req_ 加 12 位十六进制（48 bit 随机熵）。
    对单进程内的协议请求，碰撞概率可忽略；track 里还有重复检查兜底。
    用 uuid4 而非自增计数器，是为了避免多进程/重启后 ID 复用。
    '''
    @staticmethod
    def new_request_id() -> str:
        """生成 req_<uuid12>:48 bit 随机熵,单进程内碰撞可忽略。"""
        return f"req_{uuid.uuid4().hex[:12]}"

    '''
    请求注册入口
        1. 重复检查：request_id 已在 pending_requests 中则抛 ProtocolError，防止同一个 ID 被跟踪两次导致响应匹配歧义
        2. 补算 TTL：若 state.expires_at 为空，设为 created_at + self.ttl。允许调用方自带 expires_at（比如来自上游的截止时间）
        3. 先 prune 再检查容量：self._prune_locked(now) 先清理陈旧记录，腾出空间。
            1. 先 prune 后检查容量是刻意设计——如果反过来，刚能通过清理腾出的空间就用不上，会在临界点误拒。
        4. 容量检查：统计 status == "pending" 的数量，>= max_pending 则抛异常。注意这里统计的是 pending 数，不是 dict 总数。
        5. 入队并记指标：写入 dict，TEAM_METRICS.record("protocol_tracked")。
    '''
    def track(self, state: ProtocolState):
        """注册一个请求;重复 ID 或挂起数超限抛 ProtocolError。"""
        with self._lock:
            now = self._clock()
            # 闸门 1:重复 ID 会导致响应匹配歧义,直接拒绝
            if state.request_id in self.pending_requests:
                raise ProtocolError(
                    f"duplicate request_id: {state.request_id}")
            # 闸门 2:调用方未带 expires_at 时按 ttl 补算
            if state.expires_at is None:
                state.expires_at = state.created_at + self.ttl
            # 先清理陈旧记录,再检查容量:顺序不能反,否则临界点误拒
            self._prune_locked(now)

            # 闸门 3:按 pending 数检查容量(与 dict 总数口径不同)
            active = sum(1 for item in self.pending_requests.values()
                         if item.status == "pending")
            if self.max_pending and active >= self.max_pending:
                raise ProtocolError(
                    f"too many pending protocol requests (max {self.max_pending})")
            self.pending_requests[state.request_id] = state
            TEAM_METRICS.record("protocol_tracked")

    def get(self, request_id: str) -> ProtocolState | None:
        """按 ID 取状态;返回引用(非副本),供内部只读查询。"""
        with self._lock:
            return self.pending_requests.get(request_id)

    def pending_count(self) -> int:
        """当前处于 pending 的请求数,口径与 track 的容量检查一致。"""
        with self._lock:
            return sum(1 for state in self.pending_requests.values()
                       if state.status == "pending")

    '''
    纯函数，定义"请求类型 → 合法响应类型"的映射。只有这两种配对合法。
    放在静态方法里便于单测，也避免在 match_response 里写一长串 if/elif
    '''
    @staticmethod
    def _response_type_matches(state_type: str, response_type: str) -> bool:
        """请求类型 -> 合法响应类型的白名单映射。"""
        return ((state_type == "shutdown"
                 and response_type == "shutdown_response")
                or (state_type == "plan_approval"
                    and response_type == "plan_approval_response"))

    '''
    响应匹配核心
        这是防重放、防串号的主战场，按顺序做了六道闸门：
            1. 存在性：request_id 不存在 → None。
            2. 状态闸门：status != "pending" → None。这就是一次性消费的实现——已批准/已拒绝/已过期的请求不再接受响应。
            3. 过期检查：若 expires_at 已到，把状态落成 expired、写 decided_at，返回 None。注意这里顺手做了状态迁移，而不是留给 expire()。
            4. 类型匹配：调 _response_type_matches，不匹配 → None。
            5. 方向匹配：这里根据请求类型反推出"谁应该是响应的发送者/接收者"：
                1. plan_approval：请求是队友 → lead，响应是 lead → 队友。所以 expected_sender = "lead"，expected_target = state.sender（原请求发起者）。
                2. shutdown：请求是 lead → 队友，响应是队友 → lead。所以 expected_sender = state.target（原请求接收者），expected_target = "lead"。
                3. 宽容模式：if sender and sender != expected_sender —— 传空串则跳过该校验，调用方可以选择只校验类型不校验方向。
            6. 落状态：approved 或 rejected，decided_at = consumed_at = now。
    '''
    def match_response(self, response_type: str, request_id: str,
                       approve: bool, sender: str = "",
                       target: str = "") -> ProtocolState | None:
        """把一条响应匹配到挂起请求;六道闸门,失败静默返回 None。"""
        with self._lock:
            state = self.pending_requests.get(request_id)
            # 闸门 1:未知请求
            if state is None:
                return None
            
            # 闸门 2:一次性消费,防重放
            if state.status != "pending":
                return None

            # 闸门 3:过期检查,顺手把状态落成 expired
            if state.expires_at is not None and \
                    self._clock() >= state.expires_at:
                state.status = "expired"
                state.decided_at = self._clock()
                return None
            
            # 闸门 4:类型配对
            if not self._response_type_matches(state.type, response_type):
                return None

            # 闸门 5:方向校验(允许空串跳过,宽容模式)
            # plan_approval: 队友→lead 发出,lead→队友 响应
            # shutdown:      lead→队友 发出,队友→lead 响应
            if state.type == "plan_approval":
                expected_sender, expected_target = "lead", state.sender
            else:
                expected_sender, expected_target = state.target, "lead"

            if sender and sender != expected_sender:
                return None
            if target and target != expected_target:
                return None

            # 闸门 6:落终态,decided_at 与 consumed_at 同步
            state.status = "approved" if approve else "rejected"
            state.decided_at = self._clock()
            state.consumed_at = state.decided_at
            return state

    '''
    lead 原子决策
        处理 plan_approval 的主动批准/拒绝，返回 (ok, message) 二元组，是给调用方（通常是 lead 的决策逻辑）看的.锁内做了六道检查：
            1. 请求存在
            2. type == "plan_approval"（decide 只管计划审批，shutdown 走 match_response）。
            3. status == "pending"。
            4. 未过期；若过期则落 expired 并返回失败。
            5. 落 approved/rejected，写 decided_at
            6. 只取 sender 就出锁
    '''
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
        '''
        不在锁内发消息是这里最重要的设计。
        原因有二：
            1. 消息总线的 send 可能同步触发订阅者回调，回调里若再访问 PROTOCOL 就会死锁（RLock 虽可重入，但跨线程不行）
            2. 另外 bus.send 可能阻塞，持锁发消息会把整个协议系统的吞吐拖死。
        '''
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

    '''
    批量过期扫描
        由外部定时器（比如每秒一次）驱动，或测试里手动调用。
            1. now 参数允许传入假时钟时间，不传则用 self._clock()。
            2. 遍历所有状态，status == "pending" 且 expires_at <= now 的落 expired、写 decided_at、记 protocol_expired 指标。
            3. 最后调 _prune_locked(now) 清理。
            4. 返回本批过期的 request_id 列表，便于上层发通知或记日志。
    '''
    def expire(self, now: float | None = None) -> list[str]:
        """批量扫描并标记过期;返回本批过期的 request_id 列表。"""
        now = self._clock() if now is None else now
        expired = []
        with self._lock:
            for state in self.pending_requests.values():
                '''
                expire 用 expires_at <= now，match_response/decide 用 self._clock() >= expires_at
                两者在边界时刻等价——都认为"到点即过期"
                避免边界上出现"expire 没扫到但 match 拒绝"的不一致。
                '''
                if (state.status == "pending" and state.expires_at is not None
                        and state.expires_at <= now):
                    state.status = "expired"
                    state.decided_at = now
                    expired.append(state.request_id)
                    TEAM_METRICS.record("protocol_expired")
            self._prune_locked(now)
        return expired

    '''
    清理策略（约定锁内调用）
    '''
    def _prune_locked(self, now: float):
        '''
        容量清理
            当 dict 总数超过 max_pending 时，按 created_at 从老到新删非 pending 的记录，删到总数不超过上限。
            只删终态，不碰 pending——活跃请求受 track 的 active 检查保护，这里的清理是"归档腾位"。
        '''
        if self.max_pending and len(self.pending_requests) > self.max_pending:
            oldest = sorted(
                (state for state in self.pending_requests.values()
                 if state.status != "pending"),
                key=lambda state: state.created_at)
            for state in oldest[:len(self.pending_requests) - self.max_pending]:
                self.pending_requests.pop(state.request_id, None)

        # 清理超过 2 个 TTL 的终态请求
        '''
        时间清理
            1. 无条件清理 created_at 早于 now - 2*TTL 的终态记录。max(self.ttl, 1) 防止 ttl <= 0 时 cutoff 跑到未来。
            2. 注意这里用 created_at 而非 decided_at 做判据——一个刚创建就被决策的请求，若创建时间已很早，也会被清，这是可接受的（它已经终态且超过保留窗口）。
        '''
        cutoff = now - max(self.ttl, 1) * 2
        for request_id, state in list(self.pending_requests.items()):
            if state.status != "pending" and state.created_at < cutoff:
                self.pending_requests.pop(request_id, None)

    '''
    邮箱消费 + 自动路由
        1. 一次性从 lead 邮箱读出所有消息
        2. 对每条消息，若 metadata 里有 request_id 且 type 以 _response 结尾，就尝试 match_response
        3. match_response 的返回值被忽略——匹配成功或失败都不影响消息继续返回给调用方。这是"尽力而为"：路由是为了更新协议状态，消息本身还要交给上层业务逻辑处理。
        4. sender/target 从消息的 from/to 字段取，而不是硬编码 "lead"。这样 shutdown_response（队友 → lead，to="lead"）和可能出现在 lead 邮箱里的其他响应都能按消息自身字段校验。
    '''
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
