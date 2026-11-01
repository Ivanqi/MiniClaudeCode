"""统一记忆子包:数据模型、存储核心、提示词渲染与工具注册。"""

from src.memory_store.models import (BUILTIN_NAMESPACES,
                                     DEFAULT_SEARCH_NAMESPACES,
                                     INDEX_NAMESPACES, NAMESPACE_RE,
                                     SOURCE_KINDS, TOOL_DERIVED_KINDS,
                                     MemoryHit, MemoryItem,
                                     MemoryProvenanceError,
                                     MemoryValidationError, UpsertResult,
                                     content_hash, normalize_tags,
                                     normalize_text, tokenize)
from src.memory_store.store import MemoryStore

# 全局默认实例:路径惰性解析,便于测试重定向 Configs.WORKDIR。
MEMORY_STORE = MemoryStore()

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
