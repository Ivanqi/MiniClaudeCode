"""餐饮业务数据模型:告警、步骤结果、发现、证据、诊断报告、历史案例。

均为 dataclass;to_dict() 用 asdict 输出,from_dict() 手工还原嵌套结构。
报告与状态文件带 schema_version,未知版本由 Guardrails 报错,不做静默迁移。
"""
from dataclasses import asdict, dataclass, field
from datetime import datetime

from Catering.Configs import CateringConfigs

# ── 诊断报告固定 schema 版本 ──
REPORT_SCHEMA_VERSION = CateringConfigs.SCHEMA_VERSION

# 主因维度枚举(诊断链 metric_path 的前缀)
MAIN_FACTORS = ("traffic", "ticket", "none")

# 数据源类别(需求 2 的八类数据源)
SOURCE_CATEGORIES = (
    "store_db",          # 门店经营数据库
    "pos",               # POS 收银数据
    "takeout",           # 外卖平台数据
    "knowledge_base",    # 企业知识库(历史案例)
    "external_api",      # 第三方 API(天气/市场)
    "internal_system",   # 内部业务系统(活动/排班)
    "analysis_service",  # 数据分析服务(拆解计算)
)

# 步骤状态枚举
STEP_STATUSES = ("normal", "anomaly", "no_data", "excluded")


@dataclass
class AnomalyAlert:
    """一条经营异常告警:某店某日某指标相对基准的变化。"""

    store_id: str
    date: str
    metric: str = "revenue"
    baseline: float | None = None
    current: float | None = None
    change_pct: float | None = None
    source: str = "manual"     # detector(规则扫描) | manual(人工输入)
    message: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "AnomalyAlert":
        return cls(
            store_id=str(d["store_id"]), date=str(d["date"]),
            metric=str(d.get("metric", "revenue")),
            baseline=d.get("baseline"), current=d.get("current"),
            change_pct=d.get("change_pct"),
            source=str(d.get("source", "manual")),
            message=str(d.get("message", "")))


@dataclass
class StepResult:
    """诊断链中单步的执行结果(运行期结构,不落盘)。"""

    step_id: str
    status: str                  # normal | anomaly | no_data | excluded
    message: str = ""
    data: dict = field(default_factory=dict)
    rule_hits: list[dict] = field(default_factory=list)   # 命中的规则判定
    errors: list[str] = field(default_factory=list)


@dataclass
class Finding:
    """一个维度的诊断发现:结论 + 关键数值快照 + 证据引用。"""

    step_id: str
    dimension: str               # traffic/ticket/periods/channels/dish/promotion/rating/staff/external/case
    verdict: str                 # normal | anomaly | no_data | info
    summary: str                 # 人类可读的一句话结论
    metrics: dict = field(default_factory=dict)        # 关键数值快照
    evidence_refs: list[str] = field(default_factory=list)  # 引用的 step_id

    @classmethod
    def from_dict(cls, d: dict) -> "Finding":
        return cls(step_id=str(d["step_id"]), dimension=str(d["dimension"]),
                   verdict=str(d["verdict"]), summary=str(d["summary"]),
                   metrics=dict(d.get("metrics") or {}),
                   evidence_refs=list(d.get("evidence_refs") or []))


@dataclass
class Evidence:
    """一条工具取证记录:哪个工具、什么参数、返回什么数据、完整结果落盘在哪。"""

    step_id: str
    tool: str
    params: dict
    data: dict                   # 归一化结果快照(已截断,完整数据见 path)
    source: str                  # SOURCE_CATEGORIES 之一
    path: str | None = None      # 完整原始结果落盘路径(审计用)

    @classmethod
    def from_dict(cls, d: dict) -> "Evidence":
        return cls(step_id=str(d["step_id"]), tool=str(d["tool"]),
                   params=dict(d.get("params") or {}),
                   data=dict(d.get("data") or {}),
                   source=str(d.get("source", "store_db")),
                   path=d.get("path"))


@dataclass
class Case:
    """知识库中的历史诊断案例(归档即记忆)。"""

    case_id: str
    store_id: str
    date: str
    main_factor: str
    dimensions: list[str]        # 命中维度,如 ["traffic", "channels", "external"]
    summary: str
    actions: list[str]
    tags: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "Case":
        return cls(case_id=str(d["case_id"]), store_id=str(d["store_id"]),
                   date=str(d["date"]), main_factor=str(d["main_factor"]),
                   dimensions=list(d.get("dimensions") or []),
                   summary=str(d["summary"]),
                   actions=list(d.get("actions") or []),
                   tags=list(d.get("tags") or []))


@dataclass
class DiagnosisReport:
    """门店经营异常诊断报告(固定 schema,唯一出口)。

    每个结论必须能回溯到 evidence 中的工具结果;
    meta.template_fallback 暴露 LLM 是否降级为模板,保证报告永不缺失。
    """

    report_id: str
    schema_version: int
    alert: AnomalyAlert
    main_factor: str             # traffic | ticket | none
    main_factor_contrib: float | None     # 主因贡献额(元,负数为下拉)
    findings: list[Finding]
    evidence: list[Evidence]
    matched_cases: list[dict]
    conclusions: str             # 自然语言结论(LLM 或模板)
    recommendations: list[dict]  # [{action, reason, evidence_refs, case_refs, source}]
    completeness: dict           # {ran_steps, excluded_steps, no_data_steps, errors}
    meta: dict                   # {llm_used, template_fallback, attempts, model, case_mode}
    created_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "DiagnosisReport":
        return cls(
            report_id=str(d["report_id"]),
            schema_version=int(d["schema_version"]),
            alert=AnomalyAlert.from_dict(d["alert"]),
            main_factor=str(d["main_factor"]),
            main_factor_contrib=d.get("main_factor_contrib"),
            findings=[Finding.from_dict(f) for f in (d.get("findings") or [])],
            evidence=[Evidence.from_dict(e) for e in (d.get("evidence") or [])],
            matched_cases=list(d.get("matched_cases") or []),
            conclusions=str(d.get("conclusions") or ""),
            recommendations=list(d.get("recommendations") or []),
            completeness=dict(d.get("completeness") or {}),
            meta=dict(d.get("meta") or {}),
            created_at=str(d.get("created_at") or ""))


def now_iso() -> str:
    """当前时间 ISO 字符串(统一出口,方便测试替换)。"""
    return datetime.now().isoformat(timespec="seconds")
