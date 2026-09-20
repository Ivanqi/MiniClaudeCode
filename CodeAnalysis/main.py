"""CodeAnalysis CLI 入口:代码资产安全分析 Agent。

用法(在项目根目录运行):
  python -m CodeAnalysis.main                       交互模式(安全分析)
  python -m CodeAnalysis.main --project PJ-A-001    分析指定入库项目
  python -m CodeAnalysis.main --eval                离线评测场景集(无需 API Key)
  python -m CodeAnalysis.main --agentic "问题"      探索模式(LLM 自由调用分析工具)
  python -m CodeAnalysis.main --list-assets         列出已入库的代码资产
  python -m CodeAnalysis.main --research all        生成研究报告/部署技术规范交付物
  python -m CodeAnalysis.main --schedule "30 8 * * *"  定时自动扫描并常驻
  python -m CodeAnalysis.main --no-llm ...          工作流模式不用 LLM(模板结论)
  python -m CodeAnalysis.main --auto ...            跳过人工审批环节
  python -m CodeAnalysis.main --report-dir DIR      指定运行产物目录

环境初始化(读 .env 构造真实 LLM 客户端)只发生在本文件;
CodeAnalysis 包内模块禁止 import dotenv,保证测试离线。
"""
import argparse
import json
import sys
from pathlib import Path

from CodeAnalysis.CodeAnalysisAgent import CodeAnalysisAgent
from CodeAnalysis.Configs import CodeAnalysisConfigs
from CodeAnalysis.DataHub import DataHub
from CodeAnalysis.Evaluator import Evaluator
from CodeAnalysis.Models import AnalysisReport, CodeAsset
from CodeAnalysis.ResearchReports import ResearchReports
from CodeAnalysis.Scheduler import CodeAnalysisScheduler
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


def known_projects(hub: DataHub) -> list[str]:
    return sorted(a["project_id"] for a in hub.all_assets())


def asset_for_project(hub: DataHub, project_id: str) -> CodeAsset | None:
    """从资产库构造 CodeAsset(入库日期取自资产元数据)。"""
    payload = hub.asset(project_id)
    if payload is None:
        return None
    return CodeAsset(project_id=payload["project_id"], name=payload["name"],
                     language=payload["language"], version=payload["version"],
                     ingest_date=payload["ingest_date"], source="manual")


def parse_asset_query(query: str, hub: DataHub) -> CodeAsset | None:
    """从自然语言解析项目(如 '科技管理系统入库,请做安全分析')。"""
    assets = hub.all_assets()
    match = next((a for a in assets
                  if a["project_id"] in query or a["name"] in query), None)
    if match is None:
        return None
    return CodeAsset(project_id=match["project_id"], name=match["name"],
                     language=match["language"], version=match["version"],
                     ingest_date=match["ingest_date"], source="manual",
                     message=query)


def print_report(report: AnalysisReport):
    """终端输出分析报告(可验证:结论 -> 证据链 -> 建议)。"""
    asset = report.asset
    Terminal.print(f"\n{Terminal.MAGENTA}=== 安全分析报告 {report.report_id} ==={Terminal.RESET}")
    Terminal.print(f"{asset.name}({asset.project_id}) / 入库 {asset.ingest_date} / "
                   f"语言 {asset.language} / 版本 {asset.version}")
    Terminal.print(f"安全等级: {report.security_level} / 主风险: {report.main_risk} "
                   f"(风险分 {report.main_risk_score}) / 总分 {report.risk_score}")
    Terminal.print(f"\n{report.conclusions}")
    Terminal.print(f"\n{Terminal.CYAN}证据链(工具取证):{Terminal.RESET}")
    for ev in report.evidence:
        Terminal.print(f"  - [{ev.source}] {ev.step_id} <- {ev.tool}")
    Terminal.print(f"\n{Terminal.CYAN}整改建议:{Terminal.RESET}")
    for i, rec in enumerate(report.recommendations, 1):
        Terminal.print(f"  {i}. {rec['action']} "
                       f"({rec.get('source', '?')}, 证据 {rec.get('evidence_refs')})")
    meta = report.meta
    Terminal.print(f"\nmeta: llm_used={meta.get('llm_used')} "
                   f"template_fallback={meta.get('template_fallback')} "
                   f"review={meta.get('review')}")
    if meta.get("report_path"):
        Terminal.print(f"报告已落盘: {meta['report_path']}")


def _build_agent(client, report_dir, auto, tasks):
    task_system = (TaskSystem(Path(report_dir) / "tasks" if report_dir
                              else CodeAnalysisConfigs.RUN_DIR / "tasks")
                   if tasks else None)
    return CodeAnalysisAgent.build(client=client, run_dir=report_dir,
                                   interactive=not auto, tasks=task_system)


def cmd_run(args, client) -> int:
    hub = DataHub()
    agent = _build_agent(client, args.report_dir, args.auto, args.tasks)
    query = args.query
    if args.project:
        projects = known_projects(hub)
        if args.project not in projects:
            Terminal.print(f"未知项目 {args.project},可选: {projects}")
            return 1
        asset = asset_for_project(hub, args.project)
    elif query:
        asset = parse_asset_query(query, hub)
        if asset is None:
            Terminal.print("无法从输入解析项目,格式示例: "
                           "'科技管理系统' / 'PJ-A-001 入库'")
            return 1
    else:
        # 交互模式:列出资产,支持直接输入项目 ID/名称或自然语言
        assets = hub.all_assets()
        Terminal.print("已入库的代码资产:")
        for i, a in enumerate(assets, 1):
            Terminal.print(f"  {i}. {a['project_id']} {a['name']}"
                           f"(语言 {a['language']},版本 {a['version']})")
        raw = input("\n输入编号/项目 ID/项目名称分析,或 'q' 退出: ").strip()
        if raw.lower() in ("q", "exit", ""):
            return 0
        if raw.isdigit() and 1 <= int(raw) <= len(assets):
            asset = asset_for_project(hub, assets[int(raw) - 1]["project_id"])
        else:
            asset = parse_asset_query(raw, hub)
            if asset is None:
                Terminal.print("无法解析输入,再见。")
                return 1
    report = agent.run_analysis(asset)
    print_report(report)
    return 0


def cmd_eval(client) -> int:
    agent = CodeAnalysisAgent.build(client=None, interactive=False)
    evaluator = Evaluator()
    summary = evaluator.run_suite(agent)
    Evaluator.print_report(summary)
    return 0 if summary["passed"] == summary["total"] else 1


def cmd_schedule(args, client) -> int:
    """定时自动扫描:注册 cron 任务并常驻(复用 src.CronScheduler)。"""
    import time

    agent = _build_agent(client, args.report_dir, auto=True,
                         tasks=args.tasks)
    durable = (Path(args.report_dir) / "scheduled_codeanalysis.json"
               if args.report_dir
               else CodeAnalysisConfigs.RUN_DIR / "scheduled_codeanalysis.json")
    scheduler = CodeAnalysisScheduler(agent, durable_path=durable)
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
        Terminal.print("已停止定时自动扫描。")
    return 0


def cmd_explore(args, client) -> int:
    if client is None:
        Terminal.print("探索模式需要 LLM(未配置 DEEPSEEK_API_KEY),"
                       "工作流模式可用 --no-llm 离线运行。")
        return 1
    agent = CodeAnalysisAgent.build(client=client, run_dir=args.report_dir,
                                    interactive=False)
    result = agent.explore(args.agentic)
    if result.get("error"):
        Terminal.print(f"{Terminal.RED}{result['error']}{Terminal.RESET}")
        return 1
    Terminal.print(f"\n{Terminal.MAGENTA}=== 探索回答 "
                   f"(steps={result['steps']}, denials={result['denials']}, "
                   f"truncated={result['truncated']}) ==={Terminal.RESET}")
    Terminal.print(result["final_text"])
    return 0


def cmd_research(args) -> int:
    """生成研究交付物(量化指标 1/3/4)。"""
    try:
        paths = ResearchReports().generate(args.research)
    except ValueError as e:
        Terminal.print(f"{Terminal.RED}{e}{Terminal.RESET}")
        return 1
    Terminal.print(f"{Terminal.GREEN}已生成 {len(paths)} 份交付物:{Terminal.RESET}")
    for path in paths:
        Terminal.print(f"  - {path}")
    return 0


def cmd_list_assets() -> int:
    hub = DataHub()
    assets = hub.all_assets()
    if not assets:
        Terminal.print("无入库代码资产。")
        return 0
    Terminal.print("已入库的代码资产:")
    for a in assets:
        Terminal.print(f"  - {a['project_id']} {a['name']}"
                       f"(语言 {a['language']},版本 {a['version']},"
                       f"入库 {a['ingest_date']},部署范围 {a['deploy_scope']})")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="代码资产安全分析 Agent")
    parser.add_argument("--project", help="项目 ID(如 PJ-A-001)")
    parser.add_argument("query", nargs="?", help="自然语言分析请求")
    parser.add_argument("--eval", action="store_true", help="运行离线评测场景集")
    parser.add_argument("--agentic", metavar="PROMPT", help="探索模式:LLM 自由调用分析工具")
    parser.add_argument("--list-assets", action="store_true", help="列出已入库的代码资产")
    parser.add_argument("--schedule", metavar="CRON", help="注册定时自动扫描并常驻,如 '30 8 * * *'")
    parser.add_argument("--list-jobs", action="store_true", help="列出已注册的定时扫描任务")
    parser.add_argument("--tasks", action="store_true", help="把分析步骤镜像到 src.TaskSystem 任务看板")
    parser.add_argument("--no-llm", action="store_true", help="不调用 LLM(模板结论)")
    parser.add_argument("--auto", action="store_true", help="跳过人工审批")
    parser.add_argument("--report-dir", help="运行产物目录(默认 .codeanalysis)")
    parser.add_argument("--research", choices=["static", "scan", "deploy", "all"],
                        help="生成研究报告/技术规范交付物")
    args = parser.parse_args(argv)

    run_dir = Path(args.report_dir) if args.report_dir else None
    CodeAnalysisConfigs.RUN_DIR = run_dir or CodeAnalysisConfigs.RUN_DIR

    if args.eval:
        return cmd_eval(None)
    client = None if args.no_llm or args.eval else build_client()
    if args.schedule or args.list_jobs:
        return cmd_schedule(args, client)
    if args.research:
        return cmd_research(args)
    if args.list_assets:
        return cmd_list_assets()
    if args.agentic:
        return cmd_explore(args, client)
    return cmd_run(args, client)


if __name__ == "__main__":
    sys.exit(main())
