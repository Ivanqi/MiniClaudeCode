"""任务数据模型与去重指纹。

``Task`` 覆盖工作/集成/计划步骤三类任务;``canonical_fingerprint`` 用
规范化后的 subject/scope/targets/依赖/验收标准生成稳定哈希,用于精确去重。
"""
import hashlib
import json
from dataclasses import dataclass, field

from src.MemoryStore import normalize_text, tokenize


@dataclass
class Task:
    """一条任务记录;序列化到 tasks_dir/task_<id>.json。"""

    id: str
    subject: str
    description: str
    status: str
    owner: str | None
    blockedBy: list[str] = field(default_factory=list)
    worktree: str | None = None
    lease_until: float | None = None
    heartbeat_at: float | None = None
    attempts: int = 0
    last_error: str | None = None
    idempotency_key: str | None = None
    created_at: float | None = None
    updated_at: float | None = None
    completed_at: float | None = None
    kind: str = "work"                  # work | integration
    scope: str = ""
    target_files: list[str] = field(default_factory=list)
    acceptance: str = ""
    fingerprint: str = ""
    duplicate_of: str | None = None
    dedupe_score: float = 0.0
    claim_token: str | None = None
    base_ref: str = ""
    base_sha: str = ""
    source_tasks: list[str] = field(default_factory=list)
    integration_worktree: str | None = None
    integration_branch: str | None = None
    acceptance_command: str = ""
    merge_status: str = ""
    conflict_files: list[str] = field(default_factory=list)
    preflight_base_sha: str = ""
    source_heads: dict = field(default_factory=dict)
    preflight_exit_code: int | None = None
    preflight_output: str = ""
    apply_exit_code: int | None = None
    apply_output: str = ""
    merged_commit: str = ""
    approved_by: str = ""
    approved_at: float | None = None
    review_status: str = ""
    review_findings: list[str] = field(default_factory=list)
    reviewed_at: float | None = None
    reviewed_by: str = ""
    reviewed_head: str = ""
    # ── Plan step ──
    plan_id: str = ""
    step_id: str = ""
    plan_version: int = 0
    step_result: dict = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    requires_approval: bool = False
    approval_status: str = ""
    rollback: str = ""
    rollback_status: str = ""
    input_refs: list[str] = field(default_factory=list)
    output_refs: list[str] = field(default_factory=list)


@dataclass
class CreateResult:
    """创建结果:created/reused + 去重原因 + 相似任务列表。"""

    task: Task
    created: bool = False
    reused: bool = False
    reason: str = ""
    similar: list[tuple[str, str, float]] = field(default_factory=list)


def normalize_targets(target_files) -> list[str]:
    """target_files 接受字符串或列表;规范化、去重、限长限量。"""
    if target_files is None:
        return []
    if isinstance(target_files, str):
        target_files = [target_files]
    targets, seen = [], set()
    for item in target_files:
        value = normalize_text(str(item)).strip()
        if not value or value in seen:
            continue
        seen.add(value)
        targets.append(value[:300])
        if len(targets) >= 100:
            break
    return targets


def canonical_fingerprint(subject: str, scope: str = "",
                          target_files=None, blocked_by=None,
                          acceptance: str = "") -> str:
    """精确去重指纹:文本归一化 + 排序 + SHA256。"""
    payload = {
        "subject": normalize_text(subject).casefold(),
        "scope": normalize_text(scope).casefold(),
        "targets": sorted(normalize_targets(target_files)),
        "blockedBy": sorted(str(item) for item in (blocked_by or [])),
        "acceptance": normalize_text(acceptance).casefold(),
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def token_set(task: Task) -> set[str]:
    """任务 subject/scope/targets 的分词集合,用于模糊相似度。"""
    text = " ".join([task.subject or "", task.scope or "",
                     *list(task.target_files or [])])
    return tokenize(text)
