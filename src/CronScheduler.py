"""Cron 调度器:定时任务注册、触发与持久化。

cron 任务独立于对话历史存储;触发后进入队列,
由 AgentLoop 把计划提示重新注入到同一个 agent 循环中。
"""
import json
import random
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path

from src.Configs import Configs
from src.FileLock import atomic_write_text, file_lock
from src.LlmClient import make_function_tool
from src.Terminal import Terminal

'''
recurring 和 durable 是两个独立维度：
    recurring=True, durable=True → 每天早上 9 点提醒（持久周期）
    recurring=False, durable=False → 10 分钟后提醒一次（会话内一次性）

'''
@dataclass
class CronJob:
    id: str           # 唯一标识，如 "cron_123456"
    cron: str         # 5 字段 cron 表达式
    prompt: str       # 触发时要注入的提示词
    recurring: bool   # True=周期任务，False=一次性任务
    durable: bool     # True=持久化到磁盘，False=仅会话内有效

'''
1. LLM 调用 schedule_cron(cron="0 9 * * *", prompt="提醒我喝水")
   │
   ▼
2. run_schedule_cron("0 9 * * *", "提醒我喝水")
   │
   ▼
3. schedule_job(...)
   ├─ validate_cron("0 9 * * *") → None（合法）
   ├─ prompt 非空、长度合法
   ├─ _min_interval_minutes → 1440（一天一次），≥ 最小值
   ├─ 加锁，检查任务数 < CRON_MAX_JOBS
   ├─ 生成 job_id = "cron_123456"
   ├─ scheduled_jobs["cron_123456"] = CronJob(...)
   └─ save_durable_jobs() → 写入磁盘
   │
   ▼
4. 返回 "Scheduled cron_123456: '0 9 * * *' -> 提醒我喝水"

... 时间流逝 ...

5. 第二天 9:00:00，_scheduler_loop 醒来
   ├─ cron_matches("0 9 * * *", 9:00) → True
   ├─ _last_fired["cron_123456"] != "2026-01-06 09:00"
   ├─ queue.append(job)
   ├─ _last_fired["cron_123456"] = "2026-01-06 09:00"
   └─ recurring=True，不删除任务

6. AgentLoop 调用 consume_queue()
   └─ 返回 [CronJob(id="cron_123456", prompt="提醒我喝水", ...)]

7. AgentLoop 把 prompt 注入 agent 循环，执行提醒逻辑
'''
class CronScheduler:
    """Cron 调度器:校验、注册、触发与持久化定时任务。"""

    def __init__(self, durable_path: Path | None = None):
        self.durable_path = durable_path or Configs.DURABLE_CRON_PATH
        self.scheduled_jobs: dict[str, CronJob] = {}
        self.queue: list[CronJob] = []
        self._lock = threading.RLock()
        self._last_fired: dict[str, str] = {}
        self.load_durable_jobs()

    # ── cron 表达式解析与校验 ──
    '''
    单个字段匹配
    '''
    @staticmethod
    def _field_matches(field: str, value: int) -> bool:
        """判断 cron 的单个字段是否匹配给定数值。"""
        if field == "*":
            return True # 通配符：永远匹配
        if field.startswith("*/"):
            step = int(field[2:])   # 步长，如 "*/5" → step=5
            return step > 0 and value % step == 0
        if "," in field: # 枚举，如 "1,3,5"
            return any(CronScheduler._field_matches(part.strip(), value)
                       for part in field.split(","))
        if "-" in field: # 范围，如 "1-5"
            lo, hi = field.split("-", 1)
            return int(lo) <= value <= int(hi)
        return value == int(field) # 精确值

    '''
    完整表达式匹配
    "0 9 * * *" → 每天 9:00 触发
    "*/5 * * * *" → 每 5 分钟触发
    "0 9 * * 1-5" → 周一到周五 9:00 触发
    "0 0 1 * *" → 每月 1 号 0:00 触发
    "0 0 1 * 0" → 每月 1 号 或 每周日 0:00 触发（OR 语义）
    '''
    @classmethod
    def cron_matches(cls, cron_expr: str, dt: datetime) -> bool:
        """判断 5 字段 cron 表达式是否匹配给定时间。"""
        fields = cron_expr.strip().split()
        if len(fields) != 5:
            return False
        minute, hour, dom, month, dow = fields

        # Python weekday(): 周一=0...周日=6
        # cron dow:       周日=0, 周一=1...周六=6
        # 星期映射：Python 的 weekday() 周一=0，而 cron 周日=0。用 (weekday() + 1) % 7 转换
        # 周一(0) → (0+1)%7 = 1 ✓
        # 周日(6) → (6+1)%7 = 0 ✓
        dow_val = (dt.weekday() + 1) % 7


        m = cls._field_matches(minute, dt.minute)
        h = cls._field_matches(hour, dt.hour)
        dom_ok = cls._field_matches(dom, dt.day)
        month_ok = cls._field_matches(month, dt.month)
        dow_ok = cls._field_matches(dow, dow_val)
        if not (m and h and month_ok):
            return False

        # DOM 和 DOW 的经典 cron 语义：两者都限定时取 OR，否则取 AND
        # 如果 dom=1 且 dow=0（每月 1 号或每周日），实际含义是 “每月 1 号 或 每周日”
        # 而不是“每月 1 号且是周日”。这个实现正确遵循了该语义
        if dom == "*" and dow == "*":
            return True # 都不限定，只看分/时/月
        if dom == "*":
            return dow_ok # 只限定 DOW
        if dow == "*":
            return dom_ok # 只限定 DOM
        return dom_ok or dow_ok # 都限定 → OR

    '''
    单字段校验
    '''
    @classmethod
    def _validate_field(cls, field: str, lo: int, hi: int) -> str | None:
        """校验单个字段，返回 None 表示合法，否则返回错误信息。"""
        if field == "*":
            return None
        
        # "*/0"错误，step 必须 > 0
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

        '''
        "1-5-7"错误： 范围格式错误（多了一个 -）
        "9-3"错误: start > end
        '''
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

        # "60"错误: 分钟超出 [0,59]
        # "abc"错误: 非数字
        value = int(field)
        if value < lo or value > hi:
            return f"Value {value} out of bounds [{lo}-{hi}]"
        return None

    '''
    完整校验
    '''
    @classmethod
    def validate_cron(cls, cron_expr: str) -> str | None:
        """校验完整 5 字段 cron 表达式。"""
        fields = cron_expr.strip().split()
        if len(fields) != 5:
            return f"Expected 5 fields, got {len(fields)}"
        
        '''
        minute	0-59
        hour	0-23
        day-of-month	1-31
        month	1-12
        day-of-week	0-6（0=周日）
        '''
        bounds = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
        names = ["minute", "hour", "day-of-month", "month", "day-of-week"]
        for field, (lo, hi), name in zip(fields, bounds, names):
            err = cls._validate_field(field, lo, hi)
            if err:
                return f"{name}: {err}"
        return None

    # ── 任务注册与持久化 ──

    '''
    最小触发间隔
        为什么扫一周？
            cron 表达式可能按天、按周、按月触发，一周能覆盖所有 DOW 组合，且大多数月级规则在一周内也能看出相邻间隔的下界。

            防止用户注册过于频繁的任务，比如 "* * * * *"（每分钟）。配合 CRON_MIN_INTERVAL_MINUTES 配置，可以拒绝高频任务。

            "*/5 * * * *" → 扫描结果 minimum=5

            "0 9 * * *" → 扫描结果 minimum=1440（一天一次）

            "0 9 * * 1" → 扫描结果 minimum=10080（一周一次）
    '''
    @classmethod
    def _min_interval_minutes(cls, cron_expr: str) -> int | None:
        """扫描一周,返回相邻触发的最小间隔;匹配不足两次返回 None。"""
        start = datetime(2026, 1, 5, 0, 0)  # 周一,覆盖 DOW
        last, minimum = None, None
        for offset in range(7 * 24 * 60):
            moment = start + timedelta(minutes=offset)
            if not cls.cron_matches(cron_expr, moment):
                continue
            if last is not None:
                gap = int((moment - last).total_seconds() // 60)
                minimum = gap if minimum is None else min(minimum, gap)
            last = moment
        return minimum

    '''
    注册任务
        返回类型是 CronJob | str：成功返回 CronJob，失败返回错误字符串。调用方需要判断类型。
        多层防护：cron 合法性 → prompt 非空 → prompt 长度 → 触发频率 → 任务总数。
        ID 碰撞处理：6 位随机数，最多 100 万种可能，while 循环保证唯一。
        加锁范围：从 len(scheduled_jobs) 检查到 save_durable_jobs 全程持锁，避免并发注册导致超出上限。
    '''
    def schedule_job(self, cron: str, prompt: str,
                     recurring: bool = True, durable: bool = True) -> CronJob | str:
        # 1. 校验 cron 表达式
        err = self.validate_cron(cron)
        if err:
            return err

        # 2. 校验 prompt
        if not isinstance(prompt, str) or not prompt.strip():
            return "prompt cannot be empty"
        if len(prompt) > Configs.CRON_MAX_PROMPT_CHARS:
            return (f"prompt too long: {len(prompt)} > "
                    f"{Configs.CRON_MAX_PROMPT_CHARS}")
        
        # 3. 校验触发频率
        interval = self._min_interval_minutes(cron)
        if interval is not None and \
                interval < Configs.CRON_MIN_INTERVAL_MINUTES:
            return (f"schedule too frequent: every {interval} minute(s), "
                    f"minimum is {Configs.CRON_MIN_INTERVAL_MINUTES}")
        
        # 4. 加锁注册
        with self._lock:
            if len(self.scheduled_jobs) >= Configs.CRON_MAX_JOBS:
                return f"too many cron jobs (max {Configs.CRON_MAX_JOBS})"
            
            # 5. 生成唯一 ID，避免碰撞
            job_id = f"cron_{random.randint(0, 999999):06d}"
            while job_id in self.scheduled_jobs:
                job_id = f"cron_{random.randint(0, 999999):06d}"
            
            job = CronJob(
                id=job_id, cron=cron, prompt=prompt,
                recurring=recurring, durable=durable)
            self.scheduled_jobs[job.id] = job
            
            # 6. 持久化
            if durable:
                self.save_durable_jobs()
        return job

    '''
    取消任务
        pop(job_id, None)：不存在时返回 None，不会抛 KeyError。
        只对 durable 任务重新持久化，因为非持久化任务本来就不在磁盘上。
    '''
    def cancel_job(self, job_id: str) -> str:
        with self._lock:
            job = self.scheduled_jobs.pop(job_id, None)
            if not job:
                return f"Job {job_id} not found"
            if job.durable:
                self.save_durable_jobs()
        return f"Cancelled {job_id}"

    '''
    持久化保存
        文件锁（file_lock）：防止多个进程/线程同时写同一个文件，避免交错写入。
        原子写入（atomic_write_text）：先写临时文件，再 os.replace() 重命名。即使写入过程中崩溃，原文件也不会损坏。

        锁范围设计：
            self._lock 只保护内存中的字典遍历和序列化。
            文件锁在序列化完成后才获取，缩短文件锁持有时间。
    '''
    def save_durable_jobs(self):
        with self._lock:
            # 只序列化 durable=True 的任务
            durable = [asdict(job) for job in self.scheduled_jobs.values()
                       if job.durable]
        
        payload = json.dumps(durable, indent=2, ensure_ascii=False)
        # 文件锁 + 原子写入
        with file_lock(Path(str(self.durable_path) + ".lock")):
            atomic_write_text(self.durable_path, payload)

    '''
    加载持久化任务
        为什么用 CronJob(**item)？
            item 是字典，**item 展开成关键字参数。
            如果字典多了字段（版本升级），TypeError 捕获；如果少了字段，同样 TypeError。
            比手动逐个 item.get() 更简洁，且强制字段完整性。
    '''
    def load_durable_jobs(self):
        if not self.durable_path.exists():
            return
        try:
            raw = json.loads(self.durable_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return # 文件损坏时静默跳过
        
        if not isinstance(raw, list):
            return # 格式不对时静默跳过
        
        for item in raw:
            try:
                job = CronJob(**item) # 用字典展开构造
            except TypeError:
                continue # 字段缺失/多余时跳过
            if self.validate_cron(job.cron):
                continue # cron 不合法时跳过
            if len(job.prompt) > Configs.CRON_MAX_PROMPT_CHARS:
                continue # prompt 超长时跳过
            if len(self.scheduled_jobs) >= Configs.CRON_MAX_JOBS:
                break # 达到上限后停止
            self.scheduled_jobs[job.id] = job

    # ── 调度线程与队列 ──
    '''
    启动调度线程
        daemon=True：主线程退出时，调度线程自动终止，不会阻止进程退出。
        注释说“每分钟粒度”，但实际是每秒检查一次，靠 _last_fired 去重来实现分钟级触发（见下）。
    '''
    def start(self):
        """启动调度线程(守护线程,每分钟粒度检查一次)。"""
        threading.Thread(target=self._scheduler_loop, daemon=True).start()

    '''
    调度主循环
        为什么用每秒轮询而不是精确 sleep 到下一分钟？
            实现简单，鲁棒性好（不怕系统时间跳变）。
            每秒检查的开销极小（几十个任务的字符串匹配）。
            精度足够（分钟级任务，误差 < 1 秒）。

        recurring=False 的处理：
            一次性任务触发后立即从 scheduled_jobs 删除。
            如果是 durable，还要持久化，确保重启后不会重复触发。
            注意：先从内存删除，再持久化。如果持久化失败，内存中已经删了，但磁盘上还在，重启后会重复触发。这是一个已知的权衡（at-least-once 语义）。
    '''
    def _scheduler_loop(self):
        while True:
            time.sleep(1) # 每秒检查一次
            now = datetime.now()
            marker = now.strftime("%Y-%m-%d %H:%M") # 分钟级去重标记

            with self._lock:
                for job in list(self.scheduled_jobs.values()):
                    try:
                        if self.cron_matches(job.cron, now) and self._last_fired.get(job.id) != marker:
                            # 1. 入队
                            self.queue.append(job)
                            # 2. 记录本分钟已触发
                            self._last_fired[job.id] = marker
                            # 3. 一次性任务触发后自动删除
                            if not job.recurring:
                                self.scheduled_jobs.pop(job.id, None)
                                if job.durable:
                                    self.save_durable_jobs()
                    except Exception as e:
                        Terminal.info("cron error", f"{job.id}: {e}", Terminal.RED)

    '''
    消费队列
        复制+清空：在锁内完成，保证原子性。
        返回的是列表副本，调用方处理时不会影响队列。
        AgentLoop 定期调用此方法，拿到本周期内触发的任务，把 prompt 注入 agent 循环。
    '''
    def consume_queue(self) -> list[CronJob]:
        with self._lock:
            fired = list(self.queue) # 复制一份
            self.queue.clear() # 清空原队列
        return fired

    # ── 工具层 ──

    '''
    把 CronJob | str 统一转换成字符串，方便 LLM 理解。
    成功时返回包含 job_id 的消息，LLM 可以记住这个 ID 用于后续取消。
    '''
    def run_schedule_cron(self, cron: str, prompt: str,
                          recurring: bool = True, durable: bool = True) -> str:
        result = self.schedule_job(cron, prompt, recurring, durable)
        if isinstance(result, str):
            return f"Error: {result}"
        return f"Scheduled {result.id}: '{cron}' -> {prompt}"

    '''
    prompt[:40]：截断长 prompt，避免输出过长。
    标注 recurring/one-shot 和 durable/session，让 LLM 清楚任务属性。
    '''
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

    '''
    工具定义
        schedule_cron 的 description 特意提示 
        LLM: “一次性提醒要自己算目标分钟，并设 recurring=false”
        因为 LLM 不会自动做时间计算。
    '''
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
