"""CodeAnalysis 业务包测试:风险评估模型、扫描引擎、部署模块、分析链、双模式与集成。

运行: python -m unittest discover -s tests -v
全部离线:不读 .env、不碰网络;LLM 用 FakeClient 脚本回放,
数据来自 CodeAnalysis/data 样本(fixtures 由 gen_fixtures.py 确定性生成)。
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.CronScheduler import CronJob
from src.LlmClient import LlmResponse, ToolCall
from src.McpSystem import McpSystem
from src.SkillSystem import SKILL_SYSTEM
from src.TaskSystem import TaskSystem

from CodeAnalysis.CodeAnalysisAgent import CodeAnalysisAgent
from CodeAnalysis.Configs import CodeAnalysisConfigs
from CodeAnalysis.DataHub import DataHub, DataSourceError
from CodeAnalysis.Deployer import Deployer
from CodeAnalysis.Evaluator import Evaluator
from CodeAnalysis.Guardrails import (OutputValidator, ToolGuard, eval_rule,
                                     eval_when, format_summary)
from CodeAnalysis.Metrics import (classify_level, clamp_likelihood,
                                  count_critical, determine_main_risk,
                                  overall_assessment, risk_score, sha256_file,
                                  subtotals)
from CodeAnalysis.Models import CodeAsset
from CodeAnalysis.Reasoner import Reasoner
from CodeAnalysis.ResearchReports import ResearchReports
from CodeAnalysis.Scanner import Scanner, ScannerError
from CodeAnalysis.Scheduler import CodeAnalysisScheduler
from CodeAnalysis.Tools import CodeAnalysisTools
from CodeAnalysis.Workflow import (CodeAnalysisStateMachine, Planner,
                                   SecurityAnalysisEngine, STATE_EVENTS)

DATA_DIR = Path(__file__).resolve().parent.parent / "CodeAnalysis" / "data"
TEST_ASSET_DATE = "2026-09-18"

# ── 离线测试基础设施 ──


class FakeClient:
    """按脚本回放 LLM 回复,记录每次请求快照(与 test_catering.py 同款)。"""

    def __init__(self, script: list[LlmResponse]):
        self.script = list(script)
        self.calls = []
        self.fallback_model = None
        self.model = "fake-model"

    def create(self, messages, system=None, tools=None, max_tokens=4096,
               model=None):
        self.calls.append({"messages": json.loads(json.dumps(messages,
                                                             default=str)),
                           "system": system,
                           "tool_names": [t["function"]["name"]
                                          for t in (tools or [])],
                           "max_tokens": max_tokens})
        return self.script.pop(0)


def build_agent(client=None, scope=None, run_dir=None, interactive=False,
                enable_writes=None, mcp=None, tasks=None):
    """构造离线可用的分析 Agent(运行产物落在临时目录)。"""
    run_dir = Path(run_dir) if run_dir else Path(tempfile.mkdtemp())
    return CodeAnalysisAgent.build(client=client, run_dir=run_dir,
                                   scope=scope, interactive=interactive,
                                   enable_writes=enable_writes, mcp=mcp,
                                   tasks=tasks), run_dir


def make_asset(project_id: str, source: str = "manual") -> CodeAsset:
    return CodeAsset(project_id=project_id, name="", language="",
                     version="", ingest_date=TEST_ASSET_DATE, source=source)


def llm_text(text: str) -> LlmResponse:
    return LlmResponse(content=text, finish_reason="stop")


def tool_call_resp(name: str, arguments: dict,
                   call_id: str = "c1") -> LlmResponse:
    return LlmResponse(content="", finish_reason="tool_calls",
                       tool_calls=[ToolCall(id=call_id, name=name,
                                            arguments=arguments)])


def load_expected() -> dict:
    return json.loads((DATA_DIR / "expected_assessments.json")
                      .read_text(encoding="utf-8"))["projects"]


# ── 风险评估模型 ──


class TestRiskScoring(unittest.TestCase):
    """单条风险分 = 严重度权重 × 可能性;可能性夹取与非法输入。"""

    def test_weights_table(self):
        pairs = [("critical", 10.0), ("high", 7.0), ("medium", 4.0),
                 ("low", 1.0)]
        for sev, weight in pairs:
            self.assertEqual(risk_score(sev, 1.0,
                                        CodeAnalysisConfigs.STATIC_WEIGHTS),
                             weight)
            self.assertEqual(risk_score(sev, 0.5,
                                        CodeAnalysisConfigs.STATIC_WEIGHTS),
                             round(weight * 0.5, 4))

    def test_env_and_deploy_scales(self):
        self.assertEqual(risk_score("critical", 1.0,
                                    CodeAnalysisConfigs.ENV_WEIGHTS), 8.0)
        self.assertEqual(risk_score("high", 1.0,
                                    CodeAnalysisConfigs.ENV_WEIGHTS), 5.0)
        self.assertEqual(CodeAnalysisConfigs.DEPLOY_WEIGHTS["integrity"], 15.0)

    def test_likelihood_clamped(self):
        self.assertEqual(clamp_likelihood(5.0), 1.0)
        self.assertEqual(clamp_likelihood(0.01), 0.1)
        self.assertIsNone(clamp_likelihood(None))
        self.assertIsNone(clamp_likelihood("high"))

    def test_unknown_severity_none(self):
        self.assertIsNone(risk_score("nope", 1.0,
                                     CodeAnalysisConfigs.STATIC_WEIGHTS))


class TestSubtotals(unittest.TestCase):
    """三类风险子项求和(手算期望值)。"""

    def test_static_subtotal(self):
        findings = [{"risk_score": 6.3}, {"risk_score": 2.8},
                    {"risk_score": 2.4}]
        sub = subtotals(findings, [], [])
        self.assertEqual(sub["static_vuln"], 11.5)

    def test_env_subtotal(self):
        issues = [{"risk_score": 8.0}, {"risk_score": 5.0},
                  {"risk_score": 4.0}]
        sub = subtotals([], issues, [])
        self.assertEqual(sub["env_config"], 17.0)

    def test_deploy_subtotal(self):
        events = [{"risk_score": 15.0}]
        sub = subtotals([], [], events)
        self.assertEqual(sub["deploy_integrity"], 15.0)


class TestSecurityLevel(unittest.TestCase):
    """等级分档边界: 0→A;<10→B;<25→C;<45→D;≥45→E。"""

    def test_boundaries(self):
        cases = [(0.0, "A"), (9.99, "B"), (10.0, "C"), (24.99, "C"),
                 (25.0, "D"), (44.99, "D"), (45.0, "E"), (200.0, "E")]
        for score, want in cases:
            self.assertEqual(classify_level(score, 0), want,
                             f"score={score} 应落在 {want}")


class TestHardCaps(unittest.TestCase):
    """critical 硬门槛:1 个不优于 C,2 个不优于 D,≥3 直接 E;只降不升。"""

    def test_caps_apply(self):
        self.assertEqual(classify_level(0.0, 1), "C")
        self.assertEqual(classify_level(5.0, 2), "D")
        self.assertEqual(classify_level(5.0, 3), "E")
        self.assertEqual(classify_level(5.0, 9), "E")

    def test_caps_never_improve(self):
        # 基础等级已差于门槛时,cap 不升档
        self.assertEqual(classify_level(45.0, 1), "E")
        self.assertEqual(classify_level(13.0, 1), "C")   # 基础 C,cap C 不变
        self.assertEqual(classify_level(0.0, 0), "A")    # 无 critical 不封顶

    def test_cap_table_complete(self):
        for n in range(10):
            self.assertIn(classify_level(0.0, n), "ABCDE")


class TestMainRisk(unittest.TestCase):
    """主风险 = 三子项最高者;全 0 → none;平局按 static>env>deploy。"""

    def test_four_outcomes(self):
        self.assertEqual(determine_main_risk(
            {"static_vuln": 11.5, "env_config": 17.0,
             "deploy_integrity": 0.0})[0], "env_config")
        self.assertEqual(determine_main_risk(
            {"static_vuln": 12.5, "env_config": 0.5,
             "deploy_integrity": 0.0})[0], "static_vuln")
        self.assertEqual(determine_main_risk(
            {"static_vuln": 7.6, "env_config": 1.9,
             "deploy_integrity": 15.0})[0], "deploy_integrity")

    def test_all_zero_none(self):
        main, score = determine_main_risk(
            {"static_vuln": 0.0, "env_config": 0.0, "deploy_integrity": 0.0})
        self.assertEqual(main, "none")
        self.assertIsNone(score)

    def test_tie_order(self):
        main, _ = determine_main_risk(
            {"static_vuln": 5.0, "env_config": 5.0, "deploy_integrity": 1.0})
        self.assertEqual(main, "static_vuln")

    def test_overall_assessment_critical_count(self):
        assessment = overall_assessment(
            [{"severity": "high", "risk_score": 6.3}],
            [{"severity": "critical", "risk_score": 8.0}],
            [{"severity": "critical", "risk_score": 15.0}])
        self.assertEqual(assessment["critical_count"], 2)
        self.assertEqual(count_critical([{"severity": "critical"}], [], []), 1)


# ── 扫描引擎与规则 ──


class TestRulePrecision(unittest.TestCase):
    """每条规则的 planted/clean 片段对:命中植入样本、不误报净化对照。"""

    def test_all_snippet_pairs(self):
        scanner = Scanner(CodeAnalysisConfigs.RULES_DIR)
        snippets_dir = CodeAnalysisConfigs.SNIPPETS_DIR
        rules = {}
        for lang_file in CodeAnalysisConfigs.RULES_DIR.glob("*.json"):
            payload = json.loads(lang_file.read_text(encoding="utf-8"))
            for rule in payload.get("rules") or []:
                rules[rule["rule_id"]] = rule
        pairs = sorted(snippets_dir.glob("*_planted.txt"))
        self.assertTrue(pairs, "snippets 目录应存在片段对")
        checked = 0
        for planted_path in pairs:
            rule_id = planted_path.name[:-len("_planted.txt")]
            clean_path = snippets_dir / f"{rule_id}_clean.txt"
            self.assertIn(rule_id, rules, f"{rule_id} 缺少对应规则")
            self.assertTrue(clean_path.exists(), f"{rule_id} 缺少 clean 片段")
            planted_hits = scanner.scan_file(planted_path, [rules[rule_id]],
                                             rel_path=planted_path.name)
            clean_hits = scanner.scan_file(clean_path, [rules[rule_id]],
                                           rel_path=clean_path.name)
            self.assertEqual(len(planted_hits), 1,
                             f"{rule_id} planted 应命中 1 次")
            self.assertEqual(len(clean_hits), 0, f"{rule_id} clean 误报")
            checked += 1
        self.assertEqual(checked, len(rules), "片段对必须覆盖全部规则")


class TestScannerPerLanguage(unittest.TestCase):
    """4 个项目扫描结果 == 期望漏洞清单(无多无漏)。"""

    def setUp(self):
        self.tools = CodeAnalysisTools(enable_writes=False)
        self.expected = load_expected()

    def test_scan_matches_expected(self):
        for pid, exp in self.expected.items():
            result = self.tools.execute("scan_static", {"project_id": pid})
            got_ids = sorted(f["rule_id"] for f in result.get("findings") or [])
            self.assertEqual(got_ids,
                             sorted(exp["expected_vuln_ids"]), pid)
            self.assertEqual(result["missing_fields"], [], pid)

    def test_unknown_language_rules(self):
        scanner = Scanner(CodeAnalysisConfigs.RULES_DIR)
        with self.assertRaises(ScannerError):
            scanner.load_rules("ruby")

    def test_rules_library(self):
        result = self.tools.execute("list_scan_rules", {})
        self.assertEqual(result["rule_count"], 16)
        langs = self.tools.execute("list_scan_rules",
                                   {"language": "python"})
        self.assertEqual(langs["rule_count"], 4)


class TestNoiseFloorCleanAsset(unittest.TestCase):
    """对照组(PJ-D-004)零命中:三子项全 0 → 等级 A、主风险 none。"""

    def test_clean_asset_zero_findings(self):
        tools = CodeAnalysisTools(enable_writes=False)
        scan = tools.execute("scan_static", {"project_id": "PJ-D-004"})
        self.assertEqual(scan["total"], 0)
        env = tools.execute("review_env", {"project_id": "PJ-D-004"})
        self.assertEqual(env["issue_count"], 0)
        assess = tools.execute("assess_security", {"project_id": "PJ-D-004"})
        self.assertEqual(assess["level"], "A")
        self.assertEqual(assess["score"], 0.0)


# ── 数据源与环境审查 ──


class TestDataHub(unittest.TestCase):
    """五类数据源健康与未知项目报错。"""

    def test_asset_and_env_and_registry(self):
        hub = DataHub(DATA_DIR)
        asset = hub.asset("PJ-A-001")
        self.assertEqual(asset["module_count"], 1)
        self.assertEqual(asset["language"], "Java")
        self.assertEqual(len(hub.registry_entries()), 5)
        self.assertIsNone(hub.asset("PJ-NOPE"))

    def test_unknown_project_raises_with_hint(self):
        hub = DataHub(DATA_DIR)
        with self.assertRaises(DataSourceError) as ctx:
            hub.query("asset_db", "PJ-NOPE")
        self.assertIn("可用项目", str(ctx.exception))

    def test_search_cases(self):
        hub = DataHub(DATA_DIR)
        result = hub.search_cases("PJ-C-003",
                                  "dimension:deploy deploy_integrity")
        self.assertGreaterEqual(result["match_count"], 1)
        self.assertEqual(result["matches"][0]["case_id"],
                         "CASE-2026-0812-PJ-C-003")


class TestEnvReview(unittest.TestCase):
    """运行环境审查:风险分与期望一致。"""

    def test_env_issues(self):
        hub = DataHub(DATA_DIR)
        a = hub.env_review("PJ-A-001")
        self.assertEqual(a["issue_count"], 3)
        self.assertEqual(sum(i["risk_score"] for i in a["issues"]), 17.0)
        self.assertEqual(a["max_severity"], "critical")
        c = hub.env_review("PJ-C-003")
        self.assertEqual(round(sum(i["risk_score"] for i in c["issues"]), 2),
                         1.9)
        d = hub.env_review("PJ-D-004")
        self.assertEqual(d["issue_count"], 0)


# ── 部署模块 ──


def _deployer_and_registry():
    hub = DataHub(DATA_DIR)
    deployer = Deployer(hub.cloud_source, hub.cloud_source.registry_map(),
                        assets_dir=DATA_DIR / "assets", id_seq=100)
    return hub, deployer


class TestDeployerGate(unittest.TestCase):
    """高危门禁:等级 E 拒绝部署并说明原因;等级 D 放行。"""

    def test_level_e_blocked(self):
        hub, deployer = _deployer_and_registry()
        asset = hub.asset("PJ-A-001")
        record = deployer.deploy(asset, {"security_level": "E"})
        self.assertEqual(record["status"], "blocked")
        self.assertIn("安全门禁", record["blocked_reason"])
        self.assertIsNone(record["deploy_id"])
        self.assertEqual(record["risk_events"], [])

    def test_level_d_passes_gate(self):
        hub, deployer = _deployer_and_registry()
        asset = hub.asset("PJ-A-001")
        record = deployer.deploy(asset, {"security_level": "D"})
        self.assertEqual(record["status"], "deployed")
        self.assertTrue(record["deploy_id"].startswith("DEP-"))


class TestDeployerIntegrity(unittest.TestCase):
    """篡改制品(PJ-C-003)被完整性校验拦截。"""

    def test_tampered_blocked(self):
        hub, deployer = _deployer_and_registry()
        asset = hub.asset("PJ-C-003")
        record = deployer.deploy(asset, {"security_level": "B"})
        self.assertEqual(record["status"], "blocked")
        self.assertIsNone(record["deploy_id"])
        self.assertIn("integrity", record["blocked_reason"])
        self.assertEqual(len(record["risk_events"]), 1)
        event = record["risk_events"][0]
        self.assertEqual(event["check"], "integrity")
        self.assertEqual(event["weight"], 15.0)
        self.assertEqual(record["verification"]["integrity"]["passed"], False)
        # 未执行的验证项保持 None(不计入失败)
        self.assertIsNone(record["verification"]["authenticity"])
        self.assertIsNone(record["verification"]["availability"])


class TestDeployPartial(unittest.TestCase):
    """部分部署(工程技术一体化系统):只部署 required_modules 且验证全过。"""

    def test_partial_deploy(self):
        hub, deployer = _deployer_and_registry()
        asset = hub.asset("PJ-B-002")
        record = deployer.deploy(asset, {"security_level": "C"})
        self.assertEqual(record["status"], "deployed")
        self.assertTrue(record["partial"])
        self.assertEqual(record["modules_deployed"], 2)
        self.assertEqual(sorted(record["requested_modules"]),
                         ["api", "web"])
        self.assertEqual(record["risk_events"], [])
        self.assertTrue(record["verification"]["integrity"]["passed"])
        self.assertTrue(record["verification"]["authenticity"]["passed"])
        self.assertTrue(record["verification"]["availability"]["passed"])

    def test_verify_record_independent(self):
        hub, deployer = _deployer_and_registry()
        asset = hub.asset("PJ-B-002")
        record = deployer.deploy(asset, {"security_level": "C"})
        result = deployer.verify(asset, record["deploy_id"])
        self.assertTrue(result["all_passed"])
        self.assertEqual(result["analysis"]["modules_verified"], 2)


class TestSha256Checksum(unittest.TestCase):
    """校验和:一致项目匹配、篡改项目持续不匹配。"""

    def test_checksums(self):
        hub = DataHub(DATA_DIR)
        for entry in hub.registry_entries():
            path = DATA_DIR / "assets" / entry["asset_id"] / entry["artifact"]
            actual = sha256_file(path)
            if entry["asset_id"] == "PJ-C-003":
                self.assertNotEqual(actual, entry["sha256"],
                                    "篡改制品应持续不匹配")
            else:
                self.assertEqual(actual, entry["sha256"])


# ── 护栏 ──


class TestRuleOps(unittest.TestCase):
    """规则运算:in 白名单、summary 模板、when 分支。"""

    def test_in_op_and_summary(self):
        hit = eval_rule({"metric": "level", "op": "in",
                         "threshold": ["C", "D", "E"], "verdict": "anomaly",
                         "summary": "安全等级 {value}"},
                        {"level": "D"})
        self.assertIsNotNone(hit)
        self.assertEqual(hit["summary"], "安全等级 D")
        miss = eval_rule({"metric": "level", "op": "in",
                          "threshold": ["C", "D", "E"], "verdict": "anomaly",
                          "summary": ""}, {"level": "B"})
        self.assertIsNone(miss)

    def test_format_summary_paths(self):
        text = format_summary("发现 {value} 个问题,占比 {analysis.share:.1%}",
                              3, {"analysis": {"share": 0.3333}})
        self.assertEqual(text, "发现 3 个问题,占比 33.3%")

    def test_eval_when_stage_flags(self):
        self.assertTrue(eval_when("deploy_done", False,
                                  {"deploy_done": True}))
        self.assertFalse(eval_when("deploy_done", False,
                                   {"deploy_done": False}))
        self.assertFalse(eval_when("deploy_done", False, None))
        self.assertTrue(eval_when("level_is:E", False, {"level": "E"}))
        self.assertFalse(eval_when("level_is:E", False, {"level": "C"}))
        # 旧 3 参路径兼容
        self.assertTrue(eval_when("any_anomaly", True))
        self.assertFalse(eval_when("any_anomaly", False))


class TestChainValidation(unittest.TestCase):
    """分析链校验:未知工具/非法 op/非法 when/in 非 list 拒绝启动。"""

    def test_real_chain_valid(self):
        planner = Planner()
        self.assertEqual(planner.validate_chain(planner.load_chain()), [])

    def test_rejections(self):
        planner = Planner()
        base = planner.load_chain()
        # 未知工具
        bad = json.loads(json.dumps(base))
        bad["steps"][0]["tool"] = "hack_the_planet"
        self.assertTrue(planner.validate_chain(bad))
        # 非法 op
        bad = json.loads(json.dumps(base))
        bad["steps"][1]["rules"][0]["op"] = "exec"
        self.assertTrue(planner.validate_chain(bad))
        # 非法 when
        bad = json.loads(json.dumps(base))
        bad["steps"][0]["when"] = "maybe"
        self.assertTrue(planner.validate_chain(bad))
        # in 的 threshold 非数组
        bad = json.loads(json.dumps(base))
        bad["steps"][2]["rules"][0] = {"metric": "level", "op": "in",
                                       "threshold": "C", "verdict": "anomaly",
                                       "summary": "x"}
        self.assertTrue(planner.validate_chain(bad))
        # deploy_done/level_is 合法
        bad = json.loads(json.dumps(base))
        bad["steps"][5]["when"] = "level_is:D"
        self.assertEqual(planner.validate_chain(bad), [])

    def test_build_plan_rejects(self):
        planner = Planner()
        bad = planner.load_chain()
        bad["steps"][0]["tool"] = "hack"
        with self.assertRaises(ValueError):
            planner.build_plan(bad, make_asset("PJ-A-001"))


class TestOutputValidator(unittest.TestCase):
    """报告结构化校验:真实报告干净,七类篡改各报错。"""

    def test_real_report_valid(self):
        agent, _ = build_agent(client=None)
        report = agent.run_analysis(make_asset("PJ-A-001"))
        self.assertEqual(OutputValidator.validate_report(report.to_dict()), [])

    def test_mutations_detected(self):
        agent, _ = build_agent(client=None)
        report = agent.run_analysis(make_asset("PJ-A-001"))
        mutations = [
            ({k: v for k, v in report.to_dict().items() if k != "report_id"}),
            ({**report.to_dict(), "main_risk": "banana"}),
            ({**report.to_dict(), "security_level": "Z"}),
            ({**report.to_dict(), "schema_version": 99}),
            ({**report.to_dict(), "meta": {**report.meta,
                                           "template_fallback": "yes"}}),
        ]
        dangling = report.to_dict()
        dangling["findings"][0]["evidence_refs"] = ["ghost_step"]
        mutations.append(dangling)
        for i, mutated in enumerate(mutations):
            self.assertTrue(OutputValidator.validate_report(mutated),
                            f"变异 {i} 应被检出")


class TestToolGuard(unittest.TestCase):
    """工具权限:越界项目/写工具/白名单/MCP 危险词。"""

    def _guard(self, allow_writes=False):
        tools = CodeAnalysisTools(enable_writes=False)
        read_tools = [t["function"]["name"] for t in tools.tool_defs()]
        return ToolGuard(read_tools=read_tools + ["connect_mcp"],
                         scope=("PJ-A-001",), allow_writes=allow_writes)

    def test_scope_denial(self):
        ok, why = self._guard().can_call("get_asset",
                                         {"project_id": "PJ-B-002"})
        self.assertFalse(ok)
        self.assertIn("超出授权范围", why)
        ok, _ = self._guard().can_call("get_asset", {"project_id": "PJ-A-001"})
        self.assertTrue(ok)

    def test_write_tool_denial(self):
        ok, why = self._guard().can_call("deploy_asset",
                                         {"project_id": "PJ-A-001"})
        self.assertFalse(ok)
        self.assertIn("默认禁用", why)
        ok, _ = self._guard(allow_writes=True).can_call(
            "deploy_asset", {"project_id": "PJ-A-001"})
        self.assertTrue(ok)

    def test_whitelist_denial(self):
        ok, why = self._guard().can_call("bash", {"command": "ls"})
        self.assertFalse(ok)
        self.assertIn("不在白名单内", why)

    def test_mcp_dangerous_words(self):
        ok, why = self._guard().can_call("mcp__deploy__trigger", {})
        self.assertFalse(ok)
        self.assertIn("破坏性", why)
        ok, _ = self._guard(allow_writes=True).can_call("mcp__deploy__trigger",
                                                        {})
        self.assertTrue(ok)
        ok, _ = self._guard().can_call("mcp__docs__search", {})
        self.assertTrue(ok)


class TestToolDefinitions(unittest.TestCase):
    """工具双接口:defs/handlers 一致性,写工具按开关暴露。"""

    def test_defs_vs_handlers(self):
        tools = CodeAnalysisTools(enable_writes=False)
        handlers = set(tools.handlers())
        defs = {t["function"]["name"] for t in tools.tool_defs()}
        self.assertEqual(defs, handlers - {"deploy_asset", "register_asset"})

    def test_writes_exposed_when_enabled(self):
        tools = CodeAnalysisTools(enable_writes=True)
        defs = {t["function"]["name"] for t in tools.tool_defs()}
        self.assertEqual(defs, set(tools.handlers()))

    def test_register_asset_gated(self):
        tools = CodeAnalysisTools(enable_writes=False)
        with self.assertRaises(ValueError):
            tools.run_register_asset("PJ-X", "测试", "java", "1.0.0")


# ── 工作流与状态机 ──


class TestWorkflowEndToEnd(unittest.TestCase):
    """4 场景端到端:(main_risk, level) 期望对、部署状态、报告落盘、校验干净。"""

    def test_all_scenarios(self):
        agent, run_dir = build_agent(client=None)
        expected = load_expected()
        scenarios = json.loads(
            (DATA_DIR / "eval_scenarios.json").read_text(encoding="utf-8"))
        for scenario in scenarios["scenarios"]:
            pid = scenario["project_id"]
            report = agent.run_analysis(make_asset(pid, source="detector"))
            self.assertEqual(report.main_risk,
                             scenario["expected_main_risk"], pid)
            self.assertEqual(report.security_level,
                             scenario["expected_level"], pid)
            self.assertAlmostEqual(report.risk_score,
                                   expected[pid]["risk_score"], places=4)
            self.assertEqual(OutputValidator.validate_report(
                report.to_dict()), [], pid)
            # 报告与状态落盘
            self.assertTrue(
                (run_dir / "reports" / f"{report.report_id}.json").exists(),
                pid)
            self.assertTrue((run_dir / f"{report.report_id}.json").exists(),
                            pid)
            # 部署状态与分支排除
            deploy = next(e.data for e in report.evidence
                          if e.step_id == "deploy")
            self.assertEqual(deploy.get("status"),
                             scenario["expected_deploy_status"], pid)
            for step_id in scenario["expected_excluded_steps"]:
                self.assertIn(step_id,
                              report.completeness["excluded_steps"], pid)

    def test_deploy_done_semantics(self):
        agent, _ = build_agent(client=None)
        a = agent.run_analysis(make_asset("PJ-A-001"))
        self.assertIn("verify", [e.step_id for e in a.evidence])
        verify = next(e.data for e in a.evidence if e.step_id == "verify")
        self.assertTrue(verify["all_passed"])
        c = agent.run_analysis(make_asset("PJ-C-003"))
        self.assertIn("verify", c.completeness["excluded_steps"])
        self.assertNotIn("verify", [e.step_id for e in c.evidence])


class TestStateMachine(unittest.TestCase):
    """状态机:迁移表强制、持久化往返、schema_version 校验。"""

    def test_transitions_and_persist(self):
        machine = CodeAnalysisStateMachine("run_x")
        self.assertEqual(machine.state, "NEW")
        with self.assertRaises(ValueError):
            machine.transition("approve")
        machine.transition("accept")
        machine.transition("plan_ready")
        machine.transition("scan_start")
        machine.transition("scan_ok")
        machine.transition("reason_ok")
        machine.transition("rework")
        self.assertEqual(machine.state, "REASONING")
        machine.transition("reason_ok")
        machine.transition("approve")
        self.assertEqual(machine.state, "DONE")

    def test_fail_and_retry(self):
        machine = CodeAnalysisStateMachine("run_y")
        machine.transition("accept")
        machine.fail("boom")
        self.assertEqual(machine.state, "FAILED")
        self.assertEqual(machine.error, "boom")
        machine.transition("retry")
        self.assertEqual(machine.state, "ACCEPTED")

    def test_persist_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            machine = CodeAnalysisStateMachine("run_z")
            machine.transition("accept")
            path = machine.persist(Path(tmp))
            loaded = CodeAnalysisStateMachine.load(path)
            self.assertEqual(loaded.to_dict(), machine.to_dict())

    def test_schema_version_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run_w.json"
            path.write_text(json.dumps({"run_id": "run_w", "state": "NEW",
                                        "history": [], "error": None,
                                        "schema_version": 99}))
            with self.assertRaises(ValueError):
                CodeAnalysisStateMachine.load(path)

    def test_event_table_consistent(self):
        for state, events in STATE_EVENTS.items():
            for event in events:
                self.assertIn(event, ("accept", "fail", "plan_ready",
                                      "scan_start", "scan_ok", "scan_fail",
                                      "reason_ok", "reason_fail", "approve",
                                      "rework", "retry"))


# ── 推理与降级 ──


class TestReasoner(unittest.TestCase):
    """LLM 归纳:成功/纠错重试/双垃圾模板降级(降级输出与无 client 路径一致)。"""

    def _args(self):
        agent, _ = build_agent(client=None)
        report = agent.run_analysis(make_asset("PJ-A-001"))
        return (report.asset, report.findings, report.matched_cases,
                report.main_risk, report.main_risk_score, report.risk_score,
                report.security_level)

    def test_llm_success(self):
        asset, findings, matched, main, score, total, level = self._args()
        refs = [f.step_id for f in findings]
        client = FakeClient([llm_text(json.dumps({
            "conclusions": "存在环境配置风险。",
            "recommendations": [{"action": "关闭 debug_mode",
                                 "reason": "生产环境调试模式开启",
                                 "evidence_refs": refs[:1]}]},
            ensure_ascii=False))])
        reason = Reasoner(client).generate(asset, findings, matched, main,
                                           score, total, level)
        self.assertTrue(reason["meta"]["llm_used"])
        self.assertFalse(reason["meta"]["template_fallback"])
        self.assertEqual(reason["meta"]["attempts"], 1)
        self.assertEqual(reason["recommendations"][0]["source"], "llm")

    def test_garbage_then_valid(self):
        asset, findings, matched, main, score, total, level = self._args()
        refs = [f.step_id for f in findings]
        client = FakeClient([
            llm_text("抱歉,我无法输出 JSON。"),
            llm_text(json.dumps({"conclusions": "结论",
                                 "recommendations": [
                                     {"action": "整改",
                                      "reason": "依据",
                                      "evidence_refs": refs[:1]}]},
                                ensure_ascii=False)),
        ])
        reason = Reasoner(client).generate(asset, findings, matched, main,
                                           score, total, level)
        self.assertTrue(reason["meta"]["llm_used"])
        self.assertEqual(reason["meta"]["attempts"], 2)

    def test_double_garbage_falls_back(self):
        asset, findings, matched, main, score, total, level = self._args()
        client = FakeClient([llm_text("垃圾输出 A"), llm_text("垃圾输出 B")])
        got = Reasoner(client).generate(asset, findings, matched, main,
                                        score, total, level)
        want = Reasoner(None).generate(asset, findings, matched, main,
                                       score, total, level)
        self.assertFalse(got["meta"]["llm_used"])
        self.assertTrue(got["meta"]["template_fallback"])
        self.assertEqual(got["conclusions"], want["conclusions"])
        self.assertEqual(got["recommendations"], want["recommendations"])

    def test_dangling_refs_fall_back(self):
        asset, findings, matched, main, score, total, level = self._args()
        client = FakeClient([llm_text(json.dumps({
            "conclusions": "结论",
            "recommendations": [{"action": "整改", "reason": "依据",
                                 "evidence_refs": ["ghost"]}]},
            ensure_ascii=False))])
        reason = Reasoner(client).generate(asset, findings, matched, main,
                                           score, total, level)
        self.assertTrue(reason["meta"]["template_fallback"])


# ── 编排与集成 ──


class TestExploreMode(unittest.TestCase):
    """探索模式:工具调用执行、越权拒绝、步数上限强制收敛。"""

    def test_tool_call_and_denial(self):
        client = FakeClient([
            tool_call_resp("inventory_assets", {}, call_id="t1"),
            tool_call_resp("get_asset", {"project_id": "PJ-B-002"},
                           call_id="t2"),
            llm_text("结论:仅能分析授权项目。"),
        ])
        agent, _ = build_agent(client=client, scope=("PJ-A-001",),
                               enable_writes=False)
        result = agent.explore("分析资产", max_steps=5)
        self.assertEqual(result["final_text"], "结论:仅能分析授权项目。")
        self.assertEqual(result["denials"], 1)
        self.assertEqual(result["steps"], 2)
        self.assertFalse(result["truncated"])
        # 拒绝消息追加在分发之后 → 出现在下一轮 create 的快照末尾
        tool_msg = client.calls[2]["messages"][-1]["content"]
        self.assertIn("Denied", tool_msg)

    def test_step_cap_converges(self):
        client = FakeClient([
            tool_call_resp("inventory_assets", {}, call_id="t1"),
            tool_call_resp("inventory_assets", {}, call_id="t2"),
            llm_text("最终回答。"),
        ])
        agent, _ = build_agent(client=client)
        result = agent.explore("分析资产", max_steps=2)
        self.assertEqual(result["steps"], 2)
        self.assertTrue(result["truncated"])
        self.assertEqual(len(client.calls), 3)   # 2 轮工具 + 1 次收敛

    def test_explore_requires_client(self):
        agent, _ = build_agent(client=None)
        result = agent.explore("分析资产")
        self.assertIn("error", result)


class TestMcpIntegration(unittest.TestCase):
    """MCP 晚绑定:connect 后工具并入下一轮;deploy 类工具仅写模式放行。"""

    def test_late_binding_and_gate(self):
        mcp = McpSystem()
        client = FakeClient([
            tool_call_resp("connect_mcp", {"name": "deploy"}, call_id="t1"),
            tool_call_resp("mcp__deploy__trigger",
                           {"service": "PJ-A-001"}, call_id="t2"),
            llm_text("已连接部署服务。"),
        ])
        agent, _ = build_agent(client=client, mcp=mcp, enable_writes=True)
        result = agent.explore("部署", max_steps=5)
        self.assertEqual(result["denials"], 0)
        self.assertIn("mcp__deploy__trigger", client.calls[1]["tool_names"])

    def test_dangerous_mcp_denied_without_writes(self):
        mcp = McpSystem()
        client = FakeClient([
            tool_call_resp("connect_mcp", {"name": "deploy"}, call_id="t1"),
            tool_call_resp("mcp__deploy__trigger",
                           {"service": "PJ-A-001"}, call_id="t2"),
            llm_text("无法部署。"),
        ])
        agent, _ = build_agent(client=client, mcp=mcp, enable_writes=False)
        result = agent.explore("部署", max_steps=5)
        self.assertEqual(result["denials"], 1)
        tool_msg = client.calls[2]["messages"][-1]["content"]
        self.assertIn("Denied", tool_msg)


class TestTaskSystemMirror(unittest.TestCase):
    """分析步骤镜像到 src.TaskSystem 任务看板。"""

    def test_tasks_mirrored(self):
        with tempfile.TemporaryDirectory() as tmp:
            tasks = TaskSystem(tasks_dir=Path(tmp))
            agent, _ = build_agent(client=None, tasks=tasks)
            chain = Planner().load_chain()
            agent.run_analysis(make_asset("PJ-A-001"))
            board = tasks.list_tasks()
            self.assertEqual(len(board), len(chain["steps"]))
            for task in board:
                self.assertEqual(task.status, "completed")
                self.assertEqual(task.owner, "security-analysis-engine")
                self.assertIn("安全分析步骤", task.subject)


class TestScheduler(unittest.TestCase):
    """定时自动扫描:注册校验 + 触发队列消费后跑全量资产。"""

    def test_schedule_and_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent, run_dir = build_agent(client=None)
            scheduler = CodeAnalysisScheduler(
                agent, durable_path=Path(tmp) / "scheduled.json")
            self.assertTrue(scheduler.schedule_scan("bad cron")
                            .startswith("Error"))
            result = scheduler.schedule_scan("*/5 * * * *")
            self.assertTrue(result.startswith("Scheduled"))
            self.assertTrue((Path(tmp) / "scheduled.json").exists())
            self.assertEqual(scheduler.process_once(), 0)
            scheduler.cron.queue.append(
                CronJob(id="j1", cron="* * * * *", prompt="自动扫描",
                        recurring=True, durable=False))
            count = scheduler.process_once()
            self.assertEqual(count, 4)   # 4 个入库资产全量分析
            self.assertEqual(scheduler.process_once(), 0)
            reports = list((run_dir / "reports").glob("*.json"))
            self.assertEqual(len(reports), 4)


class TestSkillCatalog(unittest.TestCase):
    """技能目录收录 code-analysis-ops。"""

    def test_skill_listed(self):
        self.assertIn("code-analysis-ops", SKILL_SYSTEM.list_skills())


# ── 评测与交付物 ──


class TestEvaluator(unittest.TestCase):
    """离线评测 4/4;篡改期望值 → 恰好 1 个场景失败。"""

    def test_suite_all_pass(self):
        agent, _ = build_agent(client=None)
        summary = Evaluator().run_suite(agent)
        self.assertEqual(summary["total"], 4)
        self.assertEqual(summary["passed"], 4)

    def test_tampered_expectation_fails_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            scenarios = json.loads(
                (DATA_DIR / "eval_scenarios.json").read_text(encoding="utf-8"))
            scenarios["scenarios"][0]["expected_main_risk"] = "static_vuln"
            path = Path(tmp) / "eval_scenarios.json"
            path.write_text(json.dumps(scenarios, ensure_ascii=False))
            agent, _ = build_agent(client=None)
            summary = Evaluator(scenarios_path=path).run_suite(agent)
            self.assertEqual(summary["passed"], 3)
            self.assertTrue(any("主风险不符" in e
                                for r in summary["scenarios"]
                                for e in r["errors"]))


class TestResearchReports(unittest.TestCase):
    """3 份交付物从真实配置渲染,关键章节与统计正确。"""

    def test_generate_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = ResearchReports(out_dir=Path(tmp)).generate("all")
            self.assertEqual(len(paths), 3)
            static = (Path(tmp) / "静态代码自评估技术研究报告.md") \
                .read_text(encoding="utf-8")
            scan = (Path(tmp) / "自动化代码安全扫描技术研究报告.md") \
                .read_text(encoding="utf-8")
            deploy = (Path(tmp) / "自动化部署技术规范.md") \
                .read_text(encoding="utf-8")
            self.assertIn("ISO/IEC 27001", static)
            self.assertIn("## 4 安全等级定义", static)
            self.assertIn("env_config", static)          # 验证数据表
            self.assertIn("## 3 规则库统计", scan)
            self.assertIn("| Java | 3", scan)
            self.assertIn("| C# | 4", scan)
            self.assertIn("| PHP | 5", scan)
            self.assertIn("| Python | 4", scan)
            self.assertIn("## 2 部署流程", deploy)
            self.assertIn("PJ-A-001", deploy)            # 覆盖系统清单
            self.assertIn("PJ-B-002", deploy)
            self.assertIn("PJ-C-003", deploy)
            self.assertIn("完整性", deploy)
            self.assertIn("真实性", deploy)
            self.assertIn("可用性", deploy)


class TestFixturesIdempotent(unittest.TestCase):
    """fixtures 生成幂等:重复生成注册表哈希不变、篡改不匹配持续。"""

    def test_regenerate_identical(self):
        import CodeAnalysis.gen_fixtures as gen
        registry_path = DATA_DIR / "cloud" / "registry.json"
        before = registry_path.read_text(encoding="utf-8")
        gen.generate()
        after = registry_path.read_text(encoding="utf-8")
        self.assertEqual(before, after)


# ── 工具执行入口 ──


class TestToolsExecute(unittest.TestCase):
    """统一执行入口:异常归一化为 error/missing_fields,不抛出。"""

    def test_error_normalized(self):
        tools = CodeAnalysisTools(enable_writes=False)
        result = tools.execute("get_asset", {"project_id": "PJ-NOPE"})
        self.assertIn("error", result)
        self.assertTrue(result["missing_fields"])
        result = tools.execute("get_asset", {"project_id": "PJ-A-001"})
        self.assertEqual(result["module_count"], 1)
        result = tools.execute("verify_deployment",
                               {"project_id": "PJ-A-001",
                                "deploy_id": "DEP-NOPE"})
        self.assertFalse(result["all_passed"])


if __name__ == "__main__":
    unittest.main()
