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
        self.run_dir = run_dir or CateringConfigs.RUN_DIR
        self.tasks = tasks   # 可选的 src.TaskSystem(默认不镜像)

    def run(self, chain: dict, alert) -> tuple[list[StepResult], list[Finding],
                                              list[Evidence], dict]:
        """执行整条链;返回 (步骤结果, 发现, 证据, 完整性)。"""
        bind = {"store_id": alert.store_id, "date": alert.date}
        step_results: list[StepResult] = []
        findings: list[Finding] = []
        evidence: list[Evidence] = []
        anomaly_seen = False
        culprit: str | None = None
        symptom_parts: list[str] = []

        for step in chain["steps"]:
            if not eval_when(step["when"], anomaly_seen, culprit):
                step_results.append(StepResult(
                    step_id=step["id"], status="excluded",
                    message=f"分支条件不满足: {step['when']}"))
                self._finish_task(self._start_task(step, alert))
                continue

            task_id = self._start_task(step, alert)
            args = {k: self._resolve_arg(v, bind, symptom_parts)
                    for k, v in step.get("args", {}).items()}
            result = self.tools.execute(step["tool"], args)
            ev_path = self._persist_evidence(step["id"], result)

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
                evidence.append(Evidence(
                    step_id=step["id"], tool=step["tool"], params=args,
                    data=result, source=TOOL_SOURCES.get(step["tool"], "store_db"),
                    path=ev_path))
                self._finish_task(task_id)
                continue

            rule_hits = [hit for rule in step["rules"]
                         if (hit := eval_rule(rule, result)) is not None]
            anomaly = any(h["verdict"] == "anomaly" for h in rule_hits)
            anomaly_seen = anomaly_seen or anomaly
            if step["id"] == "decompose":
                culprit, _ = determine_main_factor(result)

            if rule_hits:
                summary = ";".join(h["summary"] for h in rule_hits)
                verdict = "anomaly" if anomaly else "info"
            else:
                summary = f"{step['title']}: 未见明显异常"
                verdict = "normal"
            status = "anomaly" if anomaly else "normal"

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

            if verdict in ("anomaly", "info"):
                symptom_parts.append(f"dimension:{step['dimension']}")
                symptom_parts.append(summary)
            self._finish_task(task_id)

        completeness = {
            "ran_steps": [s.step_id for s in step_results
                          if s.status != "excluded"],
            "excluded_steps": [s.step_id for s in step_results
                               if s.status == "excluded"],
            "no_data_steps": [s.step_id for s in step_results
                              if s.status == "no_data"],
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
                    .replace("{{date}}", str(bind["date"]))

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
        run_dir = run_dir or CateringConfigs.RUN_DIR
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
