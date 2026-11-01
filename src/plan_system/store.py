"""Plan 版本化持久化:当前版本 + history 归档,保存时禁止 revision 回退。"""
import json
import time
from pathlib import Path

from src.Configs import Configs
from src.FileLock import atomic_write_text, ensure_private_dir, file_lock
from src.plan_system.models import PLAN_ID_RE, Plan


class PlanStore:
    """`.plans/<id>.json` 当前版本 + `.plans/history/<id>.v<rev>.json` 历史。"""

    def __init__(self, root: Path | None = None):
        self._root = Path(root) if root is not None else None

    @property
    def root(self) -> Path:
        """惰性解析根目录,便于测试重定向 Configs.WORKDIR。"""
        return self._root or Configs.plans_dir()

    def path(self, plan_id: str) -> Path:
        if not PLAN_ID_RE.match(str(plan_id or "")):
            raise ValueError(f"invalid plan id: {plan_id!r}")
        return self.root / f"{plan_id}.json"

    def history_path(self, plan_id: str, revision: int) -> Path:
        return self.root / "history" / f"{plan_id}.v{int(revision)}.json"

    def save(self, plan: Plan) -> Path:
        """保存当前版本;revision 增加时把旧版本移入 history。"""
        target = self.path(plan.id)
        ensure_private_dir(self.root)
        with file_lock(self.root / ".lock"):
            existing = None
            if target.exists():
                try:
                    existing = Plan.from_dict(json.loads(
                        target.read_text(encoding="utf-8")))
                except (OSError, json.JSONDecodeError):
                    existing = None
            if existing is not None and existing.revision > plan.revision:
                raise ValueError(
                    f"plan revision regression: {existing.revision} -> "
                    f"{plan.revision}")
            if existing is not None and existing.revision < plan.revision:
                history = self.history_path(plan.id, existing.revision)
                ensure_private_dir(history.parent)
                atomic_write_text(history, json.dumps(
                    existing.to_dict(), ensure_ascii=False, indent=2))
            plan.updated_at = time.time()
            atomic_write_text(target, json.dumps(
                plan.to_dict(), ensure_ascii=False, indent=2))
        return target

    def load(self, plan_id: str) -> Plan:
        path = self.path(plan_id)
        if not path.exists():
            raise FileNotFoundError(f"plan {plan_id} not found")
        plan = Plan.from_dict(json.loads(path.read_text(encoding="utf-8")))
        if plan.id != plan_id:
            raise ValueError(f"plan file id mismatch: {plan.id} != {plan_id}")
        return plan

    def load_revision(self, plan_id: str, revision: int) -> Plan:
        path = self.history_path(plan_id, revision)
        if not path.exists():
            raise FileNotFoundError(
                f"plan {plan_id} revision {revision} not found")
        return Plan.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def list_plans(self, limit: int = 20) -> list[Plan]:
        """按 updated_at 倒序列出最近计划;坏文件跳过。"""
        if not self.root.exists():
            return []
        plans = []
        for path in self.root.glob("plan_*.json"):
            try:
                plans.append(Plan.from_dict(json.loads(
                    path.read_text(encoding="utf-8"))))
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                continue
        plans.sort(key=lambda plan: plan.updated_at, reverse=True)
        return plans[:max(1, int(limit))]
