"""进度账本:按“产出增量”判断是否真的前进了,而不是按工具调用次数。"""
import time

from src.loop_guard.signals import (_MUTATION_TOOLS, _WRITE_TOOLS,
                                    is_wait_observation, short_digest)


class ProgressLedger:
    """记录每轮的新输出/新产物/新写入/新变更及最近一次前进的时间。"""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self.round = 0
        self.last_progress_round = 0
        self.last_progress_at = clock()
        self.seen_outputs: set[str] = set()
        self.seen_artifacts: set[str] = set()
        self.seen_writes: set[str] = set()
        self.seen_mutations: set[str] = set()
        self.last_todo: str | None = None

    def begin_round(self, round_index: int):
        """轮次只增不减;乱序调用不会把进度纪元拉回去。"""
        self.round = max(self.round, int(round_index or 0))

    def observe(self, obs) -> bool:
        """记录一次观察并返回是否产生新进展。

        - 后台任务观察不参与进度判定(其状态由专门信号处理);
        - 合法等待视为前进,避免把轮询误判为空转;
        - 新内容哈希、新产物、新写入、新 todo 或新变更都算前进。
        """
        if obs.background:
            return False
        progress = False
        args_hash = short_digest(obs.args_key)
        if obs.ok and is_wait_observation(obs):
            self.last_progress_round = max(self.round,
                                           self.last_progress_round)
            self.last_progress_at = self._clock()
            return True
        if obs.ok and not obs.denied:
            if obs.content_excerpt and obs.content_excerpt != "(no output)" \
                    and obs.content_hash not in self.seen_outputs:
                self.seen_outputs.add(obs.content_hash)
                progress = True
            for artifact in obs.artifacts:
                if artifact and artifact not in self.seen_artifacts:
                    self.seen_artifacts.add(artifact)
                    progress = True
            if obs.tool in _WRITE_TOOLS and args_hash not in self.seen_writes:
                self.seen_writes.add(args_hash)
                progress = True
            if obs.tool == "todo_write" and args_hash != self.last_todo:
                self.last_todo = args_hash
                progress = True
            mutation_key = f"{obs.tool}\0{args_hash}"
            if obs.tool in _MUTATION_TOOLS \
                    and mutation_key not in self.seen_mutations:
                self.seen_mutations.add(mutation_key)
                progress = True
        if progress:
            self.last_progress_round = max(self.round, self.last_progress_round)
            self.last_progress_at = self._clock()
        return progress

    def stale_rounds(self) -> int:
        """距离最近一次真实进展已经过去多少轮。"""
        return max(0, self.round - self.last_progress_round)
