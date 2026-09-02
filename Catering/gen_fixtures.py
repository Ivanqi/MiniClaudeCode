"""样本数据生成器:确定性生成 3 店 × 8 日经营数据与期望贡献值。

运行: python Catering/gen_fixtures.py   (确定性重跑,覆盖 data/ 下生成产物)

口径说明:
- 基准 = 告警日(2026-09-01)前 7 日均值,前 7 日数据全部取锚点值(均匀),
  保证同比变化率与锚点精确一致;
- 三家门店场景: A店 客流型异常(-18%)、B店 客单价型异常(-10%)、C店 正常对照(+2%);
- 数据按 8 类数据源分文件: stores(门店经营库)/pos(堂食)/takeout(外卖)/
  internal(内部系统·活动)/external(第三方·天气/市场)/cases(知识库,手写不生成);
- 本文件内联实现 LMDI 贡献度公式(与 Metrics.py 独立),结果写入
  expected_contributions.json,供测试与评测交叉验证;
- 生成后自断言加和一致:Σ时段=日总、Σ渠道=日总、每渠道 revenue=客流×客单、Σ菜品=日总。

注意: 本脚本是开发期工具,不参与运行时;运行时数据源接口见 DataHub.py。
"""
import json
import math
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"

DAYS = [f"2026-08-{d:02d}" for d in range(25, 32)] + ["2026-09-01"]
ALERT_DATE = "2026-09-01"
PERIOD_NAMES = ["早市", "午市", "下午茶", "晚市", "夜宵"]


# ── 分配辅助 ──

def distribute_floats(weights: list[float], total: float) -> list[float]:
    """按权重比例分配 total(2 位小数),最后一项吸收舍入残差。"""
    s = sum(weights)
    if s == 0:
        return [total] + [0.0] * (len(weights) - 1)
    out = [round(total * w / s, 2) for w in weights]
    out[-1] = round(total - sum(out[:-1]), 2)
    return out


def distribute_ints(weights: list[float], total: int) -> list[int]:
    """按权重比例分配 total(整数,客流),最后一项吸收残差。"""
    s = sum(weights)
    if s == 0:
        return [total] + [0] * (len(weights) - 1)
    out = [int(round(total * w / s)) for w in weights]
    out[-1] = total - sum(out[:-1])
    return out


def build_day(date: str, revenue: float, traffic: int,
              dinein_revenue: float, dinein_traffic: int,
              takeout_revenue: float, takeout_traffic: int,
              period_traffic: list[int], dishes: list[dict],
              rating: float, reviews: int, complaints: int,
              staff_on_duty: int, staff_planned: int) -> dict:
    """组装一天的完整记录:store_db + pos + takeout 三份数据。

    period_traffic 为全店各时段客流;渠道内时段按渠道客流占比同分布,
    时段营收按渠道营收占比同分布(渠道内客单均匀假设)。
    """
    store = {
        "date": date,
        "revenue": round(revenue, 2),
        "traffic": traffic,
        "ticket": round(revenue / traffic, 6),
        "rating": rating,
        "review_count": reviews,
        "complaints": complaints,
        "staff_on_duty": staff_on_duty,
        "staff_planned": staff_planned,
        "dishes": build_dishes(dishes, revenue),
    }

    def channel_day(rev: float, tr: int) -> dict:
        weights = period_traffic
        periods = {}
        p_rev = distribute_floats(weights, rev)
        p_tr = distribute_ints(weights, tr)
        for name, r, t in zip(PERIOD_NAMES, p_rev, p_tr):
            periods[name] = {"revenue": r, "traffic": t}
        return {"date": date, "revenue": round(rev, 2), "traffic": tr,
                "orders": tr, "periods": periods}

    return {
        "store": store,
        "pos": channel_day(dinein_revenue, dinein_traffic),
        "takeout": channel_day(takeout_revenue, takeout_traffic),
    }


def build_dishes(dishes: list[dict], day_revenue: float) -> list[dict]:
    """菜品结构: {category, share, ticket} -> {category, revenue, orders, ticket, share}。

    份额归一化、营收按份额分配(最后一项吸收残差)、订单数 = 营收/客单取整。
    注意: 品类客单以"订单"为分母,与门店客单价(客流分母)口径不同。
    """
    total_share = sum(d["share"] for d in dishes)
    rows = []
    for i, d in enumerate(dishes):
        share = d["share"] / total_share
        revenue = share * day_revenue if i < len(dishes) - 1 else None
        rows.append({"category": d["category"], "share": round(share, 6),
                     "ticket": d["ticket"], "revenue": revenue})
    assigned = sum(r["revenue"] for r in rows[:-1])
    rows[-1]["revenue"] = round(day_revenue - assigned, 2)
    for r in rows:
        r["orders"] = int(round(r["revenue"] / r["ticket"]))
    return rows


# ── 三家门店场景 ──

def gen_store_a() -> dict:
    """A店: 客流型异常。D7 暴雨+竞对新店促销,堂食客流暴跌,外卖上升。

    锚点: 基准 营收 99,000 / 客流 1,800 / 客单 55;
          D7   营收 81,180 (-18%) / 客流 1,350 (-25%) / 客单 60.13 (+9.3%)。
    """
    base_kwargs = dict(
        revenue=99000.0, traffic=1800,
        dinein_revenue=88000.0, dinein_traffic=1620,
        takeout_revenue=11000.0, takeout_traffic=180,
        period_traffic=[90, 720, 180, 720, 90],
        dishes=[{"category": "招牌炒菜", "share": 0.35, "ticket": 60.0},
                {"category": "面食快餐", "share": 0.25, "ticket": 30.0},
                {"category": "套餐组合", "share": 0.25, "ticket": 55.0},
                {"category": "饮品甜点", "share": 0.15, "ticket": 20.0}],
        rating=4.5, reviews=42, complaints=0, staff_on_duty=12, staff_planned=12)
    d7_kwargs = dict(
        revenue=81180.0, traffic=1350,
        dinein_revenue=62280.0, dinein_traffic=1080,
        takeout_revenue=18900.0, takeout_traffic=270,
        period_traffic=[80, 420, 140, 590, 120],
        dishes=[{"category": "招牌炒菜", "share": 0.35, "ticket": 60.0},
                {"category": "面食快餐", "share": 0.25, "ticket": 30.0},
                {"category": "套餐组合", "share": 0.25, "ticket": 55.0},
                {"category": "饮品甜点", "share": 0.15, "ticket": 20.0}],
        rating=4.4, reviews=38, complaints=1, staff_on_duty=12, staff_planned=12)
    return {"store_id": "A店", "base": base_kwargs, "d7": d7_kwargs}


def gen_store_b() -> dict:
    """B店: 客单价型异常。D7 上线 9.9 元引流套餐,客流大涨但客单价被拉低。

    锚点: 基准 营收 88,000 / 客流 1,600 / 客单 55;
          D7   营收 79,200 (-10%) / 客流 1,920 (+20%) / 客单 41.25 (-25%)。
    """
    base_kwargs = dict(
        revenue=88000.0, traffic=1600,
        dinein_revenue=52000.0, dinein_traffic=1000,
        takeout_revenue=36000.0, takeout_traffic=600,
        period_traffic=[80, 800, 80, 560, 80],
        dishes=[{"category": "商务套餐", "share": 0.35, "ticket": 60.0},
                {"category": "招牌菜", "share": 0.30, "ticket": 70.0},
                {"category": "面食小吃", "share": 0.20, "ticket": 25.0},
                {"category": "饮品", "share": 0.10, "ticket": 15.0},
                {"category": "引流套餐", "share": 0.05, "ticket": 30.0}],
        rating=4.6, reviews=55, complaints=0, staff_on_duty=10, staff_planned=10)
    d7_kwargs = dict(
        revenue=79200.0, traffic=1920,
        dinein_revenue=54720.0, dinein_traffic=1200,
        takeout_revenue=24480.0, takeout_traffic=720,
        period_traffic=[90, 1100, 100, 550, 80],
        dishes=[{"category": "引流套餐", "share": 0.30, "ticket": 12.0},
                {"category": "商务套餐", "share": 0.25, "ticket": 60.0},
                {"category": "招牌菜", "share": 0.20, "ticket": 70.0},
                {"category": "面食小吃", "share": 0.15, "ticket": 25.0},
                {"category": "饮品", "share": 0.10, "ticket": 15.0}],
        rating=4.5, reviews=91, complaints=2, staff_on_duty=10, staff_planned=10)
    return {"store_id": "B店", "base": base_kwargs, "d7": d7_kwargs}


def gen_store_c() -> dict:
    """C店: 正常对照。D7 全维度 +2%,不触发任何异常规则。"""
    base_kwargs = dict(
        revenue=120000.0, traffic=2000,
        dinein_revenue=96000.0, dinein_traffic=1600,
        takeout_revenue=24000.0, takeout_traffic=400,
        period_traffic=[100, 900, 200, 700, 100],
        dishes=[{"category": "家常菜", "share": 0.40, "ticket": 65.0},
                {"category": "套餐", "share": 0.30, "ticket": 55.0},
                {"category": "小吃", "share": 0.20, "ticket": 45.0},
                {"category": "饮品", "share": 0.10, "ticket": 20.0}],
        rating=4.7, reviews=30, complaints=0, staff_on_duty=14, staff_planned=14)
    d7_kwargs = dict(
        revenue=122400.0, traffic=2040,
        dinein_revenue=97920.0, dinein_traffic=1632,
        takeout_revenue=24480.0, takeout_traffic=408,
        period_traffic=[102, 918, 204, 714, 102],
        dishes=[{"category": "家常菜", "share": 0.40, "ticket": 65.0},
                {"category": "套餐", "share": 0.30, "ticket": 55.0},
                {"category": "小吃", "share": 0.20, "ticket": 45.0},
                {"category": "饮品", "share": 0.10, "ticket": 20.0}],
        rating=4.7, reviews=33, complaints=0, staff_on_duty=14, staff_planned=14)
    return {"store_id": "C店", "base": base_kwargs, "d7": d7_kwargs}


# ── 事件数据(内部系统 / 第三方 API) ──

PROMOTIONS = {"records": [
    {"store_id": "B店", "date": "2026-09-01", "name": "9.9元引流套餐",
     "type": "low_price_set", "channel": "堂食+外卖", "discount_pct": 55.0,
     "detail": "9.9 元套餐(原价 22 元)上线首日,主走外卖渠道,低价引流目的"},
]}

WEATHER = {"records": [
    {"store_id": "A店", "date": "2026-09-01", "value": "暴雨",
     "severity": "severe", "detail": "全天暴雨,橙色预警,出行受阻"},
]}

MARKET = {"events": [
    {"store_id": "A店", "date": "2026-09-01", "type": "competitor_promotion",
     "name": "竞对『蜀香居』新店开业全场7折", "distance_km": 0.5,
     "detail": "距 A店 500 米,9/1 开业大促,全场 7 折"},
]}


# ── LMDI 期望值(独立于 Metrics.py 的内联实现,用于交叉验证) ──

def lmdi(R0: float, Rt: float, T0: float, Tt: float) -> dict:
    """营业额 = 客流 × 客单价 的对数贡献度拆解(精确无残差)。"""
    if min(R0, Rt, T0, Tt) <= 0:
        raise ValueError("LMDI 要求基期/当期均为正数")
    log_mean = (Rt - R0) / math.log(Rt / R0) if Rt != R0 else R0
    traffic_contrib = log_mean * math.log(Tt / T0)
    total_delta = Rt - R0
    ticket_contrib = total_delta - traffic_contrib
    return {
        "total_delta": round(total_delta, 2),
        "traffic_contrib": round(traffic_contrib, 2),
        "ticket_contrib": round(ticket_contrib, 2),
        "traffic_share": round(traffic_contrib / total_delta, 6),
        "ticket_share": round(ticket_contrib / total_delta, 6),
    }


# ── 生成与自校验 ──

def generate() -> dict:
    """生成全部 fixture 文件,返回 {store_id: {base 日记录, d7 日记录}}。"""
    stores = [gen_store_a(), gen_store_b(), gen_store_c()]
    data: dict[str, list] = {"stores": [], "pos": [], "takeout": []}
    results = {}

    for store in stores:
        sid = store["store_id"]
        store_days, pos_days, takeout_days = [], [], []
        for date in DAYS:
            kwargs = store["d7"] if date == ALERT_DATE else store["base"]
            day = build_day(date, **kwargs)
            store_days.append(day["store"])
            pos_days.append(day["pos"])
            takeout_days.append(day["takeout"])
        results[sid] = {"store_days": store_days, "pos_days": pos_days,
                        "takeout_days": takeout_days}
        data["stores"].append({"store_id": sid, "days": store_days})
        data["pos"].append({"store_id": sid, "days": pos_days})
        data["takeout"].append({"store_id": sid, "days": takeout_days})

    # 自断言:加和一致 + 渠道自洽 + 菜品自洽
    for sid, r in results.items():
        for i, (s, p, t) in enumerate(zip(r["store_days"], r["pos_days"],
                                          r["takeout_days"])):
            assert abs((p["revenue"] + t["revenue"]) - s["revenue"]) < 0.01, \
                f"{sid} {s['date']} 渠道营收加和不一致"
            assert p["traffic"] + t["traffic"] == s["traffic"], \
                f"{sid} {s['date']} 渠道客流加和不一致"
            assert sum(v["traffic"] for v in p["periods"].values()) == p["traffic"], \
                f"{sid} {s['date']} 堂食时段加和不一致"
            assert sum(v["traffic"] for v in t["periods"].values()) == t["traffic"], \
                f"{sid} {s['date']} 外卖时段加和不一致"
            assert abs(sum(v["revenue"] for v in p["periods"].values()) - p["revenue"]) < 0.01, \
                f"{sid} {s['date']} 堂食时段营收加和不一致"
            assert abs(sum(v["revenue"] for v in t["periods"].values()) - t["revenue"]) < 0.01, \
                f"{sid} {s['date']} 外卖时段营收加和不一致"
            assert abs(sum(d["revenue"] for d in s["dishes"]) - s["revenue"]) < 0.01, \
                f"{sid} {s['date']} 菜品营收加和不一致"
            for i2, d in enumerate(s["dishes"]):
                assert abs(d["revenue"] / d["orders"] - d["ticket"]) < 0.51, \
                    f"{sid} {s['date']} 菜品 {d['category']} 客单不匹配"

    # 期望贡献值
    expected = {}
    for sid, r in results.items():
        base = r["store_days"][:-1]
        d7 = r["store_days"][-1]
        R0 = sum(d["revenue"] for d in base) / len(base)
        T0 = sum(d["traffic"] for d in base) / len(base)
        expected[sid] = {"date": ALERT_DATE,
                         "baseline_revenue": round(R0, 2),
                         "baseline_traffic": round(T0, 2),
                         **lmdi(R0, d7["revenue"], T0, d7["traffic"])}

    for category, payload in (("stores", data["stores"]),
                              ("pos", data["pos"]),
                              ("takeout", data["takeout"])):
        for entry in payload:
            (DATA_DIR / category / f"{entry['store_id']}.json").write_text(
                json.dumps(entry, ensure_ascii=False, indent=2))
    (DATA_DIR / "internal" / "promotions.json").write_text(
        json.dumps(PROMOTIONS, ensure_ascii=False, indent=2))
    (DATA_DIR / "external" / "weather.json").write_text(
        json.dumps(WEATHER, ensure_ascii=False, indent=2))
    (DATA_DIR / "external" / "market.json").write_text(
        json.dumps(MARKET, ensure_ascii=False, indent=2))
    (DATA_DIR / "expected_contributions.json").write_text(
        json.dumps({"schema_version": 1, **expected},
                   ensure_ascii=False, indent=2))
    return results


if __name__ == "__main__":
    for sub in ("stores", "pos", "takeout", "internal", "external", "cases"):
        (DATA_DIR / sub).mkdir(parents=True, exist_ok=True)
    generate()
    print(f"fixtures written to {DATA_DIR}")
