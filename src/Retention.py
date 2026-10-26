"""情景数据 retention:过期后压缩归档,不直接删除。

- transcripts/tool_outputs:逐文件 gzip 到 <dir>/archive/<date>/;
- backups:按 call-id 目录打 tar.gz;
- .training_data 默认关闭自动清理,需 RETENTION_TRAINING_DATA=1;
- 每个目录独立锁,跳过 symlink/archive/lock/temp/新文件;
- 归档先写临时文件、校验、再复核源文件 mtime/size,成功后才删源。
"""
import gzip
import os
import shutil
import tarfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from src.AuditLog import AuditLog
from src.Configs import Configs
from src.FileLock import ensure_private_dir, file_lock
from src.Terminal import Terminal


@dataclass
class RetentionPolicy:
    name: str
    directory: Path
    patterns: tuple[str, ...] = ()
    keep_days: int = 30
    max_entries: int = 0
    mode: str = "gzip"       # gzip | tar
    kind: str = "files"      # files | dirs
    lock_name: str = ".retention.lock"
    enabled: bool = True
    protect_grace_seconds: int = 3600


@dataclass
class RetentionResult:
    policy: str
    archived: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


def default_policies() -> list[RetentionPolicy]:
    return [
        RetentionPolicy(
            "transcripts", Configs.transcript_dir(),
            ("transcript_*.jsonl",),
            keep_days=Configs.RETENTION_TRANSCRIPTS_DAYS,
            max_entries=Configs.RETENTION_TRANSCRIPTS_MAX,
            lock_name=".transcripts.lock"),
        RetentionPolicy(
            "tool_outputs", Configs.task_outputs_dir(),
            ("*.txt",),
            keep_days=Configs.RETENTION_TOOL_OUTPUTS_DAYS,
            max_entries=Configs.RETENTION_TOOL_OUTPUTS_MAX,
            lock_name=".tool-results.lock"),
        RetentionPolicy(
            "backups", Configs.backup_dir(),
            keep_days=Configs.RETENTION_BACKUPS_DAYS,
            max_entries=Configs.RETENTION_BACKUPS_MAX,
            mode="tar", kind="dirs",
            lock_name=".backups.lock"),
        RetentionPolicy(
            "training_data", Configs.training_data_dir(),
            ("*.jsonl",),
            keep_days=Configs.RETENTION_TRAINING_DAYS,
            max_entries=Configs.RETENTION_TRAINING_MAX,
            lock_name=".training.lock",
            enabled=Configs.RETENTION_TRAINING_DATA),
    ]


class RetentionManager:
    """按策略把旧文件/目录压缩归档,永不直接删除。"""

    def __init__(self, policies=None, audit: AuditLog | None = None,
                 clock=None):
        self.policies = policies if policies is not None else default_policies()
        self.audit = audit or AuditLog()
        self._clock = clock or time.time

    def run(self, protected_paths=()) -> list[RetentionResult]:
        if not Configs.RETENTION_ENABLED:
            return []
        protected = set()
        for item in protected_paths:
            try:
                protected.add(str(Path(item).resolve()))
            except OSError:
                protected.add(str(item))
        results = []
        for policy in self.policies:
            if not policy.enabled:
                continue
            try:
                results.append(self._apply(policy, protected))
            except Exception as exc:
                Terminal.info("retention", f"{policy.name} failed: {exc}",
                              Terminal.RED)
                self._audit("retention_failure", policy=policy.name,
                            error=f"{type(exc).__name__}: {exc}")
                results.append(RetentionResult(policy.name, failed=[str(exc)]))
        return results

    def run_startup(self, protected_paths=()) -> list[RetentionResult]:
        """启动维护:任何异常只记录,不阻断 CLI。"""
        try:
            return self.run(protected_paths=protected_paths)
        except Exception as exc:
            Terminal.info("retention", f"startup failed: {exc}", Terminal.RED)
            return []

    def _apply(self, policy: RetentionPolicy,
               protected: set[str]) -> RetentionResult:
        directory = Path(policy.directory)
        result = RetentionResult(policy.name)
        if not directory.exists() or not directory.is_dir():
            return result
        now = self._clock()
        lock_path = directory / policy.lock_name
        with file_lock(lock_path):
            entries = self._entries(policy, directory)
            selected = self._select(policy, entries, now, protected)
            if not selected:
                return result
            archive_root = ensure_private_dir(
                directory / "archive" /
                datetime.fromtimestamp(now).strftime("%Y-%m-%d"))
            for entry in selected:
                if self._is_protected(entry, protected):
                    continue
                try:
                    size = self._size(entry)
                    if policy.kind == "dirs":
                        archive = self._archive_dir(entry, archive_root)
                    else:
                        archive = self._archive_file(entry, archive_root)
                except Exception as exc:
                    result.failed.append(str(entry))
                    self._audit("retention_failure", policy=policy.name,
                                source=str(entry), error=str(exc))
                    continue
                if archive is None:
                    result.failed.append(str(entry))
                    continue
                result.archived.append(str(entry))
                self._audit("retention_archive", policy=policy.name,
                            source=str(entry), archive=str(archive),
                            bytes=size)
        return result

    def _entries(self, policy: RetentionPolicy,
                 directory: Path) -> list[Path]:
        entries = []
        if policy.kind == "dirs":
            for path in directory.iterdir():
                if path.name == "archive" or path.is_symlink():
                    continue
                if path.is_dir():
                    entries.append(path)
        else:
            for pattern in policy.patterns:
                for path in directory.glob(pattern):
                    if path.is_symlink() or not path.is_file():
                        continue
                    if path.name.startswith("."):
                        continue
                    entries.append(path)
        deduped, seen = [], set()
        for path in entries:
            key = str(path)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(path)
        return deduped

    def _select(self, policy: RetentionPolicy, entries: list[Path],
                now: float, protected: set[str]) -> list[Path]:
        eligible = [entry for entry in entries
                    if not self._is_protected(entry, protected)]
        if not eligible:
            return []
        stamps = {str(entry): self._mtime(entry) for entry in eligible}
        selected = set()
        if policy.keep_days > 0:
            cutoff = now - policy.keep_days * 86400
            for entry in eligible:
                if stamps[str(entry)] < cutoff and stamps[str(entry)] < (
                        now - policy.protect_grace_seconds):
                    selected.add(entry)
        if policy.max_entries > 0 and len(eligible) > policy.max_entries:
            newest = sorted(eligible, key=lambda entry: stamps[str(entry)],
                            reverse=True)[:policy.max_entries]
            keep = set(newest)
            selected.update(entry for entry in eligible if entry not in keep)
        return sorted(selected, key=lambda entry: stamps[str(entry)])

    @staticmethod
    def _is_protected(path: Path, protected: set[str]) -> bool:
        try:
            resolved = str(path.resolve())
        except OSError:
            resolved = str(path)
        if resolved in protected:
            return True
        for item in protected:
            try:
                if (Path(item).is_relative_to(Path(resolved))
                        or Path(resolved).is_relative_to(Path(item))):
                    return True
            except (OSError, ValueError):
                continue
        return False

    @staticmethod
    def _mtime(path: Path) -> float:
        try:
            if path.is_file():
                return path.stat().st_mtime
            return max((child.stat().st_mtime for child in path.rglob("*")
                        if child.is_file()), default=path.stat().st_mtime)
        except OSError:
            return 0.0

    @staticmethod
    def _size(path: Path) -> int:
        try:
            if path.is_file():
                return path.stat().st_size
            return sum(child.stat().st_size for child in path.rglob("*")
                       if child.is_file())
        except OSError:
            return 0

    def _archive_file(self, source: Path, archive_root: Path) -> Path | None:
        stat = source.stat()
        target = archive_root / f"{source.name}.gz"
        if target.exists():
            target = archive_root / f"{source.name}-{uuid.uuid4().hex[:8]}.gz"
        temp = target.with_name(target.name + f".tmp-{uuid.uuid4().hex[:8]}")
        try:
            with source.open("rb") as src, gzip.open(temp, "wb") as dst:
                shutil.copyfileobj(src, dst)
            with gzip.open(temp, "rb") as check:
                check.read(1)
            os.replace(temp, target)
        except Exception:
            temp.unlink(missing_ok=True)
            raise
        after = source.stat()
        if (after.st_mtime_ns, after.st_size) != (
                stat.st_mtime_ns, stat.st_size):
            target.unlink(missing_ok=True)
            raise RuntimeError("source changed during archive")
        source.unlink()
        return target

    def _archive_dir(self, source: Path, archive_root: Path) -> Path | None:
        snapshot = self._dir_snapshot(source)
        target = archive_root / f"{source.name}.tar.gz"
        if target.exists():
            target = archive_root / f"{source.name}-{uuid.uuid4().hex[:8]}.tar.gz"
        temp = target.with_name(target.name + f".tmp-{uuid.uuid4().hex[:8]}")
        try:
            with tarfile.open(temp, "w:gz") as archive:
                archive.add(source, arcname=source.name)
            with tarfile.open(temp, "r:gz") as check:
                if not check.getmembers():
                    raise RuntimeError("empty archive")
            os.replace(temp, target)
        except Exception:
            temp.unlink(missing_ok=True)
            raise
        if self._dir_snapshot(source) != snapshot:
            target.unlink(missing_ok=True)
            raise RuntimeError("source changed during archive")
        shutil.rmtree(source)
        return target

    @staticmethod
    def _dir_snapshot(path: Path):
        files = []
        for child in path.rglob("*"):
            if child.is_file() and not child.is_symlink():
                stat = child.stat()
                files.append((str(child), stat.st_size, stat.st_mtime_ns))
        return sorted(files)

    def _audit(self, event: str, **fields):
        if self.audit is not None:
            self.audit.log(event, **fields)
