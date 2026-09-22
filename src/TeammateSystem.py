"""队友系统:自治队友线程、空闲轮询与 lead 侧团队管理工具。

队友线程通过 MessageBus 与 lead 通信;空闲时轮询收件箱,
其次扫描可认领的任务。计划审批是真实闸门:
submit_plan 之后,队友在收到 plan_approval_response 之前
不再执行任何模型/工具步骤。
"""
import json
import re
import threading
import time
from pathlib import Path

from src.ActiveTeammates import ACTIVE_TEAMMATES, ActiveTeammates
from src.BasicOper import BASIC_OPER, BasicOper
from src.Configs import Configs
from src.LlmClient import (assistant_message, make_function_tool, tool_message,
                           user_message)
from src.MessageBus import BUS, MessageBus
from src.ProtocolSystem import PROTOCOL, ProtocolState, ProtocolSystem
from src.TaskSystem import TASK_SYSTEM, TaskSystem
from src.Terminal import Terminal


class TeammateSystem:
    """队友系统:负责队友线程生命周期与 lead 侧团队工具。"""

    INNER_ROUND_LIMIT = 10
    MESSAGE_WINDOW = 20

    def __init__(self, client, router=None,
                 bus: MessageBus = BUS,
                 protocol: ProtocolSystem = PROTOCOL,
                 tasks: TaskSystem = TASK_SYSTEM,
                 teammates: ActiveTeammates = ACTIVE_TEAMMATES,
                 basic: BasicOper = BASIC_OPER):
        self.client = client
        self.router = router  # None 表示不路由,直接用 self.client(向后兼容)
        self.bus = bus
        self.protocol = protocol
        self.tasks = tasks
        self.teammates = teammates
        self.basic = basic

    # ── 自治代理:空闲轮询 ──
    # 扫描任务看板
    '''
    必须是 pending、没有 owner、所有 blockedBy 依赖已完成。
    scan_start 检查依赖任务的状态有依赖不代表不能做，只有被未完成的任务阻塞才不能做。
    '''
    def scan_unclaimed_tasks(self) -> list[dict]:
        unclaimed = []
        for f in sorted(self.tasks.tasks_dir.glob("task_*.json")):
            task = json.loads(f.read_text())
            if (task.get("status") == "pending"
                    and not task.get("owner")
                    and self.tasks.can_start(task["id"])):
                unclaimed.append(task)
        return unclaimed

    '''
    idle_poll 是队友在无任务时执行的轮询逻辑，用于检查收件箱中的新消息或系统中可认领的新任务，并返回状态指示（"work"、"shutdown" 或 "timeout"）。
        agent_name：当前队友的名字。

        messages：对话消息列表（用于在发现新消息时插入上下文）。

        name 和 role：队友身份，用于消息构造（但未直接使用，可能用于日志）。

        worktree_context：可选字典，用于在认领带工作树的任务时更新路径。
    '''
    def idle_poll(self, agent_name: str, messages: list,
                  name: str, role: str,
                  worktree_context: dict | None = None) -> str:
        """空闲轮询:收件箱消息优先于可认领任务。"""
        '''
        轮询次数：总等待时间 = Configs.IDLE_TIMEOUT（例如 60 秒），每次间隔 Configs.IDLE_POLL_INTERVAL（例如 2 秒）。

        收件箱优先：每次醒来先检查收件箱：

        若存在 shutdown_request，立即响应关机并返回 "shutdown"。

        否则，将整个收件箱内容作为一条用户消息加入对话，并返回 "work"（表示有消息需处理）。

        无消息时查找新任务：

        调用 scan_unclaimed_tasks 获取所有待认领且满足依赖条件的任务。

        认领第一个任务（claim_task）：

        若成功，提取工作树信息，更新 worktree_context（若提供），并构造自动认领消息加入对话，返回 "work"。

        超时：若整个循环结束仍未找到工作，返回 "timeout"，主循环将退出线程。
        '''
        for _ in range(Configs.IDLE_TIMEOUT // Configs.IDLE_POLL_INTERVAL):
            time.sleep(Configs.IDLE_POLL_INTERVAL)
            inbox = self.bus.read_inbox(agent_name)
            if inbox:
                for msg in inbox:
                    if msg.get("type") == "shutdown_request":
                        req_id = msg.get("metadata", {}).get("request_id", "")
                        self.bus.send(name, "lead", "Shutting down.",
                                      "shutdown_response",
                                      {"request_id": req_id, "approve": True})
                        return "shutdown"
                messages.append(user_message("<inbox>" + json.dumps(inbox) + "</inbox>"))
                return "work"
            unclaimed = self.scan_unclaimed_tasks()
            if unclaimed:
                task_data = unclaimed[0]
                result = self.tasks.claim_task(task_data["id"], agent_name)
                if "Claimed" in result:
                    wt_info = ""
                    if task_data.get("worktree"):
                        wt_path = Configs.WORKTREES_DIR / task_data["worktree"]
                        wt_info = f"\nWork directory: {wt_path}"
                        if worktree_context is not None:
                            worktree_context["path"] = str(wt_path)
                    messages.append(user_message(
                        f"<auto-claimed>Task {task_data['id']}: "
                        f"{task_data['subject']}{wt_info}</auto-claimed>"))
                    return "work"
        return "timeout"

    # ── 队友线程 ──
   
    def spawn(self, name: str, role: str, prompt: str) -> str:
        if self.teammates.contains(name):
            return f"Teammate '{name}' already exists"
        self.teammates.add(name)
        threading.Thread(target=self._run_teammate, args=(name, role, prompt),
                         daemon=True).start()
        return f"Teammate '{name}' spawned as {role}"

    # _run_teammate 方法是自治队友线程的核心
    # 它实现了一个具有自主工作循环、计划审批闸门和任务认领能力的智能代理
    def _run_teammate(self, name: str, role: str, prompt: str):
        '''
        protocol_ctx：记录当前正在等待审批的计划请求ID（waiting_plan），非空时队友不会执行任何模型/工具步骤，实现“审批闸门”。

        system：系统提示，固定队友身份。

        wt_ctx：保存当前工作树路径（若有），使得后续文件操作自动在该目录下进行。
        '''
        protocol_ctx = {"waiting_plan": None}
        system = (f"You are '{name}', a {role}. "
                  f"Use tools to complete tasks. "
                  f"If a task has a worktree, work in that directory.")
        wt_ctx = {"path": None}

        # ── 工具包装:认领带 worktree 的任务后,文件工具透明地改在该目录内执行 ──
        '''
        工作树辅助函数
            _wt_cwd() 返回当前工作树路径（若有），否则返回 None。

            三个文件/命令执行函数均将 cwd 参数传递给底层 BasicOper，确保操作在工作树目录内进行。
        '''
        def _wt_cwd():
            p = wt_ctx["path"]
            return Path(p) if p else None

        def _run_bash(command: str) -> str:
            return self.basic.run_bash(command, cwd=_wt_cwd())

        def _run_read(path: str) -> str:
            return self.basic.run_read(path, cwd=_wt_cwd())

        def _run_write(path: str, content: str) -> str:
            return self.basic.run_write(path, content, cwd=_wt_cwd())

        '''
        任务相关辅助函数
            这三个函数实现了队友对任务的查看、认领和完成能力。

            认领任务时，若任务关联了工作树，则自动设置 wt_ctx["path"]，使后续操作在该工作树目录内执行。

            完成任务后，清空工作树路径。
        '''
        def _run_list_tasks():
            tasks = self.tasks.list_tasks()
            if not tasks:
                return "No tasks."
            return "\n".join(
                f"  {t.id}: {t.subject} [{t.status}]"
                + (f" (wt:{t.worktree})" if t.worktree else "")
                for t in tasks)

        def _run_claim_task(task_id: str):
            result = self.tasks.claim_task(task_id, owner=name)
            if "Claimed" in result:
                task = self.tasks.load_task(task_id)
                wt_ctx["path"] = (str(Configs.WORKTREES_DIR / task.worktree)
                                  if task.worktree else None)
            return result

        def _run_complete_task(task_id: str):
            result = self.tasks.complete_task(task_id)
            wt_ctx["path"] = None
            return result

        '''
        收件箱消息处理函数
            处理两类特殊消息：
                关机请求：回复确认，并返回 True 表示需要退出线程。

                计划审批回复：若匹配当前等待的请求ID，则清除等待标记，并将审批结果作为用户消息加入对话历史，以便模型知悉。
        '''
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
                if req_id == protocol_ctx["waiting_plan"]:
                    protocol_ctx["waiting_plan"] = None
                messages.append(user_message(
                    "[Plan approved]" if approve
                    else f"[Plan rejected] {msg['content']}"))
            return False

        # ── 队友工具集(不经过主循环的权限钩子,保证自主性) ──

        sub_tools = [
            make_function_tool("bash", "Run a shell command.",
                {"type": "object",
                 "properties": {"command": {"type": "string"}},
                 "required": ["command"]}),
            make_function_tool("read_file", "Read file.",
                {"type": "object",
                 "properties": {"path": {"type": "string"},
                                "limit": {"type": "integer"},
                                "offset": {"type": "integer"}},
                 "required": ["path"]}),
            make_function_tool("write_file", "Write file.",
                {"type": "object",
                 "properties": {"path": {"type": "string"},
                                "content": {"type": "string"}},
                 "required": ["path", "content"]}),
            make_function_tool("send_message", "Send message to another agent.",
                {"type": "object",
                 "properties": {"to": {"type": "string"},
                                "content": {"type": "string"}},
                 "required": ["to", "content"]}),
            make_function_tool("submit_plan", "Submit a plan for Lead approval.",
                {"type": "object",
                 "properties": {"plan": {"type": "string"}},
                 "required": ["plan"]}),
            make_function_tool("list_tasks", "List all tasks.",
                {"type": "object", "properties": {}, "required": []}),
            make_function_tool("claim_task", "Claim a pending task.",
                {"type": "object",
                 "properties": {"task_id": {"type": "string"}},
                 "required": ["task_id"]}),
            make_function_tool("complete_task", "Mark an in-progress task as completed.",
                {"type": "object",
                 "properties": {"task_id": {"type": "string"}},
                 "required": ["task_id"]}),
        ]
        sub_handlers = {
            "bash": _run_bash, "read_file": _run_read,
            "write_file": _run_write,
            "send_message": lambda to, content: (self.bus.send(name, to, content),
                                                 "Sent")[1],
            "list_tasks": _run_list_tasks,
            "claim_task": _run_claim_task,
            "complete_task": _run_complete_task,
        }

        messages = [user_message(prompt)]
        while True:
            '''
            初始消息为 prompt。
            若消息数量不足 3，注入身份提醒，确保模型始终清楚自身角色（循环中可能因压缩导致身份信息丢失）。
            '''
            if len(messages) <= 3:
                messages.insert(0, user_message(
                    f"<identity>You are '{name}', role: {role}. "
                    f"Continue your work.</identity>"))

            '''
            每次内层循环首先读取收件箱，处理协议消息（关机、计划审批）。

            若处于 waiting_plan 状态，则不断轮询（等待审批回复），不执行任何模型或工具调用，真正实现“审批闸门”。

            若非等待状态，则将有收件箱中的普通消息（非协议）作为用户消息注入对话。
            '''
            should_shutdown = False
            for _ in range(self.INNER_ROUND_LIMIT): # 最多10轮
                inbox = self.bus.read_inbox(name)
                for msg in inbox:
                    if handle_inbox_message(msg, messages):
                        should_shutdown = True
                        break
                if should_shutdown:
                    break
                if protocol_ctx["waiting_plan"]:
                    # 审批闸门关闭期间只轮询协议回复,不让模型继续推进任务。
                    time.sleep(Configs.IDLE_POLL_INTERVAL)
                    continue
                if inbox:
                    non_protocol = [m for m in inbox if m.get("type") == "message"]
                    if non_protocol:
                        messages.append(user_message(
                            "<inbox>" + json.dumps(non_protocol) + "</inbox>"))
                '''
                调用 LLM，传入最近的 MESSAGE_WINDOW 条消息、系统提示、工具定义。

                将模型回复加入 messages。

                若模型未请求工具调用，则结束内层循环（进入空闲轮询）。

                遍历工具调用：

                    若为 submit_plan，调用 self.submit_plan，从返回值中提取请求ID，并设置 waiting_plan（即进入审批闸门）。

                    其他工具调用，通过 handlers 执行，并将结果以 tool_message 加入。

                    若 waiting_plan 被设置（即刚提交了计划），则 忽略后续的工具调用（因为模型可能在同一回复中请求多个工具，但提交计划后应立即暂停，等待审批），并立即跳出内层循环。
                '''
                try:
                    if self.router is not None:
                        # 长时自治队友默认走 API(caller=teammate 恒返回 api)
                        decision = self.router.route(
                            messages[-self.MESSAGE_WINDOW:], caller="teammate")
                        client, model, max_tokens = self.router.apply(
                            decision, Configs.TEAMMATE_MAX_TOKENS)
                    else:
                        client, model, max_tokens = self.client, None, Configs.TEAMMATE_MAX_TOKENS
                    response = client.create(
                        messages[-self.MESSAGE_WINDOW:], system=system,
                        tools=sub_tools, max_tokens=max_tokens, model=model)
                except Exception:
                    break
                if response.has_parse_error:
                    response.tool_calls = [c for c in response.tool_calls
                                           if not c.parse_error]
                    Terminal.info("parse-error",
                                  "teammate dropped malformed call", Terminal.RED)
                messages.append(assistant_message(response))
                if not response.has_tool_calls:
                    break
                results = []
                for call in response.tool_calls:
                    if call.name == "submit_plan":
                        output = self.submit_plan(name, call.arguments.get("plan", ""))
                        match = re.search(r"\((req_\d+)\)", output)
                        protocol_ctx["waiting_plan"] = (
                            match.group(1) if match else output)
                    else:
                        handler = sub_handlers.get(call.name)
                        output = self.basic.call_tool_handler(handler, call.arguments, call.name)
                    results.append(tool_message(call.id, str(output)))
                    if protocol_ctx["waiting_plan"]:
                        # 同一条回复中审批之后的工具调用一律忽略,等审批后再执行。
                        break
                messages.extend(results)
                if protocol_ctx["waiting_plan"]:
                    break
            '''
            若收到关机请求则退出。

            若正在等待审批，则 continue 回到外层循环顶部，跳过空闲轮询（因为审批闸门开启时，队友不应主动扫任务）。

            否则，调用 idle_poll 进行空闲轮询（扫描收件箱、认领未分配任务）。若返回 shutdown 或 timeout，则退出外层循环。
            '''
            if should_shutdown:
                break
            if protocol_ctx["waiting_plan"]:
                continue
            idle_result = self.idle_poll(name, messages, name, role, wt_ctx)
            if idle_result in ("shutdown", "timeout"):
                break

        '''
        从最后一条助手消息中提取非空文本作为最终总结。

        向 lead 发送结果消息（类型为 "result"）。

        从 ActiveTeammates 中移除该队友（释放资源）。
        '''
        summary = "Done."
        for msg in reversed(messages):
            if msg.get("role") == "assistant":
                text = msg.get("content")
                if isinstance(text, str) and text.strip():
                    summary = text.strip()
                    break
        self.bus.send(name, "lead", summary, "result")
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
        state = self.protocol.pending_requests.get(request_id)
        if not state:
            return f"Request {request_id} not found"
        state.status = "approved" if approve else "rejected"
        self.bus.send("lead", state.sender,
                      feedback or ("Approved" if approve else "Rejected"),
                      "plan_approval_response",
                      {"request_id": request_id, "approve": approve})
        return f"Plan {'approved' if approve else 'rejected'}"

    def run_send_message(self, to: str, content: str) -> str:
        self.bus.send("lead", to, content)
        return f"Sent to {to}"

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
        return [
            make_function_tool("spawn_teammate", "Spawn an autonomous teammate.",
                {"type": "object",
                 "properties": {"name": {"type": "string"},
                                "role": {"type": "string"},
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
            "request_shutdown": self.run_request_shutdown,
            "request_plan": self.run_request_plan,
            "review_plan": self.run_review_plan,
        }
