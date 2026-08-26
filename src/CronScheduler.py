"""Cron 调度器:定时任务注册、触发与持久化。

cron 任务独立于对话历史存储;触发后进入队列,
由 AgentLoop 把计划提示重新注入到同一个 agent 循环中。
"""
import json
import random
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from src.Configs import Configs
from src.LlmClient import make_function_tool
from src.Terminal import Terminal


@dataclass
class CronJob:
    id: str
    cron: str
    prompt: str
    recurring: bool
    durable: bool


class CronScheduler:
    """Cron 调度器:校验、注册、触发与持久化定时任务。"""

    def __init__(self, durable_path: Path | None = None):
        self.durable_path = durable_path or Configs.DURABLE_CRON_PATH
        self.scheduled_jobs: dict[str, CronJob] = {}
        self.queue: list[CronJob] = []
        self._lock = threading.Lock()
        self._last_fired: dict[str, str] = {}
        self.load_durable_jobs()

    # ── cron 表达式解析与校验 ──

    @staticmethod
    def _field_matches(field: str, value: int) -> bool:
        if field == "*":
            return True
        if field.startswith("*/"):
            step = int(field[2:])
            return step > 0 and value % step == 0
        if "," in field:
            return any(CronScheduler._field_matches(part.strip(), value)
                       for part in field.split(","))
        if "-" in field:
            lo, hi = field.split("-", 1)
            return int(lo) <= value <= int(hi)
        return value == int(field)

    @classmethod
    def cron_matches(cls, cron_expr: str, dt: datetime) -> bool:
        """判断 5 字段 cron 表达式是否匹配给定时间。"""
        fields = cron_expr.strip().split()
        if len(fields) != 5:
            return False
        minute, hour, dom, month, dow = fields
        dow_val = (dt.weekday() + 1) % 7
        m = cls._field_matches(minute, dt.minute)
        h = cls._field_matches(hour, dt.hour)
        dom_ok = cls._field_matches(dom, dt.day)
        month_ok = cls._field_matches(month, dt.month)
        dow_ok = cls._field_matches(dow, dow_val)
        if not (m and h and month_ok):
            return False
        if dom == "*" and dow == "*":
            return True
        if dom == "*":
            return dow_ok
        if dow == "*":
            return dom_ok
        return dom_ok or dow_ok

    @classmethod
    def _validate_field(cls, field: str, lo: int, hi: int) -> str | None:
        if field == "*":
            return None
        if field.startswith("*/"):
            step = field[2:]
            if not step.isdigit() or int(step) <= 0:
                return f"Invalid step: {field}"
            return None
        if "," in field:
            for part in field.split(","):
                err = cls._validate_field(part.strip(), lo, hi)
                if err:
                    return err
            return None
        if "-" in field:
            left, right = field.split("-", 1)
            if not left.isdigit() or not right.isdigit():
                return f"Invalid range: {field}"
            a, b = int(left), int(right)
            if a < lo or a > hi or b < lo or b > hi:
                return f"Range {field} out of bounds [{lo}-{hi}]"
            if a > b:
                return f"Range start > end: {field}"
            return None
        if not field.isdigit():
            return f"Invalid field: {field}"
        value = int(field)
        if value < lo or value > hi:
            return f"Value {value} out of bounds [{lo}-{hi}]"
        return None

    @classmethod
    def validate_cron(cls, cron_expr: str) -> str | None:
        fields = cron_expr.strip().split()
        if len(fields) != 5:
            return f"Expected 5 fields, got {len(fields)}"
        bounds = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
        names = ["minute", "hour", "day-of-month", "month", "day-of-week"]
        for field, (lo, hi), name in zip(fields, bounds, names):
            err = cls._validate_field(field, lo, hi)
            if err:
                return f"{name}: {err}"
        return None

    # ── 任务注册与持久化 ──

    def schedule_job(self, cron: str, prompt: str,
                     recurring: bool = True, durable: bool = True) -> CronJob | str:
        err = self.validate_cron(cron)
        if err:
            return err
        job = CronJob(
            id=f"cron_{random.randint(0, 999999):06d}",
            cron=cron, prompt=prompt,
            recurring=recurring, durable=durable)
        with self._lock:
            self.scheduled_jobs[job.id] = job
        if durable:
            self.save_durable_jobs()
        return job

    def cancel_job(self, job_id: str) -> str:
        with self._lock:
            job = self.scheduled_jobs.pop(job_id, None)
        if not job:
            return f"Job {job_id} not found"
        if job.durable:
            self.save_durable_jobs()
        return f"Cancelled {job_id}"

    def save_durable_jobs(self):
        durable = [asdict(job) for job in self.scheduled_jobs.values() if job.durable]
        self.durable_path.write_text(json.dumps(durable, indent=2))

    def load_durable_jobs(self):
        if not self.durable_path.exists():
            return
        try:
            for item in json.loads(self.durable_path.read_text()):
                job = CronJob(**item)
                if not self.validate_cron(job.cron):
                    self.scheduled_jobs[job.id] = job
        except Exception:
            pass

    # ── 调度线程与队列 ──

    def start(self):
        """启动调度线程(守护线程,每分钟粒度检查一次)。"""
        threading.Thread(target=self._scheduler_loop, daemon=True).start()

    def _scheduler_loop(self):
        while True:
            time.sleep(1)
            now = datetime.now()
            marker = now.strftime("%Y-%m-%d %H:%M")
            with self._lock:
                for job in list(self.scheduled_jobs.values()):
                    try:
                        if self.cron_matches(job.cron, now) and self._last_fired.get(job.id) != marker:
                            self.queue.append(job)
                            self._last_fired[job.id] = marker
                            if not job.recurring:
                                self.scheduled_jobs.pop(job.id, None)
                                if job.durable:
                                    self.save_durable_jobs()
                    except Exception as e:
                        Terminal.info("cron error", f"{job.id}: {e}", Terminal.RED)

    def consume_queue(self) -> list[CronJob]:
        with self._lock:
            fired = list(self.queue)
            self.queue.clear()
        return fired

    # ── 工具层 ──

    def run_schedule_cron(self, cron: str, prompt: str,
                          recurring: bool = True, durable: bool = True) -> str:
        result = self.schedule_job(cron, prompt, recurring, durable)
        if isinstance(result, str):
            return f"Error: {result}"
        return f"Scheduled {result.id}: '{cron}' -> {prompt}"

    def run_list_crons(self) -> str:
        with self._lock:
            jobs = list(self.scheduled_jobs.values())
        if not jobs:
            return "No cron jobs."
        return "\n".join(
            f"  {job.id}: '{job.cron}' -> {job.prompt[:40]} "
            f"[{'recurring' if job.recurring else 'one-shot'}, "
            f"{'durable' if job.durable else 'session'}]"
            for job in jobs)

    def run_cancel_cron(self, job_id: str) -> str:
        return self.cancel_job(job_id)

    def tool_defs(self) -> list[dict]:
        return [
            make_function_tool("schedule_cron",
                "Schedule a cron job. cron is 5-field: min hour dom month dow. "
                "For one-shot reminders, compute the target minute and set recurring=false.",
                {"type": "object",
                 "properties": {"cron": {"type": "string"},
                                "prompt": {"type": "string"},
                                "recurring": {"type": "boolean"},
                                "durable": {"type": "boolean"}},
                 "required": ["cron", "prompt"]}),
            make_function_tool("list_crons", "List registered cron jobs.",
                {"type": "object", "properties": {}, "required": []}),
            make_function_tool("cancel_cron", "Cancel a cron job by ID.",
                {"type": "object",
                 "properties": {"job_id": {"type": "string"}},
                 "required": ["job_id"]}),
        ]

    def handlers(self) -> dict:
        return {
            "schedule_cron": self.run_schedule_cron,
            "list_crons": self.run_list_crons,
            "cancel_cron": self.run_cancel_cron,
        }


# 全局单例。调度线程由 AgentLoop 显式启动(start)。
CRON_SCHEDULER = CronScheduler()
