"""小模型路由(混合路由)单元测试:全部离线,不读 .env、不碰网络。

═══════════ 功能标识:小模型路由(混合路由)═══════════
src.ModelRouter 决定每轮 LLM 调用走"本地小模型"还是"DeepSeek API",
各调用方(AgentLoop / SubagentSystem / Catering.AnomalyAgent)接入。
本文件覆盖:
- 路由决策规则:任务指纹 AND 规则、catering 概念问答/业务分析分流、
  静态调用点、模式开关、健康检查
- 升级阶梯:解析失败/空回复/工具报错/同参数空转/length → 整 turn 升级 API,
  升级后的历史不携带失败消息
- Phase 4 只读工具集:本地调用只见 read_file/glob 等,无 bash/写操作/MCP
- Phase 3 轨迹收集:四种 outcome 分型、文件名去重、默认关闭
══════════════════════════════════════════════════

运行: python -m unittest discover -s tests -v
"""
import json
import tempfile
import unittest
from pathlib import Path

from Catering.AnomalyAgent import AnomalyAgent
from src.AgentLoop import AgentLoop
from src.Configs import Configs
from src.LlmClient import LlmResponse, ToolCall, user_message
from src.ModelRouter import ModelRouter
from src.RecoverySystem import RecoveryState
from src.ToolRegistry import ToolRegistry
from src.TrajectoryLogger import TrajectoryLogger

CATERING_DATA_DIR = Path(__file__).resolve().parent.parent / "Catering" / "data"


class FakeClient:
    """按脚本回放 LLM 回复,记录请求快照(与 test_agent_loop.py 同款)。"""

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
            "model": model,
        })
        return self.script.pop(0)


def healthy_router(api: FakeClient, local: FakeClient,
                   mode: str = "auto") -> ModelRouter:
    """构造强制本地可用的路由器(离线测试跳过健康探测)。"""
    router = ModelRouter(api, local, mode=mode)
    router.local_healthy = lambda: True
    return router


def malformed_tool_call(call_id: str) -> ToolCall:
    """小模型典型失败:arguments 不是合法 JSON(parse_error 标记)。"""
    return ToolCall(id=call_id, name="bash", arguments={},
                    parse_error=True, raw_arguments="{bad json")


# ═══════════ 路由决策规则(caller=main) ═══════════


class TestMainRouteRules(unittest.TestCase):
    """小模型路由:主循环任务指纹 AND 规则。"""

    def setUp(self):
        self.api = FakeClient([LlmResponse(content="api answer")])
        self.local = FakeClient([LlmResponse(content="local answer")])
        self.router = healthy_router(self.api, self.local)

    def route(self, text, flags=None):
        return self.router.route([user_message(text)], caller="main",
                                 flags=flags or {})

    def test_pure_qna_routes_local_and_strips_tools(self):
        """纯问答:走本地小模型并剥离工具集(上下文更短)。"""
        decision = self.route("什么是装饰器?")
        self.assertTrue(decision.local)
        self.assertTrue(decision.strip_tools)

    def test_workspace_query_routes_local_but_keeps_tools(self):
        """涉及文件的查询:走本地,但保留工具集(回答需要读文件)。"""
        decision = self.route("看下 src/Configs.py 里有哪些配置")
        self.assertTrue(decision.local)
        self.assertFalse(decision.strip_tools)

    def test_action_intent_routes_api(self):
        """含动作意图(写/改/建等):保守走 API。"""
        self.assertFalse(self.route("帮我写一个测试脚本").local)

    def test_complex_marker_routes_api(self):
        """含复杂任务标记(重构/排查等):走 API。"""
        self.assertFalse(self.route("重构整个项目的模块结构").local)

    def test_long_message_routes_api(self):
        self.assertFalse(self.route("问" * (Configs.SIMPLE_TASK_MAX_CHARS + 1)).local)

    def test_short_greeting_routes_local(self):
        """极短消息(问候/闲聊):走本地。"""
        decision = self.route("你好")
        self.assertTrue(decision.local)

    def test_escalated_flag_forces_api(self):
        """本 turn 已升级后,剩余轮次强制 API。"""
        decision = self.route("什么是装饰器?", flags={"escalated": True})
        self.assertFalse(decision.local)
        self.assertIn("escalated", decision.reason)

    def test_active_teammates_forces_api(self):
        """多代理协作期间默认复杂,强制 API。"""
        decision = self.route("什么是装饰器?", flags={"active_teammates": True})
        self.assertFalse(decision.local)

    def test_unhealthy_local_falls_back_to_api(self):
        """本地服务不可达:全量降级 API(健康检查缓存内不再探测)。"""
        router = ModelRouter(self.api, self.local)
        router.local_healthy = lambda: False
        decision = router.route([user_message("什么是装饰器?")], caller="main")
        self.assertFalse(decision.local)
        self.assertIn("unhealthy", decision.reason)


# ═══════════ 静态调用点与模式开关 ═══════════


class TestStaticCallersAndModes(unittest.TestCase):
    """小模型路由:调用点静态分流与 ROUTER_MODE 开关。"""

    def setUp(self):
        self.api = FakeClient([LlmResponse(content="x")])
        self.local = FakeClient([LlmResponse(content="y")])

    def test_summary_and_subagent_default_local(self):
        """摘要与探索子代理:单次/短生命周期,默认本地。"""
        router = healthy_router(self.api, self.local)
        self.assertTrue(router.route([user_message("x")],
                                     caller="summary").local)
        self.assertTrue(router.route([user_message("x")],
                                     caller="subagent").local)

    def test_teammate_defaults_api(self):
        """长时自治队友:默认 API(小模型漂移风险)。"""
        router = healthy_router(self.api, self.local)
        self.assertFalse(router.route([user_message("x")],
                                      caller="teammate").local)

    def test_api_only_mode_kill_switch(self):
        """逃生舱:api-only 全量走 API,即使本地健康。"""
        router = healthy_router(self.api, self.local, mode="api-only")
        for caller in ("main", "summary", "subagent"):
            decision = router.route([user_message("什么是装饰器?")],
                                    caller=caller)
            self.assertFalse(decision.local, caller)

    def test_local_only_mode_forces_local(self):
        """测试模式:local-only 全量走本地(本地健康时)。"""
        router = healthy_router(self.api, self.local, mode="local-only")
        self.assertTrue(router.route([user_message("x")], caller="main").local)

    def test_shadow_mode_logs_but_does_not_activate(self):
        """调参模式:shadow 下动态决策只记录不生效(强制 api),静态路由仍生效。"""
        router = healthy_router(self.api, self.local, mode="shadow")
        main = router.route([user_message("什么是装饰器?")], caller="main")
        self.assertFalse(main.local)
        self.assertIn("shadow", main.reason)
        self.assertTrue(router.route([user_message("x")],
                                     caller="summary").local)


# ═══════════ apply:决策落成 (client, model, max_tokens) ═══════════


class TestRouterApply(unittest.TestCase):
    """小模型路由:本地调用压低 max_tokens(失败要便宜),忽略模型覆盖。"""

    def setUp(self):
        self.api = FakeClient([LlmResponse(content="x")])
        self.local = FakeClient([LlmResponse(content="y")])
        self.router = healthy_router(self.api, self.local)

    def test_local_caps_max_tokens(self):
        decision = self.router.route([user_message("什么是装饰器?")],
                                     caller="main")
        client, model, max_tokens = self.router.apply(decision, 4096)
        self.assertIs(client, self.local)
        self.assertEqual(model, self.local.model)
        self.assertEqual(max_tokens, Configs.LOCAL_MAX_TOKENS)

    def test_api_keeps_max_tokens_and_honors_override(self):
        """API 侧保留默认 max_tokens,且允许外层模型覆盖(如 FALLBACK_MODEL_ID)。"""
        decision = self.router.route([user_message("帮我写个脚本")],
                                     caller="main")
        client, model, max_tokens = self.router.apply(
            decision, 4096, model_override="fallback-x")
        self.assertIs(client, self.api)
        self.assertEqual(model, "fallback-x")
        self.assertEqual(max_tokens, 4096)


# ═══════════ 升级阶梯(AgentLoop 集成) ═══════════


class TestEscalationLadder(unittest.TestCase):
    """小模型路由:质量信号 → 整 turn 升级 API,历史不携带失败消息。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._old_workdir = Configs.WORKDIR
        Configs.WORKDIR = self.tmp  # 重定向文件工具工作目录,避免污染仓库

    def tearDown(self):
        Configs.WORKDIR = self._old_workdir

    def build_loop(self, local_script, api_script):
        local = FakeClient(local_script)
        api = FakeClient(api_script)
        router = healthy_router(api, local)
        return AgentLoop(api, router=router), local, api

    def test_parse_errors_escalate_then_api_answers(self):
        """本地连续 2 次参数解析失败 → 升级 API;坏消息不进历史。"""
        loop, local, api = self.build_loop(
            [LlmResponse(content="", tool_calls=[malformed_tool_call("b1")]),
             LlmResponse(content="", tool_calls=[malformed_tool_call("b2")])],
            [LlmResponse(content="API 最终答案。")])
        history = [user_message("什么是装饰器?")]
        loop.run_cycle(history, {})
        self.assertEqual(len(local.calls), 2)
        self.assertEqual(len(api.calls), 1)
        assistants = [m for m in history if m.get("role") == "assistant"]
        self.assertEqual(len(assistants), 1)
        self.assertNotIn("tool_calls", assistants[0])
        self.assertIn("API 最终答案", assistants[0]["content"])

    def test_empty_reply_escalates(self):
        """本地空回复(无内容无工具调用)→ 升级 API。"""
        loop, local, api = self.build_loop(
            [LlmResponse(content="", tool_calls=[])],
            [LlmResponse(content="API 补答。")])
        history = [user_message("你好")]
        loop.run_cycle(history, {})
        self.assertEqual(len(local.calls), 1)
        self.assertEqual(len(api.calls), 1)
        self.assertIn("API 补答", history[-1]["content"])

    def test_length_truncation_escalates_to_api(self):
        """本地 length 截断:不升级 max_tokens(本地生成慢),直接切 API。"""
        loop, local, api = self.build_loop(
            [LlmResponse(content="", tool_calls=[], finish_reason="length")],
            [LlmResponse(content="API 续写。")])
        history = [user_message("什么是装饰器?")]
        loop.run_cycle(history, {})
        self.assertEqual(len(local.calls), 1)
        self.assertEqual(len(api.calls), 1)
        # 截断回复不入历史,API 从同一提示词重新生成
        self.assertIn("API 续写", history[-1]["content"])

    def test_tool_loop_escalates(self):
        """同一工具+参数连续调用超限(空转)→ 下一轮升级 API。"""
        same_glob = ToolCall(id="g1", name="glob", arguments={"pattern": "*.py"})
        loop, local, api = self.build_loop(
            [LlmResponse(content="", tool_calls=[same_glob])
             for _ in range(Configs.MAX_IDENTICAL_TOOL_CALLS)],
            [LlmResponse(content="API 收尾。")])
        history = [user_message("看下当前目录有哪些 py 文件")]
        loop.run_cycle(history, {})
        self.assertEqual(len(local.calls), Configs.MAX_IDENTICAL_TOOL_CALLS)
        self.assertEqual(len(api.calls), 1)
        self.assertIn("API 收尾", history[-1]["content"])


# ═══════════ Phase 4:本地只读工具集 ═══════════


class TestSlimToolset(unittest.TestCase):
    """小模型路由 Phase 4:本地调用只暴露只读工具,无 bash/写操作/MCP。"""

    def test_slim_keeps_readonly_and_drops_dangerous(self):
        """slim 工具集:read_file/glob 等在,bash/写文件/多代理/MCP 全部排除。"""
        loop = AgentLoop(FakeClient([LlmResponse(content="x")]))
        tools, handlers = loop.tool_registry.assemble(slim=True)
        names = {t["function"]["name"] for t in tools}
        self.assertLessEqual(names, set(ToolRegistry.READONLY_TOOL_NAMES))
        self.assertIn("read_file", names)
        self.assertIn("glob", names)
        for banned in ("bash", "write_file", "edit_file", "task",
                       "spawn_teammate", "create_worktree"):
            self.assertNotIn(banned, names)
        self.assertNotIn(banned, handlers)
        self.assertFalse(any(n.startswith("mcp__") for n in names),
                         "slim 模式不含 MCP 工具")

    def test_full_assemble_keeps_all(self):
        """完整工具集不受影响:bash 与 MCP 工具仍在。"""
        loop = AgentLoop(FakeClient([LlmResponse(content="x")]))
        tools, _ = loop.tool_registry.assemble()
        names = {t["function"]["name"] for t in tools}
        self.assertIn("bash", names)
        self.assertIn("read_file", names)


# ═══════════ Phase 3:轨迹收集 ═══════════


class TestTrajectoryLogger(unittest.TestCase):
    """小模型路由 Phase 3:轨迹落盘为微调数据,四种 outcome 分型。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_four_outcomes_and_unique_filenames(self):
        logger = TrajectoryLogger(directory=self.tmp / "traj", enabled=True)
        st = RecoveryState(); st.started_local = True
        logger.log_turn([{"role": "user", "content": "x"}], st)  # local-ok
        st = RecoveryState(); st.started_local = True
        st.turn_escalated = True; st.escalation_reason = "parse-errors"
        logger.log_turn([{"role": "user", "content": "y"}], st)  # escalated
        st = RecoveryState(); st.started_local = False
        logger.log_turn([{"role": "user", "content": "z"}], st)  # api
        st = RecoveryState(); st.started_local = None
        logger.log_turn([{"role": "user", "content": "w"}], st,
                        error="boom")  # error
        files = sorted((self.tmp / "traj").glob("*.jsonl"))
        self.assertEqual(len(files), 4, "同一秒内多条轨迹不得互相覆盖")
        outcomes = []
        for f in files:
            rec = json.loads(f.read_text())
            outcomes.append(rec["metadata"]["outcome"])
            self.assertIn("messages", rec, "ShareGPT 风格 messages 字段")
        self.assertEqual(sorted(outcomes),
                         ["api", "error", "escalated", "local-ok"])

    def test_disabled_by_default(self):
        """默认关闭(COLLECT_TRAINING_DATA 未开):不产生任何文件。"""
        logger = TrajectoryLogger(directory=self.tmp / "off")
        st = RecoveryState(); st.started_local = True
        self.assertIsNone(logger.log_turn([{"role": "user", "content": "x"}], st))
        self.assertFalse((self.tmp / "off").exists())


if __name__ == "__main__":
    unittest.main()
