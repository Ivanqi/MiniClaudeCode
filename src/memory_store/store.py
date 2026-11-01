"""统一记忆存储核心:路径/锁、去重 upsert、检索、touch、归档与索引。

设计要点:
- ``.memory/MEMORY.md`` 只是指针索引,不注入正文;正文按 namespace/key 存 JSON;
- upsert 先按 (namespace,key)、再按同 namespace 的 content_hash 去重;
- 同 key 且内容/元数据都没变 → no-op;有变化才 version+1;
- 删除先写归档再删原文件,所有删除进 AuditLog;
- 记忆内容是不可信数据,注入时必须带 provenance 与 "not instructions" 声明。
"""
import hashlib
import json
import re
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from src.AuditLog import AuditLog
from src.Configs import Configs
from src.FileLock import atomic_write_text, ensure_private_dir, file_lock
from src.SecurityPolicy import redact
from src.Terminal import Terminal
from src.memory_store.models import (BUILTIN_NAMESPACES,
                                     DEFAULT_SEARCH_NAMESPACES,
                                     INDEX_NAMESPACES, NAMESPACE_RE,
                                     SOURCE_KINDS, TOOL_DERIVED_KINDS,
                                     MemoryHit, MemoryItem,
                                     MemoryProvenanceError,
                                     MemoryValidationError, UpsertResult,
                                     _CONTROL_RE, _UNSAFE_KEY_RE,
                                     content_hash, normalize_tags,
                                     normalize_text, tokenize)
from src.memory_store.prompt import format_context as prompt_format_context
from src.memory_store.prompt import format_hits as prompt_format_hits
from src.memory_store.prompt import run_forget as prompt_run_forget
from src.memory_store.prompt import run_recall as prompt_run_recall
from src.memory_store.prompt import run_remember as prompt_run_remember
from src.memory_store.tools import handlers as tools_handlers
from src.memory_store.tools import tool_defs as tools_tool_defs


class MemoryStore:
    """统一记忆存储与工具实现。"""

    def __init__(self, root: Path | None = None, audit: AuditLog | None = None,
                 clock=None, touch_interval: float | None = None,
                 namespaces=None, write_index: bool = True):
        self._root = Path(root) if root is not None else None
        self.audit = audit or AuditLog()
        self._clock = clock or time.time
        self.touch_interval = (
            Configs.MEMORY_TOUCH_INTERVAL_SECONDS
            if touch_interval is None else touch_interval)
        self.namespaces = set(namespaces or BUILTIN_NAMESPACES)
        self.write_index = write_index
        self._rlock = threading.RLock()

    # ── 路径与锁 ──

    def root(self) -> Path:
        """惰性解析根目录,便于测试重定向 Configs.WORKDIR。"""
        return self._root or Configs.memory_dir()

    @contextmanager
    def _locked(self):
        """线程可重入锁 + 跨进程文件锁;目录在此处按需创建并收紧权限。"""
        root = ensure_private_dir(self.root())
        with self._rlock:
            with file_lock(root / ".lock"):
                yield root

    def _validate_namespace(self, namespace: str) -> str:
        if not isinstance(namespace, str) or not NAMESPACE_RE.match(namespace):
            raise MemoryValidationError(f"invalid namespace: {namespace!r}")
        if namespace not in self.namespaces:
            raise MemoryValidationError(
                f"namespace '{namespace}' is not enabled "
                f"(allowed: {sorted(self.namespaces)})")
        return namespace

    @staticmethod
    def _validate_key(key) -> str:
        if not isinstance(key, str):
            raise MemoryValidationError("key must be a string")
        value = key.strip()
        if not value or value in (".", "..") or len(value) > 256:
            raise MemoryValidationError("key must be 1-256 chars")
        if "/" in value or "\\" in value:
            raise MemoryValidationError("key cannot contain path separators")
        if _CONTROL_RE.search(value):
            raise MemoryValidationError("key contains control characters")
        return value

    @staticmethod
    def _validate_source(source: str, source_kind: str,
                         namespace: str) -> tuple[str, str]:
        """校验来源;阻止工具原始输出/非技能写入高信任命名空间。"""
        value = redact(str(source or "")).strip()[:200]
        if not value:
            raise MemoryProvenanceError("source is required")

        kind = str(source_kind or "agent").strip()
        if kind not in SOURCE_KINDS:
            raise MemoryProvenanceError(
                f"unknown source_kind '{kind}' (allowed: {sorted(SOURCE_KINDS)})")

        # 防止 Agent 把工具的原始输出（可能包含脏数据或注入攻击）直接固化为
        # “事实（semantic）”或“实体（entity）”。必须是 Agent 总结后的内容
        # （source_kind="agent"）才能写入。过程性记忆只能由技能写入。
        if namespace in {"semantic", "entity"} and kind in TOOL_DERIVED_KINDS:
            raise MemoryProvenanceError(
                "tool output cannot be stored as semantic/entity fact; "
                "summarize it and use source_kind=agent/workflow")
        if namespace == "procedural" and kind != "skill":
            raise MemoryProvenanceError(
                "procedural memory is reserved for skills")
        return value, kind

    @staticmethod
    def _safe_key(key: str) -> str:
        """把任意 key 映射为安全文件名;发生映射时追加哈希避免碰撞。"""
        safe = _UNSAFE_KEY_RE.sub("_", key).strip("._")[:80] or "item"
        if safe != key:
            safe = f"{safe}-{hashlib.sha256(key.encode('utf-8')).hexdigest()[:8]}"
        return safe

    def _item_path(self, namespace: str, key: str) -> Path:
        return self.root() / namespace / f"{self._safe_key(key)}.json"

    def _archive_path(self, root: Path, namespace: str, key: str,
                      now: float) -> Path:
        directory = ensure_private_dir(root / "archive" / namespace)
        name = f"{int(now)}-{self._safe_key(key)}.json"
        path = directory / name
        if path.exists():
            path = directory / (f"{int(now)}-{self._safe_key(key)}-"
                                f"{uuid.uuid4().hex[:8]}.json")
        return path

    def _read_item(self, path: Path) -> MemoryItem | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if isinstance(data, dict) and isinstance(data.get("item"), dict):
            data = data["item"]
        try:
            item = MemoryItem.from_dict(data)
        except (TypeError, ValueError):
            return None
        if not item.content_hash:
            item.content_hash = content_hash(item.content)
        return item

    def _items_unlocked(self, namespaces=None) -> list[MemoryItem]:
        """读取指定命名空间(默认全部)的去重记忆;调用方需持有锁。"""
        if isinstance(namespaces, str):
            namespaces = [namespaces]
        items, seen = [], set()
        for namespace in sorted(namespaces or self.namespaces):
            directory = self.root() / namespace
            if not directory.exists():
                continue
            for path in sorted(directory.glob("*.json")):
                item = self._read_item(path)
                if item is None or item.namespace != namespace:
                    continue
                identity = (item.namespace, item.key)
                if identity in seen:
                    continue
                seen.add(identity)
                items.append(item)
        return items

    def _find_unlocked(self, namespace: str, key: str) -> MemoryItem | None:
        """先走安全文件名直读,未命中再全目录扫描(兼容历史文件名)。"""
        direct = self._read_item(self._item_path(namespace, key))
        if direct is not None and direct.namespace == namespace \
                and direct.key == key:
            return direct
        for item in self._items_unlocked({namespace}):
            if item.key == key:
                return item
        return None

    def _write_item_unlocked(self, item: MemoryItem):
        path = self._item_path(item.namespace, item.key)
        ensure_private_dir(path.parent)
        atomic_write_text(path, json.dumps(item.to_dict(), ensure_ascii=False,
                                          indent=2))

    def _archive_item_unlocked(self, root: Path, item: MemoryItem,
                               reason: str) -> str | None:
        """先持久化归档再删原文件;归档失败则保留原文件。"""
        now = self._clock()
        path = self._archive_path(root, item.namespace, item.key, now)
        payload = {"deleted_at": now, "reason": reason,
                   "item": item.to_dict()}
        try:
            atomic_write_text(path, json.dumps(payload, ensure_ascii=False,
                                               indent=2))
        except OSError as exc:
            Terminal.info("memory", f"archive failed: {exc}", Terminal.RED)
            return None
        try:
            self._item_path(item.namespace, item.key).unlink(missing_ok=True)
        except OSError:
            pass
        self._audit("memory_archive", namespace=item.namespace, key=item.key,
                    version=item.version, content_hash=item.content_hash,
                    reason=reason, archive_path=str(path))
        return str(path)

    # ── upsert ──

    def upsert(self, namespace: str, content: str, key: str | None = None,
               tags=None, source: str = "agent", source_kind: str = "agent",
               source_ref: str = "", confidence: float | None = None,
               expires_at: float | None = None) -> UpsertResult:
        """写入/更新记忆,返回本次是新建、更新还是命中去重。"""
        namespace = self._validate_namespace(namespace)
        raw_content = str(content or "")
        redacted_content = redact(raw_content)
        was_redacted = redacted_content != raw_content
        normalized = normalize_text(redacted_content)
        if not normalized:
            raise MemoryValidationError("content cannot be empty")
        if len(normalized) > Configs.MEMORY_MAX_CONTENT_CHARS:
            normalized = (normalized[:Configs.MEMORY_MAX_CONTENT_CHARS]
                          + "...[truncated]")
        digest = content_hash(normalized)
        if key is None:
            key = f"note:{digest[:12]}"
        key = self._validate_key(key)
        tags = normalize_tags(tags)
        source, source_kind = self._validate_source(
            source, source_kind, namespace)
        source_ref = redact(str(source_ref or ""))[:300]
        confidence = (Configs.MEMORY_DEFAULT_CONFIDENCE
                      if confidence is None else float(confidence))
        confidence = max(0.0, min(1.0, confidence))
        if expires_at is not None:
            expires_at = float(expires_at)
        now = self._clock()

        with self._locked() as root:
            existing = self._find_unlocked(namespace, key)
            matches = [item for item in self._items_unlocked({namespace})
                       if item.content_hash == digest
                       and (existing is None or item.key != existing.key)]

            if existing is not None:
                same = (existing.content_hash == digest
                        and set(existing.tags) == set(tags)
                        and existing.source == source
                        and existing.source_kind == source_kind
                        and existing.source_ref == source_ref
                        and abs(existing.confidence - confidence) < 1e-9
                        and existing.expires_at == expires_at)
                if same:
                    self._audit("memory_upsert", namespace=namespace, key=key,
                                version=existing.version, content_hash=digest,
                                source=source, source_kind=source_kind,
                                created=False, updated=False, deduped=False)
                    return UpsertResult(
                        item=existing, created=False, updated=False,
                        canonical_key=existing.key, redacted=was_redacted)
                merged_tags = sorted(set(existing.tags) | set(tags),
                                     key=str.casefold)
                version = existing.version + 1
                if matches:
                    version = max(version,
                                  max(item.version for item in matches) + 1)
                    for loser in matches:
                        self._archive_item_unlocked(
                            root, loser, "superseded_by_key")
                existing.content = normalized
                existing.tags = merged_tags
                existing.source = source
                existing.source_kind = source_kind
                existing.source_ref = source_ref
                existing.confidence = confidence
                existing.expires_at = expires_at
                existing.content_hash = digest
                existing.version = version
                existing.updated_at = now
                self._write_item_unlocked(existing)
                self._rebuild_index_unlocked(root)
                self._audit("memory_upsert", namespace=namespace, key=key,
                            version=existing.version, content_hash=digest,
                            source=source, source_kind=source_kind,
                            created=False, updated=True,
                            deduped=bool(matches))
                return UpsertResult(
                    item=existing, created=False, updated=True,
                    deduped=bool(matches), canonical_key=existing.key,
                    redacted=was_redacted)

            if matches:
                canonical = max(matches, key=lambda item:
                                (item.version, item.updated_at))
                merged_tags = sorted(set(canonical.tags) | set(tags),
                                     key=str.casefold)
                changed = (set(canonical.tags) != set(merged_tags)
                           or canonical.source != source
                           or canonical.source_kind != source_kind
                           or canonical.source_ref != source_ref
                           or abs(canonical.confidence - confidence) >= 1e-9
                           or canonical.expires_at != expires_at)
                for loser in matches:
                    if loser.key != canonical.key:
                        self._archive_item_unlocked(
                            root, loser, "duplicate_content")
                if changed:
                    canonical.tags = merged_tags
                    canonical.source = source
                    canonical.source_kind = source_kind
                    canonical.source_ref = source_ref
                    canonical.confidence = confidence
                    canonical.expires_at = expires_at
                    canonical.version += 1
                    canonical.updated_at = now
                    self._write_item_unlocked(canonical)
                    self._rebuild_index_unlocked(root)
                self._audit("memory_upsert", namespace=namespace,
                            key=canonical.key, version=canonical.version,
                            content_hash=digest, source=source,
                            source_kind=source_kind, created=False,
                            updated=changed, deduped=True)
                return UpsertResult(
                    item=canonical, created=False, updated=changed,
                    deduped=True, canonical_key=canonical.key,
                    redacted=was_redacted)

            item = MemoryItem(
                namespace=namespace, key=key, content=normalized, tags=tags,
                source=source, source_kind=source_kind,
                source_ref=source_ref, confidence=confidence,
                created_at=now, updated_at=now, last_used_at=now,
                expires_at=expires_at, content_hash=digest, version=1,
                use_count=0)
            self._write_item_unlocked(item)
            self._enforce_capacity_unlocked(root, namespace, keep_key=key)
            self._rebuild_index_unlocked(root)
            self._audit("memory_upsert", namespace=namespace, key=key,
                        version=1, content_hash=digest, source=source,
                        source_kind=source_kind, created=True, updated=False,
                        deduped=False)
            return UpsertResult(item=item, created=True, updated=False,
                                canonical_key=key, redacted=was_redacted)

    def _enforce_capacity_unlocked(self, root: Path, namespace: str,
                                   keep_key: str):
        """命名空间超限时按 (置信度×新近度) 从低到高归档,保留本次写入。"""
        items = self._items_unlocked({namespace})
        limit = Configs.MEMORY_MAX_ITEMS_PER_NAMESPACE
        if limit <= 0 or len(items) <= limit:
            return
        excess = len(items) - limit
        items.sort(key=lambda item: (
            item.confidence * self._recency(item, self._clock()),
            item.last_used_at))
        for item in items:
            if excess <= 0:
                break
            if item.key == keep_key:
                continue
            if self._archive_item_unlocked(root, item, "namespace_full"):
                excess -= 1

    # ── 读取 ──

    def get(self, namespace: str, key: str, touch: bool = False):
        namespace = self._validate_namespace(namespace)
        key = self._validate_key(key)
        item = self._read_item(self._item_path(namespace, key))
        if item is None:
            return None
        if item.expires_at is not None and item.expires_at <= self._clock():
            return None
        if touch:
            return self.touch(namespace, key)
        return item

    def touch(self, namespace: str, key: str):
        namespace = self._validate_namespace(namespace)
        key = self._validate_key(key)
        with self._locked():
            item = self._find_unlocked(namespace, key)
            if item is None:
                return None
            if item.expires_at is not None and item.expires_at <= self._clock():
                return None
            item.last_used_at = self._clock()
            item.use_count += 1
            self._write_item_unlocked(item)
            return item

    def search(self, query: str = "", tags=None, namespaces=None,
               top_k: int | None = None, touch: bool = True) -> list[MemoryHit]:
        """关键词/标签检索;分数 = 相关度 × (0.5 + 0.5×新近度)。"""
        query_tokens = tokenize(query)
        if isinstance(tags, str):
            tags = [part for part in re.split(r"[,，\s]+", tags) if part]
        wanted_tags = {str(tag).strip().casefold()
                       for tag in (tags or []) if str(tag).strip()}
        limit = max(1, min(int(top_k or Configs.MEMORY_TOP_K), 20))
        now = self._clock()
        candidates = []
        for item in self._items_unlocked(
                namespaces or DEFAULT_SEARCH_NAMESPACES):
            if item.expires_at is not None and item.expires_at <= now:
                continue
            item_tags = {tag.casefold() for tag in item.tags}
            if wanted_tags and not wanted_tags <= item_tags:
                continue
            key_cf = item.key.casefold()
            content_cf = item.content.casefold()
            tag_hits = 0
            if query_tokens:
                matched = {token for token in query_tokens
                           if token in key_cf or token in content_cf
                           or any(token in tag for tag in item_tags)}
                if not matched:
                    continue
                key_hits = sum(1 for token in matched if token in key_cf)
                content_hits = min(5, sum(content_cf.count(token)
                                          for token in matched))
                tag_hits = sum(1 for tag in item_tags
                               for token in matched if token in tag)
                keyword = min(1.0, (2 * key_hits + content_hits + tag_hits) / 8)
            else:
                keyword = 0.0
            tag_ratio = (len(wanted_tags & item_tags) / len(wanted_tags)
                         if wanted_tags else 0.0)
            usage = min(item.use_count, 5) / 5.0
            recency = self._recency(item, now)
            relevance = (0.55 * keyword + 0.15 * tag_ratio
                         + 0.20 * item.confidence + 0.10 * usage)
            score = relevance * (0.5 + 0.5 * recency)
            candidates.append(MemoryHit(item, round(score, 6)))
        candidates.sort(key=lambda hit: (
            -hit.score, -hit.item.last_used_at, hit.item.key))
        selected = candidates[:limit]
        if touch and selected:
            self._touch_hits(selected, now)
        return selected

    def _touch_hits(self, hits: list[MemoryHit], now: float):
        """批量 touch 命中项,但受 touch_interval 节流,避免写放大。"""
        with self._locked():
            for hit in hits:
                item = self._find_unlocked(hit.item.namespace, hit.item.key)
                if item is None:
                    continue
                if now - item.last_used_at < self.touch_interval:
                    continue
                item.last_used_at = now
                item.use_count += 1
                hit.item = item
                self._write_item_unlocked(item)

    @staticmethod
    def _recency(item: MemoryItem, now: float) -> float:
        """按半衰期计算新近度,最低保留 0.1 防止旧记忆完全失权。"""
        stamp = max(item.last_used_at, item.updated_at, item.created_at, 0.0)
        age_days = max(0.0, now - stamp) / 86400.0
        half_life = max(Configs.MEMORY_HALF_LIFE_DAYS, 1.0)
        return max(0.1, min(1.0, 0.5 ** (age_days / half_life)))

    # ── 删除与索引 ──

    def forget(self, namespace: str, key: str, reason: str = "forget"):
        namespace = self._validate_namespace(namespace)
        key = self._validate_key(key)
        with self._locked() as root:
            item = self._find_unlocked(namespace, key)
            if item is None:
                return None
            archive = self._archive_item_unlocked(root, item, reason)
            if archive is None:
                return None
            self._rebuild_index_unlocked(root)
            self._audit("memory_forget", namespace=namespace, key=key,
                        version=item.version, content_hash=item.content_hash,
                        reason=reason, archive_path=archive)
            return item

    def expire(self, now: float | None = None) -> list[str]:
        now = self._clock() if now is None else now
        removed = []
        with self._locked() as root:
            for item in self._items_unlocked():
                if item.expires_at is None or item.expires_at > now:
                    continue
                if self._archive_item_unlocked(root, item, "expired") is None:
                    continue
                removed.append(item.key)
                self._audit("memory_expire", namespace=item.namespace,
                            key=item.key, version=item.version,
                            content_hash=item.content_hash)
            if removed:
                self._rebuild_index_unlocked(root)
        return removed

    def rebuild_index(self):
        with self._locked() as root:
            self._rebuild_index_unlocked(root)

    def _rebuild_index_unlocked(self, root: Path):
        """生成 MEMORY.md 指针索引;正文不进入索引,读取仍需 recall。"""
        if not self.write_index:
            return
        items = [item for item in self._items_unlocked(INDEX_NAMESPACES)
                 if item.expires_at is None or item.expires_at > self._clock()]
        items.sort(key=lambda item: (item.namespace, item.key))
        limit = max(1, Configs.MEMORY_INDEX_MAX_ITEMS)
        lines = ["# Memory Index", "",
                 "Auto-generated pointers only. Use recall to read content.", ""]
        for item in items[:limit]:
            tags = ",".join(item.tags[:4]) or "-"
            lines.append(
                f"- [{item.namespace}] {item.key} · v{item.version} · "
                f"conf {item.confidence:.2f} · src {item.source} · tags {tags}")
        if len(items) > limit:
            lines.append(f"... ({len(items) - limit} more)")
        atomic_write_text(root / "MEMORY.md", "\n".join(lines) + "\n")

    # ── 提示词/工具(实现位于 prompt.py / tools.py) ──

    def format_context(self, query: str = "", top_k: int | None = None,
                       namespaces=None, tags=None) -> str:
        return prompt_format_context(self, query=query, top_k=top_k,
                                     namespaces=namespaces, tags=tags)

    def _format_hits(self, hits: list[MemoryHit], header: str) -> str:
        return prompt_format_hits(hits, header)

    def run_remember(self, content: str, key: str | None = None,
                     namespace: str = "semantic", tags=None,
                     source: str = "agent:remember",
                     source_kind: str = "agent",
                     confidence: float | None = None,
                     expires_in_days: float | None = None,
                     source_ref: str = "") -> str:
        return prompt_run_remember(
            self, content, key=key, namespace=namespace, tags=tags,
            source=source, source_kind=source_kind, confidence=confidence,
            expires_in_days=expires_in_days, source_ref=source_ref)

    def run_recall(self, query: str = "", key: str | None = None,
                   namespace: str | None = None, tags=None,
                   top_k: int | None = None) -> str:
        return prompt_run_recall(self, query, key=key, namespace=namespace,
                                 tags=tags, top_k=top_k)

    def run_forget(self, namespace: str = "semantic", key: str | None = None,
                   expired_only: bool = False) -> str:
        return prompt_run_forget(self, namespace=namespace, key=key,
                                 expired_only=expired_only)

    def _audit(self, event: str, **fields):
        if self.audit is not None:
            self.audit.log(event, **fields)

    # ── 工具注册 ──

    def tool_defs(self) -> list[dict]:
        return tools_tool_defs()

    def handlers(self) -> dict:
        return tools_handlers(self)
