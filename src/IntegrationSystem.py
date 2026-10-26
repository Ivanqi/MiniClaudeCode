"""Merge queue / Integrator:按依赖顺序预检、验收、人工批准后合并。

安全边界:
- plan 只建任务,不动 git;
- preflight 在一次性 detached worktree 试合并/跑验收,不碰 main/source;
- apply 必须人工确认,创建 integration/<task> 分支/worktree,失败
  abort/reset 到 last good;成功保留分支供 review,绝不 push、绝不 checkout main。
"""
import hashlib
import heapq
import json
import shutil
import time
import uuid
from pathlib import Path

from src.BasicOper import BASIC_OPER
from src.Configs import Configs
from src.FileLock import file_lock
from src.LlmClient import ToolCall, make_function_tool
from src.TaskSystem import TASK_SYSTEM, TaskSystem
from src.TeamMetrics import TEAM_METRICS, TeamMetrics
from src.ToolDispatcher import ToolDispatcher
from src.ToolPolicy import BASH_FULL, ToolPolicy
from src.WorktreeSystem import WORKTREE_SYSTEM, WorktreeSystem

GIT_IDENTITY = ["-c", "user.name=MiniClaudeCode",
                "-c", "user.email=agent@localhost"]


class IntegrationSystem:
    """集成任务/合并队列:plan → preflight → apply。"""

    def __init__(self, tasks: TaskSystem = TASK_SYSTEM,
                 worktrees: WorktreeSystem = WORKTREE_SYSTEM,
                 dispatcher: ToolDispatcher | None = None,
                 metrics: TeamMetrics | None = None):
        self.tasks = tasks
        self.worktrees = worktrees
        self.dispatcher = dispatcher or ToolDispatcher()
        self.audit = self.dispatcher.audit
        self.metrics = metrics or TEAM_METRICS

    # ── git ──

    def _git(self, args: list[str], cwd):
        result = self.worktrees.git_process(
            args, cwd=cwd, timeout=Configs.INTEGRATION_TIMEOUT_SECONDS)
        self.audit.log("integration_git", args=args, cwd=str(cwd),
                       code=result.returncode,
                       output=(result.stdout + result.stderr)[:1000])
        return result

    def _head(self, cwd) -> str:
        result = self._git(["rev-parse", "HEAD"], cwd)
        return result.stdout.strip() if result.returncode == 0 else ""

    def _cleanup_worktree(self, path: Path, branch: str | None = None):
        try:
            self._git(["worktree", "remove", str(path), "--force"],
                      self.worktrees.repo_root)
        except Exception:
            pass
        if branch:
            try:
                self._git(["branch", "-D", branch], self.worktrees.repo_root)
            except Exception:
                pass
        try:
            if path.exists():
                shutil.rmtree(path)
        except OSError:
            pass

    # ── plan ──

    @staticmethod
    def _topo_order(tasks: list) -> list:
        selected = {task.id for task in tasks}
        by_id = {task.id: task for task in tasks}
        indegree = {task.id: 0 for task in tasks}
        edges = {task.id: set() for task in tasks}
        for task in tasks:
            for dep in task.blockedBy:
                if dep in selected:
                    edges[dep].add(task.id)
                    indegree[task.id] += 1
        heap = [((by_id[task_id].created_at or 0), task_id)
                for task_id, count in indegree.items() if count == 0]
        heapq.heapify(heap)
        order = []
        while heap:
            _, task_id = heapq.heappop(heap)
            order.append(task_id)
            for child in sorted(edges[task_id]):
                indegree[child] -= 1
                if indegree[child] == 0:
                    heapq.heappush(
                        heap, ((by_id[child].created_at or 0), child))
        if len(order) != len(tasks):
            raise ValueError("dependency cycle among source tasks")
        return order

    def plan_integration(self, source_task_ids: list,
                         acceptance_command: str = "") -> str:
        try:
            ids = list(dict.fromkeys(str(item) for item in source_task_ids))
            if not ids:
                return "Error: source_task_ids cannot be empty"
            if len(ids) > Configs.INTEGRATION_MAX_SOURCES:
                return (f"Error: too many source tasks "
                        f"(max {Configs.INTEGRATION_MAX_SOURCES})")
            tasks = []
            for task_id in ids:
                task = self.tasks.load_task(task_id)
                if task.kind != "work":
                    return f"Error: task {task_id} is kind '{task.kind}'"
                tasks.append(task)
            order = self._topo_order(tasks)
            by_id = {task.id: task for task in tasks}
            ordered = [by_id[task_id] for task_id in order]
            bases = set()
            for task in ordered:
                info = self.worktrees.worktree_info(task.worktree) \
                    if task.worktree else {}
                base = task.base_sha or info.get("base_sha", "")
                if not task.worktree or not base:
                    return (f"Error: task {task.id} has no worktree/base_sha; "
                            "create a worktree first")
                bases.add(base)
            if len(bases) != 1:
                return ("Error: source tasks have different base SHAs; "
                        "recreate/rebase their worktrees")
            base_sha = bases.pop()
            acceptance_command = str(acceptance_command or "")[:4000]
            key = "integration:" + hashlib.sha256(json.dumps(
                {"sources": order, "acceptance": acceptance_command,
                 "base": base_sha}, sort_keys=True).encode()).hexdigest()[:24]
            targets = sorted({target for task in ordered
                              for target in (task.target_files or [])})
            result = self.tasks.create_task_result(
                f"Integrate {len(ordered)} task(s)",
                description="Integration queue for: " + ", ".join(order),
                blockedBy=order, idempotency_key=key, scope="integration",
                target_files=targets, acceptance=acceptance_command,
                kind="integration")
            task = result.task
            self.tasks.update_fields(
                task.id, source_tasks=order, base_sha=base_sha,
                acceptance_command=acceptance_command, merge_status="queued",
                conflict_files=[], source_heads={}, preflight_exit_code=None,
                preflight_output="", apply_exit_code=None, apply_output="",
                merged_commit="", approved_by="", approved_at=None)
            self.metrics.record("integration_planned")
            return (f"Integration task {task.id}: {len(order)} source(s), "
                    f"base {base_sha[:12]}\nOrder: " + " → ".join(order))
        except (ValueError, FileNotFoundError) as exc:
            return f"Error: {exc}"

    # ── source state ──

    def _source_state(self, task):
        if task.kind != "integration":
            return None, f"task {task.id} is not an integration task"
        source_ids = list(task.source_tasks or [])
        if not source_ids:
            return None, "integration task has no source_tasks"
        sources, heads, bases = [], {}, set()
        for source_id in source_ids:
            try:
                source = self.tasks.load_task(source_id)
            except FileNotFoundError:
                return None, f"source task {source_id} not found"
            if source.status != "completed":
                return None, (f"source task {source_id} is {source.status}; "
                              "complete it first")
            if not source.worktree:
                return None, f"source task {source_id} has no worktree"
            info = self.worktrees.worktree_info(source.worktree)
            base = source.base_sha or info.get("base_sha", "")
            if not base:
                return None, f"source task {source_id} has no base_sha"
            if not self.worktrees.is_clean(source.worktree):
                return None, (f"worktree '{source.worktree}' has uncommitted "
                              "changes; call seal_worktree first")
            head = self.worktrees.head_sha(source.worktree)
            if not head:
                return None, f"cannot read HEAD of '{source.worktree}'"
            if self.worktrees.ahead_count(source.worktree, base) <= 0:
                return None, (f"worktree '{source.worktree}' has no commits "
                              "above base_sha")
            branch = f"wt/{source.worktree}"
            verify = self._git(["rev-parse", "--verify", branch],
                               self.worktrees.repo_root)
            if verify.returncode != 0:
                return None, f"branch {branch} not found"
            ancestor = self._git(
                ["merge-base", "--is-ancestor", base, head],
                self.worktrees.repo_root)
            if ancestor.returncode != 0:
                return None, f"base {base[:12]} is not an ancestor of {branch}"
            bases.add(base)
            heads[source_id] = head
            sources.append(source)
        if len(bases) != 1:
            return None, "source branches have different base SHAs"
        return {"base_sha": bases.pop(), "sources": sources,
                "source_heads": heads}, ""

    # ── acceptance ──

    def _run_acceptance(self, cwd: Path, command: str):
        if not command.strip():
            return True, "", None
        policy = ToolPolicy(
            role="integrator", allowed=frozenset({"bash"}),
            writable_roots=(cwd,), force_cwd=cwd, bash_mode=BASH_FULL,
            allow_mcp=False, requires_sandbox=True)
        call = ToolCall(id=f"accept-{uuid.uuid4().hex[:8]}", name="bash",
                        arguments={"command": command,
                                   "timeout": Configs.INTEGRATION_TIMEOUT_SECONDS})
        result = self.dispatcher.execute(
            call, policy, {"bash": BASIC_OPER.run_bash},
            allow_prompt=False)
        output = result.content[:Configs.INTEGRATION_MAX_OUTPUT_CHARS]
        return result.ok, output, result.exit_code

    # ── preflight ──

    def preflight_integration(self, task_id: str) -> str:
        try:
            task = self.tasks.load_task(task_id)
        except FileNotFoundError:
            return f"Error: task {task_id} not found"
        if task.kind != "integration":
            return f"Error: task {task_id} is not an integration task"
        with file_lock(Configs.integration_lock_path()):
            state, error = self._source_state(task)
            if error:
                return f"Error: {error}"
            if task.base_sha and task.base_sha != state["base_sha"]:
                return "Error: integration base_sha is stale"
            base = state["base_sha"]
            order = [source.id for source in state["sources"]]
            by_id = {source.id: source for source in state["sources"]}
            preflight_path = (self.worktrees.worktrees_dir /
                              f".integration-preflight-{uuid.uuid4().hex[:8]}")
            conflicts, output, exit_code = [], "", None
            merged = False
            add = self._git(["worktree", "add", "--detach", str(preflight_path),
                             base], self.worktrees.repo_root)
            if add.returncode != 0:
                return f"Error: cannot create preflight worktree: {add.stderr}"
            try:
                merged = True
                for source_id in order:
                    source = by_id[source_id]
                    branch = f"wt/{source.worktree}"
                    merge = self._git(GIT_IDENTITY + [
                        "merge", "--no-ff", "--no-commit", branch],
                        preflight_path)
                    if merge.returncode != 0:
                        conflicts = [line.strip() for line in self._git(
                            ["diff", "--name-only", "--diff-filter=U"],
                            preflight_path).stdout.splitlines() if line.strip()]
                        self._git(["merge", "--abort"], preflight_path)
                        merged = False
                        break
                    commit = self._git(GIT_IDENTITY + [
                        "commit", "-m", f"preflight merge {source_id}"],
                        preflight_path)
                    if commit.returncode != 0:
                        merged = False
                        conflicts = [commit.stderr.strip()[:500]]
                        break
                if merged:
                    ok, output, exit_code = self._run_acceptance(
                        preflight_path, task.acceptance_command)
                    merged = ok
            finally:
                self._cleanup_worktree(preflight_path)
            status = "preflight_verified" if merged else "preflight_failed"
            self.tasks.update_fields(
                task.id, merge_status=status,
                conflict_files=conflicts[:100],
                preflight_base_sha=base,
                source_heads=state["source_heads"],
                preflight_exit_code=exit_code,
                preflight_output=output,
                base_sha=base, source_tasks=order,
                acceptance_command=task.acceptance_command)
            self.metrics.record(
                "integration_preflight_verified" if merged
                else "integration_conflict" if conflicts
                else "integration_failed")
            if conflicts:
                return (f"Preflight failed for {task_id}: conflicts in "
                        + ", ".join(conflicts[:20]))
            if not merged:
                return (f"Preflight failed for {task_id}: acceptance/failed "
                        f"(exit={exit_code})\n{output[:2000]}")
            return (f"Preflight verified for {task_id}: base {base[:12]}, "
                    f"sources {len(order)}, exit={exit_code}")

    # ── apply ──

    def apply_integration(self, task_id: str) -> str:
        try:
            task = self.tasks.load_task(task_id)
        except FileNotFoundError:
            return f"Error: task {task_id} not found"
        if task.kind != "integration":
            return f"Error: task {task_id} is not an integration task"
        if task.merge_status != "preflight_verified":
            return (f"Error: task {task_id} is not preflight_verified "
                    f"(status={task.merge_status})")
        auto = self.dispatcher.approval.is_auto_approved("apply_integration")
        approved_by = "auto" if auto else "human"
        branch = f"integration/{task_id}"
        path = self.worktrees.worktrees_dir / f"integration-{task_id}"
        with file_lock(Configs.integration_lock_path()):
            state, error = self._source_state(task)
            if error:
                return f"Error: {error}"
            if state["base_sha"] != task.preflight_base_sha:
                return "Error: base SHA changed since preflight; rerun preflight"
            if state["source_heads"] != (task.source_heads or {}):
                return "Error: source heads changed since preflight; rerun preflight"
            if Configs.INTEGRATION_REQUIRE_REVIEW:
                for source in state["sources"]:
                    if source.review_status != "pass" or \
                            source.reviewed_head != state["source_heads"][source.id]:
                        return (f"Error: source {source.id} is not reviewed "
                                "at its current head")
            if path.exists():
                return f"Error: integration worktree already exists: {path}"
            add = self._git(["worktree", "add", "-b", branch, str(path),
                             state["base_sha"]], self.worktrees.repo_root)
            if add.returncode != 0:
                return f"Error: cannot create integration worktree: {add.stderr}"
            last_good = state["base_sha"]
            output, exit_code, conflicts = "", None, []
            try:
                for source in state["sources"]:
                    src_branch = f"wt/{source.worktree}"
                    merge = self._git(GIT_IDENTITY + [
                        "merge", "--no-ff", src_branch,
                        "-m", f"Integrate {source.id}: {source.subject}"],
                        path)
                    if merge.returncode != 0:
                        conflicts = [line.strip() for line in self._git(
                            ["diff", "--name-only", "--diff-filter=U"],
                            path).stdout.splitlines() if line.strip()]
                        self._git(["merge", "--abort"], path)
                        raise RuntimeError("merge conflict")
                # 所有 source 合并后再跑一次完整回归;失败回滚到 base。
                ok, output, exit_code = self._run_acceptance(
                    path, task.acceptance_command)
                if not ok:
                    raise RuntimeError(
                        f"acceptance failed (exit={exit_code})")
                commit = self._head(path)
                self.tasks.update_fields(
                    task.id, merge_status="applied", merged_commit=commit,
                    approved_by=approved_by, approved_at=time.time(),
                    apply_exit_code=exit_code, apply_output=output,
                    conflict_files=[])
                self.tasks.complete_integration(task.id)
                self.metrics.record("integration_applied")
                self.audit.log("integration_applied", task=task.id,
                               branch=branch, path=str(path), commit=commit,
                               approved_by=approved_by)
                return (f"Integration {task_id} applied on branch {branch} "
                        f"at {commit[:12]} (main untouched).\n"
                        f"Worktree: {path}")
            except Exception as exc:
                self._git(["merge", "--abort"], path)
                self._git(["reset", "--hard", last_good], path)
                self._cleanup_worktree(path, branch)
                self.tasks.update_fields(
                    task.id, merge_status="apply_failed",
                    conflict_files=conflicts,
                    apply_exit_code=exit_code, apply_output=output,
                    approved_by=approved_by, approved_at=time.time())
                self.metrics.record("integration_failed")
                self.audit.log("integration_failed", task=task.id,
                               error=str(exc), conflicts=conflicts)
                return f"Error: integration failed: {exc}"

    # ── 工具 ──

    def tool_defs(self) -> list[dict]:
        return [
            make_function_tool("plan_integration",
                "Create an integration task/queue for completed source tasks. "
                "Orders by dependencies; no git mutation.",
                {"type": "object",
                 "properties": {
                     "source_task_ids": {"type": "array",
                                         "items": {"type": "string"}},
                     "acceptance_command": {"type": "string"}},
                 "required": ["source_task_ids"]}),
            make_function_tool("preflight_integration",
                "Trial-merge sources in a disposable worktree, run the "
                "acceptance command, and record conflicts/verification.",
                {"type": "object",
                 "properties": {"task_id": {"type": "string"}},
                 "required": ["task_id"]}),
            make_function_tool("apply_integration",
                "Apply a verified integration queue on a new integration "
                "branch after human confirmation. Never pushes or merges main.",
                {"type": "object",
                 "properties": {"task_id": {"type": "string"}},
                 "required": ["task_id"]}),
        ]

    def handlers(self) -> dict:
        return {
            "plan_integration": self.plan_integration,
            "preflight_integration": self.preflight_integration,
            "apply_integration": self.apply_integration,
        }
