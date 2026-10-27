"""Catering CLI 入口:门店经营异常分析 Agent。

用法(在项目根目录运行):
  python -m Catering.main                      交互模式(工作流诊断)
  python -m Catering.main --store A店          诊断指定门店最近一日
  python -m Catering.main --store A店 --date 2026-09-01
  python -m Catering.main --eval               离线评测场景集(无需 API Key)
  python -m Catering.main --agentic "问题"     探索模式(LLM 自由调用餐饮工具)
  python -m Catering.main --list-alerts        扫描样本数据中的异常告警
  python -m Catering.main --no-llm ...         工作流模式不用 LLM(模板结论)
  python -m Catering.main --auto ...           跳过人工审批环节
  python -m Catering.main --report-dir DIR     指定运行产物目录

环境初始化(读 .env 构造真实 LLM 客户端)只发生在本文件;
Catering 包内模块禁止 import dotenv,保证测试离线。
"""
import argparse
import json
import sys
from pathlib import Path

from Catering.AnomalyAgent import AnomalyAgent
from Catering.Configs import CateringConfigs
from Catering.DataHub import DataHub
from Catering.Evaluator import Evaluator
from Catering.Metrics import detect_alerts
from Catering.Models import AnomalyAlert, DiagnosisReport
from Catering.Scheduler import CateringScheduler
from src.LlmClient import DeepSeekClient
from src.TaskSystem import TaskSystem
from src.Terminal import Terminal


def build_client() -> DeepSeekClient | None:
    """按环境变量构造 DeepSeek 客户端;--no-llm 时返回 None。"""
    import os

    from dotenv import load_dotenv

    load_dotenv(override=True)
    if not os.getenv("DEEPSEEK_API_KEY"):
        return None
    return DeepSeekClient.from_env()


def known_stores(hub: DataHub) -> list[str]:
    stores_dir = hub.data_dir / "stores"
    if not stores_dir.exists():
        return []
    return sorted(p.stem for p in stores_dir.glob("*.json"))


def latest_date(hub: DataHub, store_id: str) -> str | None:
    try:
        payload = json.loads((hub.data_dir / "stores" / f"{store_id}.json")
                             .read_text())
    except (OSError, json.JSONDecodeError):
        return None
    days = payload.get("days") or []
    return str(days[-1]["date"]) if days else None


def parse_alert_query(query: str, hub: DataHub) -> AnomalyAlert | None:
    """从自然语言告警中解析门店与日期(如 'A店昨日营业额下降18%')。"""
    import re

    stores = known_stores(hub)
    store = next((s for s in stores if s in query), None)
    date_match = re.search(r"(\d{4}-\d{2}-\d{2})", query)
    date = (date_match.group(1) if date_match
            else (latest_date(hub, store) if store else None))
    if not store or not date:
        return None
    return AnomalyAlert(store_id=store, date=date, source="manual",
                        message=query)


def print_report(report: DiagnosisReport):
    """终端输出诊断报告(可验证:结论 -> 证据链 -> 建议)。"""
    alert = report.alert
    Terminal.print(f"\n{Terminal.MAGENTA}=== 诊断报告 {report.report_id} ==={Terminal.RESET}")
    Terminal.print(f"门店 {alert.store_id} / {alert.date} / "
                   f"营收同比 {alert.change_pct:+.1f}%"
                   if alert.change_pct is not None else
                   f"门店 {alert.store_id} / {alert.date}")
    Terminal.print(f"主因: {report.main_factor} "
                   f"(贡献 {report.main_factor_contrib:+,.2f} 元)"
                   if report.main_factor_contrib is not None else
                   f"主因: {report.main_factor}")
    Terminal.print(f"\n{report.conclusions}")
    Terminal.print(f"\n{Terminal.CYAN}证据链(工具取证):{Terminal.RESET}")
    for ev in report.evidence:
        Terminal.print(f"  - [{ev.source}] {ev.step_id} <- {ev.tool}")
    Terminal.print(f"\n{Terminal.CYAN}行动建议:{Terminal.RESET}")
    for i, rec in enumerate(report.recommendations, 1):
        Terminal.print(f"  {i}. {rec['action']} "
                       f"({rec.get('source', '?')}, 证据 {rec.get('evidence_refs')})")
    meta = report.meta
    Terminal.print(f"\nmeta: llm_used={meta.get('llm_used')} "
                   f"template_fallback={meta.get('template_fallback')} "
                   f"case_mode={meta.get('case_mode')} "
                   f"review={meta.get('review')}")
    if meta.get("report_path"):
        Terminal.print(f"报告已落盘: {meta['report_path']}")


def _build_agent(client, report_dir, auto, tasks):
    task_system = (TaskSystem(Path(report_dir) / "tasks" if report_dir
                              else CateringConfigs.RUN_DIR / "tasks")
                   if tasks else None)
    return AnomalyAgent.build(client=client, run_dir=report_dir,
                              interactive=not auto, tasks=task_system)


def cmd_run(args, client) -> int:
    hub = DataHub()
    agent = _build_agent(client, args.report_dir, args.auto, args.tasks)
    query = args.query
    if args.store:
        stores = known_stores(hub)
        if args.store not in stores:
            Terminal.print(f"未知门店 {args.store},可选: {stores}",
                           )
            return 1
        date = args.date or latest_date(hub, args.store)
        alert = AnomalyAlert(store_id=args.store, date=date, source="manual")
    elif query:
        alert = parse_alert_query(query, hub)
        if alert is None:
            Terminal.print("无法从输入解析门店与日期,格式示例: 'A店 2026-09-01'")
            return 1
    else:
        # 交互模式:先扫描告警,支持直接输入门店/日期或自然语言
        alerts = detect_alerts(hub.load_all_stores())
        if alerts:
            Terminal.print("检测到以下经营异常告警:")
            for i, a in enumerate(alerts, 1):
                Terminal.print(f"  {i}. {a.message}")
            Terminal.print("输入编号诊断,或输入 'A店' / 'A店 2026-09-01' / 'q' 退出:")
            raw = input(CateringConfigs.PROMPT).strip()
            if raw.lower() in ("q", "exit", ""):
                return 0
            if raw.isdigit() and 1 <= int(raw) <= len(alerts):
                alert = alerts[int(raw) - 1]
            else:
                alert = parse_alert_query(raw, hub)
                if alert is None:
                    Terminal.print("无法解析输入,再见。")
                    return 1
        else:
            Terminal.print("未检测到异常告警。可输入 'A店' / 'A店 2026-09-01':")
            raw = input(CateringConfigs.PROMPT).strip()
            if raw.lower() in ("q", "exit", ""):
                return 0
            alert = parse_alert_query(raw, hub)
            if alert is None:
                Terminal.print("无法解析输入,再见。")
                return 1
    report = agent.run_alert(alert)
    print_report(report)
    return 0


def cmd_eval(client, compare: bool = False) -> int:
    agent = AnomalyAgent.build(client=None, interactive=False)
    evaluator = Evaluator()
    summary = evaluator.run_suite(agent)
    Evaluator.print_report(summary)
    retrieval = evaluator.run_retrieval_suite(agent.tools.hub)
    Evaluator.print_retrieval_report(retrieval)
    if compare:
        comparison = evaluator.compare_retrieval_modes(agent.tools.hub)
        Evaluator.print_retrieval_comparison(comparison)
    diagnosis_ok = summary["passed"] == summary["total"]
    # group/filter/probe 是观察指标;CI 只对严格回归集设门禁。
    retrieval_ok = (retrieval.get("regression_passed", 0)
                    == retrieval.get("regression_total", 0))
    return 0 if diagnosis_ok and retrieval_ok else 1


def cmd_schedule(args, client) -> int:
    """定时自动诊断:注册 cron 任务并常驻(复用 src.CronScheduler)。"""
    import time

    agent = _build_agent(client, args.report_dir, auto=True,
                         tasks=args.tasks)
    durable = (Path(args.report_dir) / "scheduled_catering.json"
               if args.report_dir
               else CateringConfigs.RUN_DIR / "scheduled_catering.json")
    scheduler = CateringScheduler(agent, durable_path=durable)
    if args.list_jobs:
        Terminal.print(scheduler.list_jobs())
        return 0
    result = scheduler.schedule_scan(args.schedule)
    Terminal.print(result)
    if result.startswith("Error"):
        return 1
    scheduler.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        scheduler.stop()
        Terminal.print("已停止定时诊断。")
    return 0


def cmd_explore(args, client) -> int:
    if client is None:
        Terminal.print("探索模式需要 LLM(未配置 DEEPSEEK_API_KEY),"
                       "工作流模式可用 --no-llm 离线运行。")
        return 1
    # 写工具(archive_case)逐次确认;非 TTY/后台执行时 ApprovalGate 自动拒绝。
    from src.ApprovalGate import ApprovalGate
    agent = AnomalyAgent.build(client=client, run_dir=args.report_dir,
                               interactive=False,
                               confirm=ApprovalGate().confirm_tool)
    result = agent.explore(args.agentic)
    if result.get("error"):
        Terminal.print(f"{Terminal.RED}{result['error']}{Terminal.RESET}")
        return 1
    Terminal.print(f"\n{Terminal.MAGENTA}=== 探索回答 "
                   f"(steps={result['steps']}, denials={result['denials']}, "
                   f"truncated={result['truncated']}) ==={Terminal.RESET}")
    Terminal.print(result["final_text"])
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="餐饮门店经营异常分析 Agent")
    parser.add_argument("--store", help="门店 ID(如 A店)")
    parser.add_argument("--date", help="诊断日期 YYYY-MM-DD")
    parser.add_argument("query", nargs="?", help="自然语言告警描述")
    parser.add_argument("--eval", action="store_true", help="运行离线评测场景集")
    parser.add_argument("--agentic", metavar="PROMPT", help="探索模式:LLM 自由调用餐饮工具")
    parser.add_argument("--list-alerts", action="store_true", help="扫描并列出异常告警")
    parser.add_argument("--schedule", metavar="CRON", help="注册定时自动诊断并常驻,如 '30 8 * * *'")
    parser.add_argument("--list-jobs", action="store_true", help="列出已注册的定时诊断任务")
    parser.add_argument("--tasks", action="store_true", help="把诊断步骤镜像到 src.TaskSystem 任务看板")
    parser.add_argument("--no-llm", action="store_true", help="不调用 LLM(模板结论)")
    parser.add_argument("--auto", action="store_true", help="跳过人工审批")
    parser.add_argument("--case-mode", choices=CateringConfigs.CASE_RAG_MODES,
                        help="案例 RAG 模式: shadow/assist/augment(默认 shadow)")
    parser.add_argument("--retrieval-mode",
                        choices=CateringConfigs.CASE_RETRIEVAL_MODES,
                        help="案例检索模式: lexical/hybrid(默认 lexical)")
    parser.add_argument("--retrieval-compare", action="store_true",
                        help="配合 --eval:对比 lexical/hybrid/hybrid+heuristic")
    parser.add_argument("--report-dir", help="运行产物目录(默认 .catering)")
    args = parser.parse_args(argv)

    run_dir = Path(args.report_dir) if args.report_dir else None
    CateringConfigs.RUN_DIR = run_dir or CateringConfigs.RUN_DIR
    if args.case_mode:
        CateringConfigs.CASE_RAG_MODE = args.case_mode
    if args.retrieval_mode:
        CateringConfigs.CASE_RETRIEVAL_MODE = args.retrieval_mode

    if args.eval:
        return cmd_eval(None, compare=args.retrieval_compare)
    client = None if args.no_llm or args.eval else build_client()
    if args.schedule or args.list_jobs:
        return cmd_schedule(args, client)
    if args.list_alerts:
        hub = DataHub()
        alerts = detect_alerts(hub.load_all_stores())
        if not alerts:
            Terminal.print("未检测到异常告警。")
        for a in alerts:
            Terminal.print(f"  {Terminal.RED}{a.message}{Terminal.RESET}")
        return 0
    if args.agentic:
        return cmd_explore(args, client)
    return cmd_run(args, client)


if __name__ == "__main__":
    sys.exit(main())
