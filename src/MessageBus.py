"""消息总线:团队通信的追加式 JSONL 邮箱。

每个代理对应一个 mailbox 文件,消息只追加;读取时在同一把文件锁内
把 inbox 原子 rename 成 draining 文件再解析删除,避免两个读者重复消费。
崩溃后遗留的 draining 文件会在下次读取时恢复(至少一次投递)。
"""
import json
import time
import uuid
from pathlib import Path

from src.Configs import Configs
from src.FileLock import ensure_private_dir, file_lock
from src.SecurityPolicy import redact, validate_agent_name
from src.Terminal import Terminal

MAX_MESSAGE_CHARS = 100_000


class MessageBus:
    """消息总线:send 追加消息,read_inbox 原子消费某个代理的邮箱。"""

    def __init__(self, mailbox_dir: Path | None = None):
        self.mailbox_dir = mailbox_dir or Configs.MAILBOX_DIR
        ensure_private_dir(self.mailbox_dir)

    def _check_agent(self, agent: str):
        err = validate_agent_name(agent)
        if err:
            raise ValueError(err)
        return agent

    def _inbox_path(self, agent: str) -> Path:
        return self.mailbox_dir / f"{agent}.jsonl"

    def _lock_path(self, agent: str) -> Path:
        return self.mailbox_dir / f"{agent}.lock"

    def send(self, from_agent: str, to_agent: str, content: str,
             msg_type: str = "message", metadata: dict | None = None):
        self._check_agent(from_agent)
        self._check_agent(to_agent)
        text = str(content)
        if len(text) > MAX_MESSAGE_CHARS:
            text = text[:MAX_MESSAGE_CHARS] + "...[truncated]"
        msg = {"from": from_agent, "to": to_agent,
               "content": text, "type": msg_type,
               "ts": time.time(), "metadata": metadata or {}}
        with file_lock(self._lock_path(to_agent)):
            with open(self._inbox_path(to_agent), "a", encoding="utf-8") as f:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")
        Terminal.info("bus", f"{from_agent} → {to_agent}: "
                             f"({msg_type}) {redact(text[:50])}", Terminal.YELLOW)

    @staticmethod
    def _read_lines(path: Path) -> list[dict]:
        messages = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return messages
        for line in lines:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                messages.append(record)
        return messages

    def read_inbox(self, agent: str) -> list[dict]:
        try:
            self._check_agent(agent)
        except ValueError:
            return []
        messages = []
        lock_path = self._lock_path(agent)
        with file_lock(lock_path):
            # 恢复上次崩溃遗留的 draining 文件
            for orphan in sorted(self.mailbox_dir.glob(
                    f"{agent}.jsonl.draining-*")):
                messages.extend(self._read_lines(orphan))
                orphan.unlink(missing_ok=True)
            inbox = self._inbox_path(agent)
            if inbox.exists():
                drained = inbox.with_name(
                    f"{inbox.name}.draining-{uuid.uuid4().hex}")
                try:
                    inbox.replace(drained)
                except OSError:
                    drained = None
            else:
                drained = None
            if drained is not None:
                messages.extend(self._read_lines(drained))
                drained.unlink(missing_ok=True)
        messages.sort(key=lambda item: item.get("ts", 0))
        return messages


# 全局单例。
BUS = MessageBus()
