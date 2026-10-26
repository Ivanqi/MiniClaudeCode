"""bash 沙箱:优先用 bubblewrap 做只读根 + 可写工作区 + 默认无网络。

- auto:探测 bwrap,不可用则降级并告警一次;
- require:不可用直接失败(fail-closed);
- off:不包装(黑名单仍生效,但它不是安全边界)。
资源限制用 shell ulimit,避免多线程进程里使用不安全的 preexec_fn。
"""
import shutil
import subprocess
from pathlib import Path

from src.Configs import Configs
from src.Terminal import Terminal


class SandboxUnavailable(RuntimeError):
    """require 模式下沙箱不可用。"""


class Sandbox:
    """bwrap 包装器(惰性探测,不在 import/构造时执行)。"""

    def __init__(self, mode: str | None = None,
                 bwrap_path: str | None = None, probe=None,
                 memory_mb: int | None = None,
                 cpu_seconds: int | None = None):
        self.mode = (mode or Configs.SANDBOX_MODE).lower()
        if self.mode not in ("auto", "require", "off"):
            self.mode = "auto"
        self._bwrap_path = bwrap_path
        self._probe_override = probe
        self._probe_done = False
        self._available = False
        self._bwrap = None
        self._warned = False
        self.memory_mb = (Configs.SANDBOX_MEMORY_MB
                          if memory_mb is None else memory_mb)
        self.cpu_seconds = (Configs.SANDBOX_CPU_SECONDS
                            if cpu_seconds is None else cpu_seconds)

    # ── 探测 ──

    def _find_bwrap(self):
        if self._bwrap_path is not None:
            return self._bwrap_path or None
        self._bwrap = self._bwrap or shutil.which("bwrap")
        return self._bwrap

    def _probe_bwrap(self, bwrap: str) -> bool:
        if self._probe_override is not None:
            return bool(self._probe_override())
        try:
            result = subprocess.run(
                [bwrap, "--die-with-parent", "--unshare-pid",
                 "--unshare-net", "--ro-bind", "/", "/",
                 "--dev", "/dev", "--proc", "/proc", "--",
                 "/bin/true"],
                capture_output=True, timeout=5)
            return result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def available(self) -> bool:
        if self.mode == "off":
            return False
        if not self._probe_done:
            bwrap = self._find_bwrap()
            self._available = bool(bwrap) and self._probe_bwrap(bwrap)
            self._probe_done = True
        return self._available

    @property
    def sandboxed(self) -> bool:
        return self.mode != "off" and self.available()

    def _warn_fallback(self):
        if not self._warned:
            Terminal.info(
                "sandbox", "bubblewrap unavailable; running unsandboxed "
                "(command blacklist is not a security boundary)", Terminal.RED)
            self._warned = True

    # ── 命令包装 ──

    def _with_limits(self, command: str) -> str:
        memory_kb = max(self.memory_mb, 128) * 1024
        cpu = max(self.cpu_seconds, 10)
        return (f"ulimit -t {cpu} 2>/dev/null; "
                f"ulimit -v {memory_kb} 2>/dev/null; {command}")

    def wrap(self, command: str, cwd: Path,
             writable_roots=(), network: bool = False) -> list[str]:
        """返回可交给 subprocess 的 argv。"""
        limited = self._with_limits(command)
        if self.mode == "off":
            return ["/bin/sh", "-c", limited]
        if not self.available():
            if self.mode == "require":
                raise SandboxUnavailable(
                    "SANDBOX_MODE=require but bubblewrap is unavailable")
            self._warn_fallback()
            return ["/bin/sh", "-c", limited]

        bwrap = self._find_bwrap()
        # 先绑定根,再挂 /dev /proc /tmp;顺序反了会被根 bind 覆盖,
        # 导致 /dev/null 不可写(重定向失败)。
        argv = [bwrap, "--die-with-parent", "--unshare-pid",
                "--ro-bind", "/", "/",
                "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"]
        for root in writable_roots:
            resolved = str(Path(root).resolve())
            argv += ["--bind", resolved, resolved]
        if not network:
            argv.append("--unshare-net")
        argv += ["--chdir", str(Path(cwd).resolve()), "/bin/sh", "-c", limited]
        return argv

    def describe(self) -> dict:
        return {
            "mode": self.mode,
            "sandboxed": self.sandboxed,
            "bwrap": self._find_bwrap(),
        }
