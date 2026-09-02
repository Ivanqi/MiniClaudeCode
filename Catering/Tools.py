"""餐饮工具集:LLM(探索模式)与诊断引擎(工作流模式)共用的工具。

每个工具 = 一个业务查询动作,底层走 DataHub 八类数据源 + Metrics 数据分析服务;
工具结果统一为归一化 dict,缺失数据显式写入 missing_fields,不填默认值;
LLM 与规则引擎消费同一份结构化结果,保证"说"与"算"同源。
"""
import json

from src.BasicOper import BasicOper
from src.LlmClient import make_function_tool

from Catering.Configs import CateringConfigs
from Catering.DataHub import DataHub, DataSourceError
from Catering.Metrics import (channel_breakdown, lmdi_decompose,
                              metric_triplet, mix_price_decomposition,
                              period_breakdown)

METRIC_FIELDS = ("revenue", "traffic", "ticket", "rating",
                 "review_count", "complaints")


class CateringTools:
    """餐饮数据工具:tool_defs()/handlers() 双接口,可注册进 ToolRegistry。"""

    def __init__(self, hub: DataHub | None = None,
                 enable_writes: bool | None = None):
        self.hub = hub or DataHub()
        self.enable_writes = (enable_writes
                              if enable_writes is not None
                              else CateringConfigs.ENABLE_WRITES)

    # ── 通用辅助 ──

    def _day_with_baseline(self, store_id: str, date: str):
        """取 store_db 当日 + 基准日记录;数据缺失时返回带 missing 的结果。"""
        missing: list[str] = []
        try:
            day = self.hub.day("store_db", store_id, date)
        except DataSourceError as e:
            return None, None, [str(e)]
        if day is None:
            missing.append(f"store_db 当日数据缺失: {date}")
        base = self.hub.baseline("store_db", store_id, date)
        if len(base) < CateringConfigs.BASELINE_DAYS:
            missing.append(f"基准日部分缺失({len(base)}/{CateringConfigs.BASELINE_DAYS} 天)")
        return day, base, missing

    def execute(self, name: str, args: dict) -> dict:
        """统一执行入口(引擎与探索模式共用):异常归一化为带 error 的结果。"""
        handler = self.handlers().get(name)
        try:
            result = BasicOper.call_tool_handler(handler, args, name)
            if not isinstance(result, dict):
                result = {"text": str(result)}
            return result
        except (DataSourceError, ValueError) as e:
            return {"error": str(e), "missing_fields": [str(e)]}

    # ── 只读工具处理器 ──

    def run_query_metrics(self, store_id: str, date: str,
                          metrics: list) -> dict:
        """通用经营指标查询(门店经营数据库)。"""
        unknown = [m for m in metrics if m not in METRIC_FIELDS]
        if unknown:
            raise ValueError(f"未知指标: {unknown},可选: {list(METRIC_FIELDS)}")
        day, base, missing = self._day_with_baseline(store_id, date)
        if not missing and day:
            result = {m: metric_triplet(base, day, m) for m in metrics}
        else:
            result = {}
        return {"store_id": store_id, "date": date, **result,
                "missing_fields": missing}

    def run_analyze_revenue_decomposition(self, store_id: str,
                                          date: str) -> dict:
        """营业额贡献度拆解(数据分析服务):客流 vs 客单价,精确无残差。"""
        day, base, missing = self._day_with_baseline(store_id, date)
        if missing or not day:
            return {"store_id": store_id, "date": date,
                    "missing_fields": missing}
        base_revenue = sum(d["revenue"] for d in base) / len(base)
        base_traffic = sum(d["traffic"] for d in base) / len(base)
        contrib = lmdi_decompose(day["revenue"], day["traffic"],
                                 base_revenue, base_traffic)
        return {
            "store_id": store_id, "date": date,
            "baseline": {"revenue": round(base_revenue, 2),
                         "traffic": round(base_traffic, 2),
                         "ticket": round(base_revenue / base_traffic, 6)},
            "current": {"revenue": day["revenue"], "traffic": day["traffic"],
                        "ticket": day["ticket"]},
            "pct_change": (day["revenue"] - base_revenue) / base_revenue * 100,
            "contrib": contrib,
            "missing_fields": [],
        }

    def _channel_day(self, category: str, store_id: str, date: str,
                     label: str) -> dict:
        try:
            day = self.hub.day(category, store_id, date)
        except DataSourceError as e:
            return {"name": label, "error": str(e),
                    "missing_fields": [str(e)]}
        base = self.hub.baseline(category, store_id, date)
        missing: list[str] = []
        if day is None:
            missing.append(f"{label}当日数据缺失")
        if len(base) < CateringConfigs.BASELINE_DAYS:
            missing.append(f"{label}基准日部分缺失({len(base)}/{CateringConfigs.BASELINE_DAYS} 天)")
        result = {"name": label, "missing_fields": missing}
        if day is not None:
            result.update(metric_triplet(base, day, "revenue") | {"traffic": day["traffic"]})
            result["revenue"] = result.pop("value")
            result["baseline_revenue"] = result.pop("baseline")
        return result

    def run_query_pos_data(self, store_id: str, date: str) -> dict:
        """POS 收银数据:堂食渠道当日营收/客流及同比。"""
        pos = self._channel_day("pos", store_id, date, "堂食")
        return {"store_id": store_id, "date": date, "pos": pos,
                "missing_fields": pos["missing_fields"]}

    def run_query_takeout_data(self, store_id: str, date: str) -> dict:
        """外卖平台数据:外卖渠道当日营收/客流及同比。"""
        takeout = self._channel_day("takeout", store_id, date, "外卖")
        return {"store_id": store_id, "date": date, "takeout": takeout,
                "missing_fields": takeout["missing_fields"]}

    def _channel_days(self, store_id: str, date: str) -> dict:
        try:
            pos_cur = self.hub.day("pos", store_id, date)
            takeout_cur = self.hub.day("takeout", store_id, date)
            pos_base = self.hub.baseline("pos", store_id, date)
            takeout_base = self.hub.baseline("takeout", store_id, date)
        except DataSourceError as e:
            return {"error": str(e), "missing_fields": [str(e)]}
        missing: list[str] = []
        if pos_cur is None or takeout_cur is None:
            missing.append("渠道当日数据缺失")
        if (len(pos_base) < CateringConfigs.BASELINE_DAYS
                or len(takeout_base) < CateringConfigs.BASELINE_DAYS):
            missing.append("渠道基准日部分缺失")
        if missing:
            return {"missing_fields": missing}
        return {"pos_cur": pos_cur, "takeout_cur": takeout_cur,
                "pos_base": pos_base, "takeout_base": takeout_base,
                "missing_fields": []}

    def run_query_period_breakdown(self, store_id: str, date: str) -> dict:
        """时段分布(堂食 POS + 外卖平台合并)。"""
        data = self._channel_days(store_id, date)
        if data.get("missing_fields"):
            return {"store_id": store_id, "date": date, **data}
        result = period_breakdown(data["pos_cur"], data["pos_base"],
                                  data["takeout_cur"], data["takeout_base"])
        return {"store_id": store_id, "date": date, **result,
                "missing_fields": []}

    def run_query_channel_breakdown(self, store_id: str, date: str) -> dict:
        """堂食/外卖渠道拆分与降幅集中度。"""
        data = self._channel_days(store_id, date)
        if data.get("missing_fields"):
            return {"store_id": store_id, "date": date, **data}
        result = channel_breakdown(data["pos_cur"], data["pos_base"],
                                   data["takeout_cur"], data["takeout_base"])
        return {"store_id": store_id, "date": date, **result,
                "missing_fields": []}

    def run_query_dish_structure(self, store_id: str, date: str) -> dict:
        """菜品结构(门店经营数据库):品类份额/客单变化 + mix/price 拆解。"""
        day, base, missing = self._day_with_baseline(store_id, date)
        if missing or not day:
            return {"store_id": store_id, "date": date,
                    "missing_fields": missing}
        base_by_cat: dict[str, list] = {}
        for b in base:
            for d in b.get("dishes") or []:
                base_by_cat.setdefault(d["category"], []).append(d)
        categories = []
        for d in day.get("dishes") or []:
            hist = base_by_cat.get(d["category"]) or [d]
            base_share = sum(x["share"] for x in hist) / len(hist)
            base_ticket = sum(x["ticket"] for x in hist) / len(hist)
            categories.append({
                "category": d["category"], "share": d["share"],
                "baseline_share": base_share, "ticket": d["ticket"],
                "baseline_ticket": base_ticket,
                "revenue": d["revenue"], "orders": d["orders"],
                "revenue_share_change": d["share"] - base_share,
            })
        base_agg = [{"category": c["category"], "share": c["baseline_share"],
                     "ticket": c["baseline_ticket"]} for c in categories]
        analysis = mix_price_decomposition(day.get("dishes") or [], base_agg)
        return {"store_id": store_id, "date": date, "categories": categories,
                "analysis": analysis, "missing_fields": []}

    def run_query_promotions(self, store_id: str, date: str) -> dict:
        """活动检查:内部业务系统(本店活动)+ 第三方市场数据(竞对活动)。"""
        own = self.hub.query("internal_system", store_id, date)
        market = self.hub.query("external_api", store_id, date, kind="market")
        major = any(p.get("discount_pct", 0) >= 20
                    for p in own.get("promotions") or [])
        competitor = market.get("market_events") or []
        return {"store_id": store_id, "date": date,
                "own": own.get("promotions") or [], "competitor": competitor,
                "analysis": {"has_major_own_promo": major,
                             "has_competitor_promo": bool(competitor)},
                "missing_fields": []}

    def run_query_ratings(self, store_id: str, date: str) -> dict:
        """评分检查:当日评分/环比变化/投诉量。"""
        day, base, missing = self._day_with_baseline(store_id, date)
        if missing or not day:
            return {"store_id": store_id, "date": date,
                    "missing_fields": missing}
        rating = metric_triplet(base, day, "rating")
        delta = (rating["value"] - rating["baseline"]
                 if rating["value"] is not None
                 and rating["baseline"] is not None else None)
        anomaly = (delta is not None and delta <= CateringConfigs.RATING_DROP
                   ) or (rating["value"] is not None
                         and rating["value"] <= CateringConfigs.RATING_LOW)
        return {"store_id": store_id, "date": date,
                "rating": {"value": rating["value"],
                           "baseline": rating["baseline"], "delta": delta},
                "review_count": day.get("review_count"),
                "complaints": day.get("complaints"),
                "analysis": {"rating_anomaly": anomaly},
                "missing_fields": []}

    def run_query_staff(self, store_id: str, date: str) -> dict:
        """人员出勤检查:出勤率与基准对比。"""
        day, base, missing = self._day_with_baseline(store_id, date)
        if missing or not day:
            return {"store_id": store_id, "date": date,
                    "missing_fields": missing}
        on_duty, planned = day.get("staff_on_duty"), day.get("staff_planned")
        ratio = on_duty / planned if planned else None
        return {"store_id": store_id, "date": date,
                "on_duty": on_duty, "planned": planned,
                "attendance_ratio": ratio,
                "baseline_on_duty": metric_triplet(base, day,
                                                   "staff_on_duty")["baseline"],
                "analysis": {"attendance_anomaly":
                             ratio is not None
                             and ratio < CateringConfigs.ATTENDANCE_MIN_RATIO},
                "missing_fields": []}

    def run_query_external_factors(self, store_id: str, date: str) -> dict:
        """外部因素(第三方 API):天气 + 竞对市场动态。"""
        weather = self.hub.query("external_api", store_id, date,
                                 kind="weather")
        market = self.hub.query("external_api", store_id, date, kind="market")
        severe = weather.get("weather", {}).get("severity") == "severe"
        competitor = market.get("market_events") or []
        return {"store_id": store_id, "date": date,
                "weather": weather.get("weather"),
                "market_events": competitor,
                "analysis": {"severe_weather": severe,
                             "has_competitor_promo": bool(competitor)},
                "missing_fields": []}

    def run_search_cases(self, store_id: str, symptom: str,
                         top_k: int = 3) -> dict:
        """历史类似案例检索(企业知识库):按症状关键词与主因匹配打分。"""
        return self.hub.search_cases(store_id, symptom, top_k=top_k)

    # ── 受控写工具 ──

    def run_archive_case(self, store_id: str, date: str, main_factor: str,
                         dimensions: list, summary: str, actions: list,
                         tags: list | None = None) -> dict:
        """归档诊断案例到企业知识库(写操作,默认禁用,需人工确认)。"""
        if not self.enable_writes:
            raise ValueError("写工具 archive_case 默认禁用(需开启 CATERING_ENABLE_WRITES)")
        tags = tags or []
        case_id = f"CASE-{date}-{store_id}"
        case = {"case_id": case_id, "store_id": store_id, "date": date,
                "main_factor": main_factor, "dimensions": dimensions,
                "summary": summary, "actions": actions, "tags": tags}
        fp = CateringConfigs.CASES_DIR / "archive" / f"{case_id}.json"
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(json.dumps({"cases": [case]}, ensure_ascii=False,
                                 indent=2))
        return {"archived": case_id, "path": str(fp)}

    # ── 工具定义与处理器 ──

    def tool_defs(self) -> list[dict]:
        tools = [
            make_function_tool("query_metrics",
                "查询门店经营数据库的日度指标(营收/客流/客单价/评分等),返回当期值、基准均值与同比。",
                {"type": "object",
                 "properties": {"store_id": {"type": "string"},
                                "date": {"type": "string",
                                         "description": "YYYY-MM-DD"},
                                "metrics": {"type": "array",
                                            "items": {"type": "string"}}},
                 "required": ["store_id", "date", "metrics"]}),
            make_function_tool("analyze_revenue_decomposition",
                "数据分析服务:把营业额变化精确拆解为客流贡献与客单价贡献(对数均值法,无残差),判定主因。",
                {"type": "object",
                 "properties": {"store_id": {"type": "string"},
                                "date": {"type": "string"}},
                 "required": ["store_id", "date"]}),
            make_function_tool("query_pos_data",
                "查询 POS 收银数据:堂食渠道当日营收、客流及同比。",
                {"type": "object",
                 "properties": {"store_id": {"type": "string"},
                                "date": {"type": "string"}},
                 "required": ["store_id", "date"]}),
            make_function_tool("query_takeout_data",
                "查询外卖平台数据:外卖渠道当日营收、客流及同比。",
                {"type": "object",
                 "properties": {"store_id": {"type": "string"},
                                "date": {"type": "string"}},
                 "required": ["store_id", "date"]}),
            make_function_tool("query_period_breakdown",
                "查询时段分布(早市/午市/下午茶/晚市/夜宵)的客流与营收同比,并给出降幅集中时段。",
                {"type": "object",
                 "properties": {"store_id": {"type": "string"},
                                "date": {"type": "string"}},
                 "required": ["store_id", "date"]}),
            make_function_tool("query_channel_breakdown",
                "查询堂食/外卖渠道拆分:各渠道营收、客流、份额与同比,并给出降幅集中渠道。",
                {"type": "object",
                 "properties": {"store_id": {"type": "string"},
                                "date": {"type": "string"}},
                 "required": ["store_id", "date"]}),
            make_function_tool("query_dish_structure",
                "查询菜品结构:品类份额/客单与基准对比,并用 mix/price 法拆解客单价变化来源。",
                {"type": "object",
                 "properties": {"store_id": {"type": "string"},
                                "date": {"type": "string"}},
                 "required": ["store_id", "date"]}),
            make_function_tool("query_promotions",
                "查询活动:内部业务系统中的本店促销,以及第三方市场数据中的竞对促销。",
                {"type": "object",
                 "properties": {"store_id": {"type": "string"},
                                "date": {"type": "string"}},
                 "required": ["store_id", "date"]}),
            make_function_tool("query_ratings",
                "查询门店评分、环比变化与投诉量。",
                {"type": "object",
                 "properties": {"store_id": {"type": "string"},
                                "date": {"type": "string"}},
                 "required": ["store_id", "date"]}),
            make_function_tool("query_staff",
                "查询人员出勤:当日出勤/排班人数与出勤率。",
                {"type": "object",
                 "properties": {"store_id": {"type": "string"},
                                "date": {"type": "string"}},
                 "required": ["store_id", "date"]}),
            make_function_tool("query_external_factors",
                "查询外部因素(第三方 API):当日天气与竞对市场活动。",
                {"type": "object",
                 "properties": {"store_id": {"type": "string"},
                                "date": {"type": "string"}},
                 "required": ["store_id", "date"]}),
            make_function_tool("search_cases",
                "在企业知识库中检索历史类似案例,按症状关键词与主因匹配打分。",
                {"type": "object",
                 "properties": {"store_id": {"type": "string"},
                                "symptom": {"type": "string"},
                                "top_k": {"type": "integer"}},
                 "required": ["store_id", "symptom"]}),
        ]
        if self.enable_writes:
            tools.append(make_function_tool("archive_case",
                "把诊断结论归档为知识库案例(写操作,需人工确认)。",
                {"type": "object",
                 "properties": {"store_id": {"type": "string"},
                                "date": {"type": "string"},
                                "main_factor": {"type": "string"},
                                "dimensions": {"type": "array",
                                               "items": {"type": "string"}},
                                "summary": {"type": "string"},
                                "actions": {"type": "array",
                                            "items": {"type": "string"}},
                                "tags": {"type": "array",
                                         "items": {"type": "string"}}},
                 "required": ["store_id", "date", "main_factor",
                              "dimensions", "summary", "actions"]}))
        return tools

    def handlers(self) -> dict:
        return {
            "query_metrics": self.run_query_metrics,
            "analyze_revenue_decomposition":
                self.run_analyze_revenue_decomposition,
            "query_pos_data": self.run_query_pos_data,
            "query_takeout_data": self.run_query_takeout_data,
            "query_period_breakdown": self.run_query_period_breakdown,
            "query_channel_breakdown": self.run_query_channel_breakdown,
            "query_dish_structure": self.run_query_dish_structure,
            "query_promotions": self.run_query_promotions,
            "query_ratings": self.run_query_ratings,
            "query_staff": self.run_query_staff,
            "query_external_factors": self.run_query_external_factors,
            "search_cases": self.run_search_cases,
            "archive_case": self.run_archive_case,
        }
