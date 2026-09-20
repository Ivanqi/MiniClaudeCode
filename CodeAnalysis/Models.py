"""代码安全分析数据模型:代码资产、步骤结果、发现、证据、部署记录、分析报告、整改案例。

均为 dataclass;to_dict() 用 asdict 输出,from_dict() 手工还原嵌套结构。
报告与状态文件带 schema_version,未知版本由 Guardrails 报错,不做静默迁移。
"""
from dataclasses import asdict, dataclass, field
from datetime import datetime

from CodeAnalysis.Configs import CodeAnalysisConfigs

# ── 分析报告固定 schema 版本 ──
REPORT_SCHEMA_VERSION = CodeAnalysisConfigs.SCHEMA_VERSION

# 主风险维度枚举(风险评估模型的三类风险来源 + 无风险)
MAIN_RISKS = ("static_vuln", "env_config", "deploy_integrity", "none")

# 严重度枚举(参照 CWE/CVSS 简化定级)
SEVERITIES = ("critical", "high", "medium", "low")

# 数据源类别(资产库/规则库/环境库/云平台/知识库/分析服务/部署模块)
SOURCE_CATEGORIES = (
    "asset_db",          # 代码资产库(入库登记)
    "rule_library",      # 扫描规则库
    "env_config_db",     # 运行环境配置库
    "cloud_platform",    # 公司云平台(制品注册表/部署)
    "knowledge_base",    # 知识库(历史整改案例)
    "analysis_service",  # 分析服务(风险计算)
    "deploy_module",     # 一键部署模块
)

# 步骤状态枚举
STEP_STATUSES = ("normal", "anomaly", "no_data", "excluded")


@dataclass
class CodeAsset:
    """一个代码资产(入库登记):某项目某版本某语言的代码与制品。"""

    project_id: str
    name: str
    language: str              # 主语言(多模块时以 / 连接)
    version: str
    ingest_date: str           # 入库日期 YYYY-MM-DD
    source: str = "manual"     # detector(定时扫描) | manual(入库/人工)
    message: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "CodeAsset":
        return cls(
            project_id=str(d["project_id"]), name=str(d.get("name", "")),
            language=str(d.get("language", "")),
            version=str(d.get("version", "")),
            ingest_date=str(d.get("ingest_date", "")),
            source=str(d.get("source", "manual")),
            message=str(d.get("message", "")))


@dataclass
class StepResult:
    """分析链中单步的执行结果(运行期结构,不落盘)。"""

    step_id: str
    status: str                  # normal | anomaly | no_data | excluded
    message: str = ""
    data: dict = field(default_factory=dict)
    rule_hits: list[dict] = field(default_factory=list)   # 命中的规则判定
    errors: list[str] = field(default_factory=list)


@dataclass
class Finding:
    """一个维度的安全发现:结论 + 关键数值快照 + 证据引用。"""

    step_id: str
    dimension: str               # inventory/static_scan/assess/env_review/deploy/verify/case
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
    data: dict                   # 归一化结果快照(完整数据见 path)
    source: str                  # SOURCE_CATEGORIES 之一
    path: str | None = None      # 完整原始结果落盘路径(审计用)

    @classmethod
    def from_dict(cls, d: dict) -> "Evidence":
        return cls(step_id=str(d["step_id"]), tool=str(d["tool"]),
                   params=dict(d.get("params") or {}),
                   data=dict(d.get("data") or {}),
                   source=str(d.get("source", "asset_db")),
                   path=d.get("path"))


@dataclass
class Case:
    """知识库中的历史整改案例(批准归档即记忆)。"""

    case_id: str
    project_id: str
    main_risk: str              # static_vuln | env_config | deploy_integrity
    summary: str
    actions: list[str]
    tags: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "Case":
        return cls(case_id=str(d["case_id"]), project_id=str(d["project_id"]),
                   main_risk=str(d["main_risk"]), summary=str(d["summary"]),
                   actions=list(d.get("actions") or []),
                   tags=list(d.get("tags") or []))


@dataclass
class AnalysisReport:
    """代码资产安全分析报告(固定 schema,唯一出口)。

    每个结论必须能回溯到 evidence 中的工具结果;
    安全等级与主风险由风险评估模型(静态+环境+部署)确定性计算,
    meta.template_fallback 暴露 LLM 是否降级为模板,保证报告永不缺失。
    """

    report_id: str
    schema_version: int
    asset: CodeAsset
    main_risk: str              # static_vuln | env_config | deploy_integrity | none
    main_risk_score: float | None   # 主风险维度风险分(元凶指标)
    risk_score: float           # 项目总分(静态+环境+部署)
    security_level: str         # A(优秀) | B(良好) | C(一般) | D(较差) | E(危险)
    findings: list[Finding]
    evidence: list[Evidence]
    matched_cases: list[dict]
    conclusions: str            # 自然语言结论(LLM 或模板)
    recommendations: list[dict]  # [{action, reason, evidence_refs, source}]
    completeness: dict          # {ran_steps, excluded_steps, no_data_steps, errors}
    meta: dict                  # {llm_used, template_fallback, attempts, model, review}
    created_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "AnalysisReport":
        return cls(
            report_id=str(d["report_id"]),
            schema_version=int(d["schema_version"]),
            asset=CodeAsset.from_dict(d["asset"]),
            main_risk=str(d["main_risk"]),
            main_risk_score=d.get("main_risk_score"),
            risk_score=float(d["risk_score"]),
            security_level=str(d["security_level"]),
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
