"""定时自动诊断:复用 src.CronScheduler 的 cron 解析/校验/触发/持久化。

业务场景: 每天营业结束后定时扫描全部门店异常告警并自动跑诊断,
报告落盘 run_dir/reports/,可作为日报数据源。

- 每个 agent 持独立 CronScheduler 实例(durable_path 注入,不污染主代理的
  .scheduled_tasks.json);
- cron 表达式校验/匹配、持久化、触发队列全部复用 src 实现,
  本模块只负责"队列触发后做什么"(扫描 → 诊断 → 汇总)。
"""
import threading

from src.CronScheduler import CronScheduler
from src.Terminal import Terminal

from Catering.Metrics import detect_alerts


class CateringScheduler:
    """定时扫描告警并自动诊断。"""

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
        return f"Scheduled {result.id}: '{cron}' 定时自动诊断"

    def list_jobs(self) -> str:
        return self.cron.run_list_crons()

    def cancel_job(self, job_id: str) -> str:
        return self.cron.cancel_job(job_id)

    # ── 触发后处理 ──

    def process_once(self) -> int:
        """消费一次触发队列:扫描告警 → 逐条诊断 → 返回诊断条数。"""
        jobs = self.cron.consume_queue()
        if not jobs:
            return 0
        alerts = detect_alerts(self.agent.tools.hub.load_all_stores())
        count = 0
        for alert in alerts:
            Terminal.info("auto-diagnose",
                          f"{alert.message} → 开始诊断", Terminal.MAGENTA)
            try:
                report = self.agent.run_alert(alert)
                count += 1
                Terminal.info("auto-diagnose",
                              f"{alert.store_id} 主因={report.main_factor} "
                              f"贡献={report.main_factor_contrib}",
                              Terminal.GREEN)
            except Exception as e:
                Terminal.info("auto-diagnose",
                              f"{alert.store_id} 失败: {type(e).__name__}: {e}",
                              Terminal.RED)
        if not alerts:
            Terminal.info("auto-diagnose", "未检测到异常告警", Terminal.CYAN)
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
        Terminal.info("scheduler", "定时自动诊断已启动(Ctrl+C 退出)",
                      Terminal.GREEN)

    def stop(self):
        self._stop.set()
