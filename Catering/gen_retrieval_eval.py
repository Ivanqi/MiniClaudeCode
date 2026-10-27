"""确定性生成检索评测集:保留 R01–R22 严格回归,追加分层样本。

输出 `data/retrieval_eval.json`:
- regression: 人工标注的 22 条严格 pair(保持原查询/期望不变);
- group:      18 类合成原型的同组案例(60 条近重复,整组相关);
- filter:     未来/废弃/过期边界案例不得泄漏(must_exclude);
- negative:   纯同店无主题,必须显式无匹配;
- probe:      口语化改写,只报告不设门禁,作为 P2 语义检索的决策证据。

用法: python Catering/gen_retrieval_eval.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from Catering.gen_case_corpus import (  # noqa: E402
    ARCHETYPES, STORES, build_cases)

OUTPUT = Path(__file__).resolve().parent / "data" / "retrieval_eval.json"
HISTORY = Path(__file__).resolve().parent / "data" / "cases" / "history_cases.json"
ALERT_DATE = "2026-09-01"

# curated 案例与合成原型的语义归属;组标签要包含它们,避免“相关但未标注”。
GROUP_EXTRAS = {
    "weather_traffic": ("CASE-2026-0622-A",),
    "discount_ticket": ("CASE-2026-0712-B",),
}

PROBES = (
    ("下大雨了店里没人", "weather_traffic"),
    ("隔壁新店开业把客人抢走了", "competitor_traffic"),
    ("门口修路进不去", "roadwork_traffic"),
    ("外卖排名掉单子少", "platform_rank_traffic"),
    ("会员折扣叠太多不赚钱", "member_ticket"),
    ("高峰期人手不够出餐慢", "staff_shortage"),
)

FILTERS = (
    ("旧版菜单 价格调整", "CASE-SYN-EXPIRED-1"),
    ("旧版排班 高峰班次", "CASE-SYN-EXPIRED-2"),
)

NEGATIVES = (
    ("S05店 露营 宠物 团建", ""),
)


def _base_case_id(ai: int, si: int) -> str:
    return f"CASE-SYN-{ai * len(STORES) + si + 1:04d}"


def _group_queries(archetype: dict) -> tuple[str, str]:
    tags = " ".join(archetype["tags"])
    symptom = archetype["symptom"]
    actions = " ".join(archetype["actions"][:2])
    outcome = archetype["outcome"]
    return (
        f"{tags} {symptom}",
        f"{symptom} {outcome} {actions}",
    )


def build_groups(cases: list[dict], history: dict[str, dict]) -> dict:
    by_id = {case["case_id"]: case for case in cases}
    groups: dict[str, list[str]] = {}
    for ai, archetype in enumerate(ARCHETYPES):
        members = [_base_case_id(ai, si) for si in range(len(STORES))]
        for case_id in members:
            if case_id not in by_id:
                raise ValueError(f"合成语料缺少案例 {case_id}")
        extras = [case_id for case_id in GROUP_EXTRAS.get(
            archetype["key"], ()) if case_id in history]
        groups[archetype["key"]] = members + extras
    return groups


def build_pairs(cases: list[dict], groups: dict,
                regression: list[dict]) -> list[dict]:
    archetype_by_key = {item["key"]: item for item in ARCHETYPES}
    pairs: list[dict] = []
    for pair in regression:
        pair = dict(pair)
        pair.setdefault("split", "regression")
        if pair.get("id") == "R21":
            pair["must_exclude"] = ["CASE-SYN-FUTURE-1"]
        elif pair.get("id") == "R22":
            pair["must_exclude"] = ["CASE-SYN-DEPRECATED-1"]
        pairs.append(pair)

    group_index = 1
    for archetype in ARCHETYPES:
        key = archetype["key"]
        for query in _group_queries(archetype):
            pairs.append({
                "id": f"G{group_index:02d}",
                "query": query,
                "store_id": "",
                "date": ALERT_DATE,
                "main_factor": archetype["main_factor"],
                "top_k": 3,
                "split": "group",
                "relevance": "group",
                "group": key,
                "expected_case_ids": [],
            })
            group_index += 1

    for index, (query, group) in enumerate(PROBES, 1):
        pairs.append({
            "id": f"P{index:02d}",
            "query": query,
            "store_id": "",
            "date": ALERT_DATE,
            "main_factor": archetype_by_key[group]["main_factor"],
            "top_k": 3,
            "split": "probe",
            "relevance": "group",
            "group": group,
            "expected_case_ids": [],
        })

    for index, (query, excluded) in enumerate(FILTERS, 1):
        pairs.append({
            "id": f"F{index:02d}",
            "query": query,
            "store_id": "",
            "date": ALERT_DATE,
            "main_factor": "",
            "top_k": 3,
            "split": "filter",
            "relevance": "none",
            "expected_case_ids": [],
            "must_exclude": [excluded],
        })

    for index, (query, main_factor) in enumerate(NEGATIVES, 1):
        pairs.append({
            "id": f"N{index:02d}",
            "query": query,
            "store_id": "S05店",
            "date": ALERT_DATE,
            "main_factor": main_factor,
            "top_k": 3,
            "split": "negative",
            "relevance": "none",
            "expected_case_ids": [],
        })
    return pairs


def main() -> int:
    payload = json.loads(OUTPUT.read_text())
    regression = [pair for pair in payload.get("pairs") or []
                  if pair.get("split", "regression") == "regression"]
    cases = build_cases()
    history_payload = json.loads(HISTORY.read_text())
    history = {case["case_id"]: case
               for case in history_payload.get("cases") or []}
    groups = build_groups(cases, history)
    pairs = build_pairs(cases, groups, regression)
    ids = [pair["id"] for pair in pairs]
    if len(ids) != len(set(ids)):
        raise ValueError("检索评测 pair id 重复")
    OUTPUT.write_text(json.dumps({
        "schema_version": 1,
        "description": (
            "案例检索标注集:regression/strict + group/group + "
            "filter/must_exclude + negative + probe;空期望表示不应召回。"
        ),
        "groups": groups,
        "pairs": pairs,
    }, ensure_ascii=False, indent=2))
    print(f"wrote {len(pairs)} pairs / {len(groups)} groups -> {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
