"""TeammateSystem facade:组合空闲轮询、队友线程、lead 协议与工具。"""
from src.ActiveTeammates import ACTIVE_TEAMMATES, ActiveTeammates
from src.BasicOper import BASIC_OPER, BasicOper
from src.MessageBus import BUS, MessageBus
from src.ProtocolSystem import PROTOCOL, ProtocolSystem
from src.Roles import RoleRegistry
from src.TaskSystem import TASK_SYSTEM, TaskSystem
from src.TeamMetrics import TEAM_METRICS, TeamMetrics
from src.ToolDispatcher import ToolDispatcher
from src.teammate_system.agent import TeammateAgentMixin
from src.teammate_system.idle import TeammateIdleMixin
from src.teammate_system.lead import TeammateLeadMixin
from src.teammate_system.tools import TeammateToolsMixin


class TeammateSystem(TeammateIdleMixin, TeammateAgentMixin,
                     TeammateLeadMixin, TeammateToolsMixin):
    """队友系统:负责队友线程生命周期与 lead 侧团队工具。

    mixin 分层:
    - idle   空闲轮询与自动认领;
    - agent  spawn、ReAct 循环、心跳与退出收尾;
    - lead   关机/计划审批/消息/看板;
    - tools  lead 侧工具 Schema 与 handler。
    """

    INNER_ROUND_LIMIT = 10
    MESSAGE_WINDOW = 20

    def __init__(self, client, router=None,
                 bus: MessageBus = BUS,
                 protocol: ProtocolSystem = PROTOCOL,
                 tasks: TaskSystem = TASK_SYSTEM,
                 teammates: ActiveTeammates = ACTIVE_TEAMMATES,
                 basic: BasicOper = BASIC_OPER,
                 dispatcher: ToolDispatcher | None = None,
                 roles: RoleRegistry | None = None,
                 metrics: TeamMetrics | None = None):
        self.client = client
        self.router = router  # None 表示不路由,直接用 self.client(向后兼容)
        self.bus = bus
        self.protocol = protocol
        self.tasks = tasks
        self.teammates = teammates
        self.basic = basic
        self.roles = roles
        self.metrics = metrics or TEAM_METRICS
        self.dispatcher = dispatcher or ToolDispatcher(basic=self.basic)

    def role_registry(self) -> RoleRegistry:
        """角色注册表惰性创建,避免仅使用工具定义时读取磁盘。"""
        if self.roles is None:
            self.roles = RoleRegistry()
        return self.roles
