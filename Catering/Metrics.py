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


# 计算一个列表中所有非 None 数值的平均值。如果列表中没有有效数值，则返回 None。
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
'''
实现了一个用于业务指标拆解的经典方法——对数均值迪氏指数分解（LMDI），常用于将营业额的变动分解为客流贡献和客单价贡献


例子1 ：将 20,000 元的增长拆解为客流增长和客单价增长各自贡献了多少。
    cur_revenue：当前期营业额 120000 元
    cur_traffic：当前期客流量 6000 人
    base_revenue：基期营业额 100000 元
    base_traffic：基期客流量 5000 人

    1. total_delta = 120000 - 100000 = 20000
    2. ratio = 120000 / 100000 = 1.2
    3. 对数均值 L = 20000 / ln(1.2) = 20000 / 0.18232 ≈ 109695 （此处 ln(1.2) 约 0.1823）
    4. 客流贡献：L * ln(6000/5000) = 109695 * ln(1.2) = 109695 * 0.18232 ≈ 20000
        此时客流贡献占总变动的 100%？等一下，算出来可能不准确，重新计算
        1. 实际上，ln(6000/5000) = ln(1.2) ≈ 0.18232，和 ln(ratio) 相同，因为 ratio = cur_revenue / base_revenue，而 cur_revenue = cur_traffic * cur_ticket，所以这里客
        流量和营业额同比例增长（都是 1.2 倍），则客流贡献应为全部增长，客单价贡献为 0。
        2. 精确计算：traffic_contrib = 109695 * 0.18232 ≈ 20000
        3. ticket_contrib = 20000 - 20000 = 0
        4. 占比：traffic_share = 1.0，ticket_share = 0.0
    5. 总结: 营业额增长了 20%，客流量也增长了 20%，说明客单价没有变化，增长完全由客流驱动

例子2： 如果客流量增长 10%，营业额增长 20%，则客单价必然上升。
    基期：base_revenue=1000, base_traffic=100（客单价=10）
    当期：cur_revenue=1200, cur_traffic=110（客单价≈10.91）

    1. total_delta = 200
    2. ratio = 1.2
    3. log_mean = 200 / ln(1.2) ≈ 200/0.1823 ≈ 1097
    4. traffic_contrib = 1097 * ln(110/100) = 1097 * ln(1.1) ≈ 1097 * 0.09531 ≈ 104.5
    5. ticket_contrib = 200 - 104.5 ≈ 95.5
    6. traffic_share ≈ 0.5225，ticket_share ≈ 0.4775
    7. 总结: 即客流贡献约 104.5 元，客单价贡献约 95.5 元，两者共同推动增长，且无残差。
'''
def lmdi_decompose(cur_revenue: float, cur_traffic: float,
                   base_revenue: float, base_traffic: float) -> dict:
    """营业额 = 客流 × 客单价 的对数均值(LMDI)拆解。

    客流贡献 = L·ln(T_t/T_0),客单价贡献 = ΔR - 客流贡献(精确无残差)。
    基期/当期任一侧非正数时抛 ValueError,由调用方标记结构变化,不猜值。
    """
    # 异常检查与初值验证
    # 确保所有输入为正数，否则无法计算对数（math.log 要求参数 > 0）。
    if min(cur_revenue, cur_traffic, base_revenue, base_traffic) <= 0:
        raise ValueError("LMDI 拆解要求基期与当期营收/客流均为正数")
    # total_delta：营业额的绝对变化量（当期 - 基期）。
    total_delta = cur_revenue - base_revenue
    # ratio：营业额的变化倍数（当期 / 基期），用于计算对数均值。
    ratio = cur_revenue / base_revenue

    # 计算对数均值 (Logarithmic Mean)
    # 对数均值（L）是 LMDI 方法的核心，定义为 (Y_t - Y_0) / ln(Y_t / Y_0)，其中 Y 是被分解的变量（此处是营业额）
    # 当 ratio == 1 时，对数均值为 base_revenue（即恒等值），避免除以零。否则按公式计算。
    log_mean = total_delta / math.log(ratio) if ratio != 1 else base_revenue

    # 计算客流贡献与客单价贡献
    # 客流贡献：对数均值 × 客流的变化对数。它衡量了客流变化对营业额的贡献（以货币单位计）
    traffic_contrib = log_mean * math.log(cur_traffic / base_traffic)
    # 客单价贡献：总变动减去客流贡献，为剩余部分。由于 LMDI 满足完全分解（无残差），两者之和恰好等于 total_delta
    ticket_contrib = total_delta - traffic_contrib
    return {
        "total_delta": round(total_delta, 2), # 营业额的绝对变化
        "traffic_contrib": round(traffic_contrib, 2), # 客流带来的绝对贡献
        "ticket_contrib": round(ticket_contrib, 2), # 客单价带来的绝对贡献
        "traffic_share": traffic_contrib / total_delta, # 客流贡献占总变动的比例
        "ticket_share": ticket_contrib / total_delta, # 客单价贡献占总变动的比例
    }


def top_negative(rows: list[dict], delta_field: str,
                 total_delta: float | None, name_key: str) -> dict:
    """降幅集中度:总变化为负时,找出下拉最大的行及其占总支出的份额。

    name_key 决定结果中的名称键(如 top_negative_period /
    top_negative_channel),份额键统一为 top_negative_share。
    """
    empty = {name_key: None, "top_negative_share": None}
    # 如果 total_delta 为 None 或 非负（即没有下降），则直接返回空结果，因为分析负向变化无意义
    if total_delta is None or total_delta >= 0:
        return empty
    # 构建一个字典 negatives，键为行的 "name" 字段，值为该行的 delta 值（负值）
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

"""
用于拆解加权平均客单价的变化，将其分解为结构效应（品类份额变化）和价格效应（品类自身定价变化），且两者之和精确等于总变化（无残差）。
这在零售、餐饮等行业常用于分析客单价变动是由“顾客买什么”还是“东西卖多少钱”造成的

当期菜品结构 (cur["dishes"])：
品类	份额 (st)	客单价 (pt)
引流套餐	0.30	12.0
商务套餐	0.25	60.0
招牌菜	    0.20	70.0
面食小吃	0.15	25.0
饮品	    0.10	15.0

基期菜品结构 (base_agg)：
品类	份额 (s0)	客单价 (p0)
引流套餐	0.05	30.0
商务套餐	0.35	60.0
招牌菜	    0.30	70.0
面食小吃	0.20	25.0
饮品	    0.10	15.0

分品类计算
1. 引流套餐
    st - s0 = 0.30 - 0.05 = 0.25
    p0 = 30.0 → 结构效应：0.25 * 30.0 = 7.5
    pt - p0 = 12.0 - 30.0 = -18.0 → 价格效应：0.30 * (-18.0) = -5.4
    基期贡献：s0 * p0 = 0.05 * 30.0 = 1.5
    当期贡献：st * pt = 0.30 * 12.0 = 3.6

2. 商务套餐
    st - s0 = 0.25 - 0.35 = -0.10
    p0 = 60.0 → 结构效应：-0.10 * 60.0 = -6.0
    pt - p0 = 0 → 价格效应：0.25 * 0 = 0
    基期贡献：0.35 * 60.0 = 21.0
    当期贡献：0.25 * 60.0 = 15.0

3. 招牌菜
    st - s0 = 0.20 - 0.30 = -0.10
    p0 = 70.0 → 结构效应：-0.10 * 70.0 = -7.0
    pt - p0 = 0 → 价格效应：0
    基期贡献：0.30 * 70.0 = 21.0
    当期贡献：0.20 * 70.0 = 14.0

4. 面食小吃
    st - s0 = 0.15 - 0.20 = -0.05
    p0 = 25.0 → 结构效应：-0.05 * 25.0 = -1.25
    pt - p0 = 0 → 价格效应：0
    基期贡献：0.20 * 25.0 = 5.0
    当期贡献：0.15 * 25.0 = 3.75

5. 饮品
    st - s0 = 0.10 - 0.10 = 0
    结构效应：0
    pt - p0 = 0 → 价格效应：0
    基期贡献：0.10 * 15.0 = 1.5
    当期贡献：0.10 * 15.0 = 1.5 

汇总结果
    a0 = 1.5 + 21.0 + 21.0 + 5.0 + 1.5 = 50.0
    at = 3.6 + 15.0 + 14.0 + 3.75 + 1.5 = 37.85
    ticket_delta = 37.85 - 50.0 = -12.15
    mix = 7.5 + (-6.0) + (-7.0) + (-1.25) + 0 = -6.75
    price = -5.4 + 0 + 0 + 0 + 0 = -5.4
    验证：mix + price = -6.75 + (-5.4) = -12.15 ✅ 精确匹配

加权客单价下降了 12.15 元。
    结构效应（-6.75 元）：顾客转向了更多引流套餐（低客单价）而减少了商务套餐等高价品类。
    价格效应（-5.4 元）：引流套餐主动降价（从 30 元降至 12 元），拉低了整体均价。

"""
def mix_price_decomposition(cur_dishes: list[dict],
                            base_dishes: list[dict]) -> dict:
    """菜品结构拆解:加权均价变化 ΔA = 结构效应(mix) + 价格效应(price),精确无残差。

    mix   = Σ (s_t - s_0)·p_0   结构向低价/高价品类偏移的影响
    price = Σ s_t·(p_t - p_0)   品类自身定价变化的影响
    """
    '''
    将基期 (base_dishes) 和当期 (cur_dishes) 的菜品列表转换为以品类名为键的字典，方便快速查找。
    categories 取两个字典键的并集，确保所有出现过的品类都被纳入计算（缺失一侧用 0 处理）。
    '''
    base_map = {d["category"]: d for d in base_dishes}
    cur_map = {d["category"]: d for d in cur_dishes}
    categories = set(base_map) | set(cur_map)
    '''
    mix（结构效应）：份额变化 × 基期价格。
    price（价格效应）：当期份额 × 价格变化。
    a0：基期加权均价（基期各品类份额 × 价格之和）
    at：当期加权均价（当期各品类份额 × 价格之和）
    '''
    mix = price = a0 = at = 0.0
    for cat in categories:
        '''
        若基期不存在，则 s0=0, p0=0；若当期不存在，则 st=0, pt=0。
        结构效应：(st - s0) * p0，衡量品类份额向高/低价品类偏移的影响（以基期价格固定）。
        价格效应：st * (pt - p0)，衡量品类自身定价变化的影响（以当期份额加权）。
        累积 a0 和 at
        '''
        b, c = base_map.get(cat), cur_map.get(cat)
        s0, p0 = (b["share"], b["ticket"]) if b else (0.0, 0.0)
        st, pt = (c["share"], c["ticket"]) if c else (0.0, 0.0)
        mix += (st - s0) * p0
        price += st * (pt - p0)
        a0 += s0 * p0
        at += st * pt
    return {
        "baseline_weighted_ticket": a0, # 基期加权均价
        "weighted_ticket": at, # 当期加权均价
        "ticket_delta": at - a0, # ticket_delta：两者之差，等于 mix + price （保证无残差）
        "mix_effect": mix,
        "price_effect": price
    }


# ── 告警扫描 ──
'''
对每个店铺，检查最近一日的营收相对于前 7 日均值的变化幅度，若超过 ±10% 则生成告警
营收: [90, 85, 92, 88, 95, 87, 93, 100, 98, 115], 下标是天数
确定窗口：n = 7，需要至少 8 天数据，这里满足
最近一天：last = days[-1] → 营收 = 115
计算基线：取 days[-8:-1]，即索引 2~8 的 7 天数据：
    值：[92, 88, 95, 87, 93, 100, 98]
    平均值 = (92+88+95+87+93+100+98) / 7 = 653 / 7 = 93.2857
计算变化率：
    pct_change(115, 93.2857) = (115 - 93.2857) / 93.2857 * 100 ≈ 23.27%
    绝对值 23.27% ≥ 10%，触发告警
'''
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
