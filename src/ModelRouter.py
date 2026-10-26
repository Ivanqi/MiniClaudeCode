"""模型路由:混合路由(本地小模型 / DeepSeek API)的决策层。

设计原则:
1. 路由器无会话状态(纯函数式):升级标志由调用方持有
   (主循环用 RecoveryState,子代理/队友用局部变量),route() 只按输入决策;
2. 保守 AND 规则:默认走 api,只有"明确简单"的任务才走 local,
   宁可漏判(多花点 API 钱)不可误判(小模型瞎搞再切回,浪费时间);
3. 失败要便宜:local 调用的 max_tokens 被压低(Configs.LOCAL_MAX_TOKENS),
   升级成本控制在几秒内;
4. 逃生舱:ROUTER_MODE=api-only 全量走 api;本地服务不可达时自动全量降级 api。

调用方用法:
    decision = router.route(messages, caller="main", flags={...})
    client, model, max_tokens = router.apply(decision, default_max_tokens)
    response = client.create(messages, system=..., tools=..., max_tokens=..., model=...)

caller 取值:
- main:     主循环,走动态规则(任务指纹 + 会话状态)
- summary:  上下文摘要,单次无工具调用,默认 local
- subagent: 探索式子代理,工具集小,默认 local
- teammate: 长时自治队友,默认 api
- catering: 餐饮 Agent 探索循环,默认 api;只有纯概念问答走 local 且不带工具
- plan_step: 通用 Plan 的单步 ReAct,强制 api(不进入 local 的只读窄工具集)
"""
import json
import time
from dataclasses import dataclass

import httpx

from src.Configs import Configs
from src.LlmClient import DeepSeekClient
from src.Terminal import Terminal

# ── 关键词表(中英双语,全部小写匹配) ──

# 信息查询类意图:命中一个即视为"简单"候选(只读不写)
SIMPLE_INTENT_WORDS = [
    "看", "读", "查", "找", "搜", "解释", "说明", "什么是", "怎么用",
    "在哪里", "列出", "显示", "统计", "为什么", "怎么样", "多少",
    "look", "read", "check", "find", "search", "grep", "explain",
    "what is", "what does", "how does", "where", "list", "show",
    "count", "why", "tell me", "describe",
]

# 动作意图:命中即排除 local(保守:凡是可能动文件/跑命令的都走 api)
ACTION_INTENT_WORDS = [
    "写", "建", "删", "改", "加", "修", "跑", "执行", "启动", "停止",
    "部署", "安装", "升级", "测试", "生成", "创建", "运行", "编译",
    "write", "create", "add", "delete", "remove", "edit", "modify",
    "change", "fix", "run", "start", "stop", "restart", "install",
    "deploy", "upgrade", "test", "build", "generate", "implement",
    "compile", "commit", "push",
]

# 复杂任务标记:命中即排除 local(即使消息很短)
COMPLEX_MARKER_WORDS = [
    "重构", "排查", "设计", "开发", "新增", "优化", "迁移", "多文件",
    "整个项目", "架构", "改造", "性能", "安全", "并发",
    "refactor", "design", "develop", "migrate", "debug", "optimize",
    "architect", "troubleshoot", "debugging", "performance", "security",
]

# 工作区相关词:命中说明回答需要读文件/跑命令,不能剥离工具集
WORKSPACE_INTENT_WORDS = [
    "文件", "目录", "路径", "代码", "函数", "类", "日志", "配置",
    "环境", "分支", "仓库", "模块", "接口", "报错",
    "file", "dir", "path", "code", "function", "class", "log",
    "config", "branch", "repo", "module", "api", "error", "git",
]

# 餐饮业务分析词(caller=catering 专用):命中即走 API。
# 小模型只承接纯概念问答,经营数据分析必须由强模型做,
# 避免"客流为什么降"这类问题被 3B 小模型误路由后浪费时间再升级。
CATERING_ANALYSIS_WORDS = [
    "门店", "客流", "营业额", "营收", "销售", "客单价", "数据",
    "分析", "趋势", "异常", "下降", "上升", "降了", "升了", "多少",
    "占比", "环比", "同比", "诊断", "报表", "经营", "毛利", "利润",
    "销量", "订单", "店铺", "业绩",
]


@dataclass
class RouterDecision:
    """一次路由决策。"""
    local: bool
    reason: str
    strip_tools: bool = False  # 纯问答且不涉及工作区时,不带工具集调用(上下文更短)


class ModelRouter:
    """模型路由器:持有双客户端,按规则选择,不做会话状态。"""

    def __init__(self, api_client: DeepSeekClient,
                 local_client: DeepSeekClient | None = None,
                 api_model: str | None = None,
                 local_model: str | None = None,
                 mode: str | None = None):
        self.api_client = api_client
        self.local_client = local_client
        # getattr 兼容测试用的假客户端(无 model 属性)
        self.api_model = api_model or getattr(api_client, "model", None)
        self.local_model = (local_model
                            or (getattr(local_client, "model", None)
                                if local_client else None))
        self.mode = mode or Configs.ROUTER_MODE
        # 健康检查缓存:避免每次路由都探测本地服务
        self._health_checked_at = 0.0
        self._health_ok = False

    @classmethod
    def from_env(cls, api_client: DeepSeekClient | None = None) -> "ModelRouter":
        """从环境变量构建:api 客户端必填(可复用调用方已构建的实例),
        LOCAL_BASE_URL + LOCAL_MODEL_ID 齐备才启用本地。"""
        api_client = api_client or DeepSeekClient.from_env()
        local_client = None
        if Configs.LOCAL_BASE_URL and Configs.LOCAL_MODEL_ID:
            local_client = DeepSeekClient(
                api_key=Configs.LOCAL_API_KEY,
                base_url=Configs.LOCAL_BASE_URL,
                model=Configs.LOCAL_MODEL_ID,
                fallback_model=None,  # 本地服务上没有 api 模型,禁止模型级降级
                timeout=Configs.LOCAL_LLM_TIMEOUT,
            )
        return cls(api_client, local_client,
                   api_model=Configs.API_MODEL_ID or api_client.model,
                   local_model=Configs.LOCAL_MODEL_ID)

    # ── 健康检查 ──

    def local_healthy(self) -> bool:
        """本地服务可达性探测(30 秒缓存),不可达时全量降级 api。"""
        if self.local_client is None:
            return False
        now = time.monotonic()
        if now - self._health_checked_at < Configs.HEALTH_CHECK_TTL:
            return self._health_ok
        self._health_checked_at = now
        try:
            response = httpx.get(f"{self.local_client.base_url}/health",
                                 timeout=Configs.HEALTH_CHECK_TIMEOUT)
            self._health_ok = response.status_code < 500
        except Exception:
            self._health_ok = False
        if not self._health_ok:
            Terminal.info("router", "local server unreachable → api", Terminal.RED)
        return self._health_ok

    # ── 路由主入口 ──

    def route(self, messages: list, caller: str = "main",
              flags: dict | None = None) -> RouterDecision:
        """按调用点与消息内容决策。

        flags 支持:
        - escalated: 本 turn 已升级,剩余轮次强制 api
        - active_teammates: 有活跃队友(多代理协作默认复杂)
        """
        flags = flags or {}
        if caller == "plan_step":
            decision = RouterDecision(False, "caller:plan_step")
        elif self.mode == "api-only":
            decision = RouterDecision(False, "mode:api-only")
        elif self.mode == "local-only":
            decision = RouterDecision(self.local_healthy(), "mode:local-only")
        elif caller in ("summary", "subagent"):
            decision = RouterDecision(self.local_healthy(), f"caller:{caller}")
        elif caller == "teammate":
            decision = RouterDecision(False, "caller:teammate")
        elif caller == "catering":
            decision = self._route_catering(messages, flags)
            if self.mode == "shadow":
                # shadow 模式:动态决策只记录不生效,用于调参观察
                decision = RouterDecision(False,
                                          f"shadow({decision.reason})")
        else:
            decision = self._route_main(messages, flags)
            if self.mode == "shadow":
                # shadow 模式:动态决策只记录不生效,用于调参观察
                decision = RouterDecision(False,
                                          f"shadow({decision.reason})")
        self._log(decision, caller)
        return decision

    def apply(self, decision: RouterDecision, default_max_tokens: int | None,
              model_override: str | None = None):
        """把决策落成 (client, model, max_tokens)。本地调用压低 max_tokens。"""
        if decision.local:
            client = self.local_client
            model = self.local_model
            max_tokens = min(default_max_tokens or Configs.LOCAL_MAX_TOKENS,
                             Configs.LOCAL_MAX_TOKENS)
        else:
            client = self.api_client
            # API 侧允许外层覆盖(如 overload 降级到 FALLBACK_MODEL_ID);
            # 本地侧忽略覆盖,避免把 api 模型名发给本地服务
            model = model_override or self.api_model
            max_tokens = default_max_tokens
        return client, model, max_tokens

    # ── 主循环动态规则 ──

    def _route_main(self, messages: list, flags: dict) -> RouterDecision:
        """保守 AND 规则,任一条件不满足即走 api。"""
        if flags.get("escalated"):
            return RouterDecision(False, "turn-escalated")
        if not self.local_healthy():
            return RouterDecision(False, "local-unhealthy")
        if flags.get("active_teammates"):
            return RouterDecision(False, "teammates-active")
        if len(json.dumps(messages, default=str)) > Configs.LOCAL_MAX_CONTEXT_CHARS:
            return RouterDecision(False, "context-too-long")
        text = self._last_user_text(messages)
        if not text:
            return RouterDecision(False, "no-user-text")
        if len(text) > Configs.SIMPLE_TASK_MAX_CHARS:
            return RouterDecision(False, "message-too-long")
        lowered = text.lower()
        if any(word in lowered for word in ACTION_INTENT_WORDS):
            return RouterDecision(False, "action-intent")
        if any(word in lowered for word in COMPLEX_MARKER_WORDS):
            return RouterDecision(False, "complex-marker")
        is_question = (len(text) < Configs.VERY_SHORT_CHARS
                       or any(word in lowered for word in SIMPLE_INTENT_WORDS))
        if not is_question:
            return RouterDecision(False, "no-simple-intent")
        # 纯问答且不涉及工作区:剥离工具集,上下文更短、小模型更稳
        strip_tools = not any(word in lowered for word in WORKSPACE_INTENT_WORDS)
        return RouterDecision(True, "simple-task", strip_tools=strip_tools)

    def _route_catering(self, messages: list, flags: dict) -> RouterDecision:
        """餐饮 Agent 探索循环:默认 api,只有纯概念问答才走 local 且不带工具。

        与 _route_main 的区别:业务分析词(门店/客流/营业额/数据等)命中
        直接排除 local——经营数据分析必须由强模型完成,小模型只承接
        "什么是客单价"这类概念问答。
        """
        if flags.get("escalated"):
            return RouterDecision(False, "turn-escalated")
        if not self.local_healthy():
            return RouterDecision(False, "local-unhealthy")
        text = self._last_user_text(messages)
        if not text:
            return RouterDecision(False, "no-user-text")
        if len(text) > Configs.SIMPLE_TASK_MAX_CHARS:
            return RouterDecision(False, "message-too-long")
        lowered = text.lower()
        if any(word in lowered for word in CATERING_ANALYSIS_WORDS):
            return RouterDecision(False, "catering-analysis")
        if any(word in lowered for word in ACTION_INTENT_WORDS):
            return RouterDecision(False, "action-intent")
        if any(word in lowered for word in COMPLEX_MARKER_WORDS):
            return RouterDecision(False, "complex-marker")
        is_question = (len(text) < Configs.VERY_SHORT_CHARS
                       or any(word in lowered for word in SIMPLE_INTENT_WORDS))
        if not is_question:
            return RouterDecision(False, "no-simple-intent")
        # 餐饮场景本地只做无工具的概念问答(本地小模型不碰业务数据工具)
        return RouterDecision(True, "catering-qa", strip_tools=True)

    @staticmethod
    def _last_user_text(messages: list) -> str:
        """取最后一条 user 消息的文本内容。"""
        for message in reversed(messages):
            if message.get("role") != "user":
                continue
            content = message.get("content")
            return content if isinstance(content, str) else ""
        return ""

    # ── 日志 ──

    def _log(self, decision: RouterDecision, caller: str):
        """local 路由打绿标;shadow 模式下记录"本会走 local"的决策供调参。"""
        if decision.local:
            Terminal.info("route", f"{caller} → local ({decision.reason})",
                          Terminal.GREEN)
        elif self.mode == "shadow" and "shadow(" in decision.reason:
            Terminal.info("route-shadow",
                          f"{caller} would use local ({decision.reason})",
                          Terminal.YELLOW)
