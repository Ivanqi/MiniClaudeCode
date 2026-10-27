"""案例检索:CaseRetriever 接口 + 词法/BM25/混合实现。

P0 的边界仍然保留:
- 结构化硬过滤只做 deprecated、告警日不早于案例、生效期过期;
  门店/主因/维度是加权信号,不做硬过滤(跨店经验同样有价值)。
- 同店只加分,纯同店无主题信号不召回。
- 打分保持确定性、可解释:每项信号进入 score_breakdown,便于检索日志审计。

P1 混合检索(默认关闭,`CASE_RETRIEVAL_MODE=lexical` 保持原行为):
- BM25 路由:Catering/CaseIndex.py 的内存 FTS5/纯 Python 倒排;
- structured 路由:主因/维度/标签/同店/时间衰减/confidence,不做文本重叠;
- RRF 融合后做 lexical 主导的 heuristic rerank(0.75 lexical + 0.25 RRF);
- 索引/路由异常或超时降级到 LexicalCaseRetriever,合法无匹配仍显式返回 0。
- query expansion 只做紧同义词,且扩展词不能单独召回文档(必须命中原始词),
  避免 台风预警/POS宕机 这类负例被同义扩展放大。
"""
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, Sequence

from Catering.CaseIndex import (
    CaseIndex,
    IndexSyncStats,
    as_list as _as_list,
    cjk_bigrams as _cjk_bigrams,
    exclusion_reason,
    fold as _fold,
    indexed_terms,
    query_dimensions as _query_dimensions,
    search_tokens,
    words as _words,
)
from Catering.Cases import case_search_decay
from Catering.Configs import CateringConfigs

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_MATCH_FIELDS = (
    "case_id", "store_id", "date", "main_factor", "dimensions", "summary",
    "actions", "tags", "symptoms", "metrics_snapshot", "outcome",
    "effective_from", "effective_to", "deprecated", "version",
    "updated_at", "source", "confidence",
)
_CANDIDATE_FIELDS = (
    "case_id", "store_id", "date", "main_factor", "dimensions", "tags",
    "confidence", "version", "updated_at", "source", "deprecated",
)

_QUERY_MAX_TERMS = 48
_QUERY_MAX_EXPANSIONS = 10

# surface form -> 索引里的规范词。只有 query 中出现 surface form 才扩展;
# 不做"台风 -> 暴雨"这类关联扩展,避免负例查询被放大。
_SYNONYM_GROUPS: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("traffic", ("客流", "到店", "进店"), ("traffic",)),
    ("ticket", ("客单价", "客单"), ("ticket",)),
    ("revenue", ("营业额", "营收", "销售额"), ("revenue",)),
    ("weather", ("暴雨", "降雨", "大雨", "天气"), ("weather", "external")),
    ("competitor", ("竞对", "竞品", "竞争对手"),
     ("competitor", "external")),
    ("promotion", ("促销", "活动", "优惠券", "折扣"), ("promotion",)),
    ("rating", ("评分", "差评", "口碑"), ("rating",)),
    ("staff", ("排班", "人员", "出勤", "员工"), ("staff",)),
    ("dish", ("菜品", "菜单", "品类"), ("dish",)),
    ("channels", ("渠道", "外卖", "堂食"), ("channels",)),
    ("periods", ("时段", "午市", "晚市"), ("periods",)),
)


def _strip_store_id(query: str, store_id: str) -> str:
    """门店 ID 是结构化参数,不作为文本主题信号参与匹配。

    否则 query 里出现 "S05店" 时,该 token 会命中所有 S05店 案例摘要,
    让"纯同店无主题"查询被误召回。
    """
    if not store_id:
        return query
    return re.sub(re.escape(str(store_id)), " ", query,
                  flags=re.IGNORECASE)


def _case_text(case: dict) -> str:
    """参与词法重叠的案例文本(摘要/症状/动作/标签/结论)。"""
    parts = [case.get("summary"), case.get("outcome"),
             case.get("main_factor")]
    for key in ("symptoms", "tags", "actions", "dimensions"):
        parts.extend(_as_list(case.get(key)))
    return _fold(" ".join(str(part) for part in parts if part))


def _metadata_breakdown(case: dict, *, query: str, query_words: set[str],
                        dimensions: set[str], store_folded: str,
                        factor_folded: str,
                        folded_query: str | None = None) -> dict:
    """主因/维度/标签/同店信号;Lexical 与 structured 路由共用。"""
    tags = [str(tag) for tag in _as_list(case.get("tags")) if tag]
    matched_dimensions = sorted(
        {_fold(item) for item in _as_list(case.get("dimensions"))}
        & dimensions)
    if folded_query is None:
        folded_query = _fold(query)
    breakdown = {
        "store": 1.0 if store_folded and _fold(
            case.get("store_id")) == store_folded else 0.0,
        "main_factor": 0.0,
        "dimensions": min(2.0, 1.0 * len(matched_dimensions)),
        "tags": sum(
            (1.0 if _fold(tag) in folded_query else 0.0)
            + (0.5 if _fold(tag) in query_words else 0.0)
            for tag in tags),
    }
    case_factor = _fold(case.get("main_factor"))
    if (factor_folded and factor_folded != "none"
            and case_factor == factor_folded):
        breakdown["main_factor"] = 2.0
    elif case_factor and case_factor in query_words:
        breakdown["main_factor"] = 2.0
    return breakdown


def _text_overlap(case: dict, query_words: set[str],
                  query_grams: set[str]) -> float:
    text = _case_text(case)
    word_hits = query_words & set(_words(text))
    gram_hits = query_grams & _cjk_bigrams(text)
    return (min(2.0, 0.5 * len(word_hits))
            + min(1.5, 0.2 * len(gram_hits)))


def _score_case(case: dict, *, query: str, query_words: set[str],
                query_grams: set[str], dimensions: set[str],
                store_folded: str, factor_folded: str, now: float,
                include_text: bool = True,
                folded_query: str | None = None) -> dict | None:
    """单案例打分;返回 None 表示无主题信号,不召回。"""
    if folded_query is None:
        folded_query = _fold(query)
    breakdown = _metadata_breakdown(
        case, query=query, query_words=query_words,
        dimensions=dimensions, store_folded=store_folded,
        factor_folded=factor_folded, folded_query=folded_query)
    breakdown["text_overlap"] = (
        _text_overlap(case, query_words, query_grams)
        if include_text else 0.0)
    base = sum(breakdown.get(key, 0.0) for key in
               ("store", "main_factor", "dimensions", "tags",
                "text_overlap"))
    # 纯同店不足以构成召回:必须有主题信号(主因/维度/标签/文本)。
    if base - breakdown["store"] <= 0:
        return None
    recency = case_search_decay(case, now)
    confidence = max(0.0, min(1.0,
                               float(case.get("confidence", 0.5) or 0.5)))
    score = base * (0.75 + 0.25 * recency) + 0.25 * confidence
    return {
        "case": case,
        "score": score,
        "score_breakdown": {
            **breakdown, "base": base,
            "recency": recency, "confidence": confidence},
    }


def _project_item(item: dict, fields: tuple) -> dict:
    case = item["case"]
    return {key: case.get(key) for key in fields} | {
        "score": item.get("score", 0.0),
        "score_breakdown": dict(item.get("score_breakdown") or {}),
    }


def _dedupe(values) -> list:
    seen: set = set()
    result = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _join_reason(*reasons) -> str | None:
    parts = [str(reason) for reason in reasons if reason]
    return ";".join(parts) or None


class CaseRetriever(ABC):
    """案例检索接口:业务层只依赖此协议。"""

    @abstractmethod
    def search(self, query: str, *, store_id: str = "", date: str = "",
               main_factor: str = "", top_k: int = 3,
               include_deprecated: bool = False) -> dict:
        """返回 {match_count, matches, candidates, filters, ...}。"""


class LexicalCaseRetriever(CaseRetriever):
    """确定性词法 + 元数据加权检索(零依赖,可解释,可离线评测)。"""

    def __init__(self, cases_provider: Callable[[], list[dict]],
                 clock=None, max_candidates: int | None = None):
        self._cases_provider = cases_provider
        self._clock = clock or time.time
        self._max_candidates = (max_candidates
                                or CateringConfigs.CASE_SEARCH_CANDIDATES)

    def search(self, query: str, *, store_id: str = "", date: str = "",
               main_factor: str = "", top_k: int = 3,
               include_deprecated: bool = False) -> dict:
        query = str(query or "")
        if date and not _DATE_RE.match(date):
            raise ValueError(f"非法日期格式: {date}")
        top_k = 3 if top_k is None else int(top_k)
        matching_query = _strip_store_id(query, store_id)
        store_folded = _fold(store_id)
        factor_folded = _fold(main_factor)
        query_words = _words(matching_query)
        query_grams = _cjk_bigrams(matching_query)
        dimensions = _query_dimensions(matching_query)
        folded_query = _fold(matching_query)
        now = self._clock()

        excluded = {"deprecated": 0, "future": 0, "expired": 0,
                    "undated": 0}
        scored: list[dict] = []
        cases = list(self._cases_provider() or [])
        for case in cases:
            reason = exclusion_reason(case, date, include_deprecated)
            if reason:
                excluded[reason] += 1
                continue
            item = _score_case(
                case, query=matching_query, query_words=query_words,
                query_grams=query_grams, dimensions=dimensions,
                store_folded=store_folded, factor_folded=factor_folded,
                now=now, folded_query=folded_query)
            if item is not None:
                scored.append(item)

        scored.sort(key=lambda item: (
            item["score"], item["case"].get("updated_at") or 0,
            item["case"].get("case_id") or ""), reverse=True)
        matches = [_project_item(item, _MATCH_FIELDS)
                   for item in scored[:max(0, top_k)]]
        candidates = [_project_item(item, _CANDIDATE_FIELDS)
                      for item in scored[:self._max_candidates]]
        return {
            "store_id": store_id, "date": date, "main_factor": main_factor,
            "query": query, "symptom": query,
            "match_count": len(scored), "matches": matches,
            "candidates": candidates,
            "filters": {
                "date_max": date or None,
                "include_deprecated": include_deprecated,
                "total_cases": len(cases),
                "excluded": excluded,
            },
            "missing_fields": [],
        }

    @staticmethod
    def _project(item: dict, fields: tuple) -> dict:
        return _project_item(item, fields)


@dataclass(frozen=True)
class QueryPlan:
    query: str
    original_terms: tuple[str, ...]
    expanded_terms: tuple[str, ...]
    terms: tuple[str, ...]
    dimensions: frozenset
    main_factor: str
    query_words: frozenset
    query_grams: frozenset
    concepts: tuple[str, ...]


def analyze_query(query: str, main_factor: str = "") -> QueryPlan:
    """构造 BM25/结构化查询;main_factor 只做结构化信号,不注入文本。"""
    query = str(query or "")
    folded_query = _fold(query)
    original = _dedupe(search_tokens(query))[:_QUERY_MAX_TERMS]
    original_set = set(original)
    expanded: list[str] = []
    concepts: list[str] = []
    for name, surfaces, additions in _SYNONYM_GROUPS:
        if not any(surface in folded_query for surface in surfaces):
            continue
        concepts.append(name)
        for term in additions:
            if term not in original_set and term not in expanded:
                expanded.append(term)
    expanded = expanded[:_QUERY_MAX_EXPANSIONS]
    terms = tuple(original) + tuple(expanded)
    return QueryPlan(
        query=query,
        original_terms=tuple(original),
        expanded_terms=tuple(expanded),
        terms=terms,
        dimensions=frozenset(_query_dimensions(query)),
        main_factor=_fold(main_factor),
        query_words=frozenset(_words(query)),
        query_grams=frozenset(_cjk_bigrams(query)),
        concepts=tuple(concepts),
    )


def _route_items(route: str, items: list[dict]) -> list[dict]:
    for rank, item in enumerate(items, 1):
        item["rank"] = rank
        breakdown = item.setdefault("score_breakdown", {})
        breakdown[f"{route}"] = item["score"]
        breakdown[f"{route}_rank"] = rank
    return items


class Bm25CaseRetriever(CaseRetriever):
    """BM25 路由:索引文本召回,硬过滤与 Lexical 一致。"""

    def __init__(self, cases_provider: Callable[[], list[dict]],
                 *, index: CaseIndex | None = None, clock=None,
                 max_candidates: int | None = None):
        self._cases_provider = cases_provider
        self._clock = clock or time.time
        self._index = index or CaseIndex()
        self._max_candidates = (max_candidates
                                or CateringConfigs.CASE_SEARCH_CANDIDATES)

    def close(self):
        self._index.close()

    def rank(self, cases: list[dict], eligible: list[dict],
             plan: QueryPlan, *, date: str = "",
             include_deprecated: bool = False, limit: int = 50,
             sync: bool = True) -> tuple[list[dict], IndexSyncStats | None, int]:
        """返回 (top 命中, 索引统计, 被原始词守卫丢弃的扩展词命中数)。"""
        stats = self._index.sync(cases) if sync else None
        limit = max(0, int(limit))
        original = set(plan.original_terms)
        if not original or limit == 0:
            return [], stats, 0
        # 多取一些,抵消 expansion-only 命中被守卫丢弃后的缺口。
        fetch_limit = min(max(limit * 2, limit + 10), 200)
        hits = self._index.search(
            plan.terms, date=date,
            include_deprecated=include_deprecated, limit=fetch_limit)
        by_id = {case.get("case_id"): case for case in eligible
                 if case.get("case_id")}
        ranked: list[dict] = []
        dropped = 0
        for hit in hits:
            case = by_id.get(hit.case_id)
            if case is None:
                continue
            # 扩展词只能改善原始词命中的排序,不能单独召新文档。
            if not (indexed_terms(case) & original):
                dropped += 1
                continue
            ranked.append({
                "case": case,
                "score": hit.score,
                "score_breakdown": {"bm25": hit.score},
            })
            if len(ranked) >= limit:
                break
        return _route_items("bm25", ranked), stats, dropped

    def search(self, query: str, *, store_id: str = "", date: str = "",
               main_factor: str = "", top_k: int = 3,
               include_deprecated: bool = False) -> dict:
        query = str(query or "")
        if date and not _DATE_RE.match(date):
            raise ValueError(f"非法日期格式: {date}")
        top_k = 3 if top_k is None else int(top_k)
        started = time.perf_counter()
        cases = list(self._cases_provider() or [])
        eligible: list[dict] = []
        excluded = {"deprecated": 0, "future": 0, "expired": 0,
                    "undated": 0}
        for case in cases:
            reason = exclusion_reason(case, date, include_deprecated)
            if reason:
                excluded[reason] += 1
            else:
                eligible.append(case)
        plan = analyze_query(_strip_store_id(query, store_id), main_factor)
        ranked, stats, dropped = self.rank(
            cases, eligible, plan, date=date,
            include_deprecated=include_deprecated,
            limit=self._max_candidates)
        latency = (time.perf_counter() - started) * 1000.0
        matches = [_project_item(item, _MATCH_FIELDS)
                   for item in ranked[:max(0, top_k)]]
        candidates = [_project_item(item, _CANDIDATE_FIELDS)
                      for item in ranked[:self._max_candidates]]
        return {
            "store_id": store_id, "date": date,
            "main_factor": main_factor, "query": query, "symptom": query,
            "match_count": len(ranked), "matches": matches,
            "candidates": candidates,
            "filters": {
                "date_max": date or None,
                "include_deprecated": include_deprecated,
                "total_cases": len(cases),
                "eligible_cases": len(eligible),
                "excluded": excluded,
            },
            "missing_fields": [],
            "retrieval": {
                "mode": "bm25",
                "engine": self._index.engine,
                "index_version": self._index.version,
                "doc_count": self._index.doc_count,
                "route_counts": {"bm25": len(ranked)},
                "query_terms": list(plan.terms),
                "original_terms": list(plan.original_terms),
                "expanded_terms": list(plan.expanded_terms),
                "concepts": list(plan.concepts),
                "dropped_expanded_only": dropped,
                "index_rebuilt": bool(stats and stats.rebuilt),
                "cold_build_ms": (stats.build_ms if stats else 0.0),
                "latency_ms": latency,
                "degraded": False,
                "degraded_reason": None,
            },
            "degraded": False,
            "latency_ms": latency,
        }


class StructuredRanker:
    """只看元数据的召回路由:主因/维度/标签/同店/时间衰减/confidence。"""

    def rank(self, cases: list[dict], plan: QueryPlan, *,
             store_id: str = "", now: float | None = None,
             limit: int = 50) -> list[dict]:
        now = time.time() if now is None else now
        store_folded = _fold(store_id)
        folded_query = _fold(plan.query)
        query_words = set(plan.query_words)
        query_grams = set(plan.query_grams)
        dimensions = set(plan.dimensions)
        scored: list[dict] = []
        for case in cases:
            item = _score_case(
                case, query=plan.query, query_words=query_words,
                query_grams=query_grams, dimensions=dimensions,
                store_folded=store_folded,
                factor_folded=plan.main_factor, now=now,
                include_text=False, folded_query=folded_query)
            if item is not None:
                scored.append(item)
        scored.sort(key=lambda item: (
            item["score"], item["case"].get("updated_at") or 0,
            item["case"].get("case_id") or ""), reverse=True)
        ranked = scored[:max(0, int(limit))]
        return _route_items("structured", ranked)


def rrf_fuse(routes: dict[str, Sequence[dict]], *, k: int = 60,
             weights: dict[str, float] | None = None) -> list[dict]:
    """Reciprocal Rank Fusion;不做任何原始分数/元数据 boost。"""
    k = max(1, int(k))
    weights = weights or {}
    scores: dict[str, float] = {}
    details: dict[str, dict] = {}
    for route, items in routes.items():
        weight = float(weights.get(route, 1.0))
        seen: set[str] = set()
        for index, item in enumerate(items, 1):
            case = item.get("case") or {}
            case_id = case.get("case_id") or item.get("case_id")
            if not case_id or case_id in seen:
                continue
            seen.add(case_id)
            rank = int(item.get("rank") or index)
            contribution = weight / (k + rank)
            scores[case_id] = scores.get(case_id, 0.0) + contribution
            details.setdefault(case_id, {})[route] = {
                "rank": rank,
                "score": item.get("score"),
                "rrf": contribution,
            }
    fused = [
        {"case_id": case_id, "rrf": score, "routes": details[case_id]}
        for case_id, score in scores.items()
    ]
    fused.sort(key=lambda item: (-item["rrf"], item["case_id"]))
    return fused


class HeuristicReranker:
    """零成本重排:复用 lexical 全分,min-max 后 lexical 主导。"""

    def __init__(self, *, lexical_weight: float = 0.75,
                 rrf_weight: float = 0.25):
        self.lexical_weight = lexical_weight
        self.rrf_weight = rrf_weight

    @staticmethod
    def _route_breakdown(fused: dict) -> dict:
        breakdown = {"rrf": fused["rrf"]}
        for route, info in (fused.get("routes") or {}).items():
            breakdown[route] = info.get("score")
            breakdown[f"{route}_rank"] = info.get("rank")
        return breakdown

    def rerank(self, fused: list[dict], *, plan: QueryPlan,
               store_id: str = "", now: float | None = None) -> list[dict]:
        now = time.time() if now is None else now
        store_folded = _fold(store_id)
        folded_query = _fold(plan.query)
        query_words = set(plan.query_words)
        query_grams = set(plan.query_grams)
        dimensions = set(plan.dimensions)
        items: list[dict] = []
        for entry in fused:
            case = entry.get("case")
            if not case:
                continue
            scored = _score_case(
                case, query=plan.query, query_words=query_words,
                query_grams=query_grams, dimensions=dimensions,
                store_folded=store_folded,
                factor_folded=plan.main_factor, now=now,
                include_text=True, folded_query=folded_query)
            if scored is None:
                continue
            merged = dict(entry)
            merged["case"] = case
            merged["lexical"] = scored["score"]
            merged["score_breakdown"] = {
                **scored["score_breakdown"],
                **self._route_breakdown(entry),
            }
            items.append(merged)
        if not items:
            return []

        lexical_values = [item["lexical"] for item in items]
        rrf_values = [item["rrf"] for item in items]
        lex_min, lex_max = min(lexical_values), max(lexical_values)
        rrf_min, rrf_max = min(rrf_values), max(rrf_values)

        def normalize(value: float, low: float, high: float) -> float:
            return 0.0 if high <= low else (value - low) / (high - low)

        for item in items:
            item["lexical_norm"] = normalize(
                item["lexical"], lex_min, lex_max)
            item["rrf_norm"] = normalize(item["rrf"], rrf_min, rrf_max)
            item["rerank"] = (self.lexical_weight * item["lexical_norm"]
                              + self.rrf_weight * item["rrf_norm"])
            item["score"] = item["rerank"]
            item["score_breakdown"].update({
                "lexical": item["lexical"],
                "lexical_norm": item["lexical_norm"],
                "rrf_norm": item["rrf_norm"],
                "rerank": item["rerank"],
                "score_kind": "rerank",
            })
        items.sort(key=lambda item: (
            -item["rerank"], -item["rrf"], -item["lexical"],
            item["case"].get("case_id") or ""))
        return items


class HybridCaseRetriever(CaseRetriever):
    """BM25 + structured 两路召回 → RRF → heuristic rerank。"""

    def __init__(self, cases_provider: Callable[[], list[dict]],
                 *, clock=None, mode: str = "hybrid",
                 rerank_mode: str | None = None,
                 index: CaseIndex | None = None,
                 max_candidates: int | None = None,
                 timeout_ms: int | None = None,
                 rrf_k: int | None = None):
        self._cases_provider = cases_provider
        self._clock = clock or time.time
        self._mode = (mode or "hybrid").strip().lower()
        self._rerank_mode = (
            (rerank_mode if rerank_mode is not None
             else CateringConfigs.CASE_RERANK_MODE) or "off").strip().lower()
        if self._rerank_mode not in CateringConfigs.CASE_RERANK_MODES:
            self._rerank_mode = "off"
        self._max_candidates = (max_candidates
                                or CateringConfigs.CASE_SEARCH_CANDIDATES)
        self._per_route = max(1, min(
            CateringConfigs.CASE_RECALL_PER_ROUTE, self._max_candidates))
        self._timeout_ms = int(
            timeout_ms if timeout_ms is not None
            else CateringConfigs.CASE_RETRIEVAL_TIMEOUT_MS)
        self._rrf_k = int(rrf_k or CateringConfigs.CASE_FUSION_RRF_K)
        self._index = index or CaseIndex()
        self._bm25 = Bm25CaseRetriever(
            cases_provider, index=self._index, clock=self._clock,
            max_candidates=self._max_candidates)
        self._structured = StructuredRanker()
        self._reranker = HeuristicReranker()
        self._fallback = LexicalCaseRetriever(
            cases_provider, clock=self._clock,
            max_candidates=self._max_candidates)
        self._first_sync = True

    def warmup(self):
        """预加载案例并构建索引;把冷启动成本移到 Agent 初始化。

        混合模式在 DataHub 构造时调用,保证交互式首次检索也在预算内;
        默认 lexical 模式不触发,不改变原有启动开销。
        """
        try:
            cases = list(self._cases_provider() or [])
            self._index.sync(cases)
            self._first_sync = False
        except Exception:
            # 预热失败不影响后续检索:首次 search 会重试或降级 lexical。
            pass

    def close(self):
        self._index.close()

    def search(self, query: str, *, store_id: str = "", date: str = "",
               main_factor: str = "", top_k: int = 3,
               include_deprecated: bool = False) -> dict:
        query = str(query or "")
        if date and not _DATE_RE.match(date):
            raise ValueError(f"非法日期格式: {date}")
        top_k = 3 if top_k is None else int(top_k)
        started = time.perf_counter()
        cases = list(self._cases_provider() or [])
        eligible: list[dict] = []
        excluded = {"deprecated": 0, "future": 0, "expired": 0,
                    "undated": 0}
        for case in cases:
            reason = exclusion_reason(case, date, include_deprecated)
            if reason:
                excluded[reason] += 1
            else:
                eligible.append(case)

        plan = analyze_query(_strip_store_id(query, store_id), main_factor)
        now = self._clock()
        route_latencies: dict[str, float] = {}
        route_errors: list[str] = []
        structured: list[dict] = []
        bm25: list[dict] = []
        dropped = 0
        sync_stats: IndexSyncStats | None = None

        try:
            sync_stats = self._index.sync(cases)
        except Exception as exc:  # 索引不可用:整体降级 lexical
            return self._fallback_payload(
                query, store_id=store_id, date=date,
                main_factor=main_factor, top_k=top_k,
                include_deprecated=include_deprecated,
                reason=f"index_error:{exc}", started=started)

        first_sync = self._first_sync
        self._first_sync = False

        route_started = time.perf_counter()
        try:
            structured = self._structured.rank(
                eligible, plan, store_id=store_id, now=now,
                limit=self._per_route)
        except Exception as exc:
            route_errors.append(f"structured_error:{exc}")
        route_latencies["structured"] = (
            time.perf_counter() - route_started) * 1000.0

        route_started = time.perf_counter()
        try:
            bm25, _, dropped = self._bm25.rank(
                cases, eligible, plan, date=date,
                include_deprecated=include_deprecated,
                limit=self._per_route, sync=False)
        except Exception as exc:
            route_errors.append(f"bm25_error:{exc}")
        route_latencies["bm25"] = (
            time.perf_counter() - route_started) * 1000.0

        if not structured and not bm25:
            if route_errors:
                return self._fallback_payload(
                    query, store_id=store_id, date=date,
                    main_factor=main_factor, top_k=top_k,
                    include_deprecated=include_deprecated,
                    reason=";".join(route_errors), started=started)
            retrieval = self._retrieval_meta(
                plan, route_latencies, route_errors, bm25, structured,
                dropped, fused_count=0, sync_stats=sync_stats,
                first_sync=first_sync, degraded_reason=None)
            return self._payload(
                query, store_id=store_id, date=date,
                main_factor=main_factor, top_k=top_k,
                ranked=[], cases=cases, eligible_count=len(eligible),
                excluded=excluded, started=started,
                retrieval=retrieval, include_deprecated=include_deprecated)

        fused = rrf_fuse(
            {"bm25": bm25, "structured": structured}, k=self._rrf_k)
        case_by_id = {case.get("case_id"): case for case in eligible
                      if case.get("case_id")}
        fused = [
            {**entry, "case": case_by_id[entry["case_id"]]}
            for entry in fused if entry["case_id"] in case_by_id
        ]

        degraded_reason = ";".join(route_errors) or None
        deadline = started + max(1, self._timeout_ms) / 1000.0
        if self._rerank_mode == "heuristic" and fused:
            if time.perf_counter() > deadline:
                degraded_reason = _join_reason(
                    degraded_reason, "rerank_skipped_timeout")
                ranked = [self._fused_item(entry) for entry in fused]
            else:
                rerank_n = max(1, int(CateringConfigs.CASE_RERANK_TOP_N))
                try:
                    ranked = self._reranker.rerank(
                        fused[:rerank_n], plan=plan, store_id=store_id,
                        now=now)
                except Exception as exc:
                    degraded_reason = _join_reason(
                        degraded_reason, f"rerank_error:{exc}")
                    ranked = []
                if not ranked:
                    degraded_reason = _join_reason(
                        degraded_reason, "rerank_empty_fallback")
                    ranked = [self._fused_item(entry) for entry in fused]
        else:
            ranked = [self._fused_item(entry) for entry in fused]

        retrieval = self._retrieval_meta(
            plan, route_latencies, route_errors, bm25, structured,
            dropped, fused_count=len(fused), sync_stats=sync_stats,
            first_sync=first_sync, degraded_reason=degraded_reason)
        return self._payload(
            query, store_id=store_id, date=date,
            main_factor=main_factor, top_k=top_k,
            ranked=ranked, cases=cases, eligible_count=len(eligible),
            excluded=excluded, started=started,
            retrieval=retrieval, include_deprecated=include_deprecated)

    @staticmethod
    def _fused_item(entry: dict) -> dict:
        breakdown = {"rrf": entry["rrf"], "score_kind": "rrf"}
        for route, info in (entry.get("routes") or {}).items():
            breakdown[route] = info.get("score")
            breakdown[f"{route}_rank"] = info.get("rank")
        return {
            **entry,
            "score": entry["rrf"],
            "score_breakdown": breakdown,
        }

    def _retrieval_meta(self, plan: QueryPlan, route_latencies: dict,
                        route_errors: list[str], bm25: list[dict],
                        structured: list[dict], dropped: int, *,
                        fused_count: int,
                        sync_stats: IndexSyncStats | None,
                        first_sync: bool,
                        degraded_reason: str | None) -> dict:
        return {
            "mode": "hybrid",
            "rerank_mode": (self._rerank_mode
                            if self._rerank_mode == "heuristic" else "off"),
            "engine": "unknown" if sync_stats is None else sync_stats.engine,
            "index_version": None if sync_stats is None
            else sync_stats.version,
            "doc_count": None if sync_stats is None
            else sync_stats.doc_count,
            "route_counts": {"bm25": len(bm25),
                             "structured": len(structured)},
            "route_latencies_ms": dict(route_latencies),
            "query_terms": list(plan.terms),
            "original_terms": list(plan.original_terms),
            "expanded_terms": list(plan.expanded_terms),
            "concepts": list(plan.concepts),
            "dropped_expanded_only": dropped,
            "rrf_k": self._rrf_k,
            "fused_count": fused_count,
            "index_rebuilt": bool(sync_stats and sync_stats.rebuilt),
            "cold_build_ms": (sync_stats.build_ms
                              if first_sync and sync_stats else 0.0),
            "degraded": bool(degraded_reason),
            "degraded_reason": degraded_reason,
            "route_errors": list(route_errors),
        }

    def _payload(self, query: str, *, store_id: str, date: str,
                 main_factor: str, top_k: int, ranked: list[dict],
                 cases: list[dict], eligible_count: int, excluded: dict,
                 started: float, retrieval: dict,
                 include_deprecated: bool) -> dict:
        latency = (time.perf_counter() - started) * 1000.0
        retrieval = dict(retrieval)
        retrieval["latency_ms"] = latency
        retrieval["degraded"] = bool(retrieval.get("degraded"))
        matches = [_project_item(item, _MATCH_FIELDS)
                   for item in ranked[:max(0, top_k)]]
        candidates = [_project_item(item, _CANDIDATE_FIELDS)
                      for item in ranked[:self._max_candidates]]
        return {
            "store_id": store_id, "date": date,
            "main_factor": main_factor, "query": query, "symptom": query,
            "match_count": len(ranked), "matches": matches,
            "candidates": candidates,
            "filters": {
                "date_max": date or None,
                "include_deprecated": include_deprecated,
                "total_cases": len(cases),
                "eligible_cases": eligible_count,
                "excluded": excluded,
            },
            "missing_fields": [],
            "retrieval": retrieval,
            "degraded": retrieval["degraded"],
            "latency_ms": latency,
        }

    def _fallback_payload(self, query: str, *, store_id: str, date: str,
                          main_factor: str, top_k: int,
                          include_deprecated: bool, reason: str,
                          started: float) -> dict:
        result = self._fallback.search(
            query, store_id=store_id, date=date,
            main_factor=main_factor, top_k=top_k,
            include_deprecated=include_deprecated)
        latency = (time.perf_counter() - started) * 1000.0
        result = dict(result)
        result["degraded"] = True
        result["latency_ms"] = latency
        result["retrieval"] = {
            "mode": "lexical_fallback",
            "engine": "lexical",
            "route_counts": {},
            "latency_ms": latency,
            "degraded": True,
            "degraded_reason": reason,
        }
        return result


def build_case_retriever(cases_provider: Callable[[], list[dict]],
                         *, mode: str | None = None,
                         rerank_mode: str | None = None,
                         clock=None) -> CaseRetriever:
    """按配置构建检索器;默认/非法配置回落 lexical。"""
    selected = (mode if mode is not None
                else CateringConfigs.CASE_RETRIEVAL_MODE)
    selected = (selected or "lexical").strip().lower()
    if selected == "hybrid":
        return HybridCaseRetriever(
            cases_provider, clock=clock, mode="hybrid",
            rerank_mode=rerank_mode)
    return LexicalCaseRetriever(cases_provider, clock=clock)
