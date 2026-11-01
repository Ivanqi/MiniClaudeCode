"""记忆数据模型、常量与文本规范化/分词算法。

文本处理分两层:
1. ``normalize_text`` 做 NFKC、控制字符清理、换行/空白归一,检索与 hash 共用;
2. ``content_hash`` 在规范化后额外 casefold,消除大小写差异造成的重复存储。

分词兼顾英文按词与中文无词典切分:中文长句、相邻 2-gram 和大写文本都会
入索引,用空间换查全率(例如“深色模式” → 深色模式/深色/色模/模式)。
"""
import hashlib
import re
import unicodedata
from dataclasses import asdict, dataclass, field, fields

from src.SecurityPolicy import redact

# 内置命名空间:working 是临时工作记忆,其余参与检索/索引。
BUILTIN_NAMESPACES = ("working", "episodic", "semantic", "procedural", "entity")
DEFAULT_SEARCH_NAMESPACES = ("semantic", "entity", "episodic", "procedural")
INDEX_NAMESPACES = {"semantic", "entity", "procedural", "episodic"}
# 来源类型:工具原始输出不能直接固化为事实,过程性记忆只能由技能写入。
SOURCE_KINDS = {"user", "agent", "tool", "workflow", "skill", "transcript",
                "import", "system", "tool_output"}
TOOL_DERIVED_KINDS = {"tool", "tool_output"}
NAMESPACE_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_ASCII_TOKEN_RE = re.compile(r"[a-z0-9_]+")
_CJK_RUN_RE = re.compile(r"[一-鿿]+")
_UNSAFE_KEY_RE = re.compile(r"[^A-Za-z0-9._-]+")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class MemoryValidationError(ValueError):
    """记忆输入不合法。"""


class MemoryProvenanceError(MemoryValidationError):
    """来源/命名空间不允许。"""


@dataclass
class MemoryItem:
    """一条持久化记忆;content_hash/version 由 upsert 维护。"""

    namespace: str
    key: str
    content: str
    tags: list[str] = field(default_factory=list)
    source: str = ""
    source_kind: str = "agent"
    source_ref: str = ""
    confidence: float = 0.6
    created_at: float = 0.0
    updated_at: float = 0.0
    last_used_at: float = 0.0
    expires_at: float | None = None
    content_hash: str = ""
    version: int = 1
    use_count: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "MemoryItem":
        """容忍旧文件中的未知字段,并补齐 tags 类型。"""
        allowed = {item.name for item in fields(cls)}
        item = cls(**{key: value for key, value in data.items()
                      if key in allowed})
        item.tags = list(item.tags or [])
        return item


@dataclass
class UpsertResult:
    """upsert 的结果:区分新建、更新、按内容去重、内容未变。"""

    item: MemoryItem
    created: bool = False
    updated: bool = False
    deduped: bool = False
    canonical_key: str = ""
    redacted: bool = False


@dataclass
class MemoryHit:
    """检索命中:item + 归一化后的相关度分数。"""

    item: MemoryItem
    score: float


def normalize_text(text) -> str:
    """NFKC + 换行/空白规范化;记忆检索和 hash 共用。"""
    value = unicodedata.normalize("NFKC", str(text or ""))
    value = _CONTROL_RE.sub(" ", value)
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"[ \t\f\v]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def content_hash(text) -> str:
    """规范化 + casefold 后的 SHA256;大小写/空白差异不会重复存储。"""
    normalized = normalize_text(text).casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def tokenize(text) -> set[str]:
    """英文按词切分,中文切为完整长句 + 所有相邻 2-gram。"""
    value = normalize_text(text).casefold()
    tokens = set(_ASCII_TOKEN_RE.findall(value))  # 提取连续字母数字
    for run in _CJK_RUN_RE.findall(value):  # 提取连续汉字
        tokens.add(run)
        if len(run) > 1:
            tokens.update(run[i:i + 2] for i in range(len(run) - 1))
    return {token for token in tokens if token}


def normalize_tags(tags) -> list[str]:
    """接受字符串/列表,脱敏、去重、限量;供 upsert 与工具入参共用。"""
    if tags is None:
        return []
    if isinstance(tags, str):
        tags = [part for part in re.split(r"[,，\s]+", tags) if part]
    result, seen = [], set()
    for tag in tags:
        value = redact(str(tag)).strip()[:64]
        if not value or value.casefold() in seen:
            continue
        seen.add(value.casefold())
        result.append(value)
        if len(result) >= 20:
            break
    return result
