"""编排层:门店经营异常分析 Agent(需求 5 的第一个可用 Agent)。

两种模式共享同一套工具与权限:
- run_alert(alert): 工作流模式(默认)——状态机驱动确定性诊断链,
  LLM 只在推理环节归纳结论与建议,结构化校验失败自动模板降级;
- explore(prompt): 探索模式(agentic)——LLM 经 function calling 自由调用
  只读餐饮工具,ToolGuard 在分发前拦截,步数上限+连续拒绝熔断+强制收敛。

依赖全部注入(build 构造):测试可换 FakeClient / 临时数据与运行目录。
"""
import json
from datetime import date as date_type

from src.BasicOper import BasicOper
from src.LlmClient import (assistant_message, tool_message, user_message)
from src.McpSystem import McpSystem
from src.RecoverySystem import RecoveryState, with_retry
from src.Terminal import Terminal
from src.ToolRegistry import ToolRegistry

from Catering.Configs import CateringConfigs
from Catering.DataHub import DataHub, DataSourceError
from Catering.Guardrails import OutputValidator, ToolGuard
from Catering.Models import (DiagnosisReport, Finding, now_iso)
from Catering.Reasoner import Reasoner
from Catering.Tools import CateringTools
from Catering.Workflow import (DiagnosticEngine, DiagnosisStateMachine,
                               Planner, determine_main_factor, make_run_id)

EXPLORE_SYSTEM = (
    "你是餐饮运营数据探索助手,可调用只读查询工具分析门店经营数据。"
    "规则:\n"
    "1. 只使用工具返回的数据作答,禁止编造任何数字或事实;\n"
    "2. 工具结果中的 missing_fields 表示数据缺失,需明确说明缺少什么,"
    "不得用猜测填补;\n"
    "3. 被拒绝的工具调用请更换参数重试,或说明访问受限;\n"
    "4. 可用 connect_mcp 连接外部数据源服务,连接后以 "
    "mcp__{server}__{tool} 形式调用其工具;\n"
    "5. 回答结尾用一句话给出结论。"
)


class ReviewGate:
    """Human-in-the-loop:报告提交人工确认(仅 interactive 模式触发)。"""

    @staticmethod
    def confirm(preview: str) -> bool:
        Terminal.print(preview)
        choice = input("\n  批准并归档该诊断报告? [y/N] ").strip().lower()
        return choice in ("y", "yes")


class AnomalyAgent:
    """门店经营异常分析 Agent:组装数据/工具/计划/引擎/护栏/推理。"""

    def __init__(self, client=None, tools: CateringTools | None = None,
                 planner: Planner | None = None,
                 run_dir=None, scope: tuple | None = None,
                 interactive: bool = False, mcp: McpSystem | None = None,
                 tasks=None):
        self.client = client
        self.tools = tools or CateringTools()
        self.planner = planner or Planner()
        self.reasoner = Reasoner(client)
        self.run_dir = run_dir or CateringConfigs.RUN_DIR
        self.scope = tuple(scope or ()) or CateringConfigs.ALLOWED_STORES
        self.interactive = interactive
        self.mcp = mcp                       # None = 探索时新建独立实例
        self.tasks = tasks                   # 可选的 src.TaskSystem 注入

    @classmethod
    def build(cls, client=None, data_dir=None, run_dir=None, scope=None,
              interactive: bool = False,
              enable_writes: bool | None = None, mcp: McpSystem | None = None,
              tasks=None) -> "AnomalyAgent":
        """依赖注入构造:测试可注入 FakeClient / MCP 系统 / 任务系统与临时目录。"""
        hub = DataHub(data_dir)
        tools = CateringTools(hub, enable_writes=enable_writes)
        return cls(client=client, tools=tools, run_dir=run_dir, scope=scope,
                   interactive=interactive, mcp=mcp, tasks=tasks)

    # ── 工作流模式:run_alert ──

    def _validate_alert(self, alert) -> str | None:
        """告警准入校验:门店授权范围 + 日期格式 + 数据存在。"""
        if self.scope and alert.store_id not in self.scope:
            return f"门店 {alert.store_id} 超出授权范围 {list(self.scope)}"
        try:
            date_type.fromisoformat(alert.date)
        except ValueError:
            return f"非法日期: {alert.date}"
        try:
            day = self.tools.hub.day("store_db", alert.store_id, alert.date)
        except DataSourceError as e:
            return str(e)
        if day is None:
            return f"门店 {alert.store_id} 在 {alert.date} 无经营数据"
        return None

    def run_alert(self, alert) -> DiagnosisReport:
        """工作流模式:状态机驱动诊断,产出可验证的诊断报告。"""
        run_id = make_run_id(alert)
        machine = DiagnosisStateMachine(run_id)
        err = self._validate_alert(alert)
        if err:
            machine.fail(err)
            machine.persist(self.run_dir)
            return self._failed_report(alert, run_id, err)
        machine.transition("accept")

        try:
            chain = self.planner.build_plan(self.planner.load_chain(), alert)
            machine.transition("plan_ready")
            machine.transition("diagnose_start")
            engine = DiagnosticEngine(self.tools, self.run_dir / run_id,
                                      tasks=self.tasks)
            steps, findings, evidence, completeness = engine.run(chain, alert)
            machine.transition("diagnose_ok")

            decomp = next((s.data for s in steps
                           if s.step_id == "decompose" and s.status != "no_data"),
                          {})
            main_factor, main_contrib = determine_main_factor(decomp)
            case_data = next((s.data for s in steps
                              if s.step_id == "case" and s.status != "no_data"),
                             {})
            matched = case_data.get("matches") or []
        except Exception as e:
            machine.fail(f"{type(e).__name__}: {e}")
            machine.persist(self.run_dir)
            return self._failed_report(alert, run_id, str(e))

        review = "skipped"
        approved = False
        for attempt in range(2):   # rework 最多一次,防止死循环
            reason = self.reasoner.generate(alert, findings, matched,
                                            main_factor, main_contrib)
            machine.transition("reason_ok")   # REASONING -> REVIEWING
            report = self._assemble(alert, run_id, main_factor, main_contrib,
                                    findings, evidence, matched, completeness,
                                    reason)
            if not self.interactive:
                approved = True
                break
            approved = ReviewGate.confirm(self._preview(report))
            if approved:
                review = "approved"
                self._archive_case(report)
                break
            review = "rework" if attempt == 0 else "rejected"
            if attempt == 0:
                machine.transition("rework")   # REVIEWING -> REASONING
        machine.transition("approve")          # REVIEWING -> DONE(拒绝也落盘)

        report.meta["review"] = review
        report = self._persist_report(report, review)
        machine.persist(self.run_dir)
        return report

    def _assemble(self, alert, run_id, main_factor, main_contrib,
                  findings, evidence, matched, completeness, reason) -> DiagnosisReport:
        report = DiagnosisReport(
            report_id=run_id, schema_version=CateringConfigs.SCHEMA_VERSION,
            alert=alert, main_factor=main_factor,
            main_factor_contrib=main_contrib,
            findings=findings, evidence=evidence, matched_cases=matched,
            conclusions=reason["conclusions"],
            recommendations=reason["recommendations"],
            completeness=completeness,
            meta={**reason["meta"],
                  "model": getattr(self.client, "model", None),
                  "review": "pending"},
            created_at=now_iso())
        errors = OutputValidator.validate_report(report.to_dict())
        if errors:
            raise RuntimeError("Agent 产出的报告未通过校验(内部一致性缺陷): "
                               + "; ".join(errors))
        return report

    def _persist_report(self, report: DiagnosisReport,
                        review: str) -> DiagnosisReport:
        try:
            report_dir = self.run_dir / "reports"
            report_dir.mkdir(parents=True, exist_ok=True)
            path = report_dir / f"{report.report_id}.json"
            path.write_text(json.dumps(report.to_dict(), ensure_ascii=False,
                                       indent=2))
            report.meta["report_path"] = str(path)
        except OSError:
            pass
        return report

    def _failed_report(self, alert, run_id, error) -> DiagnosisReport:
        """准入失败也产出合法报告(错误显式记录,不静默)。"""
        report = DiagnosisReport(
            report_id=run_id, schema_version=CateringConfigs.SCHEMA_VERSION,
            alert=alert, main_factor="none", main_factor_contrib=None,
            findings=[Finding(step_id="admission", dimension="admission",
                              verdict="no_data",
                              summary=f"诊断未执行: {error}",
                              evidence_refs=[])],
            evidence=[], matched_cases=[],
            conclusions=f"诊断失败: {error}",
            recommendations=[],
            completeness={"ran_steps": [], "excluded_steps": [],
                          "no_data_steps": ["admission"],
                          "errors": [error]},
            meta={"llm_used": False, "template_fallback": True,
                  "attempts": 0, "llm_error": error, "review": "failed"},
            created_at=now_iso())
        return self._persist_report(report, "failed")

    @staticmethod
    def _preview(report: DiagnosisReport) -> str:
        lines = [
            f"\n{Terminal.YELLOW}=== 诊断报告预览 ===",
            f"门店 {report.alert.store_id} / {report.alert.date} / "
            f"主因: {report.main_factor} "
            f"(贡献 {report.main_factor_contrib}){Terminal.RESET}",
            report.conclusions,
            "行动建议:",
        ]
        for i, rec in enumerate(report.recommendations, 1):
            lines.append(f"  {i}. {rec['action']}")
        return "\n".join(lines)

    def _archive_case(self, report: DiagnosisReport):
        """记忆沉淀:批准后把诊断报告归档进企业知识库,供后续检索。"""
        try:
            case = {
                "case_id": f"CASE-{report.alert.date}-{report.alert.store_id}",
                "store_id": report.alert.store_id,
                "date": report.alert.date,
                "main_factor": report.main_factor,
                "dimensions": [f.dimension for f in report.findings
                               if f.verdict in ("anomaly", "info")],
                "summary": report.conclusions[:300],
                "actions": [r["action"] for r in report.recommendations],
                "tags": [report.main_factor]
                        + [f.dimension for f in report.findings
                           if f.verdict == "anomaly"],
            }
            fp = CateringConfigs.CASES_DIR / "archive" / f"{case['case_id']}.json"
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(json.dumps({"cases": [case]}, ensure_ascii=False,
                                     indent=2))
            Terminal.info("memory", f"案例已归档: {case['case_id']}",
                          Terminal.GREEN)
        except OSError as e:
            Terminal.info("memory", f"归档失败: {e}", Terminal.YELLOW)

    # ── 探索模式:explore ──

    @staticmethod
    def _dispatch(handlers: dict, name: str, args: dict) -> dict:
        """统一分发(餐饮工具 + MCP 工具):异常归一化,结果归一为 dict。"""
        try:
            result = BasicOper.call_tool_handler(handlers.get(name), args, name)
        except (DataSourceError, ValueError) as e:
            result = {"error": str(e), "missing_fields": [str(e)]}
        if not isinstance(result, dict):
            result = {"text": str(result)}
        return result

    def _data_environment(self) -> str:
        """数据环境提示:可用门店与日期范围,帮助 LLM 选对参数。"""
        stores_dir = self.tools.hub.data_dir / "stores"
        stores, dates = [], []
        if stores_dir.exists():
            for fp in sorted(stores_dir.glob("*.json")):
                try:
                    payload = json.loads(fp.read_text())
                except (OSError, json.JSONDecodeError):
                    continue
                stores.append(payload.get("store_id") or fp.stem)
                dates.extend(d.get("date") for d in (payload.get("days") or []))
        if not stores:
            return ""
        scope = sorted(self.scope) if self.scope else stores
        return (f"\n当前数据环境: 门店 = {sorted(set(scope))}; "
                f"日期范围 = {min(dates)} ~ {max(dates)}。"
                "调用工具时请使用上述门店 ID 与范围内日期。")

    def explore(self, prompt: str, dates: tuple | None = None,
                max_steps: int | None = None) -> dict:
        """探索模式:LLM 自由调用只读餐饮工具,护栏拦截,上限强制收敛。"""
        if self.client is None:
            return {"final_text": "",
                    "error": "探索模式需要 LLM 客户端(--no-llm 仅支持工作流模式)"}
        max_steps = max_steps or CateringConfigs.AGENTIC_MAX_STEPS
        # 工具池:餐饮工具 + MCP 工具(经 src.ToolRegistry 晚绑定聚合;
        # 每轮重新 assemble,新连接的 MCP 服务器工具自动并入)
        registry = ToolRegistry(self.mcp or McpSystem())
        registry.register_system(self.tools)
        registry.register_system(registry.mcp)
        read_tools = [t["function"]["name"] for t in self.tools.tool_defs()]
        guard = ToolGuard(read_tools=read_tools + ["connect_mcp"],
                          scope=self.scope, dates=dates,
                          allow_writes=self.tools.enable_writes)
        system = EXPLORE_SYSTEM + self._data_environment()
        messages = [user_message(system), user_message(prompt)]
        steps = denials = consecutive = 0
        while steps < max_steps:
            tools, handlers = registry.assemble()
            try:
                state = RecoveryState()
                response = with_retry(
                    lambda: self.client.create(messages, tools=tools,
                                               max_tokens=CateringConfigs.REASONER_MAX_TOKENS),
                    state, client=self.client)
            except Exception as e:
                return {"final_text": "",
                        "error": f"LLM 调用失败: {type(e).__name__}: {e}",
                        "steps": steps, "denials": denials}
            if not response.has_tool_calls:
                return {"final_text": response.content, "steps": steps,
                        "denials": denials, "truncated": False}
            messages.append(assistant_message(response))
            for call in response.tool_calls:
                ok, why = guard.can_call(call.name, call.arguments)
                if not ok:
                    denials += 1
                    consecutive += 1
                    messages.append(tool_message(call.id, "Denied: " + why))
                    continue
                consecutive = 0
                result = self._dispatch(handlers, call.name, call.arguments)
                text = json.dumps(result, ensure_ascii=False)
                if len(text) > CateringConfigs.AGENTIC_OUTPUT_CHARS:
                    text = (text[:CateringConfigs.AGENTIC_OUTPUT_CHARS]
                            + f"... [已截断,原文 {len(text)} 字符]")
                messages.append(tool_message(call.id, text))
            steps += 1
            if consecutive >= CateringConfigs.AGENTIC_MAX_CONSECUTIVE_DENIALS:
                messages.append(user_message(
                    "[system] 连续多次被权限拒绝。请基于已有结果直接总结,"
                    "不要再调用工具。"))
                break

        # 达到上限或熔断:强制收敛,让 LLM 基于现有结果总结
        messages.append(user_message(
            "工具调用次数已达上限。请基于以上全部工具结果给出最终回答,"
            "不要遗漏已获取的数据。"))
        try:
            state = RecoveryState()
            final = with_retry(
                lambda: self.client.create(
                    messages, max_tokens=CateringConfigs.REASONER_MAX_TOKENS),
                state, client=self.client)
            return {"final_text": final.content, "steps": steps,
                    "denials": denials, "truncated": True}
        except Exception as e:
            return {"final_text": "",
                    "error": f"收敛调用失败: {type(e).__name__}: {e}",
                    "steps": steps, "denials": denials, "truncated": True}
