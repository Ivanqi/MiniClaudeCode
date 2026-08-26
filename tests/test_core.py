"""核心纯逻辑单元测试(无需网络与 API Key)。

运行: python -m unittest discover -s tests -v
"""
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from src.BasicOper import BasicOper
from src.Configs import Configs
from src.ContextCompact import ContextCompact
from src.CronScheduler import CronJob, CronScheduler
from src.LlmClient import (DeepSeekApiError, LlmResponse, ToolCall,
                           assistant_message, classify_error,
                           make_function_tool, tool_message, user_message)
from src.TaskSystem import TaskSystem
from src.WorktreeSystem import WorktreeSystem


class TestMessageFormat(unittest.TestCase):
    """DeepSeek 消息/工具格式构造与解析。"""

    def test_tool_call_from_api(self):
        raw = {"id": "call_1", "type": "function",
               "function": {"name": "bash",
                            "arguments": '{"command": "ls"}'}}
        call = ToolCall.from_api(raw)
        self.assertEqual(call.id, "call_1")
        self.assertEqual(call.name, "bash")
        self.assertEqual(call.arguments, {"command": "ls"})

    def test_tool_call_from_api_bad_json(self):
        raw = {"id": "call_2", "function": {"name": "x", "arguments": "not json"}}
        call = ToolCall.from_api(raw)
        self.assertEqual(call.arguments, {})

    def test_assistant_message_round_trip(self):
        response = LlmResponse(
            content="",
            tool_calls=[ToolCall(id="c1", name="glob", arguments={"pattern": "*.py"})],
            finish_reason="tool_calls")
        msg = assistant_message(response)
        self.assertEqual(msg["role"], "assistant")
        # 回传 API 前,arguments 必须能重新解析为原始参数
        restored = json.loads(msg["tool_calls"][0]["function"]["arguments"])
        self.assertEqual(restored, {"pattern": "*.py"})

    def test_tool_message(self):
        self.assertEqual(tool_message("c1", "ok"),
                         {"role": "tool", "tool_call_id": "c1", "content": "ok"})

    def test_user_message(self):
        self.assertEqual(user_message("hi"), {"role": "user", "content": "hi"})

    def test_make_function_tool(self):
        tool = make_function_tool("bash", "desc", {"type": "object"})
        self.assertEqual(tool["type"], "function")
        self.assertEqual(tool["function"]["name"], "bash")
        self.assertEqual(tool["function"]["parameters"], {"type": "object"})


class TestErrorClassification(unittest.TestCase):
    def test_ratelimit(self):
        self.assertEqual(classify_error(DeepSeekApiError(429, "too many")), "ratelimit")
        self.assertEqual(classify_error(ValueError("HTTP 429 hit")), "ratelimit")

    def test_overload(self):
        self.assertEqual(classify_error(DeepSeekApiError(503, "busy")), "overload")

    def test_other(self):
        self.assertEqual(classify_error(ValueError("boom")), "other")


class TestCronScheduler(unittest.TestCase):
    def setUp(self):
        self.scheduler = CronScheduler(
            durable_path=Path(tempfile.mkdtemp()) / "cron.json")

    def test_validate_cron_ok(self):
        self.assertIsNone(self.scheduler.validate_cron("*/5 * * * *"))
        self.assertIsNone(self.scheduler.validate_cron("30 14 28 2 *"))

    def test_validate_cron_bad(self):
        self.assertIsNotNone(self.scheduler.validate_cron("*/5 * *"))
        self.assertIsNotNone(self.scheduler.validate_cron("60 * * * *"))

    def test_cron_matches_every_five_minutes(self):
        dt = datetime(2026, 8, 26, 10, 0)
        self.assertTrue(self.scheduler.cron_matches("*/5 * * * *", dt))
        self.assertFalse(self.scheduler.cron_matches("*/5 * * * *",
                                                     datetime(2026, 8, 26, 10, 1)))

    def test_cron_matches_weekly(self):
        # 2026-08-26 是周三(weekday=2 → dow_val=3)
        self.assertTrue(self.scheduler.cron_matches("0 9 * * 3",
                                                    datetime(2026, 8, 26, 9, 0)))
        self.assertFalse(self.scheduler.cron_matches("0 9 * * 3",
                                                     datetime(2026, 8, 26, 9, 1)))

    def test_schedule_and_cancel(self):
        job = self.scheduler.schedule_job("0 9 * * *", "daily standup")
        self.assertIsInstance(job, CronJob)
        self.assertEqual(len(self.scheduler.scheduled_jobs), 1)
        self.assertIn("Cancelled", self.scheduler.cancel_job(job.id))
        self.assertEqual(len(self.scheduler.scheduled_jobs), 0)

    def test_schedule_invalid(self):
        result = self.scheduler.schedule_job("bad cron", "x")
        self.assertIsInstance(result, str)


class TestTaskSystem(unittest.TestCase):
    def setUp(self):
        self.tasks = TaskSystem(tasks_dir=Path(tempfile.mkdtemp()))

    def test_lifecycle(self):
        task = self.tasks.create_task("write tests")
        self.assertEqual(task.status, "pending")
        result = self.tasks.claim_task(task.id)
        self.assertIn("Claimed", result)
        self.assertEqual(self.tasks.load_task(task.id).status, "in_progress")
        result = self.tasks.complete_task(task.id)
        self.assertIn("Completed", result)
        self.assertEqual(self.tasks.load_task(task.id).status, "completed")

    def test_blocked_task_cannot_claim(self):
        dep = self.tasks.create_task("dep")
        task = self.tasks.create_task("main", blockedBy=[dep.id])
        result = self.tasks.claim_task(task.id)
        self.assertIn("blocked by", result)
        self.tasks.claim_task(dep.id)
        self.tasks.complete_task(dep.id)
        self.assertIn("Claimed", self.tasks.claim_task(task.id))

    def test_missing_dep(self):
        task = self.tasks.create_task("main", blockedBy=["task_nope"])
        self.assertIn("missing deps", self.tasks.claim_task(task.id))

    def test_double_claim_rejected(self):
        task = self.tasks.create_task("t")
        self.tasks.claim_task(task.id)
        # 状态检查先于归属检查:已被认领的任务处于 in_progress
        self.assertIn("cannot claim", self.tasks.claim_task(task.id))


class TestContextCompact(unittest.TestCase):
    def setUp(self):
        self.compact = ContextCompact(
            tool_results_dir=Path(tempfile.mkdtemp()))

    def test_message_predicates(self):
        assistant = {"role": "assistant", "content": "",
                     "tool_calls": [{"id": "c1"}]}
        self.assertTrue(self.compact.message_has_tool_use(assistant))
        self.assertFalse(self.compact.message_has_tool_use(
            {"role": "assistant", "content": "plain"}))
        self.assertTrue(self.compact.is_tool_result_message(
            {"role": "tool", "tool_call_id": "c1", "content": "x"}))
        self.assertFalse(self.compact.is_tool_result_message(
            {"role": "user", "content": "x"}))

    def test_tool_result_budget_persists_large_output(self):
        huge = "x" * 120_000
        messages = [{"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
                    {"role": "tool", "tool_call_id": "c1", "content": huge}]
        self.compact.tool_result_budget(messages, max_bytes=100_000)
        content = messages[1]["content"]
        self.assertIn("<persisted-output>", content)
        self.assertLess(len(content), 5000)

    def test_snip_compact_keeps_tail_pairing(self):
        messages = [user_message(f"m{i}") for i in range(60)]
        messages[-2] = {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]}
        messages[-1] = {"role": "tool", "tool_call_id": "c1", "content": "r"}
        snipped = self.compact.snip_compact(messages, max_messages=50)
        # tool 结果消息必须紧跟其 tool_calls 消息
        self.assertEqual(snipped[-1]["role"], "tool")
        self.assertTrue(self.compact.message_has_tool_use(snipped[-2]))
        self.assertLessEqual(len(snipped), 52)

    def test_micro_compact_keeps_recent(self):
        messages = []
        for i in range(5):
            messages.append({"role": "assistant", "content": "",
                             "tool_calls": [{"id": f"c{i}"}]})
            messages.append({"role": "tool", "tool_call_id": f"c{i}",
                             "content": f"result-{i}" * 100})
        compacted = self.compact.micro_compact(messages)
        kept = [m for m in compacted if m["role"] == "tool"]
        self.assertEqual(len(kept), 5)
        self.assertIn("compacted", kept[0]["content"])
        self.assertEqual(kept[-1]["content"], "result-4" * 100)


class TestToolRegistry(unittest.TestCase):
    def test_assemble_includes_builtins_and_no_mcp(self):
        from src.McpSystem import McpSystem
        from src.ToolRegistry import ToolRegistry
        registry = ToolRegistry(mcp=McpSystem())  # 无已连接 MCP
        registry.register_system(BasicOper())
        registry.register_system(TaskSystem(tasks_dir=Path(tempfile.mkdtemp())))
        tools, handlers = registry.assemble()
        names = {t["function"]["name"] for t in tools}
        self.assertIn("bash", names)
        self.assertIn("create_task", names)
        self.assertEqual(len(names), len(handlers))  # 定义与处理器一一对应

    def test_assemble_merges_mcp_tools(self):
        from src.McpSystem import McpSystem
        from src.ToolRegistry import ToolRegistry
        mcp = McpSystem()
        mcp.connect("docs")
        registry = ToolRegistry(mcp=mcp)
        registry.register_system(BasicOper())
        tools, handlers = registry.assemble()
        names = {t["function"]["name"] for t in tools}
        self.assertIn("mcp__docs__search", names)
        self.assertIn("mcp__docs__get_version", names)
        # MCP 工具处理器可用
        self.assertEqual(handlers["mcp__docs__get_version"](), "[docs] API v2.1.0")


class TestWorktreeValidation(unittest.TestCase):
    def setUp(self):
        self.worktrees = WorktreeSystem(
            tasks=TaskSystem(tasks_dir=Path(tempfile.mkdtemp())),
            worktrees_dir=Path(tempfile.mkdtemp()))

    def test_valid_names(self):
        self.assertIsNone(self.worktrees.validate_worktree_name("feat-x_1.0"))

    def test_invalid_names(self):
        self.assertIsNotNone(self.worktrees.validate_worktree_name(""))
        self.assertIsNotNone(self.worktrees.validate_worktree_name(".."))
        self.assertIsNotNone(self.worktrees.validate_worktree_name("bad name"))


class TestBasicOper(unittest.TestCase):
    def setUp(self):
        self.basic = BasicOper()
        self.tmpdir = Path(tempfile.mkdtemp())

    def test_safe_path_inside(self):
        p = self.basic.safe_path("a/b.txt", cwd=self.tmpdir)
        self.assertEqual(p, (self.tmpdir / "a/b.txt").resolve())

    def test_safe_path_escape_rejected(self):
        with self.assertRaises(ValueError):
            self.basic.safe_path("../outside.txt", cwd=self.tmpdir)

    def test_write_read_edit_roundtrip(self):
        # 文件工具限定在 cwd 之内,因此传入相对路径 + cwd
        self.basic.run_write("f.txt", "hello world", cwd=self.tmpdir)
        self.assertEqual(self.basic.run_read("f.txt", cwd=self.tmpdir), "hello world")
        self.basic.run_edit("f.txt", "hello", "goodbye", cwd=self.tmpdir)
        self.assertEqual(self.basic.run_read("f.txt", cwd=self.tmpdir), "goodbye world")

    def test_todo_write_validation(self):
        self.assertIn("Error", self.basic.run_todo_write("not a list"))
        self.assertIn("Error", self.basic.run_todo_write([{"content": "x"}]))
        result = self.basic.run_todo_write(
            [{"content": "a", "status": "pending"}])
        self.assertIn("Updated 1 todos", result)
        self.assertEqual(self.basic.current_todos[0]["status"], "pending")


if __name__ == "__main__":
    unittest.main()
