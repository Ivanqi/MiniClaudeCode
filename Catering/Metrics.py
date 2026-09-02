"""数据分析服务(需求 2 的 analysis_service):确定性指标计算。

全部为纯函数:LLM 不参与任何计算,拆解、同比、结构分析均由代码完成,
LLM 只消费计算结果——这是"不让 LLM 猜原因"的基础。

核心口径:
- 营业额 = 客流 × 客单价,用 LMDI 对数均值法拆解,精确无残差;
- 贡献 share 可负、可超 100%(一方回补另一方),主因按贡献额判定;
- 品类客单以"订单"为分母,与门店客单价(客流分母)口径不同。
"""
import math

from Catering.Configs import CateringConfigs
from Catering.Models import AnomalyAlert

PERIOD_NAMES = ["早市", "午市", "下午茶", "晚市", "夜宵"]
CHANNEL_NAMES = ["堂食", "外卖"]


# ── 基础口径 ──

def pct_change(current: float | None, baseline: float | None) -> float | None:
    """同比/环比百分比;基期非正数时返回 None(不猜数)。"""
    if current is None or baseline is None or baseline <= 0:
        return None
    return (current - baseline) / baseline * 100


def _mean(values: list) -> float | None:
    nums = [v for v in values if v is not None]
    return sum(nums) / len(nums) if nums else None


def metric_triplet(baseline_days: list[dict], current_day: dict,
                   field: str) -> dict:
    """单个指标的 {当期, 基准均值, 同比} 三元组。"""
    value = current_day.get(field)
    baseline = _mean([d.get(field) for d in baseline_days])
    return {"value": value, "baseline": baseline,
            "pct_change": pct_change(value, baseline)}


# ── 贡献度拆解 ──

def lmdi_decompose(cur_revenue: float, cur_traffic: float,
                   base_revenue: float, base_traffic: float) -> dict:
    """营业额 = 客流 × 客单价 的对数均值(LMDI)拆解。

    客流贡献 = L·ln(T_t/T_0),客单价贡献 = ΔR - 客流贡献(精确无残差)。
    基期/当期任一侧非正数时抛 ValueError,由调用方标记结构变化,不猜值。
    """
    if min(cur_revenue, cur_traffic, base_revenue, base_traffic) <= 0:
        raise ValueError("LMDI 拆解要求基期与当期营收/客流均为正数")
    total_delta = cur_revenue - base_revenue
    ratio = cur_revenue / base_revenue
    log_mean = total_delta / math.log(ratio) if ratio != 1 else base_revenue
    traffic_contrib = log_mean * math.log(cur_traffic / base_traffic)
    ticket_contrib = total_delta - traffic_contrib
    return {
        "total_delta": round(total_delta, 2),
        "traffic_contrib": round(traffic_contrib, 2),
        "ticket_contrib": round(ticket_contrib, 2),
        "traffic_share": traffic_contrib / total_delta,
        "ticket_share": ticket_contrib / total_delta,
    }


def top_negative(rows: list[dict], delta_field: str,
                 total_delta: float | None, name_key: str) -> dict:
    """降幅集中度:总变化为负时,找出下拉最大的行及其占总支出的份额。

    name_key 决定结果中的名称键(如 top_negative_period /
    top_negative_channel),份额键统一为 top_negative_share。
    """
    empty = {name_key: None, "top_negative_share": None}
    if total_delta is None or total_delta >= 0:
        return empty
    negatives = {r["name"]: r[delta_field] for r in rows
                 if r[delta_field] is not None and r[delta_field] < 0}
    if not negatives:
        return empty
    top = min(negatives, key=negatives.get)
    return {name_key: top, "top_negative_share": negatives[top] / total_delta}


# ── 渠道 / 时段 / 菜品结构 ──

def channel_breakdown(pos_cur: dict, pos_base: list[dict],
                      takeout_cur: dict, takeout_base: list[dict]) -> dict:
    """堂食/外卖渠道拆分:各渠道营收与客流同比 + 降幅集中度。"""
    channels = []
    for name, cur, base in ((CHANNEL_NAMES[0], pos_cur, pos_base),
                            (CHANNEL_NAMES[1], takeout_cur, takeout_base)):
        rev = metric_triplet(base, cur, "revenue")
        tr = metric_triplet(base, cur, "traffic")
        channels.append({
            "name": name,
            "revenue": rev["value"], "baseline_revenue": rev["baseline"],
            "revenue_pct_change": rev["pct_change"],
            "traffic": tr["value"], "traffic_pct_change": tr["pct_change"],
        })
    total_rev = sum(c["revenue"] for c in channels
                    if c["revenue"] is not None)
    total_base = sum(c["baseline_revenue"] for c in channels
                     if c["baseline_revenue"] is not None)
    for c in channels:
        c["share"] = (c["revenue"] / total_rev
                      if c["revenue"] is not None and total_rev else None)
        c["baseline_share"] = (c["baseline_revenue"] / total_base
                               if c["baseline_revenue"] is not None
                               and total_base else None)
        c["delta"] = (c["revenue"] - c["baseline_revenue"]
                      if c["revenue"] is not None
                      and c["baseline_revenue"] is not None else None)
    total_delta = (total_rev - total_base
                   if total_rev is not None and total_base is not None else None)
    analysis = top_negative(channels, "delta", total_delta,
                            name_key="top_negative_channel")
    analysis["total_delta"] = total_delta
    return {"channels": channels, "analysis": analysis}


def period_breakdown(pos_cur: dict, pos_base: list[dict],
                     takeout_cur: dict, takeout_base: list[dict]) -> dict:
    """时段拆分:全店(堂食+外卖)各时段客流/营收同比 + 降幅集中度。"""
    periods = []
    for name in PERIOD_NAMES:
        cur_traffic = (pos_cur.get("periods", {}).get(name, {}).get("traffic")
                       or 0) + (takeout_cur.get("periods", {}).get(name, {})
                                .get("traffic") or 0)
        cur_revenue = (pos_cur.get("periods", {}).get(name, {}).get("revenue")
                       or 0) + (takeout_cur.get("periods", {}).get(name, {})
                                .get("revenue") or 0)
        base_traffic = _mean([
            (p.get("periods", {}).get(name, {}).get("traffic") or 0)
            + (t.get("periods", {}).get(name, {}).get("traffic") or 0)
            for p, t in zip(pos_base, takeout_base)])
        base_revenue = _mean([
            (p.get("periods", {}).get(name, {}).get("revenue") or 0)
            + (t.get("periods", {}).get(name, {}).get("revenue") or 0)
            for p, t in zip(pos_base, takeout_base)])
        periods.append({
            "name": name, "traffic": cur_traffic,
            "traffic_pct_change": pct_change(cur_traffic, base_traffic),
            "revenue": cur_revenue,
            "revenue_pct_change": pct_change(cur_revenue, base_revenue),
            "traffic_delta": (cur_traffic - base_traffic
                              if base_traffic is not None else None),
        })
    total_delta = sum(p["traffic_delta"] for p in periods
                      if p["traffic_delta"] is not None)
    analysis = top_negative(periods, "traffic_delta", total_delta,
                            name_key="top_negative_period")
    analysis["total_traffic_delta"] = total_delta
    return {"periods": periods, "analysis": analysis}


def mix_price_decomposition(cur_dishes: list[dict],
                            base_dishes: list[dict]) -> dict:
    """菜品结构拆解:加权均价变化 ΔA = 结构效应(mix) + 价格效应(price),精确无残差。

    mix   = Σ (s_t - s_0)·p_0   结构向低价/高价品类偏移的影响
    price = Σ s_t·(p_t - p_0)   品类自身定价变化的影响
    """
    base_map = {d["category"]: d for d in base_dishes}
    cur_map = {d["category"]: d for d in cur_dishes}
    categories = set(base_map) | set(cur_map)
    mix = price = a0 = at = 0.0
    for cat in categories:
        b, c = base_map.get(cat), cur_map.get(cat)
        s0, p0 = (b["share"], b["ticket"]) if b else (0.0, 0.0)
        st, pt = (c["share"], c["ticket"]) if c else (0.0, 0.0)
        mix += (st - s0) * p0
        price += st * (pt - p0)
        a0 += s0 * p0
        at += st * pt
    return {"baseline_weighted_ticket": a0, "weighted_ticket": at,
            "ticket_delta": at - a0, "mix_effect": mix,
            "price_effect": price}


# ── 告警扫描 ──

def detect_alerts(stores: list[dict]) -> list[AnomalyAlert]:
    """规则扫描:最近一日营收相对前 N 日均值变化超过阈值则产出告警。"""
    alerts = []
    n = CateringConfigs.BASELINE_DAYS
    for store in stores:
        days = store.get("days") or []
        if len(days) < n + 1:
            continue
        last = days[-1]
        baseline = _mean([d["revenue"] for d in days[-n - 1:-1]])
        change = pct_change(last.get("revenue"), baseline)
        if change is not None and abs(change) >= CateringConfigs.ALERT_THRESHOLD_PCT:
            alerts.append(AnomalyAlert(
                store_id=store["store_id"], date=str(last["date"]),
                baseline=baseline, current=last["revenue"], change_pct=change,
                source="detector",
                message=(f"{store['store_id']} {last['date']} 营业额同比 "
                         f"{change:+.1f}%")))
    return alerts


# ── 数据自洽校验 ──

def check_additive_invariants(stores: list[dict], pos: list[dict],
                              takeout: list[dict]) -> list[str]:
    """跨维度加和自洽校验:Σ时段=日总、Σ渠道=日总、Σ菜品=日总。

    返回错误描述列表(空列表 = 全部自洽),供样本数据与运行时校验复用。
    """
    errors = []
    pos_map = {p["store_id"]: p for p in pos}
    takeout_map = {t["store_id"]: t for t in takeout}
    for store in stores:
        sid = store["store_id"]
        p, t = pos_map.get(sid), takeout_map.get(sid)
        if p is None or t is None:
            errors.append(f"{sid}: 缺少 pos/takeout 数据")
            continue
        p_days = {d["date"]: d for d in (p.get("days") or [])}
        t_days = {d["date"]: d for d in (t.get("days") or [])}
        for day in store.get("days") or []:
            date = day["date"]
            pd, td = p_days.get(date), t_days.get(date)
            if pd is None or td is None:
                errors.append(f"{sid} {date}: 渠道数据缺失")
                continue
            if abs(pd["revenue"] + td["revenue"] - day["revenue"]) > 0.01:
                errors.append(f"{sid} {date}: 渠道营收加和不一致")
            if pd["traffic"] + td["traffic"] != day["traffic"]:
                errors.append(f"{sid} {date}: 渠道客流加和不一致")
            if sum(v["traffic"] for v in pd["periods"].values()) != pd["traffic"]:
                errors.append(f"{sid} {date}: 堂食时段客流加和不一致")
            if sum(v["traffic"] for v in td["periods"].values()) != td["traffic"]:
                errors.append(f"{sid} {date}: 外卖时段客流加和不一致")
            dish_rev = sum(d["revenue"] for d in day.get("dishes") or [])
            if abs(dish_rev - day["revenue"]) > 0.01:
                errors.append(f"{sid} {date}: 菜品营收加和不一致")
    return errors
