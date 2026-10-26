"""确认门与沙箱包装测试(不依赖 bwrap 真实能力)。"""
import tempfile
import unittest
from pathlib import Path

from src.ApprovalGate import ApprovalGate, ApprovalRequest
from src.Sandbox import Sandbox, SandboxUnavailable


class TestApprovalGate(unittest.TestCase):
    def setUp(self):
        self.request = ApprovalRequest(tool="write_file", summary="write x")

    def test_auto_approve_exact_and_prefix(self):
        gate = ApprovalGate(interactive=False,
                            auto_approve={"write_file", "mcp__docs__*"})
        self.assertTrue(gate.confirm(self.request))
        self.assertTrue(gate.confirm(
            ApprovalRequest(tool="mcp__docs__search", summary="x")))

    def test_non_interactive_fails_closed(self):
        gate = ApprovalGate(interactive=False, auto_approve=set())
        self.assertFalse(gate.confirm(self.request))

    def test_non_tty_fails_closed(self):
        gate = ApprovalGate(interactive=True, auto_approve=set(),
                            tty_check=lambda: False)
        self.assertFalse(gate.confirm(self.request))

    def test_worker_thread_fails_closed(self):
        gate = ApprovalGate(interactive=True, auto_approve=set(),
                            tty_check=lambda: True,
                            thread_check=lambda: False)
        self.assertFalse(gate.confirm(self.request))

    def test_interactive_yes(self):
        gate = ApprovalGate(interactive=True, auto_approve=set(),
                            tty_check=lambda: True,
                            thread_check=lambda: True,
                            prompt_func=lambda _: "y")
        self.assertTrue(gate.confirm(self.request))

    def test_eof_denies(self):
        def raise_eof(_):
            raise EOFError

        gate = ApprovalGate(interactive=True, auto_approve=set(),
                            tty_check=lambda: True,
                            thread_check=lambda: True,
                            prompt_func=raise_eof)
        self.assertFalse(gate.confirm(self.request))


class TestSandbox(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()).resolve()

    def test_off_mode_uses_plain_shell(self):
        sandbox = Sandbox(mode="off")
        argv = sandbox.wrap("echo hi", self.root)
        self.assertEqual(argv[:2], ["/bin/sh", "-c"])
        self.assertIn("ulimit", argv[2])

    def test_require_without_bwrap_fails(self):
        sandbox = Sandbox(mode="require", probe=lambda: False)
        with self.assertRaises(SandboxUnavailable):
            sandbox.wrap("echo hi", self.root)

    def test_auto_falls_back_with_warning(self):
        sandbox = Sandbox(mode="auto", probe=lambda: False)
        argv = sandbox.wrap("echo hi", self.root)
        self.assertEqual(argv[:2], ["/bin/sh", "-c"])
        self.assertFalse(sandbox.sandboxed)

    def test_bwrap_argv_has_ro_root_and_writable_root(self):
        sandbox = Sandbox(mode="auto", bwrap_path="/usr/bin/bwrap",
                          probe=lambda: True)
        argv = sandbox.wrap("echo hi", self.root,
                            writable_roots=(self.root,), network=False)
        self.assertEqual(argv[0], "/usr/bin/bwrap")
        self.assertIn("--ro-bind", argv)
        self.assertIn("--bind", argv)
        self.assertIn(str(self.root), argv)
        self.assertIn("--unshare-net", argv)
        self.assertEqual(argv[-3:-1], ["/bin/sh", "-c"])
        self.assertIn("echo hi", argv[-1])

    def test_network_disables_unshare(self):
        sandbox = Sandbox(mode="auto", bwrap_path="/usr/bin/bwrap",
                          probe=lambda: True)
        argv = sandbox.wrap("curl x", self.root, network=True)
        self.assertNotIn("--unshare-net", argv)


if __name__ == "__main__":
    unittest.main()
