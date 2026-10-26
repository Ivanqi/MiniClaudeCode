"""确定性生成案例库测试语料(>=100 条)。

输出 `data/cases/synthetic_cases.json`:
- 18 类经营异常原型 × 6 家历史门店 = 108 条基础案例;
- 4 条边界案例(未来日期/已废弃/已过期)用于检索过滤测试;
- 全部日期、指标、文案可复现,不依赖随机数,便于回归与人工阅读。

用法: python Catering/gen_case_corpus.py
"""
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

OUTPUT = Path(__file__).resolve().parent / "data" / "cases" / "synthetic_cases.json"
STORES = ("A店", "B店", "C店", "D店", "E店", "F店")

# 每类原型覆盖一种真实经营场景;门店/日期/指标在生成时做确定性变化。
ARCHETYPES = [
    {
        "key": "weather_traffic",
        "main_factor": "traffic",
        "dimensions": ["traffic", "channels", "external"],
        "metric_key": "traffic.pct_change",
        "tags": ["恶劣天气", "客流下滑", "线下"],
        "summary": "连续强降雨导致商圈客流减少 {pct}%,到店客流降幅高于线上;天气转好后逐步恢复。",
        "symptom": "到店客流明显减少",
        "outcome": "天气转好后 1-2 日恢复",
        "actions": ["暴雨时段加大线上渠道曝光", "推送雨天到店券",
                    "关注天气恢复后的客流反弹"],
    },
    {
        "key": "competitor_traffic",
        "main_factor": "traffic",
        "dimensions": ["traffic", "promotion", "external"],
        "metric_key": "traffic.pct_change",
        "tags": ["竞对活动", "客流分流", "商圈"],
        "summary": "同商圈竞对开展开业促销,分流客流约 {pct}%;本店未跟进活动,客单基本稳定。",
        "symptom": "同商圈客流被分流",
        "outcome": "竞对活动结束后回升",
        "actions": ["监控竞对活动周期与折扣力度", "非高峰时段定向发会员券",
                    "强化招牌菜品差异化"],
    },
    {
        "key": "renovation_traffic",
        "main_factor": "traffic",
        "dimensions": ["traffic", "periods", "channels"],
        "metric_key": "traffic.pct_change",
        "tags": ["门店施工", "动线调整", "进店"],
        "summary": "门店后厨改造期间动线受限,高峰时段客流下降 {pct}%;改造完成后恢复。",
        "symptom": "高峰时段动线受限",
        "outcome": "改造完成后恢复",
        "actions": ["施工期调整营业动线与取餐口", "提前告知会员并发放补偿券",
                    "改造完成后做开业预热"],
    },
    {
        "key": "roadwork_traffic",
        "main_factor": "traffic",
        "dimensions": ["traffic", "external", "channels"],
        "metric_key": "traffic.pct_change",
        "tags": ["道路施工", "围挡", "进店"],
        "summary": "门前道路围挡施工,到店便利性下降,客流减少 {pct}%;施工结束后回升。",
        "symptom": "到店便利性下降",
        "outcome": "施工结束后两周恢复",
        "actions": ["在围挡处增加指引与备用入口", "加大线上配送覆盖",
                    "与商圈物业沟通施工进度"],
    },
    {
        "key": "holiday_traffic",
        "main_factor": "traffic",
        "dimensions": ["traffic", "periods", "external"],
        "metric_key": "traffic.pct_change",
        "tags": ["节假日", "客流波动", "排班"],
        "summary": "节假日返程效应导致本地客流下滑 {pct}%,时段分布明显后移。",
        "symptom": "本地客流短期回落",
        "outcome": "假期结束后回归常态",
        "actions": ["按节假日客流曲线调整排班", "返程日减少备货",
                    "节前预售锁定客流"],
    },
    {
        "key": "menu_price_traffic",
        "main_factor": "traffic",
        "dimensions": ["traffic", "dish", "ticket"],
        "metric_key": "traffic.pct_change",
        "tags": ["菜单涨价", "客流", "客单"],
        "summary": "菜单提价后价格敏感客群流失,客流下降 {pct}%,客单小幅上升。",
        "symptom": "价格敏感客群流失",
        "outcome": "调价两周后客流趋稳",
        "actions": ["对价格敏感单品做小规格版本", "观察两周价格弹性再调整",
                    "会员价与正价分层"],
    },
    {
        "key": "food_safety_traffic",
        "main_factor": "traffic",
        "dimensions": ["traffic", "rating", "channels", "external"],
        "metric_key": "traffic.pct_change",
        "tags": ["食安投诉", "口碑", "客流"],
        "summary": "社交平台出现食安投诉,门店口碑受影响,客流下降 {pct}%;整改后差评回落。",
        "symptom": "平台差评集中出现",
        "outcome": "整改后差评回落",
        "actions": ["下架涉事批次并复检", "公开整改与客服回访",
                    "加强后厨晨检与留样"],
    },
    {
        "key": "payment_outage_traffic",
        "main_factor": "traffic",
        "dimensions": ["traffic", "periods", "channels"],
        "metric_key": "traffic.pct_change",
        "tags": ["支付故障", "时段", "结算"],
        "summary": "支付网络故障导致部分时段无法结算,有效营业时长缩短,客流下降 {pct}%。",
        "symptom": "有效营业时段缩短",
        "outcome": "故障修复后当日恢复",
        "actions": ["启用备用支付与离线结算", "故障时段引导线上下单",
                    "补录订单并核对账目"],
    },
    {
        "key": "discount_ticket",
        "main_factor": "ticket",
        "dimensions": ["ticket", "dish", "promotion"],
        "metric_key": "ticket.pct_change",
        "tags": ["优惠券", "客单下滑", "毛利承压"],
        "summary": "大额优惠券投放拉低客单价 {pct}%,客流小幅上升,毛利承压。",
        "symptom": "毛利被折扣侵蚀",
        "outcome": "活动结束后毛利修复",
        "actions": ["控制大额券发放数量与时段", "设置毛利底线",
                    "活动后评估新客复购"],
    },
    {
        "key": "mix_ticket",
        "main_factor": "ticket",
        "dimensions": ["ticket", "dish"],
        "metric_key": "ticket.pct_change",
        "tags": ["品类结构", "低价产品", "连带销售"],
        "summary": "低价产品占比上升,品类结构变化拉低客单价 {pct}%,连带销售下降。",
        "symptom": "低价产品占比上升",
        "outcome": "调整产品结构后改善",
        "actions": ["提高高毛利主推品曝光", "设计组合套餐提升连带",
                    "限制低价产品占比"],
    },
    {
        "key": "member_ticket",
        "main_factor": "ticket",
        "dimensions": ["ticket", "promotion"],
        "metric_key": "ticket.pct_change",
        "tags": ["会员权益", "折扣", "客单"],
        "summary": "会员折扣权益叠加导致客单价下降 {pct}%,复购频次上升但毛利收窄。",
        "symptom": "会员折扣透支客单",
        "outcome": "收紧权益后客单回升",
        "actions": ["收紧会员折扣叠加规则", "用积分替代直接降价",
                    "分层运营高价值会员"],
    },
    {
        "key": "delivery_channel_ticket",
        "main_factor": "ticket",
        "dimensions": ["ticket", "channels"],
        "metric_key": "ticket.pct_change",
        "tags": ["外卖佣金", "渠道结构", "毛利"],
        "summary": "外卖平台佣金与配送费调整,渠道毛利下降,客单价下滑 {pct}%。",
        "symptom": "外卖渠道毛利承压",
        "outcome": "调整渠道策略后毛利改善",
        "actions": ["重算外卖渠道毛利模型", "调整满减与配送费",
                    "提升高毛利单品在外卖菜单的权重"],
    },
    {
        "key": "promotion_cannibalization",
        "main_factor": "ticket",
        "dimensions": ["ticket", "promotion", "dish"],
        "metric_key": "ticket.pct_change",
        "tags": ["促销蚕食", "毛利", "客单"],
        "summary": "高频促销蚕食正价销售,客单价下降 {pct}%,活动 ROI 低于预期。",
        "symptom": "正价销售被促销蚕食",
        "outcome": "停止高频促销后恢复",
        "actions": ["暂停高频深度折扣", "用限时活动替代常态化促销",
                    "监控活动 ROI 与毛利"],
    },
    {
        "key": "platform_rank_traffic",
        "main_factor": "traffic",
        "dimensions": ["traffic", "channels"],
        "metric_key": "traffic.pct_change",
        "tags": ["外卖排名", "曝光", "客流"],
        "summary": "外卖平台排名下滑导致曝光减少,客流下降 {pct}%,线上订单同步走弱。",
        "symptom": "外卖曝光与订单下滑",
        "outcome": "优化菜单后排位回升",
        "actions": ["优化外卖菜单与出餐时长", "参与平台流量活动",
                    "提升评分与好评率"],
    },
    {
        "key": "period_lunch_traffic",
        "main_factor": "traffic",
        "dimensions": ["traffic", "periods"],
        "metric_key": "traffic.pct_change",
        "tags": ["午市", "时段", "写字楼"],
        "summary": "写字楼午市客流减少 {pct}%,时段营收集中度下降。",
        "symptom": "午市客流集中下降",
        "outcome": "团餐补充后午市改善",
        "actions": ["午市推写字楼团餐", "调整午市排班与出餐动线",
                    "在非高峰时段做引流"],
    },
    {
        "key": "rating_service",
        "main_factor": "none",
        "dimensions": ["rating", "staff"],
        "metric_key": "rating.delta",
        "tags": ["差评", "服务", "出餐"],
        "summary": "集中差评指向出餐速度与服务态度,评分下降 {value},营收暂未明显波动。",
        "symptom": "差评指向服务体验",
        "outcome": "培训后评分回升",
        "actions": ["高峰增派人手并优化出餐流程", "针对差评逐条回访",
                    "开展服务话术培训"],
    },
    {
        "key": "staff_shortage",
        "main_factor": "none",
        "dimensions": ["staff", "periods"],
        "metric_key": "attendance_ratio",
        "tags": ["排班缺口", "高峰人力", "出餐"],
        "summary": "高峰时段排班缺口导致出餐慢,出勤率 {value}%,顾客体验下降。",
        "symptom": "高峰人力不足",
        "outcome": "补齐排班后出餐改善",
        "actions": ["补齐高峰班次", "设置跨店支援机制", "用小时工覆盖缺口"],
    },
    {
        "key": "refund_complaint",
        "main_factor": "none",
        "dimensions": ["rating", "channels"],
        "metric_key": "complaints",
        "tags": ["退款", "投诉", "口碑"],
        "summary": "退款与投诉量上升,口碑指标走弱,退款 {value} 起,营收暂未明显波动。",
        "symptom": "退款与投诉集中",
        "outcome": "投诉处理机制上线后下降",
        "actions": ["建立退款原因分类", "24 小时内处理投诉",
                    "复盘高频问题并整改"],
    },
]

SPECIAL_CASES = [
    {
        "case_id": "CASE-SYN-FUTURE-1",
        "store_id": "C店",
        "date": "2026-12-01",
        "main_factor": "traffic",
        "dimensions": ["traffic", "external"],
        "summary": "台风预警触发临时闭店,当日无营业数据,未形成可复用结论。",
        "actions": ["仅在日期过滤测试中使用"],
        "tags": ["台风预警", "临时闭店"],
        "symptoms": ["临时闭店", "无营业数据"],
        "metrics_snapshot": {},
        "outcome": "未验证",
        "effective_from": "2026-12-01",
        "effective_to": "",
        "deprecated": False,
        "confidence": 0.2,
        "source": "synthetic:corpus:filter",
        "version": 1,
        "created_at": datetime(2026, 12, 1, tzinfo=timezone.utc).timestamp(),
        "updated_at": datetime(2026, 12, 1, tzinfo=timezone.utc).timestamp(),
    },
    {
        "case_id": "CASE-SYN-DEPRECATED-1",
        "store_id": "C店",
        "date": "2026-08-15",
        "main_factor": "traffic",
        "dimensions": ["periods", "channels"],
        "summary": "POS 系统宕机导致收银中断,数据补录后口径失真,案例已废弃。",
        "actions": ["更换收银终端", "补录期间订单"],
        "tags": ["POS宕机", "收银中断"],
        "symptoms": ["收银中断", "数据失真"],
        "metrics_snapshot": {"downtime_minutes": 95},
        "outcome": "案例已废弃,不再作为处置参考",
        "effective_from": "2026-08-15",
        "effective_to": "",
        "deprecated": True,
        "confidence": 0.2,
        "source": "synthetic:corpus:filter",
        "version": 1,
        "created_at": datetime(2026, 8, 15, tzinfo=timezone.utc).timestamp(),
        "updated_at": datetime(2026, 8, 15, tzinfo=timezone.utc).timestamp(),
    },
    {
        "case_id": "CASE-SYN-EXPIRED-1",
        "store_id": "C店",
        "date": "2025-01-10",
        "main_factor": "ticket",
        "dimensions": ["ticket", "dish"],
        "summary": "旧版菜单价格调整导致客单下滑,适用期已结束。",
        "actions": ["恢复旧价", "观察两周"],
        "tags": ["旧版菜单", "价格调整"],
        "symptoms": ["客单下滑"],
        "metrics_snapshot": {"ticket.pct_change": -9.0},
        "outcome": "旧版菜单已下线",
        "effective_from": "2025-01-10",
        "effective_to": "2025-06-30",
        "deprecated": False,
        "confidence": 0.4,
        "source": "synthetic:corpus:filter",
        "version": 1,
        "created_at": datetime(2025, 1, 10, tzinfo=timezone.utc).timestamp(),
        "updated_at": datetime(2025, 1, 10, tzinfo=timezone.utc).timestamp(),
    },
    {
        "case_id": "CASE-SYN-EXPIRED-2",
        "store_id": "D店",
        "date": "2025-02-10",
        "main_factor": "none",
        "dimensions": ["staff", "periods"],
        "summary": "旧版排班规则导致高峰出餐慢,已停用。",
        "actions": ["启用新排班模板"],
        "tags": ["旧版排班", "高峰班次"],
        "symptoms": ["出餐慢"],
        "metrics_snapshot": {"attendance_ratio": 0.85},
        "outcome": "新排班模板已替换",
        "effective_from": "2025-02-10",
        "effective_to": "2025-12-31",
        "deprecated": False,
        "confidence": 0.4,
        "source": "synthetic:corpus:filter",
        "version": 1,
        "created_at": datetime(2025, 2, 10, tzinfo=timezone.utc).timestamp(),
        "updated_at": datetime(2025, 2, 10, tzinfo=timezone.utc).timestamp(),
    },
]


def _metric_values(archetype: dict, ai: int, si: int) -> tuple[dict, float, int]:
    """返回 (metrics_snapshot, 数值用于摘要, 展示值)。"""
    key = archetype["metric_key"]
    pct = 12 + (ai * 3 + si * 2) % 15
    if key == "traffic.pct_change":
        return ({"traffic.pct_change": -pct,
                 "revenue.pct_change": round(-pct * 0.65, 1),
                 "sample_days": 7}, pct, pct)
    if key == "ticket.pct_change":
        return ({"ticket.pct_change": -pct,
                 "revenue.pct_change": round(-pct * 0.55, 1),
                 "sample_days": 7}, pct, pct)
    if key == "rating.delta":
        drop = round(0.3 + ((ai + si) % 5) * 0.1, 1)
        return ({"rating.delta": -drop,
                 "complaints": 3 + (ai + si) % 10,
                 "review_count": 20 + (ai * 3 + si * 2) % 50}, drop, drop)
    if key == "attendance_ratio":
        ratio = 78 + ((ai + si) % 8) * 2
        return ({"attendance_ratio": ratio / 100,
                 "staff_shortage_hours": 2 + (ai + si) % 6}, ratio, ratio)
    complaints = 4 + (ai * 2 + si) % 12
    return ({"complaints": complaints,
             "refund_count": max(1, complaints // 2)}, complaints, complaints)


def build_cases() -> list[dict]:
    cases: list[dict] = []
    base_day = date(2025, 2, 15)
    index = 1
    for ai, archetype in enumerate(ARCHETYPES):
        for si, store in enumerate(STORES):
            day = base_day + timedelta(days=ai * 17 + si * 5)
            metrics, value, display = _metric_values(archetype, ai, si)
            stamp = datetime(day.year, day.month, day.day,
                             tzinfo=timezone.utc).timestamp()
            if archetype["main_factor"] == "traffic":
                symptoms = [f"客流同比下降 {display}%",
                            archetype["symptom"]]
                summary = archetype["summary"].format(pct=display)
            elif archetype["main_factor"] == "ticket":
                symptoms = [f"客单价同比下降 {display}%",
                            archetype["symptom"]]
                summary = archetype["summary"].format(pct=display)
            elif archetype["metric_key"] == "attendance_ratio":
                symptoms = [f"出勤率 {display}%", archetype["symptom"]]
                summary = archetype["summary"].format(value=display)
            elif archetype["metric_key"] == "complaints":
                symptoms = [f"退款/投诉 {display} 起",
                            archetype["symptom"]]
                summary = archetype["summary"].format(value=display)
            else:
                symptoms = [f"评分下降 {display}", archetype["symptom"]]
                summary = archetype["summary"].format(value=display)
            cases.append({
                "case_id": f"CASE-SYN-{index:04d}",
                "store_id": store,
                "date": day.isoformat(),
                "main_factor": archetype["main_factor"],
                "dimensions": list(archetype["dimensions"]),
                "summary": summary,
                "actions": list(archetype["actions"]),
                "tags": list(archetype["tags"]),
                "symptoms": symptoms,
                "metrics_snapshot": metrics,
                "outcome": archetype["outcome"],
                "effective_from": day.isoformat(),
                "effective_to": "",
                "deprecated": False,
                "confidence": 0.55,
                "source": "synthetic:corpus",
                "version": 1,
                "created_at": stamp,
                "updated_at": stamp,
            })
            index += 1
    cases.extend(SPECIAL_CASES)
    return cases


def main() -> int:
    cases = build_cases()
    ids = [case["case_id"] for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("生成案例存在重复 case_id")
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps({
        "schema_version": 1,
        "description": "确定性生成的案例库测试语料:18 类场景 × 6 家门店 + 4 条过滤边界案例。",
        "cases": cases,
    }, ensure_ascii=False, indent=2))
    print(f"wrote {len(cases)} cases -> {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
