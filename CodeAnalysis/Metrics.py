"""风险评估模型与确定性计算(静态代码自评估标准的算法落地)。

参照 ISO/IEC 27001(A.8.28 安全开发)与 OWASP Top 10 2021 定级思想:
- 单条风险分 = 严重度权重 × 可能性(部署验证失败为确定性事件,可能性恒 1.0);
- 项目总分 = 静态漏洞 + 运行环境 + 部署验证 三类风险子项之和;
- 安全等级由总分分档 + critical 硬门槛(跨维度按严重度标签计数,只降不升);
- 主风险 = 三类子项中风险分最高者;全为 0 → none(平局按 static > env > deploy)。

全部纯函数、零 IO、零随机:任何输入都可复现,便于单元测试与交叉验证。
缺失/异常输入一律返回 None 或安全默认值,不编造数字。
"""
import hashlib

from CodeAnalysis.Configs import CodeAnalysisConfigs
from CodeAnalysis.Models import CodeAsset

# 主风险判定顺序(平局取先出现者)
_MAIN_RISK_ORDER = ("static_vuln", "env_config", "deploy_integrity")

'''
风险分计算的入参守卫 + 归一化函数做两件事：
1. 类型校验 — 非数值返回 None
2. 区间夹取 — 强制落到 [0.1, 1.0]
'''
def clamp_likelihood(likelihood) -> float | None:
    """把可能性夹取到 [0.1, 1.0];非数值返回 None(不猜数)。"""
    '''
    只接受 int/float。
    传入字符串、None、dict 等一律返回 None，交由调用方处理，而不是默认成 0 或 0.5 编造一个数。
    这是本文件开头声明的原则：「缺失/异常输入一律返回 None 或安全默认值，不编造数字」。
    '''
    if not isinstance(likelihood, (int, float)):
        return None
    '''
    小于 0.1 的值被抬到下限 0.1（Configs.py:40 注释写明是防零权重：避免某条风险因可能性趋近 0 而在总分里被完全抹掉）；

    大于 1.0 的值被压到 1.0（可能性是概率语义，上限就是确定性事件）；
    
    区间内的值原样返回，负数、0 也会被抬到 0.1。
    '''
    return min(max(float(likelihood), CodeAnalysisConfigs.LIKELIHOOD_MIN),
               CodeAnalysisConfigs.LIKELIHOOD_MAX)

'''
单条风险分 = 严重度权重 × 可能性
'''
def risk_score(severity: str, likelihood, weights: dict) -> float | None:
    """单条风险分 = 严重度权重 × 可能性;参数非法返回 None。"""
    weight = weights.get(severity)
    like = clamp_likelihood(likelihood)
    if weight is None or like is None:
        return None
    return round(weight * like, 4)


def static_subtotal(findings: list[dict]) -> float:
    """静态漏洞风险子项:Σ(每条发现的 risk_score)。"""
    total = 0.0
    for f in findings or []:    # 空/None 也安全
        r = f.get("risk_score") # 直接读每条已算好的 risk_score
        if isinstance(r, (int, float)): # 非法值(如 None)被跳过
            total += r
    return round(total, 4) # 保留 4 位小数


def env_subtotal(env_issues: list[dict]) -> float:
    """运行环境风险子项:Σ(每条环境问题的 risk_score)。"""
    total = 0.0
    for issue in env_issues or []:
        r = issue.get("risk_score")
        if isinstance(r, (int, float)):
            total += r
    return round(total, 4)


def deploy_subtotal(deploy_events: list[dict]) -> float:
    """部署验证风险子项:Σ(每个失败事件的 risk_score)。"""
    total = 0.0
    for event in deploy_events or []:
        r = event.get("risk_score")
        if isinstance(r, (int, float)):
            total += r
    return round(total, 4)

'''
项目风险评估的「分项汇总器」
'''
def subtotals(findings: list[dict], env_issues: list[dict],
              deploy_events: list[dict]) -> dict:
    """三类风险子项汇总(项目风险评估的输入)。"""
    return {
        "static_vuln": static_subtotal(findings),
        "env_config": env_subtotal(env_issues),
        "deploy_integrity": deploy_subtotal(deploy_events),
    }


def count_critical(findings: list[dict], env_issues: list[dict],
                   deploy_events: list[dict]) -> int:
    """critical 计数:跨静态/环境/部署全维度,按 severity 标签统计。"""
    count = 0
    for group in (findings, env_issues, deploy_events):
        for item in group or []:
            if item.get("severity") == "critical":
                count += 1
    return count


def classify_level(score: float, critical_count: int = 0) -> str:
    """总分分档 + critical 硬门槛,返回安全等级(A/B/C/D/E)。

    硬门槛只降不升:1 个 critical 不优于 C,2 个不优于 D,≥3 个直接 E。
    """
    if score <= 0:
        base = "A"
    elif score < 10:
        base = "B"
    elif score < 25:
        base = "C"
    elif score < 45:
        base = "D"
    else:
        base = "E"
    cap_rank = CodeAnalysisConfigs.CRITICAL_CAP_RANK.get(critical_count, 0)
    if cap_rank is None:
        return base
    rank = min(CodeAnalysisConfigs.LEVEL_RANK[base], cap_rank)
    return CodeAnalysisConfigs.RANK_LEVEL[rank]


def determine_main_risk(subtotals: dict) -> tuple[str, float | None]:
    """主风险判定:三类子项中风险分最高者;全为 0 → none。

    平局按 static_vuln > env_config > deploy_integrity 顺序取先出现者。
    """
    vals = {k: subtotals.get(k, 0.0) or 0.0 for k in _MAIN_RISK_ORDER}
    if all(v <= 0 for v in vals.values()):
        return "none", None
    main = max(_MAIN_RISK_ORDER, key=lambda k: vals[k])
    return main, vals[main]


def overall_assessment(findings: list[dict], env_issues: list[dict],
                       deploy_events: list[dict]) -> dict:
    """完整评估:子项 → 总分 → critical 计数 → 等级 → 主风险。"""
    sub = subtotals(findings, env_issues, deploy_events) # ① 分项汇总
    score = round(sum(sub.values()), 4) # ② 总分
    critical = count_critical(findings, env_issues, deploy_events) # ③ critical 计数
    level = classify_level(score, critical) # ④ 安全等级
    main, main_score = determine_main_risk(sub) # ⑤ 主风险
    return {"subtotals": sub, "risk_score": score,
            "critical_count": critical, "security_level": level,
            "main_risk": main, "main_risk_score": main_score}


def sha256_file(path) -> str | None:
    """文件 SHA-256(64KiB 分块,二进制模式);文件不可读返回 None。"""
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(CodeAnalysisConfigs.SHA_CHUNK_BYTES),
                              b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def detect_pending_assets(assets: list[dict]) -> list[CodeAsset]:
    """定时扫描入口:把资产库中的资产转为待分析对象(source=detector)。"""
    alerts = []
    for payload in assets or []:
        try:
            asset = CodeAsset.from_dict(payload)
        except (KeyError, TypeError):
            continue
        if not asset.project_id:
            continue
        asset.source = "detector"
        asset.message = f"{asset.name} 定时安全扫描"
        alerts.append(asset)
    return alerts
