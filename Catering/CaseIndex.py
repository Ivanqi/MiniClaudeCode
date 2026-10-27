"""案例索引:内存 FTS5/BM25,零外部依赖。

设计边界:
- 索引只负责"文本召回 + 日期/废弃硬过滤",不负责同店/主因/维度/标签加权;
  这些结构化信号由 Retrieval 的 structured 路由处理。
- 中文不做分词依赖:统一切 ASCII 词 + CJK 2-gram,入库与 query 走同一 tokenizer。
- 索引只驻内存,首次检索懒构建;每次 search 前用 content_hash 指纹差分同步,
  upsert/外部文件变更在下次检索自动生效,不引入持久化文件与回调生命周期。
- FTS5 可用时用多列 BM25(字段权重由 bm25(table, w...) 提供);不可用时退化为
  纯 Python 倒排 BM25,接口与排序契约一致,不阻断检索。
"""
from __future__ import annotations

import math
import re
import sqlite3
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable, Sequence

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TOKEN_RE = re.compile(r"[a-z0-9_]+|[一-鿿]+")
_CJK_RUN_RE = re.compile(r"[一-鿿]+")
_TOKEN_SPLIT_RE = re.compile(r"[^\w一-鿿]+")
_DIMENSION_RE = re.compile(r"dimension:([\w一-鿿-]+)")
_STOPWORDS = {"dimension"}

# 索引列及 BM25 字段权重;顺序即 bm25() 的权重顺序。
_INDEX_FIELDS: tuple[tuple[str, float], ...] = (
    ("summary", 3.0),
    ("symptoms", 2.0),
    ("actions", 1.0),
    ("outcome", 1.0),
    ("main_factor", 2.0),
    ("dimensions", 2.0),
    ("tags", 2.0),
)
_LIST_FIELDS = {"symptoms", "actions", "dimensions", "tags"}
_BM25_WEIGHTS = tuple(weight for _, weight in _INDEX_FIELDS)
_REBUILD_MIN_CHANGES = 10
_K1 = 1.5
_B = 0.75

_FTS5_AVAILABLE: bool | None = None


def fold(value) -> str:
    return str(value or "").casefold()


def as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def words(text: str) -> set[str]:
    """LexicalCaseRetriever 兼容的整词视图(保留原 P0 行为)。"""
    return {token for token in _TOKEN_SPLIT_RE.split(fold(text))
            if len(token) >= 2 and token not in _STOPWORDS}


def cjk_bigrams(text: str) -> set[str]:
    grams: set[str] = set()
    for run in _CJK_RUN_RE.findall(fold(text)):
        grams.update(run[i:i + 2] for i in range(len(run) - 1))
    return grams


def query_dimensions(query: str) -> set[str]:
    return {fold(item) for item in _DIMENSION_RE.findall(query)}


@lru_cache(maxsize=16384)
def _search_tokens_cached(text: str) -> tuple[str, ...]:
    folded = text.casefold()
    tokens: list[str] = []
    for match in _TOKEN_RE.finditer(folded):
        piece = match.group(0)
        if piece[0].isascii():
            if len(piece) >= 2 and not piece.isdigit() \
                    and piece not in _STOPWORDS:
                tokens.append(piece)
            continue
        if len(piece) >= 2:
            tokens.extend(piece[i:i + 2] for i in range(len(piece) - 1))
    return tuple(tokens)


def search_tokens(text) -> list[str]:
    """ASCII 词 + CJK 2-gram,保持出现顺序。

    纯数字丢弃(百分比/日期片段噪声大);ASCII 与 CJK 分开切分,避免
    "POS宕机" 混成一个 token。单字 CJK 不产生 2-gram。
    语料中摘要/动作/标签大量重复,按原文缓存 token,避免每次重建索引重复切分。
    """
    return list(_search_tokens_cached(str(text or "")))


def _field_tokens(case: dict, field: str) -> list[str]:
    value = case.get(field)
    values = as_list(value) if field in _LIST_FIELDS else [value]
    tokens: list[str] = []
    for item in values:
        if item in (None, ""):
            continue
        tokens.extend(search_tokens(item))
    return tokens


def indexed_terms(case: dict) -> set[str]:
    """案例全部可检索 token 集合,用于 BM25 原始词命中守卫。"""
    terms: set[str] = set()
    for field, _ in _INDEX_FIELDS:
        terms.update(_field_tokens(case, field))
    return terms


def exclusion_reason(case: dict, date: str = "",
                     include_deprecated: bool = False) -> str | None:
    """返回硬过滤原因,顺序与 LexicalCaseRetriever 完全一致。"""
    if case.get("deprecated") and not include_deprecated:
        return "deprecated"
    case_start = case.get("effective_from") or case.get("date") or ""
    if date and not case_start:
        # 无日期无法证明不晚于告警日,按泄漏风险排除。
        return "undated"
    if date and case_start > date:
        return "future"
    case_end = case.get("effective_to") or ""
    if date and case_end and date > case_end:
        return "expired"
    return None


def _case_signature(case: dict) -> tuple:
    """索引内容指纹;优先 content_hash,直接 fixture 则回退到字段元组。"""
    content_hash = case.get("content_hash")
    if content_hash:
        # 硬过滤字段一并入签名:即使调用方直接改 date/deprecated 而未重算 hash,
        # 下一次 sync 也能更新索引行。
        return ("hash", str(content_hash),
                case.get("effective_from") or case.get("date") or "",
                case.get("effective_to") or "",
                bool(case.get("deprecated")))
    payload: list[tuple] = []
    for field, _ in _INDEX_FIELDS:
        value = case.get(field)
        if field in _LIST_FIELDS:
            payload.append((field, tuple(str(item) for item in as_list(value))))
        else:
            payload.append((field, str(value or "")))
    payload.append(("case_start", case.get("effective_from")
                    or case.get("date") or ""))
    payload.append(("case_end", case.get("effective_to") or ""))
    payload.append(("deprecated", bool(case.get("deprecated"))))
    return ("raw", tuple(payload))


def _fts5_available() -> bool:
    global _FTS5_AVAILABLE
    if _FTS5_AVAILABLE is not None:
        return _FTS5_AVAILABLE
    try:
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("CREATE VIRTUAL TABLE _fts5_probe USING fts5(x)")
        finally:
            conn.close()
        _FTS5_AVAILABLE = True
    except Exception:
        _FTS5_AVAILABLE = False
    return _FTS5_AVAILABLE


@dataclass(frozen=True)
class IndexHit:
    case_id: str
    score: float


@dataclass(frozen=True)
class IndexSyncStats:
    engine: str
    version: int
    doc_count: int
    added: int = 0
    changed: int = 0
    removed: int = 0
    rebuilt: bool = False
    build_ms: float = 0.0


def _meta_eligible(meta: tuple[str, str, bool], date: str,
                   include_deprecated: bool) -> bool:
    case_start, case_end, deprecated = meta
    if deprecated and not include_deprecated:
        return False
    if date:
        if not case_start or case_start > date:
            return False
        if case_end and date > case_end:
            return False
    return True


class _PythonBm25:
    """FTS5 不可用时的纯 Python 倒排 BM25。"""

    def __init__(self):
        self.postings: dict[str, dict[str, float]] = defaultdict(dict)
        self.doc_len: dict[str, float] = {}
        self.doc_terms: dict[str, tuple[str, ...]] = {}
        self.meta: dict[str, tuple[str, str, bool]] = {}
        self.df: dict[str, int] = defaultdict(int)
        self.total_len = 0.0

    def clear(self):
        self.postings.clear()
        self.doc_len.clear()
        self.doc_terms.clear()
        self.meta.clear()
        self.df.clear()
        self.total_len = 0.0

    def add(self, case: dict):
        case_id = str(case.get("case_id"))
        if not case_id:
            return
        self.remove(case_id)
        weighted: dict[str, float] = {}
        doc_len = 0.0
        for field, weight in _INDEX_FIELDS:
            tokens = _field_tokens(case, field)
            doc_len += weight * len(tokens)
            for token in tokens:
                weighted[token] = weighted.get(token, 0.0) + weight
        self.doc_len[case_id] = doc_len
        self.doc_terms[case_id] = tuple(weighted)
        self.meta[case_id] = (
            case.get("effective_from") or case.get("date") or "",
            case.get("effective_to") or "",
            bool(case.get("deprecated")),
        )
        for term, tf in weighted.items():
            self.postings[term][case_id] = tf
            self.df[term] += 1
        self.total_len += doc_len

    def remove(self, case_id: str):
        if case_id not in self.doc_len:
            return
        self.total_len -= self.doc_len.pop(case_id)
        for term in self.doc_terms.pop(case_id, ()):
            postings = self.postings.get(term)
            if postings is None:
                continue
            postings.pop(case_id, None)
            self.df[term] = max(0, self.df.get(term, 1) - 1)
            if not postings:
                self.postings.pop(term, None)
        self.meta.pop(case_id, None)

    def search(self, terms: Sequence[str], *, date: str = "",
               include_deprecated: bool = False,
               limit: int = 50) -> list[IndexHit]:
        size = len(self.doc_len)
        if not size or limit <= 0:
            return []
        avgdl = (self.total_len / size) or 1.0
        candidate_ids: set[str] = set()
        for term in terms:
            candidate_ids.update(self.postings.get(term, {}))
        scored: list[IndexHit] = []
        for case_id in candidate_ids:
            if not _meta_eligible(self.meta[case_id], date,
                                  include_deprecated):
                continue
            doc_len = self.doc_len[case_id]
            score = 0.0
            for term in terms:
                tf = self.postings.get(term, {}).get(case_id)
                if not tf:
                    continue
                df = self.df.get(term, 0)
                idf = math.log(1.0 + (size - df + 0.5) / (df + 0.5))
                score += idf * (tf * (_K1 + 1.0)) / (
                    tf + _K1 * (1.0 - _B + _B * doc_len / avgdl))
            if score > 0:
                scored.append(IndexHit(case_id, score))
        scored.sort(key=lambda hit: (-hit.score, hit.case_id))
        return scored[:limit]


class CaseIndex:
    """内存案例索引:FTS5 优先,纯 Python BM25 兜底。"""

    def __init__(self, *, engine: str = "auto"):
        self._lock = threading.RLock()
        self._version = 0
        self._sigs: dict[str, tuple] = {}
        self._initialized = False
        self._engine = "python"
        self._conn: sqlite3.Connection | None = None
        self._py: _PythonBm25 | None = None
        self._last_build_ms = 0.0
        requested = (engine or "auto").strip().lower()
        use_fts = requested in ("auto", "fts5")
        if use_fts and _fts5_available():
            try:
                self._init_fts()
                self._engine = "fts5"
            except sqlite3.Error:
                self._init_python()
        else:
            self._init_python()

    def _init_fts(self):
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        conn.execute("""
            CREATE VIRTUAL TABLE cases_fts USING fts5(
              summary, symptoms, actions, outcome,
              main_factor, dimensions, tags,
              case_id UNINDEXED, case_start UNINDEXED,
              case_end UNINDEXED, deprecated UNINDEXED,
              tokenize='unicode61 remove_diacritics 2')
        """)
        conn.commit()
        self._conn = conn

    def _init_python(self):
        self._engine = "python"
        self._py = _PythonBm25()

    @property
    def engine(self) -> str:
        return self._engine

    @property
    def version(self) -> int:
        with self._lock:
            return self._version

    @property
    def doc_count(self) -> int:
        with self._lock:
            return len(self._sigs)

    def _fts_row(self, case: dict) -> tuple:
        columns = [" ".join(_field_tokens(case, field))
                   for field, _ in _INDEX_FIELDS]
        return (*columns, str(case.get("case_id")),
                case.get("effective_from") or case.get("date") or "",
                case.get("effective_to") or "",
                int(bool(case.get("deprecated"))))

    def _fts_insert(self, case: dict):
        placeholders = ", ".join("?" for _ in range(len(_INDEX_FIELDS) + 4))
        assert self._conn is not None
        self._conn.execute(
            f"INSERT INTO cases_fts VALUES ({placeholders})",
            self._fts_row(case))

    def _fts_delete(self, case_id: str):
        assert self._conn is not None
        self._conn.execute(
            "DELETE FROM cases_fts WHERE case_id = ?", (case_id,))

    def _rebuild(self, cases: list[dict]):
        if self._engine == "fts5":
            assert self._conn is not None
            with self._conn:
                self._conn.execute("DELETE FROM cases_fts")
                for case in cases:
                    self._fts_insert(case)
        else:
            assert self._py is not None
            self._py.clear()
            for case in cases:
                self._py.add(case)

    def sync(self, cases: Iterable[dict]) -> IndexSyncStats:
        """按 content_hash 差分同步;返回本次构建统计。"""
        with self._lock:
            started = time.perf_counter()
            current: dict[str, dict] = {}
            for case in cases or []:
                case_id = case.get("case_id")
                if case_id:
                    current[str(case_id)] = case
            new_sigs = {case_id: _case_signature(case)
                        for case_id, case in current.items()}
            if self._initialized and new_sigs == self._sigs:
                self._last_build_ms = 0.0
                return IndexSyncStats(self._engine, self._version,
                                      len(self._sigs))
            added = [cid for cid in new_sigs if cid not in self._sigs]
            removed = [cid for cid in self._sigs if cid not in new_sigs]
            changed = [cid for cid in new_sigs
                       if cid in self._sigs
                       and new_sigs[cid] != self._sigs[cid]]
            total_changes = len(added) + len(removed) + len(changed)
            rebuilt = (not self._initialized
                       or total_changes > _REBUILD_MIN_CHANGES)
            if rebuilt:
                self._rebuild(list(current.values()))
            else:
                if self._engine == "fts5":
                    assert self._conn is not None
                    with self._conn:
                        for case_id in removed:
                            self._fts_delete(case_id)
                        for case_id in changed + added:
                            self._fts_delete(case_id)
                            self._fts_insert(current[case_id])
                else:
                    assert self._py is not None
                    for case_id in removed:
                        self._py.remove(case_id)
                    for case_id in changed + added:
                        self._py.add(current[case_id])
            self._sigs = new_sigs
            self._initialized = True
            self._version += 1
            self._last_build_ms = (time.perf_counter() - started) * 1000.0
            return IndexSyncStats(
                self._engine, self._version, len(self._sigs),
                added=len(added), changed=len(changed),
                removed=len(removed), rebuilt=rebuilt,
                build_ms=self._last_build_ms)

    def search(self, terms: Sequence[str], *, date: str = "",
               include_deprecated: bool = False,
               limit: int = 50) -> list[IndexHit]:
        """按词项召回;date/deprecated 硬过滤与 exclusion_reason 一致。"""
        if date and not _DATE_RE.match(date):
            raise ValueError(f"非法日期格式: {date}")
        clean: list[str] = []
        seen: set[str] = set()
        for term in terms or []:
            # 只允许 tokenizer 可能产出的字符,避免 FTS5 运算符/引号注入。
            term = re.sub(r"[^0-9a-z_一-鿿]+", "",
                          str(term or "").casefold())
            if term and term not in seen:
                seen.add(term)
                clean.append(term)
        limit = int(limit)
        if not clean or limit <= 0:
            return []
        with self._lock:
            if self._engine == "fts5":
                return self._search_fts(clean, date, include_deprecated,
                                        limit)
            assert self._py is not None
            return self._py.search(clean, date=date,
                                   include_deprecated=include_deprecated,
                                   limit=limit)

    def _search_fts(self, terms: list[str], date: str,
                    include_deprecated: bool, limit: int) -> list[IndexHit]:
        assert self._conn is not None
        match = " OR ".join(
            '"' + term.replace('"', '""') + '"' for term in terms)
        sql = ("SELECT case_id, bm25(cases_fts, "
               + ", ".join(str(weight) for weight in _BM25_WEIGHTS)
               + ") AS rank_score FROM cases_fts WHERE cases_fts MATCH ?")
        params: list = [match]
        if not include_deprecated:
            sql += " AND deprecated = 0"
        if date:
            sql += (" AND case_start <> '' AND case_start <= ?"
                    " AND (case_end = '' OR case_end >= ?)")
            params.extend((date, date))
        sql += " ORDER BY rank_score ASC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [IndexHit(str(case_id), -float(score))
                for case_id, score in rows]

    def close(self):
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
            self._py = None
            self._sigs.clear()
            self._initialized = False
