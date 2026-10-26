"""Agent 工作流:任务拆解(Planner)、诊断链执行(引擎)、状态管理(状态机)。

需求 3 的落地:
- Planning:   诊断链 JSON(运营可改)→ 校验 → 参数绑定 → 诊断计划;
- Workflow:   引擎逐步执行链步骤:取数 → 规则判定 → 发现/证据,分支用 when 条件;
- State:      状态机全程持久化(run_dir/{run_id}.json),失败可重试续跑;
- 记忆/审计:  每步工具原始结果落盘 run_dir/evidence/{step_id}.json;
- 不让 LLM 猜: 引擎全程确定性,LLM 只出现在 REASONING 环节且无工具。
"""
import json
import threading
from datetime import datetime
from pathlib import Path

from Catering.Configs import CateringConfigs
from Catering.Guardrails import (RULE_OPS, WHEN_OPS, eval_rule, eval_when)
from Catering.Models import (Evidence, Finding, StepResult)
from Catering.Tools import CateringTools

# 工具 -> 数据源类别(需求 2 的八类映射)
TOOL_SOURCES = {
    "query_metrics": "store_db",
    "analyze_revenue_decomposition": "analysis_service",
    "query_pos_data": "pos",
    "query_takeout_data": "takeout",
    "query_period_breakdown": "analysis_service",
    "query_channel_breakdown": "analysis_service",
    "query_dish_structure": "store_db",
    "query_promotions": "internal_system",
    "query_ratings": "store_db",
    "query_staff": "store_db",
    "query_external_factors": "external_api",
    "search_cases": "knowledge_base",
}

NO_DATA_MESSAGE = "该维度数据缺失,无法判断"


# ── 主因判定 ──

def determine_main_factor(decompose_data: dict) -> tuple[str, float | None]:
    """从拆解结果判定主因:营收下降时取贡献额最负的因子(客流/客单价)。"""
    contrib = decompose_data.get("contrib") or {}
    total_delta = contrib.get("total_delta")
    if total_delta is None or total_delta >= 0:
        return "none", None
    factors = {"traffic": contrib.get("traffic_contrib"),
               "ticket": contrib.get("ticket_contrib")}
    known = {k: v for k, v in factors.items() if isinstance(v, (int, float))}
    if not known:
        return "none", None
    main = min(known, key=known.get)
    return main, known[main]


# ── 任务拆解:Planner ──

class Planner:
    """把诊断链 JSON 加载、校验并绑定为一次告警的诊断计划。"""

    def __init__(self, chain_path: Path | None = None):
        self.chain_path = chain_path or CateringConfigs.DIAGNOSIS_CHAIN_PATH

    def load_chain(self) -> dict:
        try:
            return json.loads(self.chain_path.read_text())
        except FileNotFoundError:
            raise ValueError(f"诊断链文件不存在: {self.chain_path}") from None
        except json.JSONDecodeError as e:
            raise ValueError(f"诊断链文件损坏: {e}") from None

    def validate_chain(self, chain: dict) -> list[str]:
        """链结构校验:未知工具/非法 op/非法 when 必须拒启动,防静默错误。"""
        errors: list[str] = []
        if chain.get("schema_version") != CateringConfigs.SCHEMA_VERSION:
            errors.append(f"诊断链 schema_version 不支持: "
                          f"{chain.get('schema_version')}")
        known_tools = set(CateringTools().handlers())
        steps = chain.get("steps")
        if not isinstance(steps, list) or not steps:
            errors.append("诊断链必须包含 steps 数组")
            return errors
        for i, step in enumerate(steps):
            sid = step.get("id") or f"steps[{i}]"
            for key in ("id", "title", "dimension", "tool", "args", "when"):
                if key not in step:
                    errors.append(f"{sid}: 缺少字段 {key}")
            tool = step.get("tool")
            if tool and tool not in known_tools:
                errors.append(f"{sid}: 未知工具 {tool}")
            when = step.get("when") or ""
            if when not in WHEN_OPS and not when.startswith("culprit_is:"):
                errors.append(f"{sid}: 非法 when 条件 {when}")
            for r in step.get("rules") or []:
                if r.get("op") not in RULE_OPS:
                    errors.append(f"{sid}: 非法规则操作符 {r.get('op')}")
                if r.get("verdict") not in ("anomaly", "info"):
                    errors.append(f"{sid}: 非法 verdict {r.get('verdict')}")
                if r.get("threshold") is None:
                    errors.append(f"{sid}: 规则缺少 threshold")
        return errors

    def build_plan(self, chain: dict, alert) -> dict:
        """校验并返回诊断链;步骤参数模板({{store_id}} 等)由引擎绑定告警。"""
        errors = self.validate_chain(chain)
        if errors:
            raise ValueError("诊断链校验失败: " + "; ".join(errors))
        return chain


# ── 诊断引擎 ──

class DiagnosticEngine:
    """确定性诊断引擎:沿诊断链逐步取数、判定、沉淀发现与证据。

    可选注入 src.TaskSystem:诊断链每步在任务看板创建/认领/完成,
    让诊断过程在主代理的任务体系中同样可观测、可审计。
    """

    def __init__(self, tools: CateringTools, run_dir: Path | None = None,
                 tasks=None):
        self.tools = tools
        self.run_dir = (Path(run_dir) if run_dir is not None
                        else CateringConfigs.RUN_DIR)
        self.tasks = tasks   # 可选的 src.TaskSystem(默认不镜像)

    '''
    整体流程
    for 诊断链里的每个步骤:
        ① when 条件不满足? → 标记 excluded,跳过
        ② 把 {{store_id}}/{{date}}/{{symptom}} 模板绑定成真实参数
        ③ 调用工具取数 → 原始结果落盘 evidence/{step_id}.json(审计)
        ④ 数据缺失/报错? → 标记 no_data("数据缺失,无法判断"),继续下一步(不炸链)
        ⑤ 逐条规则判定 → 命中则生成发现(Finding),没命中 → "未见明显异常"
        ⑥ decompose 步骤额外计算主因(客流/客单价)
        ⑦ 异常发现的关键词累积进 symptom,供最后案例检索步骤使用
        最后: 汇总 ran/excluded/no_data/errors → 返回四元组

    '''
    def run(self, chain: dict, alert) -> tuple[list[StepResult], list[Finding],
                                              list[Evidence], dict]:
        """执行整条链;返回 (步骤结果, 发现, 证据, 完整性)。"""
        '''
        ① 初始化
            例：A店告警进来后 bind = {"store_id": "A店", "date": "2026-09-01"}，三个列表为空，anomaly_seen=False。
        '''
        bind = {"store_id": alert.store_id, "date": alert.date}
        step_results: list[StepResult] = [] # 三个收集器
        findings: list[Finding] = [] # 三个收集器
        evidence: list[Evidence] = [] # 三个收集器
        anomaly_seen = False # 是否已出现过 anomaly(供 when 分支判断)
        culprit: str | None = None # 主因,由 decompose 步骤算出
        symptom_parts: list[str] = []  # 累积异常发现的关键词,最后拼成案例检索的输入

        for step in chain["steps"]:
            '''
            ② when 分支判断
                when 只支持白名单语义（always / any_anomaly / culprit_is:X 等），决定这一步要不要做。
                大多数步骤 when: "always" → 直接放行；
                C店跑链时没有任何 anomaly，case 步骤 when: "any_anomaly" 返回 False → 被排除，记录 status="excluded", message="分支条件不满足: any_anomaly"——这就是"没有异常就不去翻历史案例"的链分支。
            '''
            if not eval_when(step["when"], anomaly_seen, culprit):
                step_results.append(StepResult(
                    step_id=step["id"], status="excluded",
                    message=f"分支条件不满足: {step['when']}"))
                self._finish_task(self._start_task(step, alert))
                continue

            '''
            ③ 参数绑定 + 工具取数
                _resolve_arg：模板替换。{{store_id}} → "A店"；特殊的 {{symptom}} → 把前面累积的 symptom_parts 拼成字符串（只有 case 步骤用）；

                _start_task：如果 CLI 加了 --tasks，这一步会在 src.TaskSystem 任务看板上创建"诊断步骤: 客流检查"并认领（owner=diagnostic-engine），让诊断过程在主代理任务体系里可观测。

                例（traffic 步骤）：绑定出 {"store_id": "A店", "date": "2026-09-01", "metrics": ["traffic"]}，工具返回：

                {"traffic": {"value": 1350, "baseline": 1800.0, "pct_change": -25.0}}

            '''
            # 注入 TaskSystem 时在任务看板建任务
            task_id = self._start_task(step, alert) 
            # {{store_id}} → "A店"
            args = {k: self._resolve_arg(v, bind, symptom_parts)
                    for k, v in step.get("args", {}).items()}
            # 真正查数据
            result = self.tools.execute(step["tool"], args)
            # 案例检索单独落审计日志:候选、分数分解、过滤原因(不进报告的紧凑视图)
            if step["tool"] == "search_cases":
                retrieval_path = self._persist_retrieval(step["id"], args,
                                                         result)
                if retrieval_path:
                    result = {**result, "retrieval_log": retrieval_path}
            '''
            ④ 证据落盘
                把工具原始结果写到 run_dir/evidence/traffic.json。
                这是审计链的一环：报告里的结论可以回溯到落盘的原始数据；写失败也不阻断诊断（_persist_evidence 内 try/except 返回 None）。
            '''
            ev_path = self._persist_evidence(step["id"], result)

            '''
            ⑤ 数据缺失分支
                这是"缺失就明说，绝不编造"的落点：工具返回带 missing_fields（如"基准日部分缺失"）或 error 时，这一步标记 no_data，发现结论固定为 "该维度数据缺失,无法判断"。

                例：测试里把 POS 数据源抽掉后，periods/channels 步骤就走到这里，报告 completeness.no_data_steps 里出现这两个步骤，但诊断链照常跑完。

            '''
            missing = result.get("missing_fields") or []
            if result.get("error") or missing:
                message = (f"数据缺失: {result.get('error') or ', '.join(missing)}")
                step_results.append(StepResult(
                    step_id=step["id"], status="no_data", message=message,
                    data=result, errors=[message]))
                findings.append(Finding(
                    step_id=step["id"], dimension=step["dimension"],
                    verdict="no_data", summary=NO_DATA_MESSAGE,
                    evidence_refs=[step["id"]]))
                evidence.append(Evidence( # 证据照记,只是数据是缺失标记
                    step_id=step["id"], tool=step["tool"], params=args,
                    data=result, source=TOOL_SOURCES.get(step["tool"], "store_db"),
                    path=ev_path))
                self._finish_task(task_id)
                continue  # 继续下一步,不炸链

            '''
            ⑥ 规则判定
                把该步骤在 JSON 里声明的所有规则（metric 点路径 + op 白名单 + threshold）对工具结果逐一求值，收集命中的规则。

                例（traffic 步骤）：规则 {"metric": "traffic.pct_change", "op": "le", "threshold": -10.0, "verdict": "anomaly"}，取到 -25.0 ≤ -10.0 → 命中，rule_hits = [命中1条]，anomaly=True，同时 anomaly_seen 置 True（后面 case 步骤靠它分支）。
            '''
            rule_hits = [hit for rule in step["rules"]
                         if (hit := eval_rule(rule, result)) is not None]
            anomaly = any(h["verdict"] == "anomaly" for h in rule_hits)
            anomaly_seen = anomaly_seen or anomaly

            '''
            ⑦ decompose 步骤特判：主因计算
                只有 decompose 这一步会调用 determine_main_factor：营收下降时，取贡献额最负的因子。

                例：A店拆解结果 total_delta=-17820（营收下降）， traffic_contrib=-25832.55（客流下拉）、ticket_contrib=+8012.55（客单价反而回补）→ min 取最负的 → culprit = "traffic"。B店则得到 culprit = "ticket"（-24028）。C店 total_delta ≥ 0 → "none"。
            '''
            if step["id"] == "decompose":
                culprit, _ = determine_main_factor(result)
                # 供案例检索步骤绑定 {{main_factor}}(decompose 之后才可用)
                bind["main_factor"] = culprit or "none"

            '''
            ⑧ 汇总该步骤的结论与状态
                注意 verdict 和 status 的微妙区别：命中 info 规则（如"竞对存在促销"）时，verdict="info"（供报告展示参考信息），但 status="normal"（不升级为异常）。

                例：
                    external 步骤两条规则都命中 → summary = "当日天气「暴雨」,是客流下降的重要外部因素;竞对市场活动「竞对『蜀香居』新店开业全场7折」,可能分流客流"；

                    ticket 步骤（客单价 +9.3%）一条没中 → summary = "客单价检查: 未见明显异常"。
            '''
            if rule_hits:
                summary = ";".join(h["summary"] for h in rule_hits)
                verdict = "anomaly" if anomaly else "info"
            else:
                summary = f"{step['title']}: 未见明显异常"
                verdict = "normal"
            status = "anomaly" if anomaly else "normal"

            '''
            ⑨ 沉淀 StepResult / Finding / Evidence
                三个收集器各司其职：

                    StepResult：运行期状态（报告不直接用它）；
                    
                    Finding：一个维度的"发现"——结论句 + analysis/contrib 关键数值快照（_metrics_snapshot，235-241 行）+ 引用自己的 step_id；
                    
                    Evidence：一条取证记录——哪个工具、什么参数、返回什么、数据源类别（TOOL_SOURCES 映射，如 query_metrics → store_db）、完整数据落盘在哪。
                例：traffic 步骤的 Finding 为 dimension="traffic", verdict="anomaly", summary="客流同比下降 -25.0%,超过阈值 -10%", metrics={}，Evidence 的 source="store_db"。
            '''
            step_results.append(StepResult(
                step_id=step["id"], status=status, data=result,
                rule_hits=rule_hits, message=summary))
            findings.append(Finding(
                step_id=step["id"], dimension=step["dimension"],
                verdict=verdict, summary=summary,
                metrics=self._metrics_snapshot(result),
                evidence_refs=[step["id"]]))
            evidence.append(Evidence(
                step_id=step["id"], tool=step["tool"], params=args,
                data=result, source=TOOL_SOURCES.get(step["tool"], "store_db"),
                path=ev_path))

            '''
            ⑩ 症状累积
                只有异常/参考级发现才累积关键词。到 case 步骤时，_resolve_arg 把 symptom_parts 拼成检索输入。

                例：A店跑完前 10 步后，symptom_parts 大致为 ["dimension:decompose", "营业额同比下降 -18.0%...", "dimension:traffic", "客流同比下降 -25.0%...", ..., "dimension:external", "当日天气「暴雨」..."] → search_cases 用这串文本匹配，命中 CASE-2026-0622-A（暴雨案例，得分 7.0）。

                dimension:traffic 这种 token 是故意放的，让案例匹配能对上 main_factor 字段。
            '''
            if verdict in ("anomaly", "info"):
                symptom_parts.append(f"dimension:{step['dimension']}")
                symptom_parts.append(summary)
            # 任务看板上把本步骤标 completed
            self._finish_task(task_id)

        '''
        ⑪ 步骤收尾
            四元组交回 AnomalyAgent.run_alert：completeness 直接进报告（运营能看到"哪几步没数据、哪几步被分支跳过"），findings 喂给 Reasoner 生成结论，evidence 构成报告的"证据链"章节。
        '''
        completeness = {
            # 执行过的步骤(含 no_data,不含 excluded)
            "ran_steps": [s.step_id for s in step_results
                          if s.status != "excluded"],
            # 分支排除的步骤
            "excluded_steps": [s.step_id for s in step_results
                               if s.status == "excluded"],
            # 数据缺失的步骤
            "no_data_steps": [s.step_id for s in step_results
                              if s.status == "no_data"],
            # 各步骤的错误信息
            "errors": [s.message for s in step_results if s.errors],
        }
        return step_results, findings, evidence, completeness

    def _start_task(self, step: dict, alert) -> str | None:
        """在注入的 TaskSystem 上创建并认领本步骤任务(无注入则跳过)。"""
        if self.tasks is None:
            return None
        task = self.tasks.create_task(
            subject=f"诊断步骤: {step['title']}",
            description=f"{alert.store_id} {alert.date} ({step['id']})")
        self.tasks.claim_task(task.id, owner="diagnostic-engine")
        return task.id

    def _finish_task(self, task_id: str | None):
        if task_id is not None:
            self.tasks.complete_task(task_id)

    def _resolve_arg(self, value, bind: dict, symptom_parts: list[str]):
        """解析参数模板: {{store_id}}/{{date}} 绑定告警,{{symptom}} 用发现拼装。"""
        if not isinstance(value, str):
            return value
        if value == "{{symptom}}":
            return " ".join(symptom_parts) or "无异常发现"
        return value.replace("{{store_id}}", str(bind["store_id"])) \
                    .replace("{{date}}", str(bind["date"])) \
                    .replace("{{main_factor}}",
                             str(bind.get("main_factor") or "none"))

    @staticmethod
    def _metrics_snapshot(result: dict) -> dict:
        """发现的关键数值快照:analysis + contrib(排除明细列表)。"""
        snapshot = dict(result.get("analysis") or {})
        if isinstance(result.get("contrib"), dict):
            snapshot.update(result["contrib"])
        return snapshot

    def _persist_evidence(self, step_id: str, result: dict) -> str | None:
        """工具原始结果落盘(审计):失败不阻断诊断流程。"""
        try:
            ev_dir = self.run_dir / "evidence"
            ev_dir.mkdir(parents=True, exist_ok=True)
            path = ev_dir / f"{step_id}.json"
            path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
            return str(path)
        except OSError:
            return None

    def _persist_retrieval(self, step_id: str, params: dict,
                           result: dict) -> str | None:
        """案例检索审计日志:候选、分数分解、过滤原因;失败不阻断诊断。"""
        payload = {
            "step_id": step_id,
            "tool": "search_cases",
            "params": params,
            "query": result.get("query") or result.get("symptom"),
            "match_count": result.get("match_count"),
            "filters": result.get("filters"),
            "candidates": result.get("candidates") or [],
            "missing_fields": result.get("missing_fields") or [],
            "error": result.get("error"),
        }
        try:
            retrieval_dir = self.run_dir / "retrieval"
            retrieval_dir.mkdir(parents=True, exist_ok=True)
            path = retrieval_dir / f"{step_id}.json"
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
            return str(path)
        except OSError:
            return None


# ── 状态管理:状态机 ──

STATE_EVENTS = {
    "NEW": {"accept": "ACCEPTED", "fail": "FAILED"},
    "ACCEPTED": {"plan_ready": "PLANNING", "fail": "FAILED"},
    "PLANNING": {"diagnose_start": "DIAGNOSING", "fail": "FAILED"},
    "DIAGNOSING": {"diagnose_ok": "REASONING", "diagnose_fail": "FAILED"},
    "REASONING": {"reason_ok": "REVIEWING", "reason_fail": "FAILED"},
    "REVIEWING": {"approve": "DONE", "rework": "REASONING", "fail": "FAILED"},
    "DONE": {},
    "FAILED": {"retry": "ACCEPTED"},
}


class DiagnosisStateMachine:
    """诊断状态机:事件驱动转移 + 全程持久化,失败可重试续跑。"""

    def __init__(self, run_id: str):
        self.run_id = run_id
        self.state = "NEW"
        self.history: list[dict] = []
        self.error: str | None = None
        self._lock = threading.Lock()

    def transition(self, event: str) -> str:
        with self._lock:
            nxt = STATE_EVENTS.get(self.state, {}).get(event)
            if nxt is None:
                raise ValueError(f"状态 {self.state} 不接受事件 {event}")
            self.state = nxt
            self.history.append({"event": event, "state": nxt})
            return nxt

    def fail(self, error: str) -> str:
        with self._lock:
            self.error = str(error)
            self.state = "FAILED"
            self.history.append({"event": "fail", "state": "FAILED",
                                 "error": str(error)})
            return "FAILED"

    def to_dict(self) -> dict:
        return {"run_id": self.run_id, "state": self.state,
                "history": self.history, "error": self.error,
                "schema_version": CateringConfigs.SCHEMA_VERSION}

    @classmethod
    def from_dict(cls, d: dict) -> "DiagnosisStateMachine":
        if d.get("schema_version") != CateringConfigs.SCHEMA_VERSION:
            raise ValueError(f"状态文件 schema_version 不支持: "
                             f"{d.get('schema_version')}")
        machine = cls(run_id=str(d["run_id"]))
        machine.state = str(d["state"])
        machine.history = list(d.get("history") or [])
        machine.error = d.get("error")
        return machine

    def persist(self, run_dir: Path | None = None) -> Path:
        run_dir = (Path(run_dir) if run_dir is not None
                   else CateringConfigs.RUN_DIR)
        run_dir.mkdir(parents=True, exist_ok=True)
        path = run_dir / f"{self.run_id}.json"
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False,
                                   indent=2))
        return path

    @classmethod
    def load(cls, path: Path) -> "DiagnosisStateMachine":
        return cls.from_dict(json.loads(path.read_text()))


def make_run_id(alert) -> str:
    """一次诊断运行的唯一标识(状态/报告文件名共用)。"""
    stamp = datetime.now().strftime("%H%M%S%f")
    return f"run_{alert.store_id}_{alert.date}_{stamp}"
