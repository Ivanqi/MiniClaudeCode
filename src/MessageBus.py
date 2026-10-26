"""消息总线:团队通信的追加式 JSONL 邮箱。

每个代理对应一个 mailbox 文件,消息只追加;读取时在同一把文件锁内
把 inbox 原子 rename 成 draining 文件再解析删除,避免两个读者重复消费。
每条消息带 message_id;sent/delivered 两个有界 ledger 用于显式重试去重。
崩溃后遗留的 draining 文件会在下次读取时恢复(至少一次投递)。
"""
import hashlib
import json
import time
import uuid
from pathlib import Path

from src.Configs import Configs
from src.FileLock import atomic_write_text, ensure_private_dir, file_lock
from src.SecurityPolicy import redact, validate_agent_name
from src.Terminal import Terminal

MAX_MESSAGE_CHARS = 100_000


class MessageBus:
    """消息总线:send 追加消息,read_inbox 原子消费某个代理的邮箱。"""

    def __init__(self, mailbox_dir: Path | None = None):
        self.mailbox_dir = mailbox_dir or Configs.mailbox_dir()
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

    def _ledger_path(self, agent: str, kind: str) -> Path:
        return self.mailbox_dir / f"{agent}.{kind}.jsonl"

    # ── ledger ──

    def _load_ledger(self, path: Path) -> list[str]:
        ids = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return ids
        for line in lines:
            try:
                record = json.loads(line)
                message_id = record.get("message_id")
            except (json.JSONDecodeError, AttributeError):
                continue
            if message_id:
                ids.append(str(message_id))
        return ids

    def _record_ledger(self, path: Path, message_ids):
        if isinstance(message_ids, str):
            message_ids = [message_ids]
        limit = max(1, Configs.MESSAGE_DEDUPE_LEDGER_MAX)
        ids = self._load_ledger(path)
        ids.extend(str(item) for item in message_ids)
        ids = ids[-limit:]
        payload = "".join(
            json.dumps({"message_id": item, "ts": time.time()},
                       ensure_ascii=False) + "\n"
            for item in ids)
        try:
            atomic_write_text(path, payload)
        except OSError as exc:
            Terminal.info("bus", f"ledger write failed: {exc}", Terminal.RED)

    @staticmethod
    def _synthetic_id(message: dict) -> str:
        raw = "|".join(str(message.get(key, "")) for key in
                       ("from", "to", "ts", "type", "content"))
        return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:24]

    def _inbox_has_id(self, agent: str, message_id: str) -> bool:
        try:
            lines = self._inbox_path(agent).read_text(
                encoding="utf-8").splitlines()
        except OSError:
            return False
        for line in lines:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and str(
                    record.get("message_id") or "") == message_id:
                return True
        return False

    # ── 发送/读取 ──

    def send(self, from_agent: str, to_agent: str, content: str,
             msg_type: str = "message", metadata: dict | None = None,
             message_id: str | None = None) -> str:
        self._check_agent(from_agent)
        self._check_agent(to_agent)
        explicit = message_id is not None
        message_id = str(message_id) if explicit else uuid.uuid4().hex
        text = str(content)
        if len(text) > MAX_MESSAGE_CHARS:
            text = text[:MAX_MESSAGE_CHARS] + "...[truncated]"
        msg = {"message_id": message_id, "from": from_agent, "to": to_agent,
               "content": text, "type": msg_type,
               "ts": time.time(), "metadata": metadata or {}}

        with file_lock(self._lock_path(to_agent)):
            if explicit:
                seen = set(self._load_ledger(
                    self._ledger_path(to_agent, "seen")))
                sent = set(self._load_ledger(
                    self._ledger_path(to_agent, "sent")))
                if (message_id in seen or message_id in sent
                        or self._inbox_has_id(to_agent, message_id)):
                    Terminal.info("bus", f"duplicate message_id skipped: "
                                  f"{message_id}", Terminal.YELLOW)
                    return message_id
            with open(self._inbox_path(to_agent), "a",
                      encoding="utf-8") as handle:
                handle.write(json.dumps(msg, ensure_ascii=False) + "\n")
            if explicit:
                self._record_ledger(
                    self._ledger_path(to_agent, "sent"), message_id)
        Terminal.info("bus", f"{from_agent} → {to_agent}: "
                             f"({msg_type}) {redact(text[:50])}", Terminal.YELLOW)
        return message_id

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
            seen_path = self._ledger_path(agent, "seen")
            seen = set(self._load_ledger(seen_path))
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

            delivered, batch_seen = [], set()
            for message in messages:
                message_id = str(message.get("message_id")
                                  or self._synthetic_id(message))
                message["message_id"] = message_id
                if message_id in seen or message_id in batch_seen:
                    continue
                batch_seen.add(message_id)
                delivered.append(message)
            if batch_seen:
                self._record_ledger(seen_path, list(batch_seen))
        delivered.sort(key=lambda item: item.get("ts", 0))
        return delivered


# 全局单例。
BUS = MessageBus()
