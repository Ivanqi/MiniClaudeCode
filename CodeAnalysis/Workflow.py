"""Agent 工作流:任务拆解(Planner)、分析链执行(引擎)、状态管理(状态机)。

需求 3 的落地(与 Catering 同构):
- Planning:   分析链 JSON(安全团队可改)→ 校验 → 参数绑定 → 分析计划;
- Workflow:   引擎逐步执行链步骤:取数 → 规则判定 → 发现/证据,分支用 when 条件;
- State:      状态机全程持久化(run_dir/{run_id}.json),失败可重试续跑;
- 记忆/审计:  每步工具原始结果落盘 run_dir/evidence/{step_id}.json;
- 不让 LLM 猜: 引擎全程确定性,LLM 只出现在 REASONING 环节且无工具。

相对 Catering 的增量:
- when 新增 deploy_done / level_is:X,由引擎在关键步骤后写 stage_flags;
- 参数模板新增 {{ingest_date}} / {{deploy_id}}。
"""
import json
import threading
from datetime import datetime
from pathlib import Path

from CodeAnalysis.Configs import CodeAnalysisConfigs
from CodeAnalysis.Guardrails import (RULE_OPS, WHEN_OPS, eval_rule, eval_when)
from CodeAnalysis.Models import Evidence, Finding, StepResult
from CodeAnalysis.Tools import CodeAnalysisTools

# 工具 -> 数据源类别(五类数据源映射)
TOOL_SOURCES = {
    "inventory_assets": "asset_db",
    "get_asset": "asset_db",
    "scan_static": "rule_library",
    "list_scan_rules": "rule_library",
    "assess_security": "analysis_service",
    "review_env": "env_config_db",
    "deploy_asset": "deploy_module",
    "verify_deployment": "cloud_platform",
    "deploy_status": "cloud_platform",
    "search_cases": "knowledge_base",
    "register_asset": "asset_db",
}

NO_DATA_MESSAGE = "该维度数据缺失,无法判断"


# ── 任务拆解:Planner ──

class Planner:
    """把分析链 JSON 加载、校验并绑定为一次资产分析的计划。"""

    def __init__(self, chain_path: Path | None = None):
        self.chain_path = chain_path or CodeAnalysisConfigs.ANALYSIS_CHAIN_PATH

    def load_chain(self) -> dict:
        try:
            return json.loads(self.chain_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ValueError(f"分析链文件不存在: {self.chain_path}") from None
        except json.JSONDecodeError as e:
            raise ValueError(f"分析链文件损坏: {e}") from None

    def validate_chain(self, chain: dict) -> list[str]:
        """链结构校验:未知工具/非法 op/非法 when 必须拒启动,防静默错误。"""
        errors: list[str] = []
        if chain.get("schema_version") != CodeAnalysisConfigs.SCHEMA_VERSION:
            errors.append(f"分析链 schema_version 不支持: "
                          f"{chain.get('schema_version')}")
        known_tools = set(CodeAnalysisTools().handlers())
        steps = chain.get("steps")
        if not isinstance(steps, list) or not steps:
            errors.append("分析链必须包含 steps 数组")
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
            if when not in WHEN_OPS and not when.startswith("level_is:"):
                errors.append(f"{sid}: 非法 when 条件 {when}")
            for r in step.get("rules") or []:
                op = r.get("op")
                if op not in RULE_OPS:
                    errors.append(f"{sid}: 非法规则操作符 {op}")
                if op == "in" and not isinstance(r.get("threshold"), list):
                    errors.append(f"{sid}: op=in 的 threshold 必须为数组")
                if r.get("verdict") not in ("anomaly", "info"):
                    errors.append(f"{sid}: 非法 verdict {r.get('verdict')}")
                if r.get("threshold") is None:
                    errors.append(f"{sid}: 规则缺少 threshold")
        return errors

    def build_plan(self, chain: dict, asset) -> dict:
        """校验并返回分析链;步骤参数模板由引擎绑定资产。"""
        errors = self.validate_chain(chain)
        if errors:
            raise ValueError("分析链校验失败: " + "; ".join(errors))
        return chain


# ── 分析引擎 ──

class SecurityAnalysisEngine:
    """确定性安全分析引擎:沿分析链逐步执行、判定、沉淀发现与证据。

    可选注入 src.TaskSystem:分析链每步在任务看板创建/认领/完成,
    让分析过程在主代理的任务体系中同样可观测、可审计。
    """

    def __init__(self, tools: CodeAnalysisTools, run_dir: Path | None = None,
                 tasks=None):
        self.tools = tools
        self.run_dir = run_dir or CodeAnalysisConfigs.RUN_DIR
        self.tasks = tasks   # 可选的 src.TaskSystem(默认不镜像)

    '''
    整体流程(与 Catering 诊断引擎同构)
    for 分析链里的每个步骤:
        ① when 条件不满足? → 标记 excluded,跳过
        ② 把 {{project_id}}/{{ingest_date}}/{{symptom}}/{{deploy_id}} 模板绑定成真实参数
        ③ 调用工具执行 → 原始结果落盘 evidence/{step_id}.json(审计)
        ④ 数据缺失/报错? → 标记 no_data("数据缺失,无法判断"),继续下一步(不炸链)
        ⑤ 逐条规则判定 → 命中则生成发现(Finding),没命中 → "未见明显异常"
        ⑥ assess 步骤写 stage_flags.level(供 level_is:X 分支)
        ⑦ deploy 步骤写 stage_flags.deploy_done 与 bind.deploy_id
           (status=deployed 才置 True;blocked/failed 时 verify 步骤被排除)
        ⑧ 异常发现的关键词累积进 symptom,供最后案例检索步骤使用
    最后: 汇总 ran/excluded/no_data/errors → 返回四元组
    '''

    def run(self, chain: dict, asset) -> tuple[list[StepResult], list[Finding],
                                              list[Evidence], dict]:
        """执行整条链;返回 (步骤结果, 发现, 证据, 完整性)。"""
        # ① 初始化:参数绑定源 + 三收集器 + 分支状态
        bind = {"project_id": asset.project_id,
                "ingest_date": asset.ingest_date}
        stage_flags: dict = {}   # {level, deploy_done},供 when 分支
        step_results: list[StepResult] = []
        findings: list[Finding] = []
        evidence: list[Evidence] = []
        anomaly_seen = False
        symptom_parts: list[str] = []

        for step in chain["steps"]:
            # ② when 分支判断(always / any_anomaly / deploy_done / level_is:X)
            if not eval_when(step["when"], anomaly_seen, stage_flags):
                step_results.append(StepResult(
                    step_id=step["id"], status="excluded",
                    message=f"分支条件不满足: {step['when']}"))
                self._finish_task(self._start_task(step, asset))
                continue

            # ③ 参数绑定 + 工具执行
            task_id = self._start_task(step, asset)
            args = {k: self._resolve_arg(v, bind, symptom_parts)
                    for k, v in step.get("args", {}).items()}
            result = self.tools.execute(step["tool"], args)

            # ④ 证据落盘(写失败不阻断)
            ev_path = self._persist_evidence(step["id"], result)

            # ⑤ 数据缺失分支(缺失就明说,绝不编造)
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
                    data=result,
                    source=TOOL_SOURCES.get(step["tool"], "asset_db"),
                    path=ev_path))
                self._finish_task(task_id)
                continue   # 继续下一步,不炸链

            # ⑥ 规则判定(metric 点路径 + op 白名单 + threshold)
            rule_hits = [hit for rule in step["rules"]
                         if (hit := eval_rule(rule, result)) is not None]
            anomaly = any(h["verdict"] == "anomaly" for h in rule_hits)
            anomaly_seen = anomaly_seen or anomaly

            # ⑦ 阶段标记:assess 写等级,deploy 写部署完成标记与部署单号
            if step["id"] == "assess" and result.get("level"):
                stage_flags["level"] = result["level"]
            if step["id"] == "deploy":
                stage_flags["deploy_done"] = result.get("status") == "deployed"
                if result.get("deploy_id"):
                    bind["deploy_id"] = result["deploy_id"]

            # ⑧ 汇总该步骤的结论与状态(info 规则不升级为异常)
            if rule_hits:
                summary = ";".join(h["summary"] for h in rule_hits)
                verdict = "anomaly" if anomaly else "info"
            else:
                summary = f"{step['title']}: 未见明显异常"
                verdict = "normal"
            status = "anomaly" if anomaly else "normal"

            # ⑨ 沉淀 StepResult / Finding / Evidence
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
                data=result,
                source=TOOL_SOURCES.get(step["tool"], "asset_db"),
                path=ev_path))

            # ⑩ 症状累积(异常/参考级发现进入案例检索输入)
            if verdict in ("anomaly", "info"):
                symptom_parts.append(f"dimension:{step['dimension']}")
                symptom_parts.append(summary)
            self._finish_task(task_id)

        # ⑪ 步骤收尾:完整性汇总进报告
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

    def _start_task(self, step: dict, asset) -> str | None:
        """在注入的 TaskSystem 上创建并认领本步骤任务(无注入则跳过)。"""
        if self.tasks is None:
            return None
        task = self.tasks.create_task(
            subject=f"安全分析步骤: {step['title']}",
            description=f"{asset.project_id} {asset.ingest_date} "
                        f"({step['id']})")
        self.tasks.claim_task(task.id, owner="security-analysis-engine")
        return task.id

    def _finish_task(self, task_id: str | None):
        if task_id is not None:
            self.tasks.complete_task(task_id)

    def _resolve_arg(self, value, bind: dict, symptom_parts: list[str]):
        """解析参数模板: {{project_id}}/{{ingest_date}}/{{deploy_id}}/{{symptom}}。"""
        if not isinstance(value, str):
            return value
        if value == "{{symptom}}":
            return " ".join(symptom_parts) or "无异常发现"
        return value.replace("{{project_id}}", str(bind.get("project_id", ""))) \
                    .replace("{{ingest_date}}", str(bind.get("ingest_date", ""))) \
                    .replace("{{deploy_id}}", str(bind.get("deploy_id", "")))

    @staticmethod
    def _metrics_snapshot(result: dict) -> dict:
        """发现的关键数值快照:analysis 子字典(排除明细列表)。"""
        return dict(result.get("analysis") or {})

    def _persist_evidence(self, step_id: str, result: dict) -> str | None:
        """工具原始结果落盘(审计):失败不阻断分析流程。"""
        try:
            ev_dir = self.run_dir / "evidence"
            ev_dir.mkdir(parents=True, exist_ok=True)
            path = ev_dir / f"{step_id}.json"
            path.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                            encoding="utf-8")
            return str(path)
        except OSError:
            return None


# ── 状态管理:状态机 ──

STATE_EVENTS = {
    "NEW": {"accept": "ACCEPTED", "fail": "FAILED"},
    "ACCEPTED": {"plan_ready": "PLANNING", "fail": "FAILED"},
    "PLANNING": {"scan_start": "SCANNING", "fail": "FAILED"},
    "SCANNING": {"scan_ok": "REASONING", "scan_fail": "FAILED"},
    "REASONING": {"reason_ok": "REVIEWING", "reason_fail": "FAILED"},
    "REVIEWING": {"approve": "DONE", "rework": "REASONING", "fail": "FAILED"},
    "DONE": {},
    "FAILED": {"retry": "ACCEPTED"},
}


class CodeAnalysisStateMachine:
    """分析状态机:事件驱动转移 + 全程持久化,失败可重试续跑。"""

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
                "schema_version": CodeAnalysisConfigs.SCHEMA_VERSION}

    @classmethod
    def from_dict(cls, d: dict) -> "CodeAnalysisStateMachine":
        if d.get("schema_version") != CodeAnalysisConfigs.SCHEMA_VERSION:
            raise ValueError(f"状态文件 schema_version 不支持: "
                             f"{d.get('schema_version')}")
        machine = cls(run_id=str(d["run_id"]))
        machine.state = str(d["state"])
        machine.history = list(d.get("history") or [])
        machine.error = d.get("error")
        return machine

    def persist(self, run_dir: Path | None = None) -> Path:
        run_dir = run_dir or CodeAnalysisConfigs.RUN_DIR
        run_dir.mkdir(parents=True, exist_ok=True)
        path = run_dir / f"{self.run_id}.json"
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False,
                                   indent=2), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: Path) -> "CodeAnalysisStateMachine":
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))


def make_run_id(asset) -> str:
    """一次分析运行的唯一标识(状态/报告文件名共用)。"""
    stamp = datetime.now().strftime("%H%M%S%f")
    return f"run_{asset.project_id}_{asset.ingest_date}_{stamp}"
