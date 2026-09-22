"""AgentLoop 离线冒烟测试:用假客户端验证完整消息管道。

验证从"模型要工具调用"到"工具执行 → tool 消息回传 → 模型收尾"
的整个 DeepSeek 消息格式链路,无需 API Key。

小模型路由说明:本文件 AgentLoop(client) 未传 router,
AgentLoop 内部构造纯 API 路由器(本地未配置/未启用),
即"小模型路由未启用"的向后兼容路径;路由决策、升级阶梯、
只读工具集与轨迹收集的专项测试见 tests/test_model_router.py。
"""
import json
import tempfile
import unittest
from pathlib import Path

from src.AgentLoop import AgentLoop
from src.Configs import Configs
from src.LlmClient import LlmResponse, ToolCall, user_message


class FakeClient:
    """按脚本依次返回回复,并记录每次收到的请求消息。"""

    def __init__(self, script: list[LlmResponse]):
        self.script = list(script)
        self.calls = []
        self.fallback_model = None

    def create(self, messages, system=None, tools=None, max_tokens=4096,
               model=None):
        self.calls.append({
            "messages": json.loads(json.dumps(messages, default=str)),
            "system": system,
            "tool_names": [t["function"]["name"] for t in (tools or [])],
            "max_tokens": max_tokens,
        })
        return self.script.pop(0)


class TestAgentLoopSmoke(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._old_workdir = Configs.WORKDIR
        Configs.WORKDIR = self.tmp  # 重定向文件工具的工作目录,避免污染仓库

    def tearDown(self):
        Configs.WORKDIR = self._old_workdir

    def test_full_cycle_with_fake_client(self):
        script = [
            # 第一轮:glob
            LlmResponse(content="",
                        tool_calls=[ToolCall(id="c1", name="glob",
                                             arguments={"pattern": "*.py"})],
                        finish_reason="tool_calls"),
            # 第二轮:write_file
            LlmResponse(content="",
                        tool_calls=[ToolCall(id="c2", name="write_file",
                                             arguments={"path": "smoke_out.txt",
                                                        "content": "hi"})],
                        finish_reason="tool_calls"),
            # 第三轮:纯文本收尾
            LlmResponse(content="Task finished.", tool_calls=[],
                        finish_reason="stop"),
        ]
        client = FakeClient(script)
        loop = AgentLoop(client)
        history = [user_message("list python files and write a note")]

        loop.run_cycle(history, {})

        # 工具确实被执行(写入了临时工作目录)
        self.assertTrue((self.tmp / "smoke_out.txt").exists())
        self.assertEqual((self.tmp / "smoke_out.txt").read_text(), "hi")

        # 最终消息是模型收尾文本
        self.assertEqual(history[-1]["role"], "assistant")
        self.assertIn("Task finished", history[-1]["content"])

        # 回传给 API 的消息格式:tool 消息必须紧跟对应 assistant tool_calls
        first_request = client.calls[0]["messages"]
        self.assertEqual(first_request[-1]["role"], "user")
        second_request = client.calls[1]["messages"]
        self.assertEqual(second_request[-1]["role"], "tool")
        self.assertEqual(second_request[-1]["tool_call_id"], "c1")
        self.assertEqual(second_request[-2]["role"], "assistant")
        self.assertEqual(second_request[-2]["tool_calls"][0]["id"], "c1")
        # system prompt 注入且包含工作目录
        self.assertIn("Working directory", client.calls[1]["system"])

        # 工具池包含内置工具定义
        tool_names = client.calls[0]["tool_names"]
        self.assertIn("bash", tool_names)
        self.assertIn("create_task", tool_names)

    def test_length_truncation_escalates_then_continues(self):
        script = [
            LlmResponse(content="", tool_calls=[], finish_reason="length"),
            LlmResponse(content="continued ok", tool_calls=[], finish_reason="stop"),
        ]
        client = FakeClient(script)
        loop = AgentLoop(client)
        history = [user_message("go")]

        loop.run_cycle(history, {})

        # 第一次 length → 升级 max_tokens 重试;第二次成功收尾
        self.assertEqual(client.calls[0]["max_tokens"], Configs.DEFAULT_MAX_TOKENS)
        self.assertEqual(client.calls[1]["max_tokens"], Configs.ESCALATED_MAX_TOKENS)
        self.assertIn("continued ok", history[-1]["content"])


if __name__ == "__main__":
    unittest.main()
