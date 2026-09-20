"""定时自动扫描:复用 src.CronScheduler 的 cron 解析/校验/触发/持久化。

业务场景: 按周期(如每天)自动扫描全部入库代码资产并跑安全分析,
报告落盘 run_dir/reports/,作为代码资产管控的持续监测数据源。

- 每个 agent 持独立 CronScheduler 实例(durable_path 注入,不污染主代理的
  .scheduled_tasks.json);
- cron 表达式校验/匹配、持久化、触发队列全部复用 src 实现,
  本模块只负责"队列触发后做什么"(扫描 → 分析 → 汇总)。
"""
import threading

from src.CronScheduler import CronScheduler
from src.Terminal import Terminal

from CodeAnalysis.Metrics import detect_pending_assets


class CodeAnalysisScheduler:
    """定时扫描全部入库资产并自动跑安全分析。"""

    POLL_INTERVAL = 1   # 队列轮询间隔(秒),与 src.CronScheduler._scheduler_loop 一致

    def __init__(self, agent, durable_path=None):
        self.agent = agent
        self.cron = CronScheduler(durable_path=durable_path)
        self._stop = threading.Event()

    # ── 任务注册(复用 src.CronScheduler) ──

    def schedule_scan(self, cron: str, prompt: str = "自动扫描") -> str:
        """注册一个定时扫描任务;返回注册结果或错误信息。"""
        result = self.cron.schedule_job(cron, prompt)
        if isinstance(result, str):
            return f"Error: {result}"
        return f"Scheduled {result.id}: '{cron}' 定时自动安全扫描"

    def list_jobs(self) -> str:
        return self.cron.run_list_crons()

    def cancel_job(self, job_id: str) -> str:
        return self.cron.cancel_job(job_id)

    # ── 触发后处理 ──

    def process_once(self) -> int:
        """消费一次触发队列:扫描全部资产 → 逐项目分析 → 返回分析条数。"""
        jobs = self.cron.consume_queue()
        if not jobs:
            return 0
        assets = detect_pending_assets(self.agent.tools.hub.all_assets())
        count = 0
        for asset in assets:
            Terminal.info("auto-scan", f"{asset.project_id} → 开始安全分析",
                          Terminal.MAGENTA)
            try:
                report = self.agent.run_analysis(asset)
                count += 1
                Terminal.info("auto-scan",
                              f"{asset.project_id} 主风险={report.main_risk} "
                              f"等级={report.security_level}",
                              Terminal.GREEN)
            except Exception as e:
                Terminal.info("auto-scan",
                              f"{asset.project_id} 失败: "
                              f"{type(e).__name__}: {e}",
                              Terminal.RED)
        if not assets:
            Terminal.info("auto-scan", "无入库代码资产", Terminal.CYAN)
        return count

    def run_loop(self):
        """循环:轮询触发队列并处理(守护线程目标)。"""
        while not self._stop.is_set():
            try:
                self.process_once()
            except Exception as e:
                Terminal.info("scheduler", f"轮询异常: {type(e).__name__}: {e}",
                              Terminal.RED)
            self._stop.wait(self.POLL_INTERVAL)

    def start(self):
        """启动调度线程 + 处理线程。"""
        self.cron.start()
        threading.Thread(target=self.run_loop, daemon=True).start()
        Terminal.info("scheduler", "定时自动扫描已启动(Ctrl+C 退出)",
                      Terminal.GREEN)

    def stop(self):
        self._stop.set()
