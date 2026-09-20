"""编排层:代码资产安全分析 Agent(需求 1-4 的组装)。

两种模式共享同一套工具与权限:
- run_analysis(asset): 工作流模式(默认)——状态机驱动确定性分析链
  (盘点 → 静态扫描 → 自评估 → 环境审查 → 一键部署 → 部署验证 → 案例检索),
  LLM 只在推理环节归纳结论与建议,结构化校验失败自动模板降级;
- explore(prompt): 探索模式(agentic)——LLM 经 function calling 自由调用
  只读分析工具,ToolGuard 在分发前拦截,步数上限+连续拒绝熔断+强制收敛。

最终报告的安全等级与主风险由风险评估模型(静态+环境+部署三类子项)
确定性计算,每个结论都能回溯到落盘的证据文件。
"""
import json

from src.BasicOper import BasicOper
from src.LlmClient import (assistant_message, tool_message, user_message)
from src.McpSystem import McpSystem
from src.RecoverySystem import RecoveryState, with_retry
from src.Terminal import Terminal
from src.ToolRegistry import ToolRegistry

from CodeAnalysis.Configs import CodeAnalysisConfigs
from CodeAnalysis.DataHub import DataHub, DataSourceError
from CodeAnalysis.Deployer import Deployer
from CodeAnalysis.Guardrails import OutputValidator, ToolGuard
from CodeAnalysis.Metrics import overall_assessment
from CodeAnalysis.Models import AnalysisReport, Finding, now_iso
from CodeAnalysis.Reasoner import Reasoner
from CodeAnalysis.Scanner import Scanner
from CodeAnalysis.Tools import CodeAnalysisTools
from CodeAnalysis.Workflow import (CodeAnalysisStateMachine, Planner,
                                   SecurityAnalysisEngine, make_run_id)

EXPLORE_SYSTEM = (
    "你是代码资产安全分析助手,可调用只读查询工具分析入库代码资产的安全状况。"
    "规则:\n"
    "1. 只使用工具返回的数据作答,禁止编造任何漏洞、数字或事实;\n"
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
        choice = input("\n  批准并归档该安全分析报告? [y/N] ").strip().lower()
        return choice in ("y", "yes")


class CodeAnalysisAgent:
    """代码资产安全分析 Agent:组装数据/扫描/部署/计划/引擎/护栏/推理。"""

    def __init__(self, client=None, tools: CodeAnalysisTools | None = None,
                 planner: Planner | None = None, run_dir=None,
                 scope: tuple | None = None, interactive: bool = False,
                 mcp: McpSystem | None = None, tasks=None):
        self.client = client
        self.tools = tools or CodeAnalysisTools()
        self.planner = planner or Planner()
        self.reasoner = Reasoner(client)
        self.run_dir = run_dir or CodeAnalysisConfigs.RUN_DIR
        self.scope = tuple(scope or ()) or CodeAnalysisConfigs.ALLOWED_PROJECTS
        self.interactive = interactive
        self.mcp = mcp                       # None = 探索时新建独立实例
        self.tasks = tasks                   # 可选的 src.TaskSystem 注入

    @classmethod
    def build(cls, client=None, data_dir=None, run_dir=None, scope=None,
              interactive: bool = False, enable_writes: bool | None = None,
              mcp: McpSystem | None = None, tasks=None) -> "CodeAnalysisAgent":
        """依赖注入构造:测试可注入 FakeClient / MCP 系统 / 任务系统与临时目录。"""
        hub = DataHub(data_dir)
        scanner = Scanner(hub.data_dir / "rules")
        deployer = Deployer(hub.cloud_source, hub.cloud_source.registry_map(),
                            assets_dir=hub.data_dir / "assets")
        tools = CodeAnalysisTools(hub=hub, scanner=scanner, deployer=deployer,
                                  enable_writes=enable_writes)
        return cls(client=client, tools=tools, run_dir=run_dir, scope=scope,
                   interactive=interactive, mcp=mcp, tasks=tasks)

    # ── 工作流模式:run_analysis ──

    def _validate_asset(self, asset) -> str | None:
        """资产准入校验:授权范围 + 入库日期格式 + 资产存在。"""
        if self.scope and asset.project_id not in self.scope:
            return f"项目 {asset.project_id} 超出授权范围 {list(self.scope)}"
        try:
            from datetime import date as date_type
            date_type.fromisoformat(asset.ingest_date)
        except ValueError:
            return f"非法入库日期: {asset.ingest_date}"
        if self.tools.hub.asset(asset.project_id) is None:
            return f"项目 {asset.project_id} 未入库"
        return None

    def run_analysis(self, asset) -> AnalysisReport:
        """工作流模式:状态机驱动安全分析,产出可验证的分析报告。"""
        run_id = make_run_id(asset)
        machine = CodeAnalysisStateMachine(run_id)
        err = self._validate_asset(asset)
        if err:
            machine.fail(err)
            machine.persist(self.run_dir)
            return self._failed_report(asset, run_id, err)
        machine.transition("accept")

        try:
            chain = self.planner.build_plan(self.planner.load_chain(), asset)
            machine.transition("plan_ready")
            machine.transition("scan_start")
            engine = SecurityAnalysisEngine(self.tools, self.run_dir / run_id,
                                            tasks=self.tasks)
            steps, findings, evidence, completeness = engine.run(chain, asset)
            machine.transition("scan_ok")

            # 最终评估:静态+环境+部署三类风险子项(部署风险来自 deploy 步骤)
            scan_data = self._step_data(steps, "static_scan")
            env_data = self._step_data(steps, "env_review")
            deploy_data = self._step_data(steps, "deploy")
            assessment = overall_assessment(
                scan_data.get("findings") or [],
                env_data.get("issues") or [],
                deploy_data.get("risk_events") or [])
            main_risk = assessment["main_risk"]
            main_score = assessment["main_risk_score"]
            case_data = self._step_data(steps, "case")
            matched = case_data.get("matches") or []
        except Exception as e:
            machine.fail(f"{type(e).__name__}: {e}")
            machine.persist(self.run_dir)
            return self._failed_report(asset, run_id, str(e))

        review = "skipped"
        approved = False
        for attempt in range(2):   # rework 最多一次,防止死循环
            reason = self.reasoner.generate(asset, findings, matched,
                                            main_risk, main_score,
                                            assessment["risk_score"],
                                            assessment["security_level"])
            machine.transition("reason_ok")   # REASONING -> REVIEWING
            report = self._assemble(asset, run_id, assessment, main_risk,
                                    main_score, findings, evidence, matched,
                                    completeness, reason)
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

    @staticmethod
    def _step_data(steps: list, step_id: str) -> dict:
        """取某步骤的工具结果数据(no_data 步骤返回空字典)。"""
        return next((s.data for s in steps
                     if s.step_id == step_id and s.status != "no_data"), {})

    def _assemble(self, asset, run_id, assessment: dict, main_risk,
                  main_score, findings, evidence, matched, completeness,
                  reason) -> AnalysisReport:
        report = AnalysisReport(
            report_id=run_id,
            schema_version=CodeAnalysisConfigs.SCHEMA_VERSION,
            asset=asset, main_risk=main_risk, main_risk_score=main_score,
            risk_score=assessment["risk_score"],
            security_level=assessment["security_level"],
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

    def _persist_report(self, report: AnalysisReport,
                        review: str) -> AnalysisReport:
        try:
            report_dir = self.run_dir / "reports"
            report_dir.mkdir(parents=True, exist_ok=True)
            path = report_dir / f"{report.report_id}.json"
            path.write_text(json.dumps(report.to_dict(), ensure_ascii=False,
                                       indent=2), encoding="utf-8")
            report.meta["report_path"] = str(path)
        except OSError:
            pass
        return report

    def _failed_report(self, asset, run_id, error) -> AnalysisReport:
        """准入失败也产出合法报告(错误显式记录,不静默)。"""
        report = AnalysisReport(
            report_id=run_id,
            schema_version=CodeAnalysisConfigs.SCHEMA_VERSION,
            asset=asset, main_risk="none", main_risk_score=None,
            risk_score=0.0, security_level="A",
            findings=[Finding(step_id="admission", dimension="admission",
                              verdict="no_data",
                              summary=f"分析未执行: {error}",
                              evidence_refs=[])],
            evidence=[], matched_cases=[],
            conclusions=f"分析失败: {error}",
            recommendations=[],
            completeness={"ran_steps": [], "excluded_steps": [],
                          "no_data_steps": ["admission"],
                          "errors": [error]},
            meta={"llm_used": False, "template_fallback": True,
                  "attempts": 0, "llm_error": error, "review": "failed"},
            created_at=now_iso())
        return self._persist_report(report, "failed")

    @staticmethod
    def _preview(report: AnalysisReport) -> str:
        lines = [
            f"\n{Terminal.YELLOW}=== 安全分析报告预览 ===",
            f"{report.asset.name}({report.asset.project_id}) / "
            f"入库 {report.asset.ingest_date} / "
            f"等级 {report.security_level} / 主风险 {report.main_risk} "
            f"(风险分 {report.main_risk_score}){Terminal.RESET}",
            report.conclusions,
            "整改建议:",
        ]
        for i, rec in enumerate(report.recommendations, 1):
            lines.append(f"  {i}. {rec['action']}")
        return "\n".join(lines)

    def _archive_case(self, report: AnalysisReport):
        """记忆沉淀:批准后把报告归档进知识库,供后续检索。"""
        try:
            case = {
                "case_id": f"CASE-{report.asset.ingest_date}-"
                           f"{report.asset.project_id}",
                "project_id": report.asset.project_id,
                "main_risk": report.main_risk,
                "summary": report.conclusions[:300],
                "actions": [r["action"] for r in report.recommendations],
                "tags": [report.main_risk]
                        + [f.dimension for f in report.findings
                           if f.verdict == "anomaly"],
            }
            fp = (CodeAnalysisConfigs.CASES_DIR / "archive"
                  / f"{case['case_id']}.json")
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(json.dumps({"cases": [case]}, ensure_ascii=False,
                                     indent=2), encoding="utf-8")
            Terminal.info("memory", f"案例已归档: {case['case_id']}",
                          Terminal.GREEN)
        except OSError as e:
            Terminal.info("memory", f"归档失败: {e}", Terminal.YELLOW)

    # ── 探索模式:explore ──

    @staticmethod
    def _dispatch(handlers: dict, name: str, args: dict) -> dict:
        """统一分发(分析工具 + MCP 工具):异常归一化,结果归一为 dict。"""
        try:
            result = BasicOper.call_tool_handler(handlers.get(name), args, name)
        except (DataSourceError, ValueError) as e:
            result = {"error": str(e), "missing_fields": [str(e)]}
        if not isinstance(result, dict):
            result = {"text": str(result)}
        return result

    def _data_environment(self) -> str:
        """数据环境提示:可用项目清单,帮助 LLM 选对参数。"""
        assets = self.tools.hub.all_assets()
        if not assets:
            return ""
        projects = sorted(a["project_id"] for a in assets)
        scope = sorted(self.scope) if self.scope else projects
        return (f"\n当前数据环境: 可用项目 = {sorted(set(scope))}。"
                "调用工具时请使用上述项目 ID。")

    def explore(self, prompt: str, max_steps: int | None = None) -> dict:
        """探索模式:LLM 自由调用只读分析工具,护栏拦截,上限强制收敛。"""
        if self.client is None:
            return {"final_text": "",
                    "error": "探索模式需要 LLM 客户端(--no-llm 仅支持工作流模式)"}
        max_steps = max_steps or CodeAnalysisConfigs.AGENTIC_MAX_STEPS
        # 工具池:分析工具 + MCP 工具(经 src.ToolRegistry 晚绑定聚合;
        # 每轮重新 assemble,新连接的 MCP 服务器工具自动并入)
        registry = ToolRegistry(self.mcp or McpSystem())
        registry.register_system(self.tools)
        registry.register_system(registry.mcp)
        read_tools = [t["function"]["name"] for t in self.tools.tool_defs()]
        guard = ToolGuard(read_tools=read_tools + ["connect_mcp"],
                          scope=self.scope,
                          allow_writes=self.tools.enable_writes)
        system = EXPLORE_SYSTEM + self._data_environment()
        messages = [user_message(system), user_message(prompt)]

        steps = denials = consecutive = 0
        while steps < max_steps:
            tools, handlers = registry.assemble()
            try:
                state = RecoveryState()
                response = with_retry(
                    lambda: self.client.create(
                        messages, tools=tools,
                        max_tokens=CodeAnalysisConfigs.REASONER_MAX_TOKENS,
                        model=state.current_model),
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
                if len(text) > CodeAnalysisConfigs.AGENTIC_OUTPUT_CHARS:
                    text = (text[:CodeAnalysisConfigs.AGENTIC_OUTPUT_CHARS]
                            + f"... [已截断,原文 {len(text)} 字符]")
                messages.append(tool_message(call.id, text))
            steps += 1
            if consecutive >= CodeAnalysisConfigs.AGENTIC_MAX_CONSECUTIVE_DENIALS:
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
                    messages, max_tokens=CodeAnalysisConfigs.REASONER_MAX_TOKENS,
                    model=state.current_model),
                state, client=self.client)
            return {"final_text": final.content, "steps": steps,
                    "denials": denials, "truncated": True}
        except Exception as e:
            return {"final_text": "",
                    "error": f"收敛调用失败: {type(e).__name__}: {e}",
                    "steps": steps, "denials": denials, "truncated": True}
