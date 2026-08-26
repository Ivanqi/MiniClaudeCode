"""消息总线:团队通信的追加式 JSONL 邮箱。

每个代理对应一个 mailbox 文件,消息只追加、读取即消费(删除),
让通信协议在磁盘上可检查,并支持后台队友随时发消息。
"""
import json
import time
from pathlib import Path

from src.Configs import Configs
from src.Terminal import Terminal


class MessageBus:
    """消息总线:send 追加消息,read_inbox 读取并清空某个代理的邮箱。"""

    def __init__(self, mailbox_dir: Path | None = None):
        self.mailbox_dir = mailbox_dir or Configs.MAILBOX_DIR
        self.mailbox_dir.mkdir(exist_ok=True)

    def _inbox_path(self, agent: str) -> Path:
        return self.mailbox_dir / f"{agent}.jsonl"

    def send(self, from_agent: str, to_agent: str, content: str,
             msg_type: str = "message", metadata: dict | None = None):
        msg = {"from": from_agent, "to": to_agent,
               "content": content, "type": msg_type,
               "ts": time.time(), "metadata": metadata or {}}
        with open(self._inbox_path(to_agent), "a") as f:
            f.write(json.dumps(msg) + "\n")
        Terminal.info("bus", f"{from_agent} → {to_agent}: "
                             f"({msg_type}) {content[:50]}", Terminal.YELLOW)

    def read_inbox(self, agent: str) -> list[dict]:
        inbox = self._inbox_path(agent)
        if not inbox.exists():
            return []
        msgs = [json.loads(line) for line in inbox.read_text().splitlines()
                if line.strip()]
        inbox.unlink()
        return msgs


# 全局单例。
BUS = MessageBus()
