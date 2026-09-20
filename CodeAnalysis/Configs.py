"""代码安全分析业务配置:目录、风险模型权重、等级阈值、扫描限制与环境开关。

所有业务魔法数字集中在此,其余模块只读引用,不重复定义;
分析步骤/规则改 data/analysis_chain.json,数值调参改这里。
"""
import os
from pathlib import Path


class CodeAnalysisConfigs:
    """代码安全管控配置常量,按职责分组。"""

    # ── 目录 ──
    CODEANALYSIS_DIR = Path(__file__).resolve().parent
    CODEANALYSIS_DIR_PARENT = CODEANALYSIS_DIR.resolve().parent
    DATA_DIR = CODEANALYSIS_DIR / "data"
    ANALYSIS_CHAIN_PATH = DATA_DIR / "analysis_chain.json"
    EVAL_SCENARIOS_PATH = DATA_DIR / "eval_scenarios.json"
    RULES_DIR = DATA_DIR / "rules"            # 多语言扫描规则库
    ASSETS_DIR = DATA_DIR / "assets"          # 代码资产(源码+制品)
    ENVS_DIR = DATA_DIR / "envs"              # 运行环境配置快照与审查基线
    CLOUD_DIR = DATA_DIR / "cloud"            # 公司云平台模拟(制品注册表)
    CASES_DIR = DATA_DIR / "cases"            # 知识库:历史整改案例
    SNIPPETS_DIR = DATA_DIR / "snippets"      # 规则精度测试片段(planted/clean 对)
    REPORTS_DIR = DATA_DIR / "reports"        # 研究报告/技术规范交付物
    # 运行产物目录:状态、证据、报告(与 src 各 System 的 .tasks 等模式一致,可注入)
    RUN_DIR = Path(os.getenv("CODEANALYSIS_RUN_DIR")
                   or (CODEANALYSIS_DIR_PARENT / ".codeanalysis"))

    # ── 口径 ──
    SCHEMA_VERSION = 1   # 报告/状态/链/数据文件版本

    # ── 风险评估模型(静态代码自评估标准) ──
    # 三套严重度权重:静态漏洞 / 运行环境问题 / 部署验证失败(参照 ISO/IEC 27001、
    # OWASP Top 10 2021 与 CWE 定级;部署失败确定性发生,可能性恒为 1.0)
    STATIC_WEIGHTS = {"critical": 10.0, "high": 7.0, "medium": 4.0, "low": 1.0}
    ENV_WEIGHTS = {"critical": 8.0, "high": 5.0, "medium": 2.0, "low": 1.0}
    DEPLOY_WEIGHTS = {"integrity": 15.0, "authenticity": 12.0,
                      "availability": 10.0}
    LIKELIHOOD_MIN = 0.1   # 可能性下限(防零权重)
    LIKELIHOOD_MAX = 1.0

    # 安全等级分档(总分):0 → A(优秀); 0<x<10 → B(良好); 10≤x<25 → C(一般);
    # 25≤x<45 → D(较差); ≥45 → E(危险)。实现见 Metrics.classify_level。
    LEVEL_RANK = {"A": 4, "B": 3, "C": 2, "D": 1, "E": 0}
    RANK_LEVEL = {4: "A", 3: "B", 2: "C", 1: "D", 0: "E"}
    # 硬门槛(critical 计数跨静态/环境/部署全维度,按 severity 标签):
    #   1 个 critical → 不优于 C;2 个 → 不优于 D;≥3 → E;只降不升
    CRITICAL_CAP_RANK = {0: None, 1: 2, 2: 1}
    DEPLOY_GATE_LEVEL = "E"   # 高危门禁:等级 E 禁止部署

    # ── 扫描引擎限制(防超大文件/超长行/正则失控) ──
    MAX_FILE_BYTES = 1_000_000
    MAX_LINE_CHARS = 2000
    MAX_FINDINGS_PER_FILE = 50
    SHA_CHUNK_BYTES = 65536

    # ── 工作流模式 ──
    WORKFLOW_MAX_STEPS = 50   # 分析链单次执行步骤硬上限(防循环)

    # ── 探索模式(agentic) ──
    AGENTIC_MAX_STEPS = 12                # LLM 工具调用轮数硬上限
    AGENTIC_MAX_CONSECUTIVE_DENIALS = 2   # 连续被权限拒绝后熔断
    AGENTIC_OUTPUT_CHARS = 4000           # 单次工具结果截断长度

    # ── 推理环节(Reasoner) ──
    REASONER_MAX_TOKENS = 2000
    REASONER_MAX_RETRIES = 1       # 结构化输出解析失败后的重试次数
    DIGEST_MAX_CHARS = 4000        # 喂给 LLM 的证据摘要上限

    # ── 权限 ──
    # 允许分析的代码资产列表;空 = 全部放开。格式: CODEANALYSIS_SCOPE=PJ-A-001,PJ-B-002
    ALLOWED_PROJECTS = tuple(
        s.strip() for s in os.getenv("CODEANALYSIS_SCOPE", "").split(",")
        if s.strip())
    # 写工具(部署/入库登记)对 LLM 探索模式默认不暴露;置 1 后暴露
    # (工作流模式的部署步骤为流水线正规步骤,人工启动即授权,不受此开关限制)。
    ENABLE_WRITES = os.getenv("CODEANALYSIS_ENABLE_WRITES") == "1"

    # ── LLM ──
    MODEL = os.getenv("MODEL_ID") or "deepseek-chat"
