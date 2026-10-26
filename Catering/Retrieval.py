"""案例检索:CaseRetriever 接口 + 确定性结构化/词法实现。

P0 的边界:
- 结构化硬过滤只做授权范围(由 ToolGuard)、deprecated、告警日不早于案例、
  生效期过期;门店/主因/维度是加权信号,不做硬过滤(跨店经验同样有价值)。
- 同店只加分,纯同店无主题信号不召回,避免"只要是本店案例就算匹配"。
- 打分保持确定性、可解释:每项信号进入 score_breakdown,便于检索日志审计。
- 后续换 BM25/embedding/rerank 只需新增 CaseRetriever 实现,业务层不动。
"""
import re
import time
from abc import ABC, abstractmethod
from typing import Callable

from Catering.Cases import case_search_decay
from Catering.Configs import CateringConfigs

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TOKEN_SPLIT_RE = re.compile(r"[^\w一-鿿]+")
_CJK_RUN_RE = re.compile(r"[一-鿿]+")
_DIMENSION_RE = re.compile(r"dimension:([\w一-鿿-]+)")
_STOPWORDS = {"dimension"}

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


def _fold(value) -> str:
    return str(value or "").casefold()


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _words(text: str) -> set[str]:
    return {token for token in _TOKEN_SPLIT_RE.split(_fold(text))
            if len(token) >= 2 and token not in _STOPWORDS}


def _cjk_bigrams(text: str) -> set[str]:
    grams: set[str] = set()
    for run in _CJK_RUN_RE.findall(_fold(text)):
        grams.update(run[i:i + 2] for i in range(len(run) - 1))
    return grams


def _query_dimensions(query: str) -> set[str]:
    return {_fold(item) for item in _DIMENSION_RE.findall(query)}


def _case_text(case: dict) -> str:
    """参与词法重叠的案例文本(摘要/症状/动作/标签/结论)。"""
    parts = [case.get("summary"), case.get("outcome"),
             case.get("main_factor")]
    for key in ("symptoms", "tags", "actions", "dimensions"):
        parts.extend(_as_list(case.get(key)))
    return _fold(" ".join(str(part) for part in parts if part))


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
        store_folded = _fold(store_id)
        factor_folded = _fold(main_factor)
        query_words = _words(query)
        query_grams = _cjk_bigrams(query)
        dimensions = _query_dimensions(query)
        now = self._clock()

        excluded = {"deprecated": 0, "future": 0, "expired": 0,
                    "undated": 0}
        scored: list[dict] = []
        cases = list(self._cases_provider() or [])
        for case in cases:
            if case.get("deprecated") and not include_deprecated:
                excluded["deprecated"] += 1
                continue
            case_start = (case.get("effective_from")
                          or case.get("date") or "")
            if date and not case_start:
                # 无日期无法证明不晚于告警日,按泄漏风险排除。
                excluded["undated"] += 1
                continue
            if date and case_start > date:
                excluded["future"] += 1
                continue
            case_end = case.get("effective_to") or ""
            if date and case_end and date > case_end:
                excluded["expired"] += 1
                continue

            text = _case_text(case)
            case_words = set(_words(text))
            case_grams = _cjk_bigrams(text)
            tags = [str(tag) for tag in _as_list(case.get("tags")) if tag]
            matched_dimensions = sorted(
                {_fold(item) for item in _as_list(case.get("dimensions"))}
                & dimensions)

            breakdown = {
                "store": 1.0 if store_folded and _fold(
                    case.get("store_id")) == store_folded else 0.0,
                "main_factor": 0.0,
                "dimensions": min(2.0, 1.0 * len(matched_dimensions)),
                "tags": sum(
                    (1.0 if _fold(tag) in _fold(query) else 0.0)
                    + (0.5 if _fold(tag) in query_words else 0.0)
                    for tag in tags),
                "text_overlap": 0.0,
            }
            case_factor = _fold(case.get("main_factor"))
            if (factor_folded and factor_folded != "none"
                    and case_factor == factor_folded):
                breakdown["main_factor"] = 2.0
            elif case_factor and case_factor in query_words:
                breakdown["main_factor"] = 2.0

            word_hits = query_words & case_words
            gram_hits = query_grams & case_grams
            breakdown["text_overlap"] = (
                min(2.0, 0.5 * len(word_hits))
                + min(1.5, 0.2 * len(gram_hits)))

            base = sum(breakdown.get(key, 0.0) for key in
                       ("store", "main_factor", "dimensions", "tags",
                        "text_overlap"))
            # 纯同店不足以构成召回:必须有主题信号(主因/维度/标签/文本)。
            if base - breakdown["store"] <= 0:
                continue
            recency = case_search_decay(case, now)
            confidence = max(0.0, min(1.0,
                                       float(case.get("confidence", 0.5)
                                             or 0.5)))
            score = base * (0.75 + 0.25 * recency) + 0.25 * confidence
            scored.append({
                "case": case,
                "score": score,
                "score_breakdown": {
                    **breakdown, "base": base,
                    "recency": recency, "confidence": confidence},
            })

        scored.sort(key=lambda item: (
            item["score"], item["case"].get("updated_at") or 0,
            item["case"].get("case_id") or ""), reverse=True)
        matches = [self._project(item, _MATCH_FIELDS)
                   for item in scored[:max(0, top_k)]]
        candidates = [self._project(item, _CANDIDATE_FIELDS)
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
        case = item["case"]
        return {key: case.get(key) for key in fields} | {
            "score": item["score"],
            "score_breakdown": dict(item["score_breakdown"]),
        }
