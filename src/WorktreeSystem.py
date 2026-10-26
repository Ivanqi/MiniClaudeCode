"""Worktree 系统:隔离 git worktree 的创建、删除、保留、封存与元数据。

- 每个 worktree 在 `.worktrees/.meta/<name>.json` 记录 base_ref/base_sha/
  branch/task_id,供 merge queue 做祖先校验与冲突预检;
- `seal_worktree` 由 lead 进程提交队友的未提交改动(沙箱内不能可靠操作
  真实 git 目录),提交前用 SecurityPolicy 拒绝敏感文件;
- 删除前用 base_sha..HEAD 统计提交,不再依赖 `@{push}`。
"""
import json
import re
import subprocess
import time
from pathlib import Path

from src.Configs import Configs
from src.FileLock import atomic_write_text, ensure_private_dir
from src.LlmClient import make_function_tool
from src.SecurityPolicy import classify_path
from src.TaskSystem import TASK_SYSTEM, TaskSystem
from src.Terminal import Terminal

VALID_WT_NAME = re.compile(r'^[A-Za-z0-9._-]{1,64}$')
GIT_TIMEOUT_SECONDS = 30
META_DIR_NAME = ".meta"


class WorktreeSystem:
    """Worktree 系统:名称校验 + git 操作 + 元数据/事件日志。"""

    def __init__(self, tasks: TaskSystem = TASK_SYSTEM,
                 worktrees_dir: Path | None = None,
                 repo_root: Path | None = None):
        self.tasks = tasks
        self.worktrees_dir = worktrees_dir or Configs.worktrees_dir()
        self._repo_root = Path(repo_root) if repo_root is not None else None
        self.worktrees_dir.mkdir(parents=True, exist_ok=True)
        self.meta_dir = self.worktrees_dir / META_DIR_NAME

    @property
    def repo_root(self) -> Path:
        return self._repo_root or Configs.WORKDIR

    # ── 名称校验 ──

    @staticmethod
    def validate_worktree_name(name: str) -> str | None:
        """校验名称合法,返回错误描述或 None。"""
        if not name:
            return "Worktree name cannot be empty"
        if name in (".", "..", META_DIR_NAME):
            return f"'{name}' is not a valid worktree name"
        if not VALID_WT_NAME.match(name):
            return (f"Invalid worktree name '{name}': "
                    "only letters, digits, dots, underscores, dashes (1-64 chars)")
        return None

    # ── git 操作 ──

    def git_process(self, args: list[str], cwd: Path | None = None,
                    timeout: int = GIT_TIMEOUT_SECONDS,
                    env: dict | None = None):
        try:
            return subprocess.run(
                ["git"] + args, cwd=cwd or self.repo_root,
                capture_output=True, text=True, timeout=timeout,
                env=env)
        except subprocess.TimeoutExpired:
            class _Timeout:
                returncode = 124
                stdout = ""
                stderr = "Error: git timeout"
            return _Timeout()

    def run_git(self, args: list[str], cwd: Path | None = None,
                timeout: int = GIT_TIMEOUT_SECONDS) -> tuple[bool, str]:
        result = self.git_process(args, cwd=cwd, timeout=timeout)
        out = (result.stdout + result.stderr).strip()
        return result.returncode == 0, out[:5000] if out else "(no output)"

    def log_event(self, event_type: str, worktree_name: str, task_id: str = ""):
        event = {"type": event_type, "worktree": worktree_name,
                 "task_id": task_id, "ts": time.time()}
        events_file = self.worktrees_dir / "events.jsonl"
        with open(events_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    # ── 元数据 ──

    def _meta_path(self, name: str) -> Path:
        return self.meta_dir / f"{name}.json"

    def worktree_info(self, name: str) -> dict:
        info = {"name": name, "path": str(self.worktrees_dir / name),
                "branch": f"wt/{name}", "base_ref": "", "base_sha": "",
                "task_id": "", "sealed_head": ""}
        try:
            payload = json.loads(self._meta_path(name).read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                info.update(payload)
        except (OSError, json.JSONDecodeError):
            pass
        return info

    def _write_meta(self, name: str, info: dict):
        ensure_private_dir(self.meta_dir)
        atomic_write_text(self._meta_path(name),
                          json.dumps(info, ensure_ascii=False, indent=2))

    def is_clean(self, name: str) -> bool:
        path = self.worktrees_dir / name
        if not path.exists():
            return False
        result = self.git_process(["status", "--porcelain"], cwd=path,
                                  timeout=10)
        return result.returncode == 0 and not result.stdout.strip()

    def head_sha(self, name: str) -> str:
        path = self.worktrees_dir / name
        result = self.git_process(["rev-parse", "HEAD"], cwd=path, timeout=10)
        return result.stdout.strip() if result.returncode == 0 else ""

    def ahead_count(self, name: str, base_sha: str | None = None) -> int:
        base = base_sha or self.worktree_info(name).get("base_sha", "")
        if not base:
            return -1
        path = self.worktrees_dir / name
        result = self.git_process(["rev-list", "--count", f"{base}..HEAD"],
                                  cwd=path, timeout=10)
        try:
            return int(result.stdout.strip()) if result.returncode == 0 else -1
        except ValueError:
            return -1

    # ── 业务操作 ──

    def create_worktree(self, name: str, task_id: str = "",
                        base_ref: str = "HEAD") -> str:
        # 工具层校验是安全边界的一部分:在 git 看到名字之前先拦截非法名称。
        err = self.validate_worktree_name(name)
        if err:
            return f"Error: {err}"
        if task_id:
            try:
                self.tasks.load_task(task_id)
            except FileNotFoundError:
                return f"Error: task {task_id} not found"
        path = self.worktrees_dir / name
        if path.exists():
            return f"Worktree '{name}' already exists at {path}"
        base_result = self.git_process(["rev-parse", base_ref],
                                       cwd=self.repo_root, timeout=10)
        base_sha = (base_result.stdout.strip()
                    if base_result.returncode == 0 else "")
        ok, result = self.run_git(
            ["worktree", "add", str(path), "-b", f"wt/{name}", base_ref])
        if not ok:
            return f"Git error: {result}"
        info = {
            "name": name, "path": str(path), "branch": f"wt/{name}",
            "base_ref": base_ref, "base_sha": base_sha, "task_id": task_id,
            "created_at": time.time(), "sealed_head": "",
        }
        self._write_meta(name, info)
        if task_id:
            self.bind_task_to_worktree(task_id, name)
        self.log_event("create", name, task_id)
        Terminal.info("worktree", f"created: {name} at {path}", Terminal.YELLOW)
        return f"Worktree '{name}' created at {path}"

    def bind_task_to_worktree(self, task_id: str, worktree_name: str):
        setter = getattr(self.tasks, "set_worktree", None)
        if setter is not None:
            setter(task_id, worktree_name)
        else:
            task = self.tasks.load_task(task_id)
            task.worktree = worktree_name
            self.tasks.save_task(task)
        info = self.worktree_info(worktree_name)
        updater = getattr(self.tasks, "update_fields", None)
        if updater is not None and info.get("base_sha"):
            try:
                updater(task_id, base_ref=info.get("base_ref", ""),
                        base_sha=info.get("base_sha", ""))
            except (ValueError, FileNotFoundError):
                pass

    def commit_worktree(self, name: str, task_id: str = "",
                        message: str = "Seal worktree") -> tuple[bool, str, str]:
        """由 lead 进程提交 worktree 内的改动;拒绝敏感文件。"""
        err = self.validate_worktree_name(name)
        if err:
            return False, err, ""
        path = self.worktrees_dir / name
        if not path.exists():
            return False, f"Worktree '{name}' not found", ""
        changed = set()
        for args in (["diff", "--name-only"],
                     ["diff", "--cached", "--name-only"],
                     ["ls-files", "--others", "--exclude-standard"]):
            result = self.git_process(args, cwd=path, timeout=20)
            if result.returncode != 0:
                return False, (result.stderr or "git status failed").strip(), ""
            changed.update(line.strip() for line in result.stdout.splitlines()
                           if line.strip())
        if not changed:
            return True, "No changes to seal", self.head_sha(name)
        for rel in sorted(changed):
            decision = classify_path(rel, path, write=True)
            if decision.verdict == "deny":
                return False, f"Refusing to seal sensitive path: {rel}", ""
        add = self.git_process(["add", "-A"], cwd=path, timeout=30)
        if add.returncode != 0:
            return False, (add.stderr or "git add failed").strip(), ""
        commit = self.git_process(
            ["-c", "user.name=MiniClaudeCode",
             "-c", "user.email=agent@localhost",
             "commit", "-m", str(message or "Seal worktree")[:500]],
            cwd=path, timeout=30)
        if commit.returncode != 0:
            return False, (commit.stderr or commit.stdout).strip(), ""
        sha = self.head_sha(name)
        info = self.worktree_info(name)
        info.update({"sealed_head": sha, "sealed_at": time.time(),
                     "task_id": task_id or info.get("task_id", "")})
        self._write_meta(name, info)
        self.log_event("seal", name, task_id)
        return True, f"Committed {len(changed)} file(s)", sha

    def run_seal_worktree(self, task_id: str,
                          message: str = "Seal worktree") -> str:
        try:
            task = self.tasks.load_task(task_id)
        except FileNotFoundError:
            return f"Error: task {task_id} not found"
        if not task.worktree:
            return f"Error: task {task_id} has no worktree"
        ok, detail, sha = self.commit_worktree(
            task.worktree, task_id, message)
        if not ok:
            return f"Error: {detail}"
        return f"Sealed {task.worktree} at {sha or '(no changes)'}: {detail}"

    def remove_worktree(self, name: str, discard_changes: bool = False) -> str:
        err = self.validate_worktree_name(name)
        if err:
            return err
        path = self.worktrees_dir / name
        if not path.exists():
            return f"Worktree '{name}' not found"
        try:
            active = [task.id for task in self.tasks.list_tasks()
                      if task.worktree == name and task.status == "in_progress"]
        except Exception:
            active = []
        if active and not discard_changes:
            return (f"Worktree '{name}' has in-progress task(s): "
                    f"{', '.join(active)}. Complete them first, or use "
                    "discard_changes=true after confirmation.")
        if not discard_changes:
            files, commits = self._count_worktree_changes(path, name)
            if files < 0:
                return "Cannot verify status. Use discard_changes=true to force."
            if files > 0 or commits > 0:
                return (f"Worktree '{name}' has {files} file(s), {commits} commit(s). "
                        "Use discard_changes=true or keep_worktree.")
        ok1, _ = self.run_git(["worktree", "remove", str(path), "--force"])
        if not ok1:
            return f"Failed to remove worktree '{name}'"
        self.run_git(["branch", "-D", f"wt/{name}"])
        self._meta_path(name).unlink(missing_ok=True)
        self.log_event("remove", name)
        Terminal.info("worktree", f"removed: {name}", Terminal.YELLOW)
        return f"Worktree '{name}' removed"

    def keep_worktree(self, name: str) -> str:
        err = self.validate_worktree_name(name)
        if err:
            return err
        self.log_event("keep", name)
        return f"Worktree '{name}' kept for review (branch: wt/{name})"

    def _count_worktree_changes(self, path: Path,
                                name: str | None = None) -> tuple[int, int]:
        """统计未提交文件数与相对 base_sha 的提交数。"""
        try:
            r1 = subprocess.run(["git", "status", "--porcelain"],
                                cwd=path, capture_output=True, text=True,
                                timeout=10)
            files = len([line for line in r1.stdout.strip().splitlines()
                         if line.strip()])
            base = self.worktree_info(name or path.name).get("base_sha", "")
            if base:
                r2 = subprocess.run(["git", "rev-list", "--count",
                                     f"{base}..HEAD"], cwd=path,
                                    capture_output=True, text=True, timeout=10)
                commits = (int(r2.stdout.strip())
                           if r2.returncode == 0 else -1)
            else:
                r2 = subprocess.run(["git", "log", "@{push}..HEAD", "--oneline"],
                                    cwd=path, capture_output=True, text=True,
                                    timeout=10)
                commits = len([line for line in
                               r2.stdout.strip().splitlines() if line.strip()])
            return files, commits
        except Exception:
            return -1, -1

    # ── 工具层 ──

    def run_create_worktree(self, name: str, task_id: str = "") -> str:
        return self.create_worktree(name, task_id)

    def run_remove_worktree(self, name: str, discard_changes: bool = False) -> str:
        return self.remove_worktree(name, discard_changes)

    def run_keep_worktree(self, name: str) -> str:
        return self.keep_worktree(name)

    def tool_defs(self) -> list[dict]:
        return [
            make_function_tool("create_worktree", "Create an isolated git worktree.",
                {"type": "object",
                 "properties": {"name": {"type": "string"},
                                "task_id": {"type": "string"}},
                 "required": ["name"]}),
            make_function_tool("remove_worktree",
                "Remove a worktree. Refuses if changes exist.",
                {"type": "object",
                 "properties": {"name": {"type": "string"},
                                "discard_changes": {"type": "boolean"}},
                 "required": ["name"]}),
            make_function_tool("keep_worktree", "Keep a worktree for manual review.",
                {"type": "object",
                 "properties": {"name": {"type": "string"}},
                 "required": ["name"]}),
            make_function_tool("seal_worktree",
                "Commit a teammate's worktree changes (lead-side, requires "
                "confirmation) so they can be reviewed/integrated.",
                {"type": "object",
                 "properties": {"task_id": {"type": "string"},
                                "message": {"type": "string"}},
                 "required": ["task_id"]}),
        ]

    def handlers(self) -> dict:
        return {
            "create_worktree": self.run_create_worktree,
            "remove_worktree": self.run_remove_worktree,
            "keep_worktree": self.run_keep_worktree,
            "seal_worktree": self.run_seal_worktree,
        }


# 全局单例,复用全局任务系统。
WORKTREE_SYSTEM = WorktreeSystem()
