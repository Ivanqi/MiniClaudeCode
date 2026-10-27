"""案例记忆:共享 content_hash/version/provenance 契约的案例存储适配器。

保留既有文件布局 `cases/archive/<case_id>.json` 与 `{"cases":[record]}`,
因此现有工具/测试/人工可读性不变;共享的是 src.MemoryStore 的规范化与
SHA-256 指纹,以及 upsert=更新而非追加的语义。
"""
import json
import math
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from stat import S_ISREG
from threading import Lock

from src.Configs import Configs
from src.FileLock import atomic_write_text, ensure_private_dir, file_lock
from src.MemoryStore import content_hash, normalize_text
from src.SecurityPolicy import redact
from src.Terminal import Terminal

from Catering.Configs import CateringConfigs

CASE_FIELDS = ("case_id", "store_id", "date", "main_factor", "dimensions",
               "summary", "actions", "tags", "symptoms", "metrics_snapshot",
               "outcome", "effective_from", "effective_to", "deprecated")
_CASE_ID_RE = re.compile(r"^[^\x00-\x1f/\\]{1,200}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def validate_case_id(case_id: str) -> str:
    if not isinstance(case_id, str):
        raise ValueError("case_id must be a string")
    value = case_id.strip()
    if (not value or len(value) > 200 or ".." in value
            or not _CASE_ID_RE.match(value)):
        raise ValueError(f"invalid case_id: {case_id!r}")
    return value


def _normalized_date(value) -> str:
    """只接受 ISO 日期;非法值置空,避免时间过滤按字符串误比较。"""
    text = normalize_text(redact(str(value or "")))
    return text if _DATE_RE.match(text) else ""


def _normalized_list(value) -> list[str]:
    """症状等字符串列表:去空、去重、稳定排序。"""
    if isinstance(value, str):
        value = [value]
    return sorted({normalize_text(redact(str(item)))
                   for item in (value or []) if str(item).strip()},
                  key=str.casefold)


def _normalized_metrics(value) -> dict:
    """指标快照只保留标量(数值/布尔/短文本),避免嵌套结构进入 digest/hash。"""
    if not isinstance(value, dict):
        return {}
    snapshot = {}
    for key, item in value.items():
        name = normalize_text(redact(str(key)))[:100]
        if not name:
            continue
        if isinstance(item, bool):
            snapshot[name] = item
        elif isinstance(item, (int, float)):
            if not isinstance(item, float) or math.isfinite(item):
                snapshot[name] = item
        elif isinstance(item, str):
            snapshot[name] = redact(item)[:200]
    return dict(sorted(snapshot.items(), key=lambda pair: pair[0].casefold()))


def _bool_or(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y")
    if value is None:
        return default
    return bool(value)


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
        "date": _normalized_date(case.get("date")),
        "main_factor": normalize_text(
            redact(str(case.get("main_factor") or ""))),
        "dimensions": dimensions,
        "summary": normalize_text(redact(str(case.get("summary") or ""))),
        "actions": actions,
        "tags": tags,
        "symptoms": _normalized_list(case.get("symptoms")),
        "metrics_snapshot": _normalized_metrics(
            case.get("metrics_snapshot")),
        "outcome": normalize_text(redact(str(case.get("outcome") or ""))),
        "effective_from": _normalized_date(case.get("effective_from")),
        "effective_to": _normalized_date(case.get("effective_to")),
        "deprecated": _bool_or(case.get("deprecated"), False),
    }


def _canonical_hash(canonical: dict) -> str:
    return content_hash(json.dumps(canonical, ensure_ascii=False,
                                   sort_keys=True))


def case_hash(case: dict) -> str:
    return _canonical_hash(_canonical_case(case))


def _float_or(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _case_from_raw(raw: dict, path: Path, now: float,
                   stat_mtime: float | None = None) -> dict | None:
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
    if stat_mtime is None:
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
        "content_hash": str(raw.get("content_hash") or _canonical_hash(case)),
    }


# 案例目录只读缓存:按"根目录 + 文件签名"失效,避免每次检索重解析 1000+ 案例。
# 返回列表由调用方只读使用;CaseStore.upsert 写入后文件签名变化会自动失效。
_LOAD_CACHE: OrderedDict = OrderedDict()
_LOAD_CACHE_MAX = 8
_LOAD_CACHE_LOCK = Lock()


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

    file_entries = []
    for root in resolved:
        for path in sorted(root.rglob("*.json")):
            if path.is_symlink():
                continue
            try:
                file_stat = path.stat()
            except OSError:
                continue
            if not S_ISREG(file_stat.st_mode):
                continue
            file_entries.append((path, file_stat))
    signature = tuple(
        (str(path), file_stat.st_mtime_ns, file_stat.st_ctime_ns,
         file_stat.st_size)
        for path, file_stat in file_entries)
    cache_key = (tuple(str(root) for root in resolved), signature)
    with _LOAD_CACHE_LOCK:
        cached = _LOAD_CACHE.get(cache_key)
        if cached is not None:
            _LOAD_CACHE.move_to_end(cache_key)
            return cached

    records: dict[str, dict] = {}
    for path, file_stat in file_entries:
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
            record = _case_from_raw(raw, path, now, file_stat.st_mtime)
            if record is None:
                continue
            old = records.get(record["case_id"])
            if old is None or (
                    record["version"], record["updated_at"],
                    record["source"] != "legacy") > (
                    old["version"], old["updated_at"],
                    old["source"] != "legacy"):
                records[record["case_id"]] = record
    result = sorted(records.values(), key=lambda item: item["case_id"])
    with _LOAD_CACHE_LOCK:
        _LOAD_CACHE[cache_key] = result
        if len(_LOAD_CACHE) > _LOAD_CACHE_MAX:
            _LOAD_CACHE.popitem(last=False)
    return result


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
    """案例时间衰减:优先 effective_from/date,无日期时用 updated_at,保留地板。"""
    now = time.time() if now is None else now
    stamp = None
    # 业务生效时间优先于归档时间,避免重新归档刷新案例"新鲜度"。
    for date_value in (case.get("effective_from"), case.get("date")):
        if not date_value:
            continue
        try:
            stamp = datetime.fromisoformat(str(date_value)).timestamp()
            break
        except ValueError:
            continue
    if stamp is None:
        stamp = _float_or(case.get("updated_at"), now)
    age_days = max(0.0, now - stamp) / 86400.0
    half_life = max(float(Configs.CASE_SEARCH_HALF_LIFE_DAYS), 1.0)
    return max(0.2, min(1.0, 0.5 ** (age_days / half_life)))
