"""案例记忆:共享 content_hash/version/provenance 契约的案例存储适配器。

保留既有文件布局 `cases/archive/<case_id>.json` 与 `{"cases":[record]}`,
因此现有工具/测试/人工可读性不变;共享的是 src.MemoryStore 的规范化与
SHA-256 指纹,以及 upsert=更新而非追加的语义。
"""
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

from src.Configs import Configs
from src.FileLock import atomic_write_text, ensure_private_dir, file_lock
from src.MemoryStore import content_hash, normalize_text
from src.SecurityPolicy import redact
from src.Terminal import Terminal

from Catering.Configs import CateringConfigs

CASE_FIELDS = ("case_id", "store_id", "date", "main_factor", "dimensions",
               "summary", "actions", "tags")
_CASE_ID_RE = re.compile(r"^[^\x00-\x1f/\\]{1,200}$")


def validate_case_id(case_id: str) -> str:
    if not isinstance(case_id, str):
        raise ValueError("case_id must be a string")
    value = case_id.strip()
    if (not value or len(value) > 200 or ".." in value
            or not _CASE_ID_RE.match(value)):
        raise ValueError(f"invalid case_id: {case_id!r}")
    return value


def _canonical_case(case: dict) -> dict:
    """语义字段规范化;不包含 version/source/confidence 等易变元数据。"""
    dimensions = sorted({redact(str(item)).strip()
                         for item in (case.get("dimensions") or [])
                         if str(item).strip()})
    actions = [redact(str(item)).strip()
               for item in (case.get("actions") or [])
               if str(item).strip()]
    tags = sorted({redact(str(item)).strip()
                   for item in (case.get("tags") or [])
                   if str(item).strip()}, key=str.casefold)
    return {
        "store_id": normalize_text(redact(str(case.get("store_id") or ""))),
        "date": normalize_text(redact(str(case.get("date") or ""))),
        "main_factor": normalize_text(
            redact(str(case.get("main_factor") or ""))),
        "dimensions": dimensions,
        "summary": normalize_text(redact(str(case.get("summary") or ""))),
        "actions": actions,
        "tags": tags,
    }


def case_hash(case: dict) -> str:
    canonical = _canonical_case(case)
    return content_hash(json.dumps(canonical, ensure_ascii=False,
                                   sort_keys=True))


def _float_or(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _case_from_raw(raw: dict, path: Path, now: float) -> dict | None:
    if not isinstance(raw, dict):
        return None
    # 兼容将来可能出现的 MemoryItem 包装
    if isinstance(raw.get("content"), str) and "key" in raw:
        try:
            inner = json.loads(raw["content"])
            if isinstance(inner, dict):
                raw = {**inner, **{key: raw[key] for key in
                                   ("version", "created_at", "updated_at",
                                    "source", "confidence", "content_hash")
                                   if key in raw}}
        except json.JSONDecodeError:
            return None
    case_id = raw.get("case_id")
    try:
        case_id = validate_case_id(case_id)
    except ValueError:
        return None
    case = _canonical_case(raw)
    stat_mtime = None
    try:
        stat_mtime = path.stat().st_mtime
    except OSError:
        pass
    created_at = _float_or(raw.get("created_at"), stat_mtime or now)
    updated_at = _float_or(raw.get("updated_at"), created_at)
    confidence = _float_or(raw.get("confidence"), 0.5)
    confidence = max(0.0, min(1.0, confidence))
    return {
        **case,
        "case_id": case_id,
        "version": max(1, int(_float_or(raw.get("version"), 1) or 1)),
        "created_at": created_at,
        "updated_at": updated_at,
        "source": redact(str(raw.get("source") or "legacy"))[:200],
        "confidence": confidence,
        "content_hash": str(raw.get("content_hash") or case_hash(case)),
    }


def load_cases(*roots: Path | str | None) -> list[dict]:
    """递归扫描案例目录;兼容 legacy dict/envelope,按 case_id 去重取新版。"""
    now = time.time()
    resolved, seen_roots = [], set()
    for root in roots:
        if root is None:
            continue
        path = Path(root)
        try:
            key = str(path.resolve())
        except OSError:
            key = str(path)
        if key in seen_roots or not path.exists():
            continue
        seen_roots.add(key)
        resolved.append(path)

    records: dict[str, dict] = {}
    for root in resolved:
        for path in sorted(root.rglob("*.json")):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict) and isinstance(
                    payload.get("cases"), list):
                raws = payload["cases"]
            elif isinstance(payload, list):
                raws = payload
            elif isinstance(payload, dict):
                raws = [payload]
            else:
                continue
            for raw in raws:
                record = _case_from_raw(raw, path, now)
                if record is None:
                    continue
                old = records.get(record["case_id"])
                if old is None or (
                        record["version"], record["updated_at"],
                        record["source"] != "legacy") > (
                        old["version"], old["updated_at"],
                        old["source"] != "legacy"):
                    records[record["case_id"]] = record
    return sorted(records.values(), key=lambda item: item["case_id"])


@dataclass
class CaseUpsert:
    record: dict
    path: Path
    created: bool
    updated: bool


class CaseStore:
    """单文件/单 case_id 的版本化 upsert。"""

    def __init__(self, root: Path | None = None, clock=None):
        self._root = Path(root) if root is not None else None
        self._clock = clock or time.time

    @property
    def cases_dir(self) -> Path:
        return self._root or CateringConfigs.CASES_DIR

    @property
    def archive_dir(self) -> Path:
        return self.cases_dir / "archive"

    def all_cases(self) -> list[dict]:
        return load_cases(self.cases_dir)

    def upsert(self, case: dict, source: str = "tool:archive_case",
               confidence: float = 0.8) -> CaseUpsert:
        case_id = validate_case_id(case.get("case_id", ""))
        canonical = _canonical_case(case)
        digest = content_hash(json.dumps(canonical, ensure_ascii=False,
                                         sort_keys=True))
        now = self._clock()
        confidence = max(0.0, min(1.0, float(confidence)))
        source = redact(str(source or "tool:archive_case"))[:200]
        archive_dir = ensure_private_dir(self.archive_dir)
        path = archive_dir / f"{case_id}.json"
        lock_path = self.cases_dir / ".cases.lock"

        with file_lock(lock_path):
            existing = None
            if path.exists():
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    raws = (payload.get("cases") or []
                            if isinstance(payload, dict) else [])
                    if raws:
                        existing = raws[0]
                except (OSError, json.JSONDecodeError):
                    existing = None
            if existing is None:
                # legacy fixtures / 其他文件里的同 case_id 也参与版本计算
                for candidate in load_cases(self.cases_dir):
                    if candidate.get("case_id") == case_id:
                        existing = candidate
                        break
            if existing:
                old_hash = existing.get("content_hash") or case_hash(existing)
                old_source = str(existing.get("source") or "")
                old_confidence = _float_or(existing.get("confidence"), 0.5)
                same = (old_hash == digest and old_source == source
                        and abs(old_confidence - confidence) < 1e-9)
                if same:
                    record = _case_from_raw(existing, path, now) or {
                        **canonical, "case_id": case_id,
                        "version": 1, "created_at": now, "updated_at": now,
                        "source": source, "confidence": confidence,
                        "content_hash": digest,
                    }
                    return CaseUpsert(record, path, created=False,
                                      updated=False)
                version = max(1, int(_float_or(existing.get("version"), 1)
                                     or 1)) + 1
                created_at = _float_or(existing.get("created_at"), now)
                created = False
            else:
                version, created_at, created = 1, now, True

            record = {
                **canonical,
                "case_id": case_id,
                "version": version,
                "created_at": created_at,
                "updated_at": now,
                "source": source,
                "confidence": confidence,
                "content_hash": digest,
            }
            atomic_write_text(path, json.dumps(
                {"cases": [record]}, ensure_ascii=False, indent=2))
        Terminal.info("memory", f"案例已{'创建' if created else '更新'}: "
                      f"{case_id} v{version}", Terminal.GREEN)
        return CaseUpsert(record, path, created=created, updated=not created)


# 全局默认存储:路径惰性解析,便于测试 patch CateringConfigs.CASES_DIR。
CASE_STORE = CaseStore()


def case_search_decay(case: dict, now: float | None = None) -> float:
    """案例时间衰减:按 date 或 updated_at,保留一个地板。"""
    now = time.time() if now is None else now
    stamp = _float_or(case.get("updated_at"), None)
    if stamp is None:
        date_value = str(case.get("date") or "")
        try:
            from datetime import datetime
            stamp = datetime.fromisoformat(date_value).timestamp()
        except ValueError:
            stamp = now
    age_days = max(0.0, now - stamp) / 86400.0
    half_life = max(float(Configs.CASE_SEARCH_HALF_LIFE_DAYS), 1.0)
    return max(0.2, min(1.0, 0.5 ** (age_days / half_life)))
