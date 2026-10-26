"""跨线程/跨进程文件锁与原子写入。

任务认领、邮箱消费、审计写盘、cron 持久化都依赖这里的原语:
- file_lock:fcntl.flock + 进程内线程锁,避免"读 → 检查 → 写"竞态;
- atomic_write_text:同目录临时文件 + fsync + os.replace,读方永远看到完整内容;
- ensure_private_dir:审计/备份目录以 0700 创建。
"""
import os
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - 非 POSIX 平台退化为仅线程锁
    fcntl = None

_thread_locks: dict[str, threading.Lock] = {}
_thread_locks_guard = threading.Lock()


def _thread_lock(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _thread_locks_guard:
        lock = _thread_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _thread_locks[key] = lock
    return lock


@contextmanager
def file_lock(path: Path, exclusive: bool = True):
    """对 path 加跨进程锁(同时串行化同进程线程)。

    同线程不可重入;需要重入的调用方应在外层加 RLock 或拆分临界区。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _thread_lock(path):
        if fcntl is None:
            yield
            return
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


def atomic_write_text(path: Path, text: str):
    """写临时文件 → fsync → 原子替换,避免半截文件被并发读者看到。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def ensure_private_dir(path: Path) -> Path:
    """创建 0700 目录(审计与备份不应被其他用户读取)。"""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path
