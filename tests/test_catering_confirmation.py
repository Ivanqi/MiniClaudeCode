"""Catering 探索模式写工具逐次确认测试。"""
import json
import tempfile
import unittest
from pathlib import Path

from Catering.AnomalyAgent import AnomalyAgent
from Catering.Configs import CateringConfigs
from src.LlmClient import LlmResponse, ToolCall

DATA_DIR = Path(__file__).resolve().parent.parent / "Catering" / "data"

ARCHIVE_ARGS = {
    "store_id": "A店",
    "date": "2026-09-01",
    "main_factor": "traffic",
    "dimensions": ["traffic"],
    "summary": "客流下降",
    "actions": ["检查排班"],
}


class FakeClient:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def create(self, messages, system=None, tools=None, max_tokens=4096,
               model=None):
        self.calls.append({"tools": tools, "messages": messages})
        return self.script.pop(0)


def scripted_archive():
    return [
        LlmResponse(content="",
                    tool_calls=[ToolCall(id="c1", name="archive_case",
                                         arguments=ARCHIVE_ARGS)],
                    finish_reason="tool_calls"),
        LlmResponse(content="done", tool_calls=[], finish_reason="stop"),
    ]


class TestCateringConfirmation(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._old_cases = CateringConfigs.CASES_DIR
        CateringConfigs.CASES_DIR = self.tmp / "cases"

    def tearDown(self):
        CateringConfigs.CASES_DIR = self._old_cases

    def build(self, confirm):
        return AnomalyAgent.build(
            client=FakeClient(scripted_archive()), data_dir=DATA_DIR,
            enable_writes=True, confirm=confirm)

    def archived_files(self):
        return list((self.tmp / "cases" / "archive").glob("*.json"))

    def test_denied_without_confirmation_callback(self):
        result = self.build(None).explore("归档这个案例")
        self.assertEqual(result["denials"], 1)
        self.assertEqual(result["final_text"], "done")
        self.assertEqual(self.archived_files(), [])

    def test_denied_when_callback_returns_false(self):
        result = self.build(lambda name, args: False).explore("归档")
        self.assertEqual(result["denials"], 1)
        self.assertEqual(self.archived_files(), [])

    def test_denied_when_callback_raises(self):
        def boom(name, args):
            raise RuntimeError("no tty")

        result = self.build(boom).explore("归档")
        self.assertEqual(result["denials"], 1)
        self.assertEqual(self.archived_files(), [])

    def test_approved_callback_archives_case(self):
        result = self.build(lambda name, args: True).explore("归档")
        self.assertEqual(result["denials"], 0)
        files = self.archived_files()
        self.assertEqual(len(files), 1)
        payload = json.loads(files[0].read_text())
        self.assertIn("cases", payload)

    def test_unknown_write_tool_guard_still_applies(self):
        # write 工具在 enable_writes=False 时根本不暴露,confirm 不应被调用
        agent = AnomalyAgent.build(
            client=FakeClient(scripted_archive()), data_dir=DATA_DIR,
            enable_writes=False, confirm=lambda name, args: True)
        tool_names = {t["function"]["name"] for t in agent.tools.tool_defs()}
        self.assertNotIn("archive_case", tool_names)


if __name__ == "__main__":
    unittest.main()
