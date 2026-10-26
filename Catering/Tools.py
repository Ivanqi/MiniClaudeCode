"""餐饮工具集:LLM(探索模式)与诊断引擎(工作流模式)共用的工具。

每个工具 = 一个业务查询动作,底层走 DataHub 八类数据源 + Metrics 数据分析服务;
工具结果统一为归一化 dict,缺失数据显式写入 missing_fields,不填默认值;
LLM 与规则引擎消费同一份结构化结果,保证"说"与"算"同源。
"""

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
    '''
    run_query_metrics 是一个通用经营指标查询方法
    它允许调用方一次性获取多个指标（如营业额、客流、客单价等）的当期值、基准均值、同比变化百分比，同时优雅地处理数据缺失情况。
    '''
    def run_query_metrics(self, store_id: str, date: str,
                          metrics: list) -> dict:
        """通用经营指标查询(门店经营数据库)。"""
        '''
        METRIC_FIELDS 是一个预定义的元组，包含允许查询的指标名称（如 "revenue", "traffic", "ticket" 等）。

        如果 metrics 中包含不在该元组中的字段，则直接抛出 ValueError，避免后续错误。
        '''
        unknown = [m for m in metrics if m not in METRIC_FIELDS]
        if unknown:
            raise ValueError(f"未知指标: {unknown},可选: {list(METRIC_FIELDS)}")
        '''
        调用内部方法 _day_with_baseline，返回：
            day：当天的数据字典（如 {"revenue": 120000, "traffic": 2000}）。
            base：基期数据列表（通常是过去 N 天的记录）。
            missing：缺失信息列表（如数据缺失、天数不足等）。
        '''
        day, base, missing = self._day_with_baseline(store_id, date)
        '''
        为每个指标生成三元组
            如果数据完整，则对 metrics 中的每个指标，调用 metric_triplet(base, day, m) 生成包含 value（当期）、baseline（基准均值）、pct_change（同比变化百分比）的字典。
            如果数据不完整，则将 result 设为空字典（后续仍会返回 missing_fields）。
        '''
        if not missing and day:
            result = {m: metric_triplet(base, day, m) for m in metrics}
        else:
            result = {}
        return {"store_id": store_id, "date": date, **result,
                "missing_fields": missing}

    def run_analyze_revenue_decomposition(self, store_id: str,
                                          date: str) -> dict:
        """营业额贡献度拆解(数据分析服务):客流 vs 客单价,精确无残差。"""
        '''
        day：指定门店在指定日期的单日数据字典（如 {"revenue": 120000, "traffic": 2000, "ticket": 60.0}）。
        base：基期数据列表（通常是过去 N 天，如 7 天，每天的数据字典）。
        missing：缺失字段或错误信息列表（如数据缺失、天数不足等）。
        '''
        day, base, missing = self._day_with_baseline(store_id, date)
        if missing or not day:
            return {"store_id": store_id, "date": date,
                    "missing_fields": missing}
        '''
        对基期列表 base 中的营收和客流分别求和并除以天数，得到平均营收和平均客流，作为比较基准。
        base 非空且每个元素包含 "revenue" 和 "traffic" 字段
        '''
        base_revenue = sum(d["revenue"] for d in base) / len(base)
        base_traffic = sum(d["traffic"] for d in base) / len(base)
        '''
        传入当期营收、客流和基期平均营收、客流，返回包含 total_delta、traffic_contrib、ticket_contrib 等字段的字典
        '''
        contrib = lmdi_decompose(day["revenue"], day["traffic"],
                                 base_revenue, base_traffic)
        '''
        baseline：基期的平均营收、平均客流、平均客单价（营收/客流）。
        current：当天的实际营收、客流、客单价（直接从 day 中获取）。
        pct_change：当天营收相对基期平均营收的百分比变化。
        contrib：LMDI 分解的详细结果。
        missing_fields 设为空列表（因为已经检查过数据完整）
        '''
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

    '''
    实现了按时段（早市、午市等）拆分全店（堂食+外卖）客流与营收，并计算同比变化及降幅集中度的功能
    '''
    def run_query_period_breakdown(self, store_id: str, date: str) -> dict:
        """时段分布(堂食 POS + 外卖平台合并)。"""
        '''
        获取渠道数据
            调用 _channel_days，返回包含 pos_cur, takeout_cur, pos_base, takeout_base 以及 missing_fields 的字典。

            若数据缺失，则 missing_fields 非空。
        '''
        data = self._channel_days(store_id, date)
        '''
        处理数据缺失情况
            如果存在缺失字段，直接返回包含门店ID、日期和缺失信息的字典，不进行计算。
        '''
        if data.get("missing_fields"):
            return {"store_id": store_id, "date": date, **data}

        '''
        调用 period_breakdown 进行分析
            传入四个数据源，得到时段分析结果（包含 periods 和 analysis）
        '''
        result = period_breakdown(data["pos_cur"], data["pos_base"],
                                  data["takeout_cur"], data["takeout_base"])
        '''
        组装返回结果
            合并门店ID、日期和 period_breakdown 返回的字典，并确保 missing_fields 为空（因为数据已检查）
        '''
        return {"store_id": store_id, "date": date, **result,
                "missing_fields": []}

    '''
    实现了堂食与外卖两个渠道的营收/客流同比分析，并自动识别降幅最大的渠道（降幅集中度）
    '''
    def run_query_channel_breakdown(self, store_id: str, date: str) -> dict:
        """堂食/外卖渠道拆分与降幅集中度。"""
        data = self._channel_days(store_id, date)
        if data.get("missing_fields"):
            return {"store_id": store_id, "date": date, **data}
        result = channel_breakdown(data["pos_cur"], data["pos_base"],
                                   data["takeout_cur"], data["takeout_base"])
        return {"store_id": store_id, "date": date, **result,
                "missing_fields": []}

    '''
    run_query_dish_structure 方法用于查询门店在特定日期的菜品结构数据
    计算每个品类的当期和基期份额、客单价，并调用 mix_price_decomposition 进行加权均价变化的拆解（结构效应 vs 价格效应）
    '''
    def run_query_dish_structure(self, store_id: str, date: str) -> dict:
        """菜品结构(门店经营数据库):品类份额/客单变化 + mix/price 拆解。"""
        '''
        获取当天数据和基期数据
            day：当天的门店数据字典（包含 dishes 列表等）。
            base：基期数据列表（通常为过去 N 天，每天包含 dishes 列表）。
            missing：缺失信息列表。
        '''
        day, base, missing = self._day_with_baseline(store_id, date)
        if missing or not day:
            return {"store_id": store_id, "date": date,
                    "missing_fields": missing}

        '''
        构建基期品类数据字典
            遍历基期每一天的菜品列表，将每个品类对应的所有记录（包含 share, ticket 等字段）存入一个以品类名为键的列表。

            这样 base_by_cat 就聚集了基期中每个品类的所有历史数据点。
        '''
        base_by_cat: dict[str, list] = {}
        for b in base:
            for d in b.get("dishes") or []:
                base_by_cat.setdefault(d["category"], []).append(d)

        '''
        处理当天菜品，计算基期平均值并构建明细列表
            对当天出现的每个品类：
                从 base_by_cat 中取出该品类在基期中的所有记录；若基期没有该品类，则用当前记录自身作为唯一历史（此时 base_share = share, base_ticket = ticket，相当于无变化）。

                计算基期平均份额 (base_share) 和平均客单价 (base_ticket)。

                构建一个包含当期/基期份额、客单价、营收、订单量以及份额变化的字典，存入 categories 列表。
        '''
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

        '''
        生成基期汇总表（用于 LMDI 拆解）
            从 categories 中提取每个品类的基期平均份额和基期平均客单价，构造为 mix_price_decomposition 所需的基期数据格式。
        '''
        base_agg = [{"category": c["category"], "share": c["baseline_share"],
                     "ticket": c["baseline_ticket"]} for c in categories]

        '''
        调用拆解函数
            传入当天的菜品列表（包含每个品类的当前份额和客单价）和刚构造的基期汇总表，计算加权均价变化的结构效应和价格效应。
        '''
        analysis = mix_price_decomposition(day.get("dishes") or [], base_agg)
        
        '''
        合并门店ID、日期、品类明细、拆解分析结果，并确保 missing_fields 为空
        '''
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
                         top_k: int = 3, date: str = "",
                         main_factor: str = "") -> dict:
        """历史类似案例检索(企业知识库):结构化过滤 + 词法/元数据打分。"""
        if not date:
            # 工具边界强制日期切面,避免探索模式检索到告警日之后的案例。
            raise ValueError("search_cases 必须提供告警日期 date")
        return self.hub.search_cases(store_id, symptom, top_k=top_k,
                                     date=date, main_factor=main_factor)

    # ── 受控写工具 ──

    def run_archive_case(self, store_id: str, date: str, main_factor: str,
                         dimensions: list, summary: str, actions: list,
                         tags: list | None = None,
                         symptoms: list | None = None,
                         metrics_snapshot: dict | None = None,
                         outcome: str = "",
                         effective_from: str = "",
                         effective_to: str = "") -> dict:
        """归档诊断案例到企业知识库(写操作,默认禁用,需人工确认)。"""
        if not self.enable_writes:
            raise ValueError("写工具 archive_case 默认禁用(需开启 CATERING_ENABLE_WRITES)")
        tags = tags or []
        case_id = f"CASE-{date}-{store_id}"
        case = {"case_id": case_id, "store_id": store_id, "date": date,
                "main_factor": main_factor, "dimensions": dimensions,
                "summary": summary, "actions": actions, "tags": tags,
                "symptoms": symptoms or [],
                "metrics_snapshot": metrics_snapshot or {},
                "outcome": outcome,
                "effective_from": effective_from,
                "effective_to": effective_to}
        result = self.hub.archive_case(case, source="tool:archive_case",
                                       confidence=0.8)
        return {"archived": case_id, "path": str(result.path),
                "version": result.record.get("version", 1),
                "updated": result.updated}

    # ── 工具定义与处理器 ──

    def tool_defs(self) -> list[dict]:
        tools = [
            make_function_tool("query_metrics",
                "查询门店经营数据库的日度指标(营收/客流/客单价/评分等),返回当期值、基准均值与同比。",
                {"type": "object",
                 "properties": {"store_id": {"type": "string"},
                                "date": {"type": "string",
                                         "description": "YYYY-MM-DD"},
                                "metrics": {"type": "array", "minItems": 1,
                                            "items": {"type": "string",
                                                      "enum": list(METRIC_FIELDS)}}},
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
                "在企业知识库中检索历史类似案例;按告警日/主因过滤与加权,"
                "返回打分及分数分解,案例只作历史类比。",
                {"type": "object",
                 "properties": {"store_id": {"type": "string"},
                                "symptom": {"type": "string"},
                                "date": {"type": "string",
                                         "description": "告警日 YYYY-MM-DD"},
                                "main_factor": {"type": "string"},
                                "top_k": {"type": "integer"}},
                 "required": ["store_id", "symptom", "date"]}),
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
                                         "items": {"type": "string"}},
                                "symptoms": {"type": "array",
                                             "items": {"type": "string"}},
                                "metrics_snapshot": {"type": "object"},
                                "outcome": {"type": "string"},
                                "effective_from": {"type": "string"},
                                "effective_to": {"type": "string"}},
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
