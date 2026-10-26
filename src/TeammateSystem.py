"""队友系统:自治队友线程、空闲轮询与 lead 侧团队管理工具。

队友线程通过 MessageBus 与 lead 通信;空闲时轮询收件箱,
其次扫描可认领的任务。所有工具调用经过统一 ToolDispatcher:
- 有 worktree:写操作只在 worktree 内,bash cwd 强制到该目录;
- 无 worktree:文件写拒绝,bash 只能跑只读白名单;
- 线程异常/超时/生命周期上限都会给 lead 发 status=failed 的 result,
  并释放或标记失败当前任务,不再留下永远 in_progress 的任务。
"""
import json
import threading
import time

from src.ActiveTeammates import ACTIVE_TEAMMATES, ActiveTeammates
from src.BasicOper import BASIC_OPER, BasicOper
from src.Configs import Configs
from src.LlmClient import (assistant_message, make_function_tool, tool_message,
                           user_message)
from src.MessageBus import BUS, MessageBus
from src.ProtocolSystem import PROTOCOL, ProtocolState, ProtocolSystem
from src.Roles import RoleError, RoleRegistry, RoleSpec
from src.SecurityPolicy import validate_agent_name
from src.TaskSystem import TASK_SYSTEM, TaskSystem
from src.TeamMetrics import TEAM_METRICS, TeamMetrics
from src.Terminal import Terminal
from src.ToolDispatcher import ToolDispatcher


class TeammateSystem:
    """队友系统:负责队友线程生命周期与 lead 侧团队工具。"""

    INNER_ROUND_LIMIT = 10
    MESSAGE_WINDOW = 20

    def __init__(self, client, router=None,
                 bus: MessageBus = BUS,
                 protocol: ProtocolSystem = PROTOCOL,
                 tasks: TaskSystem = TASK_SYSTEM,
                 teammates: ActiveTeammates = ACTIVE_TEAMMATES,
                 basic: BasicOper = BASIC_OPER,
                 dispatcher: ToolDispatcher | None = None,
                 roles: RoleRegistry | None = None,
                 metrics: TeamMetrics | None = None):
        self.client = client
        self.router = router  # None 表示不路由,直接用 self.client(向后兼容)
        self.bus = bus
        self.protocol = protocol
        self.tasks = tasks
        self.teammates = teammates
        self.basic = basic
        self.roles = roles
        self.metrics = metrics or TEAM_METRICS
        self.dispatcher = dispatcher or ToolDispatcher(basic=self.basic)

    def role_registry(self) -> RoleRegistry:
        if self.roles is None:
            self.roles = RoleRegistry()
        return self.roles

    # ── 自治代理:空闲轮询 ──

    def scan_unclaimed_tasks(self) -> list[dict]:
        reclaim = getattr(self.tasks, "reclaim_expired", None)
        if reclaim is not None:
            try:
                reclaim(live_owners=self.teammates.live_names(),
                        grace_seconds=Configs.TEAM_RECLAIM_GRACE_SECONDS)
            except Exception as exc:
                Terminal.info("task", f"reclaim failed: {exc}", Terminal.RED)
        unclaimed = []
        for f in sorted(self.tasks.tasks_dir.glob("task_*.json")):
            try:
                task = json.loads(f.read_text())
                if (task.get("kind", "work") == "work"
                        and task.get("status") == "pending"
                        and not task.get("owner")
                        and self.tasks.can_start(task["id"])):
                    unclaimed.append(task)
            except (OSError, json.JSONDecodeError, KeyError):
                continue
        return unclaimed

    def idle_poll(self, agent_name: str, messages: list,
                  name: str, role: str,
                  worktree_context: dict | None = None,
                  deadline: float | None = None,
                  claimed: int = 0,
                  spec: RoleSpec | None = None) -> str:
        """空闲轮询:收件箱消息优先于可认领任务。"""
        max_tasks = spec.effective_max_tasks() if spec else Configs.TEAMMATE_MAX_TASKS
        can_claim = spec.can_claim if spec else True
        for _ in range(Configs.IDLE_TIMEOUT // Configs.IDLE_POLL_INTERVAL):
            time.sleep(Configs.IDLE_POLL_INTERVAL)
            self.teammates.touch(name)
            if deadline is not None and time.monotonic() >= deadline:
                return "lifetime"
            inbox = self.bus.read_inbox(agent_name)
            if inbox:
                for msg in inbox:
                    if msg.get("type") == "shutdown_request":
                        req_id = msg.get("metadata", {}).get("request_id", "")
                        self.bus.send(name, "lead", "Shutting down.",
                                      "shutdown_response",
                                      {"request_id": req_id, "approve": True})
                        return "shutdown"
                messages.append(user_message(
                    "<inbox>" + json.dumps(inbox) + "</inbox>"))
                return "work"
            if not can_claim or claimed >= max_tasks:
                return "idle"
            ok_budget, _ = self.metrics.can_claim()
            if not ok_budget or not self.metrics.reserve_task():
                return "idle"
            unclaimed = self.scan_unclaimed_tasks()
            if not unclaimed:
                self.metrics.rollback_task()
            if unclaimed:
                task_data = unclaimed[0]
                result = self.tasks.claim_task(task_data["id"], agent_name)
                if "Claimed" not in result:
                    self.metrics.rollback_task()
                if "Claimed" in result:
                    wt_info = ""
                    task = self.tasks.load_task(task_data["id"])
                    token = task.claim_token or ""
                    if worktree_context is not None:
                        worktree_context["path"] = None
                    if task.worktree:
                        wt_path = Configs.worktrees_dir() / task.worktree
                        wt_info = f"\nWork directory: {wt_path}"
                        if worktree_context is not None:
                            worktree_context["path"] = str(wt_path)
                    if worktree_context is not None:
                        worktree_context["current_task"] = task_data["id"]
                        worktree_context["claimed"] = claimed + 1
                        worktree_context["claim_token"] = token
                    self.teammates.set_task(name, task_data["id"], token)
                    messages.append(user_message(
                        f"<auto-claimed>Task {task_data['id']}: "
                        f"{task_data['subject']}{wt_info}</auto-claimed>"))
                    return "work"
        return "idle"

    # ── 队友线程 ──

    def spawn(self, name: str, role: str, prompt: str) -> str:
        err = validate_agent_name(name)
        if err:
            return f"Error: {err}"
        if name == "lead":
            return "Error: agent name 'lead' is reserved"
        if self.teammates.contains(name):
            return f"Teammate '{name}' already exists"
        if len(self.teammates.names()) >= Configs.MAX_TEAMMATES:
            return (f"Error: max active teammates reached "
                    f"({Configs.MAX_TEAMMATES})")
        try:
            spec = self.role_registry().resolve(role)
        except RoleError as exc:
            return f"Error: {exc}"
        if not spec.can_claim:
            return (f"Error: role '{spec.name}' is review-only; "
                    "use review_task instead of spawn_teammate")
        if not self.metrics.reserve_spawn():
            _, reason = self.metrics.can_spawn()
            return f"Error: {reason or 'team spawn budget exhausted'}"
        thread = threading.Thread(target=self._run_teammate,
                                  args=(name, spec, prompt), daemon=True)
        self.teammates.add(name, role=spec.name, thread=thread)
        try:
            thread.start()
        except Exception as exc:
            self.teammates.remove(name)
            self.metrics.rollback_spawn()
            return f"Error: failed to start teammate: {exc}"
        self.metrics.record("teammate_spawned")
        return f"Teammate '{name}' spawned as {spec.name}"

    @staticmethod
    def _last_text(messages: list) -> str:
        for msg in reversed(messages):
            if msg.get("role") == "assistant":
                text = msg.get("content")
                if isinstance(text, str) and text.strip():
                    return text.strip()
        return ""

    def _release_task(self, task_id: str, failed: bool, error: str = "",
                      claim_token: str = "", lease_lost: bool = False):
        """释放/标记失败当前任务;lease_lost 时不再触碰任务。"""
        if not task_id or lease_lost:
            return
        if failed:
            fn = getattr(self.tasks, "fail_task", None)
            if fn is not None:
                try:
                    result = fn(task_id, error, claim_token=claim_token)
                except TypeError:
                    result = fn(task_id, error)
                except Exception as exc:
                    Terminal.info("task", f"fail_task failed: {exc}", Terminal.RED)
                    return
                if isinstance(result, str) and "Lease lost" in result:
                    return
                return
        else:
            fn = getattr(self.tasks, "release_task", None)
            if fn is not None:
                try:
                    result = fn(task_id, claim_token=claim_token)
                except TypeError:
                    result = fn(task_id)
                except Exception as exc:
                    Terminal.info("task", f"release_task failed: {exc}",
                                  Terminal.RED)
                    return
                if isinstance(result, str) and "Lease lost" in result:
                    return
                return
        # 兼容旧 TaskSystem:至少不要让任务永远 in_progress
        try:
            task = self.tasks.load_task(task_id)
            task.status = "pending"
            task.owner = None
            self.tasks.save_task(task)
        except Exception:
            pass

    def _run_teammate(self, name: str, spec: RoleSpec | str, prompt: str):
        if isinstance(spec, str):
            try:
                spec = self.role_registry().resolve(spec)
            except RoleError as exc:
                self.bus.send(name, "lead", f"Failed: {exc}", "result",
                              {"status": "failed", "error": str(exc)})
                self.teammates.remove(name)
                return
        max_tasks = spec.effective_max_tasks()
        protocol_ctx = {"waiting_plan": None, "waiting_since": None}
        system = spec.prompt() + f"\nYour assigned name is '{name}'."
        wt_ctx = {"path": None, "current_task": None, "claimed": 0,
                  "claim_token": "", "lease_lost": False,
                  "release_only": False}
        self.teammates.set_status(name, "running")
        self.teammates.touch(name)
        deadline = time.monotonic() + Configs.TEAMMATE_MAX_LIFETIME_SECONDS
        api_calls = 0
        status = "ok"
        error = ""
        messages = [user_message(prompt)]
        should_shutdown = False

        def _run_list_tasks():
            try:
                return self.tasks.run_list_tasks()
            except Exception as exc:
                return f"Error: {exc}"

        def _run_claim_task(task_id: str):
            if not spec.can_claim:
                return f"Error: role '{spec.name}' cannot claim tasks"
            if wt_ctx["current_task"]:
                return "Error: complete the current task first"
            if wt_ctx["claimed"] >= max_tasks:
                return "Error: teammate task limit reached"
            if not self.metrics.reserve_task():
                return "Error: team task budget exhausted"
            try:
                result = self.tasks.claim_task(task_id, owner=name)
            except FileNotFoundError:
                self.metrics.rollback_task()
                return f"Error: task {task_id} not found"
            if "Claimed" not in result:
                self.metrics.rollback_task()
            if "Claimed" in result:
                task = self.tasks.load_task(task_id)
                token = task.claim_token or ""
                wt_ctx["path"] = (str(Configs.worktrees_dir() / task.worktree)
                                  if task.worktree else None)
                wt_ctx["current_task"] = task_id
                wt_ctx["claim_token"] = token
                wt_ctx["claimed"] += 1
                self.teammates.set_task(name, task_id, token)
            return result

        def _run_complete_task(task_id: str):
            try:
                result = self.tasks.complete_task(
                    task_id, claim_token=wt_ctx.get("claim_token") or None)
            except TypeError:
                result = self.tasks.complete_task(task_id)
            except FileNotFoundError:
                return f"Error: task {task_id} not found"
            if (result.startswith("Completed")
                    or "Lease lost" in result) \
                    and wt_ctx["current_task"] == task_id:
                wt_ctx["path"] = None
                wt_ctx["current_task"] = None
                wt_ctx["claim_token"] = ""
                self.teammates.clear_task(name)
            return result

        def _submit_plan(plan: str) -> str:
            req_id = self.protocol.new_request_id()
            self.protocol.track(ProtocolState(
                request_id=req_id, type="plan_approval",
                sender=name, target="lead", status="pending",
                payload=str(plan or "")))
            self.bus.send(name, "lead", plan,
                          "plan_approval_request", {"request_id": req_id})
            protocol_ctx["waiting_plan"] = req_id
            protocol_ctx["waiting_since"] = time.monotonic()
            return f"Plan submitted ({req_id})"

        def handle_inbox_message(msg: dict, messages: list) -> bool:
            """处理收件箱消息;返回 True 表示收到关机指令。"""
            msg_type = msg.get("type", "message")
            meta = msg.get("metadata", {})
            req_id = meta.get("request_id", "")
            if msg_type == "shutdown_request":
                self.bus.send(name, "lead", "Shutting down.",
                              "shutdown_response",
                              {"request_id": req_id, "approve": True})
                return True
            if msg_type == "plan_approval_response":
                approve = meta.get("approve", False)
                if req_id != protocol_ctx.get("waiting_plan"):
                    return False
                protocol_ctx["waiting_plan"] = None
                protocol_ctx["waiting_since"] = None
                messages.append(user_message(
                    "[Plan approved]" if approve
                    else f"[Plan rejected] {msg['content']}"))
            return False

        # ── 队友工具集:按 RoleSpec 过滤,仍经过统一 dispatcher ──
        tool_catalog = {
            "bash": make_function_tool("bash", "Run a shell command.",
                {"type": "object",
                 "properties": {"command": {"type": "string"},
                                "timeout": {"type": "integer", "minimum": 1,
                                            "maximum": Configs.BASH_TIMEOUT_MAX}},
                 "required": ["command"]}),
            "read_file": make_function_tool("read_file", "Read file.",
                {"type": "object",
                 "properties": {"path": {"type": "string"},
                                "limit": {"type": "integer", "minimum": 1,
                                          "maximum": Configs.READ_FILE_MAX_LINES},
                                "offset": {"type": "integer", "minimum": 0}},
                 "required": ["path"]}),
            "write_file": make_function_tool("write_file", "Write file.",
                {"type": "object",
                 "properties": {"path": {"type": "string"},
                                "content": {"type": "string"}},
                 "required": ["path", "content"]}),
            "edit_file": make_function_tool("edit_file",
                "Replace exact text in a file once.",
                {"type": "object",
                 "properties": {"path": {"type": "string"},
                                "old_text": {"type": "string"},
                                "new_text": {"type": "string"}},
                 "required": ["path", "old_text", "new_text"]}),
            "glob": make_function_tool("glob",
                "Find files matching a glob pattern.",
                {"type": "object",
                 "properties": {"pattern": {"type": "string"}},
                 "required": ["pattern"]}),
            "send_message": make_function_tool("send_message",
                "Send message to another agent.",
                {"type": "object",
                 "properties": {"to": {"type": "string"},
                                "content": {"type": "string"}},
                 "required": ["to", "content"]}),
            "submit_plan": make_function_tool("submit_plan",
                "Submit a plan for Lead approval.",
                {"type": "object",
                 "properties": {"plan": {"type": "string"}},
                 "required": ["plan"]}),
            "list_tasks": make_function_tool("list_tasks", "List all tasks.",
                {"type": "object", "properties": {}, "required": []}),
            "claim_task": make_function_tool("claim_task",
                "Claim a pending task.",
                {"type": "object",
                 "properties": {"task_id": {"type": "string"}},
                 "required": ["task_id"]}),
            "complete_task": make_function_tool("complete_task",
                "Mark an in-progress task as completed.",
                {"type": "object",
                 "properties": {"task_id": {"type": "string"}},
                 "required": ["task_id"]}),
        }
        handler_catalog = {
            "bash": self.basic.run_bash, "read_file": self.basic.run_read,
            "write_file": self.basic.run_write, "edit_file": self.basic.run_edit,
            "glob": self.basic.run_glob,
            "send_message": lambda to, content: (
                self.bus.send(name, to, content), "Sent")[1],
            "submit_plan": _submit_plan,
            "list_tasks": _run_list_tasks,
            "claim_task": _run_claim_task,
            "complete_task": _run_complete_task,
        }
        tool_order = ("bash", "read_file", "write_file", "edit_file", "glob",
                      "send_message", "submit_plan", "list_tasks",
                      "claim_task", "complete_task")
        allowed = [tool for tool in tool_order
                   if tool in spec.allowed_tools]
        sub_tools = [tool_catalog[tool] for tool in allowed]
        sub_handlers = {tool: handler_catalog[tool] for tool in allowed
                        if tool in handler_catalog}

        try:
            while True:
                self.teammates.touch(name)
                if time.monotonic() >= deadline:
                    status, error = "failed", "teammate lifetime exceeded"
                    break
                if len(messages) <= 3:
                    messages.insert(0, user_message(
                        f"<identity>You are '{name}', role: {spec.name}. "
                        f"Acceptance: {'; '.join(spec.acceptance) or '(none)'}. "
                        f"Continue your work.</identity>"))

                should_shutdown = False
                for _ in range(self.INNER_ROUND_LIMIT):
                    self.teammates.touch(name)
                    if time.monotonic() >= deadline:
                        status = "failed"
                        error = "teammate lifetime exceeded"
                        should_shutdown = True
                        break
                    current_task = wt_ctx["current_task"]
                    if current_task:
                        heartbeat = getattr(self.tasks, "heartbeat", None)
                        if heartbeat is not None:
                            try:
                                alive = heartbeat(
                                    current_task,
                                    claim_token=wt_ctx.get("claim_token") or None)
                            except TypeError:
                                alive = heartbeat(current_task)
                            except Exception:
                                alive = True
                            if not alive:
                                wt_ctx["lease_lost"] = True
                                self.metrics.record("teammate_lease_lost")
                                status = "failed"
                                error = "task lease lost"
                                should_shutdown = True
                                break
                    inbox = self.bus.read_inbox(name)
                    for msg in inbox:
                        if handle_inbox_message(msg, messages):
                            should_shutdown = True
                            break
                    if should_shutdown:
                        break
                    if protocol_ctx["waiting_plan"]:
                        req_id = protocol_ctx["waiting_plan"]
                        state = self.protocol.get(req_id)
                        if state is not None and state.status in (
                                "approved", "rejected"):
                            protocol_ctx["waiting_plan"] = None
                            protocol_ctx["waiting_since"] = None
                            messages.append(user_message(
                                "[Plan approved]"
                                if state.status == "approved"
                                else "[Plan rejected]"))
                            continue
                        since = (protocol_ctx.get("waiting_since")
                                 or time.monotonic())
                        if (state is None
                                or state.status in ("expired", "cancelled")
                                or time.monotonic() - since >=
                                Configs.PLAN_APPROVAL_TIMEOUT_SECONDS):
                            self.protocol.cancel(req_id, "plan approval timeout")
                            self.metrics.record("plan_timeout")
                            status = "failed"
                            error = "plan approval timeout"
                            wt_ctx["release_only"] = True
                            should_shutdown = True
                            break
                        time.sleep(Configs.IDLE_POLL_INTERVAL)
                        self.teammates.touch(name)
                        continue
                    if inbox:
                        non_protocol = [m for m in inbox
                                        if m.get("type") == "message"]
                        if non_protocol:
                            messages.append(user_message(
                                "<inbox>" + json.dumps(non_protocol) + "</inbox>"))
                    if api_calls >= Configs.TEAMMATE_MAX_API_CALLS:
                        status = "failed"
                        error = "teammate API call limit exceeded"
                        should_shutdown = True
                        break

                    try:
                        if self.router is not None:
                            # 长时自治队友默认走 API(caller=teammate 恒返回 api)
                            decision = self.router.route(
                                messages[-self.MESSAGE_WINDOW:],
                                caller="teammate")
                            client, model, max_tokens = self.router.apply(
                                decision, Configs.TEAMMATE_MAX_TOKENS)
                        else:
                            client, model = self.client, None
                            max_tokens = Configs.TEAMMATE_MAX_TOKENS
                        response = client.create(
                            messages[-self.MESSAGE_WINDOW:], system=system,
                            tools=sub_tools, max_tokens=max_tokens, model=model)
                    except Exception as exc:
                        status = "failed"
                        error = f"{type(exc).__name__}: {exc}"
                        should_shutdown = True
                        break
                    api_calls += 1
                    self.metrics.add_usage(
                        getattr(response, "usage", None), "teammate")
                    if response.has_parse_error:
                        response.tool_calls = [c for c in response.tool_calls
                                               if not c.parse_error]
                        Terminal.info("parse-error",
                                      "teammate dropped malformed call",
                                      Terminal.RED)
                    messages.append(assistant_message(response))
                    if not response.has_tool_calls:
                        break
                    results = []
                    for call in response.tool_calls:
                        policy = spec.to_policy(wt_ctx["path"])
                        auth = self.dispatcher.authorize(
                            call, policy, sub_handlers, allow_prompt=True,
                            agent=policy.role)
                        if not auth.allowed:
                            output = (auth.denial.content
                                      if auth.denial else "Denied")
                        else:
                            output = self.dispatcher.execute(
                                call, policy, sub_handlers,
                                authorization=auth).content
                        results.append(tool_message(call.id, output))
                        if protocol_ctx["waiting_plan"]:
                            # 同一条回复中审批之后的工具调用一律忽略
                            break
                    messages.extend(results)
                    current = wt_ctx["current_task"]
                    if current:
                        heartbeat = getattr(self.tasks, "heartbeat", None)
                        if heartbeat is not None:
                            try:
                                alive = heartbeat(
                                    current,
                                    claim_token=wt_ctx.get("claim_token") or None)
                            except TypeError:
                                alive = heartbeat(current)
                            except Exception:
                                alive = True
                            if not alive:
                                wt_ctx["lease_lost"] = True
                                self.metrics.record("teammate_lease_lost")
                                status = "failed"
                                error = "task lease lost"
                                should_shutdown = True
                                break
                    if protocol_ctx["waiting_plan"]:
                        break

                if should_shutdown:
                    break
                if protocol_ctx["waiting_plan"]:
                    continue
                idle_result = self.idle_poll(
                    name, messages, name, spec.name, wt_ctx,
                    deadline=deadline, claimed=wt_ctx["claimed"],
                    spec=spec)
                if idle_result == "lifetime":
                    status, error = "failed", "teammate lifetime exceeded"
                    break
                if idle_result in ("shutdown", "idle", "timeout"):
                    break
        except Exception as exc:
            status = "failed"
            error = f"{type(exc).__name__}: {exc}"
        finally:
            self.teammates.set_status(name, status)
            self.metrics.record(
                "teammate_exited" if status == "ok" else "teammate_failed")
            summary = self._last_text(messages)
            current = wt_ctx["current_task"]
            if current:
                self._release_task(
                    current,
                    failed=(status != "ok" and not wt_ctx["release_only"]),
                    error=error,
                    claim_token=wt_ctx.get("claim_token") or "",
                    lease_lost=wt_ctx["lease_lost"])
            if not summary:
                summary = "Done." if status == "ok" else f"Failed: {error}"
            try:
                self.bus.send(name, "lead", summary, "result",
                              {"status": status, "error": error,
                               "task_id": current or ""})
            except Exception:
                pass
            self.teammates.clear_task(name)
            self.teammates.remove(name)

    # ── 队友侧:提交计划 ──

    def submit_plan(self, from_name: str, plan: str) -> str:
        req_id = self.protocol.new_request_id()
        self.protocol.track(ProtocolState(
            request_id=req_id, type="plan_approval",
            sender=from_name, target="lead",
            status="pending", payload=plan))
        self.bus.send(from_name, "lead", plan,
                      "plan_approval_request", {"request_id": req_id})
        return f"Plan submitted ({req_id})"

    # ── lead 侧:协议与通信工具 ──

    def run_request_shutdown(self, teammate: str) -> str:
        req_id = self.protocol.new_request_id()
        self.protocol.track(ProtocolState(
            request_id=req_id, type="shutdown",
            sender="lead", target=teammate,
            status="pending", payload=""))
        self.bus.send("lead", teammate, "Shut down.", "shutdown_request",
                      {"request_id": req_id})
        return f"Shutdown request sent to {teammate}"

    def run_request_plan(self, teammate: str, task: str) -> str:
        self.bus.send("lead", teammate, f"Submit plan for: {task}", "message")
        return f"Asked {teammate} to submit a plan"

    def run_review_plan(self, request_id: str, approve: bool,
                        feedback: str = "") -> str:
        ok, message = self.protocol.decide(request_id, approve, feedback)
        return message if ok else f"Error: {message}"

    def run_send_message(self, to: str, content: str) -> str:
        self.bus.send("lead", to, content)
        return f"Sent to {to}"

    def run_team_status(self) -> str:
        return self.metrics.status_text(
            active_teammates=self.teammates, tasks=self.tasks,
            protocol=self.protocol)

    def run_check_inbox(self) -> str:
        msgs = self.protocol.consume_lead_inbox(route_protocol=True)
        if not msgs:
            return "(inbox empty)"
        lines = []
        for m in msgs:
            meta = m.get("metadata", {})
            req_id = meta.get("request_id", "")
            tag = f" [{m['type']} req:{req_id}]" if req_id else f" [{m['type']}]"
            lines.append(f"  [{m['from']}]{tag} {m['content'][:200]}")
        return "\n".join(lines)

    # ── 工具层 ──

    def tool_defs(self) -> list[dict]:
        try:
            roles = self.role_registry().spawnable_names()
        except Exception:
            roles = ["developer"]
        roles = roles or ["developer"]
        return [
            make_function_tool("spawn_teammate", "Spawn an autonomous teammate.",
                {"type": "object",
                 "properties": {"name": {"type": "string"},
                                "role": {"type": "string", "enum": roles},
                                "prompt": {"type": "string"}},
                 "required": ["name", "role", "prompt"]}),
            make_function_tool("send_message", "Send message to a teammate.",
                {"type": "object",
                 "properties": {"to": {"type": "string"},
                                "content": {"type": "string"}},
                 "required": ["to", "content"]}),
            make_function_tool("check_inbox",
                "Check inbox for messages and protocol responses.",
                {"type": "object", "properties": {}, "required": []}),
            make_function_tool("team_status",
                "Show team metrics, budget, active teammates, task board and "
                "pending protocol requests.",
                {"type": "object", "properties": {}, "required": []}),
            make_function_tool("request_shutdown",
                "Request a teammate to shut down.",
                {"type": "object",
                 "properties": {"teammate": {"type": "string"}},
                 "required": ["teammate"]}),
            make_function_tool("request_plan",
                "Ask a teammate to submit a plan.",
                {"type": "object",
                 "properties": {"teammate": {"type": "string"},
                                "task": {"type": "string"}},
                 "required": ["teammate", "task"]}),
            make_function_tool("review_plan",
                "Approve or reject a submitted plan.",
                {"type": "object",
                 "properties": {"request_id": {"type": "string"},
                                "approve": {"type": "boolean"},
                                "feedback": {"type": "string"}},
                 "required": ["request_id", "approve"]}),
        ]

    def handlers(self) -> dict:
        return {
            "spawn_teammate": self.spawn,
            "send_message": self.run_send_message,
            "check_inbox": self.run_check_inbox,
            "team_status": self.run_team_status,
            "request_shutdown": self.run_request_shutdown,
            "request_plan": self.run_request_plan,
            "review_plan": self.run_review_plan,
        }
