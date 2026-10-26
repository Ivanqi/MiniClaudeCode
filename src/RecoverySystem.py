"""LLM 调用错误恢复:重试、指数退避、备用模型降级。

with_retry 只对两类错误重试:
- ratelimit(429):指数退避后重试;
- overload(503/529):连续多次后切换到备用模型。
其余异常直接上抛,由调用方(AgentLoop)决定如何处理。
"""
import random
import time

from src.Configs import Configs
from src.LlmClient import classify_error
from src.Terminal import Terminal


class RecoveryState:
    """一次 agent 会话期间的恢复状态。"""

    def __init__(self):
        self.has_escalated = False                  # 是否已把 max_tokens 升到最高档
        self.recovery_count = 0                     # 已注入的续写提示次数
        self.consecutive_overload = 0               # 连续 overload 错误计数
        self.has_attempted_reactive_compact = False  # 是否已尝试过 reactive 压缩
        self.current_model: str | None = None       # None 表示使用客户端默认模型
        # ── 混合路由质量信号(每次 run_cycle 新建,即每个用户 turn 独立) ──
        self.turn_escalated = False   # 本 turn 已从小模型升级到 API,剩余轮次不再走 local
        self.escalation_reason = ""   # 升级原因(轨迹数据用)
        self.started_local = None     # 本 turn 首次路由是否 local(None=尚未调用)
        self.parse_failures = 0       # 本 turn 工具参数解析失败次数
        self.tool_error_count = 0     # 本 turn 工具执行报错次数
        self.last_tool_key = None     # 上一次工具调用 (name, args) 指纹,用于空转检测
        self.same_tool_streak = 0     # 相同工具+参数连续调用次数
        self.budget_exhausted_reason = ""  # 预算收尾原因(轨迹数据用)
        # ── LoopGuard / 可观测性(只增字段,保持旧测试兼容) ──
        self.turn_id = ""
        self.caller = "main"
        self.budget_snapshot: dict = {}
        self.loop_signals: list[dict] = []
        self.loop_signal_counts: dict[str, int] = {}
        self.failed_attempts: list[dict] = []
        self.tool_events: list[dict] = []
        self.stop_reason = ""
        self.loop_stop_reason = ""


def retry_delay(attempt: int) -> float:
    """指数退避,附带 25% 随机抖动,上限 32 秒。"""
    base = min(Configs.BASE_DELAY_MS * (2 ** attempt), 32000) / 1000
    return base + random.uniform(0, base * 0.25)


def with_retry(fn, state: RecoveryState, client=None):
    """执行 fn();对 ratelimit/overload 错误退避重试,其余异常上抛。"""
    for attempt in range(Configs.MAX_RETRIES):
        try:
            result = fn()
            state.consecutive_overload = 0
            return result
        except Exception as error:
            kind = classify_error(error)
            if kind == "ratelimit":
                delay = retry_delay(attempt)
                Terminal.info("429", f"retry {attempt + 1}/{Configs.MAX_RETRIES} "
                                     f"after {delay:.1f}s", Terminal.YELLOW)
                time.sleep(delay)
                continue
            if kind == "overload":
                state.consecutive_overload += 1
                if (client and client.fallback_model
                        and state.consecutive_overload >= Configs.MAX_CONSECUTIVE_OVERLOAD):
                    state.current_model = client.fallback_model
                    state.consecutive_overload = 0
                    Terminal.info("503", f"switching to {client.fallback_model}",
                                  Terminal.RED)
                delay = retry_delay(attempt)
                Terminal.info("503", f"retry {attempt + 1}/{Configs.MAX_RETRIES} "
                                     f"after {delay:.1f}s", Terminal.YELLOW)
                time.sleep(delay)
                continue
            raise
    raise RuntimeError(f"Max retries ({Configs.MAX_RETRIES}) exceeded")


def is_prompt_too_long_error(error: Exception) -> bool:
    """判断是否为上下文超长错误(需要压缩历史)。"""
    message = str(error).lower()
    return (("prompt" in message and "long" in message)
            or "context_length_exceeded" in message
            or "max_context_window" in message
            or "maximum context length" in message)
