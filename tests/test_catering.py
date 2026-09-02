"""Catering 餐饮业务包单元测试(全部离线,不读 .env、不碰网络)。

运行: python -m unittest discover -s tests -v
FakeClient 与 test_agent_loop.py 同款:script 按序回放 + calls 记录快照。
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from Catering.AnomalyAgent import AnomalyAgent
from Catering.Configs import CateringConfigs
from Catering.DataHub import DataHub, DataSourceError
from Catering.Evaluator import Evaluator
from Catering.Guardrails import OutputValidator, ToolGuard
from Catering.Metrics import (channel_breakdown, check_additive_invariants,
                              detect_alerts, lmdi_decompose,
                              mix_price_decomposition, pct_change)
from Catering.Models import AnomalyAlert, Finding
from Catering.Reasoner import Reasoner
from Catering.Scheduler import CateringScheduler
from Catering.Tools import CateringTools
from Catering.Workflow import (DiagnosticEngine, DiagnosisStateMachine,
                               Planner)

from src.LlmClient import LlmResponse, ToolCall
from src.McpSystem import McpSystem
from src.SkillSystem import SKILL_SYSTEM
from src.TaskSystem import TaskSystem

DATA_DIR = Path(__file__).resolve().parent.parent / "Catering" / "data"
ALERT_DATE = "2026-09-01"


class FakeClient:
    """按脚本回放 LLM 回复,记录每次请求快照(与 test_agent_loop.py 同款)。"""

    def __init__(self, script: list[LlmResponse]):
        self.script = list(script)
        self.calls = []
        self.fallback_model = None
        self.model = "fake-model"

    def create(self, messages, system=None, tools=None, max_tokens=4096,
               model=None):
        self.calls.append({
            "messages": json.loads(json.dumps(messages, default=str)),
            "system": system,
            "tool_names": [t["function"]["name"] for t in (tools or [])],
            "max_tokens": max_tokens,
        })
        return self.script.pop(0)


def build_agent(client=None, scope=None, data_dir: Path | None = None,
                interactive=False, enable_writes=None):
    run_dir = Path(tempfile.mkdtemp())
    agent = AnomalyAgent.build(client=client, data_dir=data_dir or DATA_DIR,
                               run_dir=run_dir, scope=scope,
                               interactive=interactive,
                               enable_writes=enable_writes)
    return agent, run_dir


def load_stores() -> list[dict]:
    stores = []
    for fp in sorted((DATA_DIR / "stores").glob("*.json")):
        stores.append(json.loads(fp.read_text()))
    return stores


def llm_text(text: str) -> LlmResponse:
    return LlmResponse(content=text, finish_reason="stop")


# ── 指标计算 ──


class TestLmdiDecomposition(unittest.TestCase):
    def test_two_factor_exact_no_residual(self):
        """营收 = 客流 × 客单价:两因子贡献之和精确等于总变化。"""
        r = lmdi_decompose(cur_revenue=81180, cur_traffic=1350,
                           base_revenue=99000, base_traffic=1800)
        self.assertAlmostEqual(r["traffic_contrib"] + r["ticket_contrib"],
                               r["total_delta"], places=6)
        self.assertAlmostEqual(r["traffic_share"] + r["ticket_share"], 1.0,
                               places=6)
        self.assertLess(r["traffic_contrib"], 0)          # 客流下拉
        self.assertGreater(r["ticket_contrib"], 0)        # 客单价回补

    def test_channel_aggregation(self):
        """渠道拆解的营收增量之和等于店级总变化。"""
        hub = DataHub(DATA_DIR)
        pos_cur = hub.day("pos", "A店", ALERT_DATE)
        tok_cur = hub.day("takeout", "A店", ALERT_DATE)
        result = channel_breakdown(
            pos_cur, hub.baseline("pos", "A店", ALERT_DATE),
            tok_cur, hub.baseline("takeout", "A店", ALERT_DATE))
        channel_delta = sum(c["delta"] for c in result["channels"])
        self.assertAlmostEqual(channel_delta, result["analysis"]["total_delta"],
                               places=6)
        self.assertAlmostEqual(result["analysis"]["total_delta"], -17820.0,
                               places=2)

    def test_mix_price_decomposition(self):
        """B店菜品结构: mix + price == 加权均价变化,方向正确。"""
        hub = DataHub(DATA_DIR)
        cur = hub.day("store_db", "B店", ALERT_DATE)
        base = hub.baseline("store_db", "B店", ALERT_DATE)
        by_cat: dict[str, list] = {}
        for day in base:
            for d in day["dishes"]:
                by_cat.setdefault(d["category"], []).append(d)
        base_agg = [{"category": c, "share": sum(x["share"] for x in v) / len(v),
                     "ticket": sum(x["ticket"] for x in v) / len(v)}
                    for c, v in by_cat.items()]
        r = mix_price_decomposition(cur["dishes"], base_agg)
        self.assertAlmostEqual(r["mix_effect"] + r["price_effect"],
                               r["ticket_delta"], places=6)
        self.assertLess(r["ticket_delta"], 0)
        self.assertLess(r["mix_effect"], r["price_effect"])   # 结构是主因

    def test_zero_or_negative_base_robust(self):
        """零/负基期:拆解抛错、同比返回 None(不猜数)。"""
        with self.assertRaises(ValueError):
            lmdi_decompose(cur_revenue=100, cur_traffic=10,
                           base_revenue=0, base_traffic=10)
        self.assertIsNone(pct_change(100, 0))
        self.assertIsNone(pct_change(100, -5))
        self.assertIsNone(pct_change(None, 10))


# ── 数据层 ──


class TestDataHub(unittest.TestCase):
    def test_all_sources_healthy(self):
        """八类数据源均可用且返回契约结构。"""
        hub = DataHub(DATA_DIR)
        self.assertEqual(hub.day("store_db", "A店", ALERT_DATE)["revenue"],
                         81180.0)
        self.assertEqual(hub.day("pos", "A店", ALERT_DATE)["traffic"], 1080)
        self.assertEqual(hub.day("takeout", "A店", ALERT_DATE)["traffic"], 270)
        promotions = hub.query("internal_system", "B店", ALERT_DATE)
        self.assertEqual(promotions["promotions"][0]["name"], "9.9元引流套餐")
        weather = hub.query("external_api", "A店", ALERT_DATE, kind="weather")
        self.assertEqual(weather["weather"]["value"], "暴雨")
        market = hub.query("external_api", "A店", ALERT_DATE, kind="market")
        self.assertEqual(market["market_events"][0]["type"],
                         "competitor_promotion")
        cases = hub.search_cases("A店", "dimension:traffic 暴雨")
        self.assertTrue(cases["matches"])

    def test_missing_source_raises(self):
        hub = DataHub(DATA_DIR)
        with self.assertRaises(DataSourceError):
            hub.day("store_db", "X店", ALERT_DATE)

    def test_fixture_additive_invariants(self):
        """样本数据跨维度自洽:Σ渠道=日总、Σ时段=日总、Σ菜品=日总。"""
        pos = [json.loads(fp.read_text())
               for fp in sorted((DATA_DIR / "pos").glob("*.json"))]
        takeout = [json.loads(fp.read_text())
                   for fp in sorted((DATA_DIR / "takeout").glob("*.json"))]
        self.assertEqual(check_additive_invariants(load_stores(), pos,
                                                   takeout), [])


class TestDetectAlerts(unittest.TestCase):
    def test_threshold_scan(self):
        """阈值扫描恰好产出 A、B 两个告警,变化率与锚点一致。"""
        alerts = detect_alerts(load_stores())
        by_store = {a.store_id: a for a in alerts}
        self.assertEqual(set(by_store), {"A店", "B店"})
        self.assertAlmostEqual(by_store["A店"].change_pct, -18.0, places=6)
        self.assertAlmostEqual(by_store["B店"].change_pct, -10.0, places=6)


# ── 权限与工具 ──


class TestToolGuard(unittest.TestCase):
    def setUp(self):
        self.guard = ToolGuard(read_tools=["query_metrics"], scope=("A店",),
                               dates=("2026-08-01", "2026-09-02"))

    def test_denials(self):
        """越界门店/越界日期/未白名单/写工具/非法日期 → 可读拒绝。"""
        cases = [
            ("query_metrics", {"store_id": "B店", "date": ALERT_DATE}),
            ("query_metrics", {"store_id": "A店", "date": "2025-01-01"}),
            ("query_metrics", {"store_id": "A店", "date": "bad-date"}),
            ("bogus_tool", {"store_id": "A店", "date": ALERT_DATE}),
            ("archive_case", {"store_id": "A店", "date": ALERT_DATE}),
        ]
        for name, args in cases:
            ok, why = self.guard.can_call(name, args)
            self.assertFalse(ok, f"{name} {args} 应被拒绝")
            self.assertTrue(why)

    def test_allow(self):
        ok, why = self.guard.can_call("query_metrics",
                                      {"store_id": "A店", "date": ALERT_DATE})
        self.assertTrue(ok)
        self.assertEqual(why, "")


class TestToolDefinitions(unittest.TestCase):
    def test_defs_handlers_consistency(self):
        """工具定义与处理器一一对应;写工具默认不暴露,开启后暴露。"""
        tools = CateringTools()
        defs = {t["function"]["name"] for t in tools.tool_defs()}
        handlers = set(tools.handlers())
        self.assertEqual(defs, handlers - {"archive_case"})
        write_tools = CateringTools(enable_writes=True)
        self.assertIn("archive_case",
                      {t["function"]["name"] for t in write_tools.tool_defs()})

    def test_archive_case_write_path(self):
        """归档写工具:开启后落盘知识库,关闭时拒绝。"""
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(CateringConfigs, "CASES_DIR",
                                   Path(tmp)):
                off = CateringTools(enable_writes=False)
                with self.assertRaises(ValueError):
                    off.run_archive_case("A店", ALERT_DATE, "traffic", [],
                                         "s", [], [])
                on = CateringTools(enable_writes=True)
                result = on.run_archive_case("A店", ALERT_DATE, "traffic",
                                             ["traffic"], "摘要", ["行动"],
                                             ["暴雨"])
                self.assertTrue((Path(tmp) / "archive").exists())
                self.assertIn("CASE-2026-09-01-A店", result["archived"])


# ── 工作流模式端到端 ──


class TestWorkflowEndToEnd(unittest.TestCase):
    def _run(self, store_id: str):
        agent, run_dir = build_agent(client=None, interactive=False)
        alert = AnomalyAlert(store_id=store_id, date=ALERT_DATE,
                             source="detector")
        report = agent.run_alert(alert)
        return agent, run_dir, report

    def test_store_a_traffic(self):
        """A店:主因客流,外部因素/历史案例证据齐全,报告通过校验。"""
        _, run_dir, report = self._run("A店")
        self.assertEqual(report.main_factor, "traffic")
        self.assertLess(report.main_factor_contrib, 0)
        steps = {e.step_id for e in report.evidence}
        self.assertIn("external", steps)
        self.assertIn("case", steps)
        self.assertTrue(report.matched_cases)
        self.assertEqual(report.meta["template_fallback"], True)
        self.assertEqual(OutputValidator.validate_report(report.to_dict()), [])
        self.assertTrue((run_dir / "reports" / f"{report.report_id}.json")
                        .exists())
        state_files = list(run_dir.glob("run_*.json"))
        self.assertTrue(state_files)

    def test_store_b_ticket(self):
        """B店:主因客单价,命中历史案例,建议引用案例动作。"""
        _, _, report = self._run("B店")
        self.assertEqual(report.main_factor, "ticket")
        self.assertTrue(any(c["case_id"] == "CASE-2026-0712-B"
                            for c in report.matched_cases))
        self.assertTrue(any(r.get("source") == "case"
                            for r in report.recommendations))

    def test_store_c_normal(self):
        """C店:正常对照,无主因,案例步骤被分支排除。"""
        _, _, report = self._run("C店")
        self.assertEqual(report.main_factor, "none")
        self.assertIsNone(report.main_factor_contrib)
        self.assertIn("case", report.completeness["excluded_steps"])
        self.assertFalse(any(f.verdict == "anomaly"
                             for f in report.findings))


class TestSingleStepFailure(unittest.TestCase):
    def test_no_data_does_not_break_chain(self):
        """单步数据缺失 → no_data 标记,诊断链继续执行并正常产出。"""
        hub = DataHub(DATA_DIR)
        real_day = hub.day
        hub.day = lambda category, store_id, date: (
            None if category == "pos" else real_day(category, store_id, date))
        tools = CateringTools(hub)
        engine = DiagnosticEngine(tools, run_dir=Path(tempfile.mkdtemp()))
        planner = Planner()
        chain = planner.load_chain()
        alert = AnomalyAlert(store_id="A店", date=ALERT_DATE,
                             source="detector")
        steps, findings, _, completeness = engine.run(chain, alert)
        self.assertIn("periods", completeness["no_data_steps"])
        self.assertIn("channels", completeness["no_data_steps"])
        no_data_finding = next(f for f in findings
                               if f.step_id == "periods")
        self.assertEqual(no_data_finding.verdict, "no_data")
        self.assertIn("数据缺失", no_data_finding.summary)
        self.assertEqual([s.step_id for s in steps if s.step_id == "decompose"]
                         [0], "decompose")


# ── 推理环节 ──


class TestReasoner(unittest.TestCase):
    @staticmethod
    def _fixture():
        alert = AnomalyAlert(store_id="A店", date=ALERT_DATE,
                             change_pct=-18.0, source="detector")
        findings = [Finding(step_id="traffic", dimension="traffic",
                            verdict="anomaly", summary="客流下降",
                            evidence_refs=["traffic"])]
        return alert, findings

    def test_llm_success(self):
        alert, findings = self._fixture()
        output = json.dumps({"conclusions": "主因是客流下降",
                             "recommendations": [
                                 {"action": "检查引流", "reason": "客流下降",
                                  "evidence_refs": ["traffic"]}]},
                            ensure_ascii=False)
        client = FakeClient([llm_text(output)])
        result = Reasoner(client).generate(alert, findings, [], "traffic",
                                           -25832.55)
        self.assertTrue(result["meta"]["llm_used"])
        self.assertFalse(result["meta"]["template_fallback"])
        self.assertEqual(result["meta"]["attempts"], 1)
        self.assertEqual(result["recommendations"][0]["source"], "llm")

    def test_llm_retry_once_then_success(self):
        alert, findings = self._fixture()
        output = json.dumps({"conclusions": "c",
                             "recommendations": [
                                 {"action": "a", "reason": "r",
                                  "evidence_refs": ["traffic"]}]},
                            ensure_ascii=False)
        client = FakeClient([llm_text("这不是 JSON"), llm_text(output)])
        result = Reasoner(client).generate(alert, findings, [], "traffic",
                                           -25832.55)
        self.assertTrue(result["meta"]["llm_used"])
        self.assertEqual(result["meta"]["attempts"], 2)

    def test_llm_double_failure_falls_back_to_template(self):
        """LLM 输出持续非法 → 模板降级,与无 client 输出完全一致(确定性)。"""
        alert, findings = self._fixture()
        client = FakeClient([llm_text("垃圾1"), llm_text("垃圾2")])
        result = Reasoner(client).generate(alert, findings, [], "traffic",
                                           -25832.55)
        self.assertTrue(result["meta"]["template_fallback"])
        self.assertFalse(result["meta"]["llm_used"])
        self.assertEqual(result["meta"]["attempts"], 2)
        offline = Reasoner(None).generate(alert, findings, [], "traffic",
                                          -25832.55)
        self.assertEqual(result["conclusions"], offline["conclusions"])
        self.assertEqual(result["recommendations"],
                         offline["recommendations"])

    def test_llm_reject_invalid_refs(self):
        """LLM 引用不存在的证据 → 判非法,走模板。"""
        alert, findings = self._fixture()
        output = json.dumps({"conclusions": "c",
                             "recommendations": [
                                 {"action": "a", "reason": "r",
                                  "evidence_refs": ["不存在"]}]},
                            ensure_ascii=False)
        client = FakeClient([llm_text(output), llm_text(output)])
        result = Reasoner(client).generate(alert, findings, [], "traffic",
                                           -25832.55)
        self.assertTrue(result["meta"]["template_fallback"])


# ── 输出结构化校验 ──


class TestOutputValidator(unittest.TestCase):
    def test_tamper_detection(self):
        """篡改报告逐项报错:缺字段/坏枚举/悬空引用/坏 schema 版本。"""
        agent, _ = build_agent(client=None, interactive=False)
        report = agent.run_alert(AnomalyAlert(store_id="A店",
                                              date=ALERT_DATE,
                                              source="detector"))
        d = report.to_dict()
        self.assertEqual(OutputValidator.validate_report(d), [])

        missing = dict(d)
        del missing["main_factor"]
        self.assertTrue(OutputValidator.validate_report(missing))

        bad_enum = dict(d)
        bad_enum["main_factor"] = "weather"
        self.assertTrue(OutputValidator.validate_report(bad_enum))

        bad_verdict = dict(d)
        bad_verdict["findings"] = [{**d["findings"][0], "verdict": "bogus"}]
        self.assertTrue(OutputValidator.validate_report(bad_verdict))

        bad_ref = dict(d)
        bad_ref["recommendations"] = [{"action": "a", "reason": "r",
                                       "evidence_refs": ["nope"],
                                       "source": "llm"}]
        self.assertTrue(OutputValidator.validate_report(bad_ref))

        bad_version = dict(d)
        bad_version["schema_version"] = 99
        self.assertTrue(OutputValidator.validate_report(bad_version))

        bad_meta = dict(d)
        bad_meta["meta"] = {**d["meta"], "template_fallback": "yes"}
        self.assertTrue(OutputValidator.validate_report(bad_meta))


# ── 探索模式(agentic) ──


class TestExploreMode(unittest.TestCase):
    def _agent(self, client, scope=None):
        agent, run_dir = build_agent(client=client, scope=scope)
        return agent

    def test_tool_calling_sequence(self):
        """LLM 调用工具 → 真实执行 → 结果回传 → 正常收敛。"""
        script = [
            LlmResponse(content="", finish_reason="tool_calls",
                        tool_calls=[ToolCall(id="c1",
                                             name="analyze_revenue_decomposition",
                                             arguments={"store_id": "A店",
                                                        "date": ALERT_DATE})]),
            llm_text("A店营收下降主因是客流。"),
        ]
        agent = self._agent(FakeClient(script))
        result = agent.explore("A店为什么下降?")
        self.assertIn("客流", result["final_text"])
        self.assertEqual(result["steps"], 1)
        self.assertEqual(result["denials"], 0)
        self.assertFalse(result["truncated"])
        # 工具确实执行:回传的 tool 结果含真实数据
        tool_msg = [m for m in agent.client.calls[1]["messages"]
                    if m.get("role") == "tool"][0]
        self.assertIn("traffic_contrib", tool_msg["content"])

    def test_permission_denial_adapts(self):
        """越界门店调用被拒绝,拒绝原因进入对话,循环继续并收敛。"""
        script = [
            LlmResponse(content="", finish_reason="tool_calls",
                        tool_calls=[ToolCall(id="c1", name="query_metrics",
                                             arguments={"store_id": "B店",
                                                        "date": ALERT_DATE,
                                                        "metrics": ["traffic"]})]),
            llm_text("无权访问 B店,已停止查询。"),
        ]
        agent = self._agent(FakeClient(script), scope=("A店",))
        result = agent.explore("查 B店")
        self.assertEqual(result["denials"], 1)
        self.assertEqual(result["steps"], 1)
        self.assertIn("无权访问", result["final_text"])
        denied = [m for m in agent.client.calls[1]["messages"]
                  if m.get("role") == "tool"][0]
        self.assertIn("Denied", denied["content"])

    def test_step_limit_forces_convergence(self):
        """步数上限:恰好 max_steps 轮工具 + 1 次强制收敛调用。"""
        script = [
            LlmResponse(content="", finish_reason="tool_calls",
                        tool_calls=[ToolCall(id=f"c{i}", name="query_metrics",
                                             arguments={"store_id": "A店",
                                                        "date": ALERT_DATE,
                                                        "metrics": ["traffic"]})])
            for i in range(2)
        ] + [llm_text("基于已有数据总结。")]
        agent = self._agent(FakeClient(script))
        result = agent.explore("查 A店", max_steps=2)
        self.assertEqual(result["steps"], 2)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["final_text"], "基于已有数据总结。")
        self.assertEqual(len(agent.client.calls), 3)   # 2 轮工具 + 1 次收敛

    def test_consecutive_denials_break(self):
        """连续拒绝熔断:达到上限后强制收敛,不再调用工具。"""
        script = [
            LlmResponse(content="", finish_reason="tool_calls",
                        tool_calls=[ToolCall(id=f"c{i}", name="query_metrics",
                                             arguments={"store_id": "B店",
                                                        "date": ALERT_DATE,
                                                        "metrics": ["traffic"]})])
            for i in range(2)
        ] + [llm_text("访问受限,无法查询。")]
        agent = self._agent(FakeClient(script), scope=("A店",))
        result = agent.explore("查 B店")
        self.assertEqual(result["denials"], 2)
        self.assertEqual(result["final_text"], "访问受限,无法查询。")


# ── 状态机与计划 ──


class TestStateMachine(unittest.TestCase):
    def test_transitions_and_persistence(self):
        sm = DiagnosisStateMachine("run_x")
        self.assertEqual(sm.transition("accept"), "ACCEPTED")
        with self.assertRaises(ValueError):
            sm.transition("approve")          # ACCEPTED 不接受 approve
        sm.transition("plan_ready")
        sm.transition("diagnose_start")
        sm.transition("diagnose_ok")
        sm.transition("reason_ok")
        sm.transition("approve")
        self.assertEqual(sm.state, "DONE")

    def test_fail_retry_and_round_trip(self):
        sm = DiagnosisStateMachine("run_y")
        sm.transition("accept")
        self.assertEqual(sm.fail("boom"), "FAILED")
        with tempfile.TemporaryDirectory() as tmp:
            path = sm.persist(Path(tmp))
            loaded = DiagnosisStateMachine.load(path)
            self.assertEqual(loaded.state, "FAILED")
            self.assertEqual(loaded.error, "boom")
        self.assertEqual(loaded.transition("retry"), "ACCEPTED")

    def test_unknown_schema_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text(json.dumps({"run_id": "x", "state": "NEW",
                                        "schema_version": 99}))
            with self.assertRaises(ValueError):
                DiagnosisStateMachine.load(path)


class TestPlannerChainValidation(unittest.TestCase):
    def test_valid_chain(self):
        planner = Planner()
        chain = planner.load_chain()
        self.assertEqual(planner.validate_chain(chain), [])
        self.assertIsNotNone(planner.build_plan(chain, AnomalyAlert(
            store_id="A店", date=ALERT_DATE)))

    def test_invalid_chain_rejected(self):
        """未知工具/非法 op/非法 when → 校验报错并拒绝构建。"""
        planner = Planner()
        bad_tool = {"schema_version": 1, "steps": [
            {"id": "s1", "title": "t", "dimension": "d", "tool": "nope",
             "args": {}, "when": "always", "rules": []}]}
        self.assertTrue(any("未知工具" in e
                            for e in planner.validate_chain(bad_tool)))
        bad_op = {"schema_version": 1, "steps": [
            {"id": "s1", "title": "t", "dimension": "d",
             "tool": "query_metrics", "args": {},
             "when": "always",
             "rules": [{"metric": "a", "op": "exec", "threshold": 1,
                        "verdict": "anomaly", "summary": "s"}]}]}
        self.assertTrue(any("非法规则操作符" in e
                            for e in planner.validate_chain(bad_op)))
        bad_when = {"schema_version": 1, "steps": [
            {"id": "s1", "title": "t", "dimension": "d",
             "tool": "query_metrics", "args": {}, "when": "maybe",
             "rules": []}]}
        self.assertTrue(any("非法 when" in e
                            for e in planner.validate_chain(bad_when)))


# ── src 基础设施集成:MCP / TaskSystem / CronScheduler / SkillSystem ──


class TestMcpIntegration(unittest.TestCase):
    def test_explore_connects_and_calls_mcp(self):
        """探索模式:connect_mcp 晚绑定 → 下轮工具池并入 → 调用成功。"""
        script = [
            LlmResponse(content="", finish_reason="tool_calls",
                        tool_calls=[ToolCall(id="c1", name="connect_mcp",
                                             arguments={"name": "docs"})]),
            LlmResponse(content="", finish_reason="tool_calls",
                        tool_calls=[ToolCall(id="c2", name="mcp__docs__search",
                                             arguments={"query": "暴雨"})]),
            llm_text("已检索文档。"),
        ]
        mcp = McpSystem()
        agent = AnomalyAgent.build(client=FakeClient(script), mcp=mcp,
                                   run_dir=Path(tempfile.mkdtemp()))
        result = agent.explore("查资料")
        self.assertEqual(result["denials"], 0)
        self.assertEqual(result["final_text"], "已检索文档。")
        round2 = agent.client.calls[1]["tool_names"]
        self.assertIn("mcp__docs__search", round2)     # 晚绑定生效
        tool_msgs = [m for m in agent.client.calls[2]["messages"]
                     if m.get("role") == "tool"]
        self.assertTrue(any("[docs] Found 3 results" in m["content"]
                            for m in tool_msgs))

    def test_guard_mcp_policy(self):
        """MCP 工具策略:只读放行,deploy/trigger 类拒绝。"""
        guard = ToolGuard(read_tools=["query_metrics"])
        ok, why = guard.can_call("mcp__docs__search", {"query": "x"})
        self.assertTrue(ok)
        ok, why = guard.can_call("mcp__deploy__trigger", {"service": "x"})
        self.assertFalse(ok)
        self.assertIn("破坏性", why)

    def test_explore_denied_mcp_tool(self):
        """探索模式中调用破坏性 MCP 工具被拦截,拒绝原因回传让 LLM 自适应。"""
        script = [
            LlmResponse(content="", finish_reason="tool_calls",
                        tool_calls=[ToolCall(id="c1", name="mcp__deploy__trigger",
                                             arguments={"service": "x"})]),
            llm_text("部署被拒绝。"),
        ]
        agent = AnomalyAgent.build(client=FakeClient(script),
                                   run_dir=Path(tempfile.mkdtemp()))
        result = agent.explore("部署")
        self.assertEqual(result["denials"], 1)
        self.assertEqual(result["final_text"], "部署被拒绝。")


class TestTaskSystemMirror(unittest.TestCase):
    def test_diagnosis_steps_mirrored_to_task_board(self):
        """注入 src.TaskSystem:诊断链每步在任务看板创建/认领/完成。"""
        tasks = TaskSystem(Path(tempfile.mkdtemp()))
        engine = DiagnosticEngine(CateringTools(),
                                  run_dir=Path(tempfile.mkdtemp()),
                                  tasks=tasks)
        chain = Planner().load_chain()
        engine.run(chain, AnomalyAlert(store_id="A店", date=ALERT_DATE,
                                       source="detector"))
        board = tasks.list_tasks()
        self.assertEqual(len(board), len(chain["steps"]))
        self.assertTrue(all(t.status == "completed" for t in board))
        self.assertTrue(all(t.owner == "diagnostic-engine" for t in board))
        self.assertTrue(all("诊断步骤" in t.subject for t in board))


class TestCateringScheduler(unittest.TestCase):
    def _scheduler(self):
        tmp = Path(tempfile.mkdtemp())
        agent = AnomalyAgent.build(client=None, run_dir=tmp,
                                   interactive=False)
        scheduler = CateringScheduler(agent,
                                      durable_path=tmp / "catering_cron.json")
        return scheduler, tmp

    def test_schedule_validation(self):
        """cron 校验复用 src.CronScheduler:非法拒绝,合法注册并持久化。"""
        scheduler, tmp = self._scheduler()
        self.assertTrue(scheduler.schedule_scan("not a cron")
                        .startswith("Error"))
        result = scheduler.schedule_scan("30 8 * * *")
        self.assertIn("Scheduled", result)
        self.assertIn("30 8 * * *", scheduler.list_jobs())
        self.assertTrue((tmp / "catering_cron.json").exists())

    def test_process_once_diagnoses_alerts(self):
        """队列触发 → 扫描告警 → 自动诊断两家异常门店并落盘报告。"""
        scheduler, tmp = self._scheduler()
        job = scheduler.cron.schedule_job("0 0 * * *", "scan", recurring=False,
                                          durable=False)
        scheduler.cron.queue.append(job)
        count = scheduler.process_once()
        self.assertEqual(count, 2)
        self.assertEqual(len(list((tmp / "reports").glob("*.json"))), 2)
        self.assertEqual(scheduler.process_once(), 0)   # 无触发时不动


class TestSkillCatalog(unittest.TestCase):
    def test_catering_ops_skill_listed(self):
        """catering-ops 技能已被 src.SkillSystem 扫描(主代理可 load_skill)。"""
        self.assertIn("catering-ops", SKILL_SYSTEM.list_skills())


# ── 评测体系 ──


class TestEvaluator(unittest.TestCase):
    def test_suite_all_pass(self):
        agent, _ = build_agent(client=None, interactive=False)
        summary = Evaluator().run_suite(agent)
        self.assertEqual(summary["passed"], summary["total"])
        self.assertEqual(summary["total"], 3)

    def test_negative_scenario_fails(self):
        """注入错误期望的场景必须被判失败(证明评测框架有效)。"""
        with tempfile.TemporaryDirectory() as tmp:
            scenarios = json.loads(
                CateringConfigs.EVAL_SCENARIOS_PATH.read_text())
            scenarios["scenarios"][0]["expected_main_factor"] = "ticket"
            path = Path(tmp) / "scenarios.json"
            path.write_text(json.dumps(scenarios, ensure_ascii=False))
            agent, _ = build_agent(client=None, interactive=False)
            summary = Evaluator(scenarios_path=path).run_suite(agent)
            self.assertEqual(summary["passed"], summary["total"] - 1)
            failed = next(r for r in summary["scenarios"]
                          if not r["passed"])
            self.assertIn("主因不符", failed["errors"][0])


if __name__ == "__main__":
    unittest.main()
