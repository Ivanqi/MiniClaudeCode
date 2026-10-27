"""餐饮业务配置:目录、阈值、诊断链路径、模式上限与环境开关。

所有业务魔法数字集中在此,其余模块只读引用,不重复定义;
阈值与诊断链分离:数值调参改这里,诊断步骤/规则改 data/diagnosis_chain.json。
"""
import os
from pathlib import Path


def _int_env(name: str, default: int, minimum: int = 0) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


class CateringConfigs:
    """餐饮业务配置常量,按职责分组。"""

    # ── 目录 ──
    CATERING_DIR = Path(__file__).resolve().parent
    CATERING_DIR_PARENT = CATERING_DIR.resolve().parent
    DATA_DIR = CATERING_DIR / "data"
    DIAGNOSIS_CHAIN_PATH = DATA_DIR / "diagnosis_chain.json"
    EVAL_SCENARIOS_PATH = DATA_DIR / "eval_scenarios.json"
    CASES_DIR = DATA_DIR / "cases"          # 知识库:已归档诊断案例
    # 运行产物目录:状态、证据、报告(与 src 各 System 的 .tasks 等模式一致,可注入)
    RUN_DIR = Path(os.getenv("CATERING_RUN_DIR") or (CATERING_DIR_PARENT / ".catering"))

    # ── 口径 ──
    BASELINE_DAYS = 7        # 基准 = 告警日前 7 日均值(周同比口径)
    SCHEMA_VERSION = 1       # 报告/状态/诊断链文件版本

    # ── 告警与异常判定阈值 ──
    ALERT_THRESHOLD_PCT = 10.0    # 营收同比 |变化| 超过该百分比触发告警
    TRAFFIC_ANOMALY_PCT = -10.0   # 客流同比降幅超过视为异常
    TICKET_ANOMALY_PCT = -8.0     # 客单价同比降幅超过视为异常
    RATING_LOW = 4.0              # 评分低于视为异常
    RATING_DROP = -0.3            # 评分环比降幅超过视为异常
    ATTENDANCE_MIN_RATIO = 0.9    # 出勤率低于视为异常
    PERIOD_DOMINANCE = 0.6        # 单时段贡献降幅占比超过视为时段主因
    CHANNEL_DOMINANCE = 0.6       # 单渠道贡献降幅占比超过视为渠道主因

    # ── 计算精度 ──
    LMDI_EPS = 1e-9               # 对数均值分母保护
    RESIDUAL_TOL = 1e-6           # 贡献度拆解残差容差

    # ── 工作流模式 ──
    WORKFLOW_MAX_STEPS = 50       # 诊断链单次执行步骤硬上限(防循环)

    # ── 探索模式(agentic) ──
    AGENTIC_MAX_STEPS = 12                # LLM 工具调用轮数硬上限
    AGENTIC_MAX_CONSECUTIVE_DENIALS = 2   # 连续被权限拒绝后熔断
    AGENTIC_OUTPUT_CHARS = 4000           # 单次工具结果截断长度

    # ── 推理环节(Reasoner) ──
    REASONER_MAX_TOKENS = 2000
    REASONER_MAX_RETRIES = 1       # 结构化输出解析失败后的重试次数
    DIGEST_MAX_CHARS = 4000        # 喂给 LLM 的证据摘要上限
    CASE_DIGEST_MAX_CHARS = 1200   # 其中案例上下文独立预算(避免挤掉 findings)

    # ── 案例 RAG ──
    # shadow: 只检索/落日志,不注入 Reasoner; assist: 作为可选证据注入;
    # augment: 允许模板降级直接采用案例动作(当前默认的保守起点是 shadow)。
    CASE_RAG_MODES = ("shadow", "assist", "augment")
    CASE_RAG_MODE = (os.getenv("CATERING_CASE_RAG_MODE", "shadow")
                     .strip().lower())
    CASE_SEARCH_CANDIDATES = 50    # 粗排候选上限(先召回再截 top_k)
    RETRIEVAL_EVAL_PATH = DATA_DIR / "retrieval_eval.json"

    # 检索管线:lexical 保持 P0 行为;hybrid 为 BM25 + structured + RRF。
    CASE_RETRIEVAL_MODES = ("lexical", "hybrid")
    CASE_RETRIEVAL_MODE = (
        os.getenv("CATERING_CASE_RETRIEVAL_MODE", "lexical")
        .strip().lower())
    CASE_RERANK_MODES = ("off", "heuristic")
    CASE_RERANK_MODE = (
        os.getenv("CATERING_CASE_RERANK_MODE", "heuristic")
        .strip().lower())
    CASE_RETRIEVAL_TIMEOUT_MS = _int_env(
        "CATERING_CASE_RETRIEVAL_TIMEOUT_MS", 300, minimum=1)
    CASE_RECALL_PER_ROUTE = CASE_SEARCH_CANDIDATES
    CASE_FUSION_RRF_K = 60
    # heuristic rerank 很便宜(全融合候选 ~6ms);20–30 的窄重排留给 P2。
    CASE_RERANK_TOP_N = 100

    # ── 权限 ──
    # 允许访问的门店列表;空 = 全部放开。格式: CATERING_SCOPE=A店,B店
    ALLOWED_STORES = tuple(
        s.strip() for s in os.getenv("CATERING_SCOPE", "").split(",") if s.strip())
    # 写工具(案例归档)默认不暴露;置 1 后暴露但仍需人工确认。
    ENABLE_WRITES = os.getenv("CATERING_ENABLE_WRITES") == "1"

    # ── LLM ──
    MODEL = os.getenv("MODEL_ID") or "deepseek-chat"


if CateringConfigs.CASE_RAG_MODE not in CateringConfigs.CASE_RAG_MODES:
    CateringConfigs.CASE_RAG_MODE = "shadow"
if (CateringConfigs.CASE_RETRIEVAL_MODE
        not in CateringConfigs.CASE_RETRIEVAL_MODES):
    CateringConfigs.CASE_RETRIEVAL_MODE = "lexical"
if CateringConfigs.CASE_RERANK_MODE not in CateringConfigs.CASE_RERANK_MODES:
    CateringConfigs.CASE_RERANK_MODE = "off"
