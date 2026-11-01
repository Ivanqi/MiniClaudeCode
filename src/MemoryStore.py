"""兼容入口:MemoryStore 实现已拆分到 src/memory_store/。

保留这个模块是为了不改变 `from src.MemoryStore import ...` 的既有调用。
各功能模块:
- models.py —— MemoryItem/UpsertResult/MemoryHit、规范化与分词
- store.py  —— 路径/锁、upsert、检索、归档与索引
- prompt.py —— 上下文渲染与 remember/recall/forget 实现
- tools.py  —— 工具 Schema 与 handler 映射
"""
from src.memory_store import (BUILTIN_NAMESPACES,
                              DEFAULT_SEARCH_NAMESPACES, INDEX_NAMESPACES,
                              MEMORY_STORE, NAMESPACE_RE, SOURCE_KINDS,
                              TOOL_DERIVED_KINDS, MemoryHit, MemoryItem,
                              MemoryProvenanceError, MemoryStore,
                              MemoryValidationError, UpsertResult,
                              content_hash, normalize_tags, normalize_text,
                              tokenize)

__all__ = [
    "BUILTIN_NAMESPACES",
    "DEFAULT_SEARCH_NAMESPACES",
    "INDEX_NAMESPACES",
    "MEMORY_STORE",
    "MemoryHit",
    "MemoryItem",
    "MemoryProvenanceError",
    "MemoryStore",
    "MemoryValidationError",
    "NAMESPACE_RE",
    "SOURCE_KINDS",
    "TOOL_DERIVED_KINDS",
    "UpsertResult",
    "content_hash",
    "normalize_tags",
    "normalize_text",
    "tokenize",
]
