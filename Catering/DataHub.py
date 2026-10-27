"""数据层:八类数据源抽象 + 模拟数据适配器 + 门面路由 + 知识库检索。

需求 2 的八类数据源映射:
- store_db         门店经营数据库   data/stores/{store_id}.json(日总+菜品+评分+人员)
- pos              POS 收银数据     data/pos/{store_id}.json(堂食渠道+时段)
- takeout          外卖平台数据     data/takeout/{store_id}.json(外卖渠道+时段)
- internal_system  内部业务系统     data/internal/promotions.json(活动记录)
- external_api     第三方 API       data/external/weather.json + market.json
- knowledge_base   企业知识库       data/cases/*.json(历史案例 + Agent 归档)
- analysis_service 数据分析服务     Metrics.py(纯计算,无存储)
- 数据库: JsonFileSource 即"数据库"适配器的模拟实现

契约(integration boundary): 每个 DataSource.query 返回归一化 dict;
行级校验发现的缺失字段显式写入 missing_fields,不填默认值、不猜数。
换真实数据库/POS/外卖/第三方系统 = 新增一个实现 DataSource 接口的适配器,
DataHub 路由、工具、诊断链、评测全部零改动。
"""
import json
from abc import ABC, abstractmethod
from datetime import date as date_type
from datetime import timedelta
from pathlib import Path

from Catering.Cases import CaseStore, load_cases
from Catering.Configs import CateringConfigs
from Catering.Retrieval import CaseRetriever, build_case_retriever

# ── 错误与记录校验 ──


class DataSourceError(Exception):
    """数据源不可用(文件缺失/损坏、连接失败等)。"""


def missing_fields_of(record: dict, required: list[str]) -> list[str]:
    """行级校验:必填键缺失或为 None 时返回缺失清单。"""
    return [k for k in required if record.get(k) is None]


# ── 数据源抽象 ──


class DataSource(ABC):
    """数据源抽象接口:归一化查询入口。"""

    category = ""   # Models.SOURCE_CATEGORIES 之一

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir

    @abstractmethod
    def query(self, store_id: str, date: str, **params) -> dict:
        """返回归一化结果 dict;数据不可用时抛 DataSourceError。"""


class JsonFileSource(DataSource):
    """JSON 文件数据源基类:加载、日记录查找。"""

    def load_json(self, rel_path: str) -> dict:
        fp = self.data_dir / rel_path
        try:
            return json.loads(fp.read_text())
        except FileNotFoundError:
            raise DataSourceError(f"数据源缺失: {rel_path}") from None
        except json.JSONDecodeError as e:
            raise DataSourceError(f"数据源损坏: {rel_path}: {e}") from None

    @staticmethod
    def find_day(days: list[dict], date: str) -> dict | None:
        for day in days:
            if day.get("date") == date:
                return day
        return None


# ── 八类数据源适配器(模拟实现) ──


class StoreDbSource(JsonFileSource):
    """门店经营数据库:日总指标 + 菜品结构 + 评分 + 人员出勤。"""

    category = "store_db"
    REQUIRED = ["date", "revenue", "traffic", "ticket", "rating",
                "review_count", "complaints", "staff_on_duty",
                "staff_planned", "dishes"]

    def query(self, store_id: str, date: str, **params) -> dict:
        payload = self.load_json(f"stores/{store_id}.json")
        day = self.find_day(payload.get("days") or [], date)
        if day is None:
            return {"store_id": store_id, "date": date,
                    "missing_fields": [f"store_db:{date}"]}
        missing = missing_fields_of(day, self.REQUIRED)
        result = dict(day)
        result["store_id"] = store_id
        result["missing_fields"] = missing
        return result


class PosSource(JsonFileSource):
    """POS 收银数据:堂食渠道日总与时段分布。"""

    category = "pos"
    REQUIRED = ["date", "revenue", "traffic", "orders", "periods"]

    def query(self, store_id: str, date: str, **params) -> dict:
        payload = self.load_json(f"pos/{store_id}.json")
        day = self.find_day(payload.get("days") or [], date)
        if day is None:
            return {"store_id": store_id, "date": date,
                    "missing_fields": [f"pos:{date}"]}
        result = dict(day)
        result["store_id"] = store_id
        result["missing_fields"] = missing_fields_of(day, self.REQUIRED)
        return result


class TakeoutSource(JsonFileSource):
    """外卖平台数据:外卖渠道日总与时段分布。"""

    category = "takeout"
    REQUIRED = ["date", "revenue", "traffic", "orders", "periods"]

    def query(self, store_id: str, date: str, **params) -> dict:
        payload = self.load_json(f"takeout/{store_id}.json")
        day = self.find_day(payload.get("days") or [], date)
        if day is None:
            return {"store_id": store_id, "date": date,
                    "missing_fields": [f"takeout:{date}"]}
        result = dict(day)
        result["store_id"] = store_id
        result["missing_fields"] = missing_fields_of(day, self.REQUIRED)
        return result


class InternalSystemSource(JsonFileSource):
    """内部业务系统:营销活动记录(含排班、采购等未来扩展)。"""

    category = "internal_system"

    def query(self, store_id: str, date: str, lookback_days: int = 3,
              **params) -> dict:
        payload = self.load_json("internal/promotions.json")
        records = [r for r in (payload.get("records") or [])
                   if r.get("store_id") == store_id and r.get("date") <= date]
        if lookback_days:
            limit = date_type.fromisoformat(date) - timedelta(days=lookback_days)
            records = [r for r in records
                       if date_type.fromisoformat(r["date"]) >= limit]
        return {"store_id": store_id, "date": date, "promotions": records,
                "missing_fields": []}


class ExternalApiSource(JsonFileSource):
    """第三方 API:天气 + 市场竞对动态(模拟实现,换真实 API 只需替换本类)。"""

    category = "external_api"

    def query(self, store_id: str, date: str, kind: str = "weather",
              **params) -> dict:
        if kind == "weather":
            payload = self.load_json("external/weather.json")
            record = next((r for r in (payload.get("records") or [])
                           if r.get("store_id") == store_id
                           and r.get("date") == date), None)
            if record is None:
                # 无异常天气记录 = 晴天(默认值来自数据契约,不是猜数)
                return {"store_id": store_id, "date": date,
                        "weather": {"value": "晴", "severity": "normal",
                                    "detail": "无异常天气"},
                        "missing_fields": []}
            return {"store_id": store_id, "date": date,
                    "weather": {"value": record["value"],
                                "severity": record["severity"],
                                "detail": record.get("detail", "")},
                    "missing_fields": []}
        if kind == "market":
            payload = self.load_json("external/market.json")
            events = [e for e in (payload.get("events") or [])
                      if e.get("store_id") == store_id and e.get("date") == date]
            return {"store_id": store_id, "date": date,
                    "market_events": events, "missing_fields": []}
        raise DataSourceError(f"未知的第三方数据类型: {kind}")


class KnowledgeBaseSource(JsonFileSource):
    """企业知识库:历史案例检索(history_cases.json + Agent 归档案例)。"""

    category = "knowledge_base"

    def __init__(self, data_dir: Path,
                 retriever: CaseRetriever | None = None):
        super().__init__(data_dir)
        self.case_store = None
        # 惰性 provider:每次检索重新扫描,兼容测试 patch CASES_DIR 与新增归档。
        # 默认 lexical;CASE_RETRIEVAL_MODE=hybrid 时由工厂构建混合检索。
        self.retriever = retriever or build_case_retriever(self.all_cases)

    def all_cases(self) -> list[dict]:
        roots = [self.data_dir / "cases"]
        try:
            global_root = CateringConfigs.CASES_DIR
        except Exception:
            global_root = None
        if global_root is not None:
            roots.append(global_root)
        store = self.case_store
        if store is not None:
            roots.append(store.cases_dir)
        return load_cases(*roots)

    def query(self, store_id: str, date: str = "", symptom: str = "",
              top_k: int = 3, main_factor: str = "",
              include_deprecated: bool = False, **params) -> dict:
        return self.retriever.search(
            symptom, store_id=store_id, date=date, main_factor=main_factor,
            top_k=top_k, include_deprecated=include_deprecated)


# ── 门面路由 ──


class DataHub:
    """数据中枢:路由查询到八类数据源,提供日记录/基准/案例检索入口。"""

    def __init__(self, data_dir: Path | None = None,
                 case_store: CaseStore | None = None,
                 case_retriever: CaseRetriever | None = None):
        self.data_dir = data_dir or CateringConfigs.DATA_DIR
        # 案例写入使用惰性 CASES_DIR(便于 mock.patch);读取同时扫 data_dir/cases。
        self.case_store = case_store or CaseStore()
        knowledge_base = KnowledgeBaseSource(self.data_dir, case_retriever)
        knowledge_base.case_store = self.case_store
        self.sources: dict[str, DataSource] = {
            "store_db": StoreDbSource(self.data_dir),
            "pos": PosSource(self.data_dir),
            "takeout": TakeoutSource(self.data_dir),
            "internal_system": InternalSystemSource(self.data_dir),
            "external_api": ExternalApiSource(self.data_dir),
            "knowledge_base": knowledge_base,
        }
        # hybrid 检索在初始化时预热索引,避免首次交互检索承担冷加载成本。
        warmup = getattr(knowledge_base.retriever, "warmup", None)
        if callable(warmup):
            warmup()

    def archive_case(self, case: dict, source: str = "tool:archive_case",
                     confidence: float = 0.8):
        return self.case_store.upsert(case, source=source,
                                      confidence=confidence)

    def query(self, category: str, store_id: str, date: str,
              **params) -> dict:
        """按类别路由查询(工具层与诊断引擎的统一入口)。"""
        source = self.sources.get(category)
        if source is None:
            raise DataSourceError(f"未知数据源类别: {category}")
        try:
            return source.query(store_id, date, **params)
        except DataSourceError as e:
            raise DataSourceError(f"{e}{self._available_hint(category)}") from None

    def _available_hint(self, category: str) -> str:
        """数据源缺失时提示可用门店,帮助调用方(LLM)自纠正。"""
        dirname = {"store_db": "stores", "pos": "pos",
                   "takeout": "takeout"}.get(category)
        if not dirname or not (self.data_dir / dirname).exists():
            return ""
        available = sorted(p.stem for p in (self.data_dir / dirname).glob("*.json"))
        return f";可用门店: {available}" if available else ""

    def day(self, category: str, store_id: str, date: str) -> dict | None:
        """取单日记录;记录缺日期时返回含 missing_fields 的占位。"""
        result = self.query(category, store_id, date)
        if result.get("missing_fields"):
            return None
        return result

    def baseline(self, category: str, store_id: str, date: str,
                 n: int | None = None) -> list[dict]:
        """取告警日前 n 天记录(缺哪天空哪,由调用方标记 missing)。"""
        n = n or CateringConfigs.BASELINE_DAYS
        days = []
        d = date_type.fromisoformat(date)
        for offset in range(1, n + 1):
            target = (d - timedelta(days=offset)).isoformat()
            record = self.day(category, store_id, target)
            if record is not None:
                days.append(record)
        return days

    def search_cases(self, store_id: str, symptom: str, top_k: int = 3,
                     date: str = "", main_factor: str = "",
                     include_deprecated: bool = False) -> dict:
        return self.query("knowledge_base", store_id, date,
                          symptom=symptom, top_k=top_k,
                          main_factor=main_factor,
                          include_deprecated=include_deprecated)

    def load_all_stores(self) -> list[dict]:
        """加载全部门店经营数据(告警扫描/定时任务的统一入口)。"""
        stores_dir = self.data_dir / "stores"
        stores = []
        if not stores_dir.exists():
            return stores
        for fp in sorted(stores_dir.glob("*.json")):
            try:
                stores.append(json.loads(fp.read_text()))
            except (OSError, json.JSONDecodeError):
                continue
        return stores
