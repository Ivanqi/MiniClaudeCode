"""数据源抽象:代码资产库、扫描规则库、运行环境库、云平台、知识库(五类)。

统一契约: DataSource.query(project_id, **params) -> 归一化 dict,
必含 project_id 与 missing_fields(缺失显式标记,永不猜数);
数据不可用时抛 DataSourceError。

换真实后端 = 新增一个适配器类并在 DataHub 注册,业务代码零改动:
- 资产库 → 公司代码资产管控系统的入库登记接口;
- 规则库 → SAST 工具规则集导入;
- 环境库 → CMDB/配置中心;
- 云平台 → 公司云环境真实部署 API(当前为模拟实现,接口契约见 readme);
- 知识库 → 企业安全整改案例库。
"""
import json
from abc import ABC, abstractmethod
from pathlib import Path

from CodeAnalysis.Configs import CodeAnalysisConfigs
from CodeAnalysis.Guardrails import dot_get
from CodeAnalysis.Metrics import risk_score

# 语言显示名(归一化资产里给人类读的)
LANGUAGE_LABELS = {"java": "Java", "csharp": "C#", "php": "PHP",
                   "python": "Python"}


class DataSourceError(Exception):
    """数据源不可用(未知项目/文件损坏)。"""


def missing_fields_of(record: dict, required: list[str]) -> list[str]:
    """行级必填校验:哪些必填字段缺失(显式标记,不填默认值)。"""
    return [k for k in required if record.get(k) is None]


class DataSource(ABC):
    """数据源统一契约(需求 2 的八类数据源模式)。"""

    category = ""   # 必须是 Models.SOURCE_CATEGORIES 之一

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir

    @abstractmethod
    def query(self, project_id: str, **params) -> dict:
        """返回归一化结果 dict;数据不可用时抛 DataSourceError。"""


class JsonFileSource(DataSource):
    """JSON 文件型数据源的公共读文件逻辑。"""

    def load_json(self, rel_path: str) -> dict:
        try:
            return json.loads((self.data_dir / rel_path).read_text(
                encoding="utf-8"))
        except FileNotFoundError:
            raise DataSourceError(f"数据文件不存在: {rel_path}") from None
        except json.JSONDecodeError as e:
            raise DataSourceError(f"数据文件损坏({rel_path}): {e}") from None


# ── 适配器:代码资产库 ──

class AssetSource(JsonFileSource):
    """代码资产库:入库登记的项目元数据 + 模块清单。"""

    category = "asset_db"

    def query(self, project_id: str, **params) -> dict:
        path = self.data_dir / "assets" / project_id / "asset.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise DataSourceError(f"未知项目: {project_id}") from None
        except json.JSONDecodeError as e:
            raise DataSourceError(f"资产文件损坏({project_id}): {e}") from None
        return self.normalize(payload, project_id)

    def all_assets(self) -> list[dict]:
        """全部入库资产(容错跳过坏文件)。"""
        assets = []
        root = self.data_dir / "assets"
        if not root.exists():
            return assets
        for project_dir in sorted(root.iterdir()):
            try:
                assets.append(self.query(project_dir.name))
            except DataSourceError:
                continue
        return assets

    @staticmethod
    def normalize(payload: dict, project_id: str) -> dict:
        """资产 JSON → 归一化资产字典(含 CodeAsset 字段与模块明细)。"""
        modules = list(payload.get("assets") or [])
        languages = sorted({str(m.get("language") or "") for m in modules
                            if m.get("language")})
        missing = missing_fields_of(payload, ["project_id", "name", "version"])
        return {
            "project_id": str(project_id),
            "name": str(payload.get("name") or ""),
            "owner": str(payload.get("owner") or ""),
            "deploy_scope": str(payload.get("deploy_scope") or "full"),
            "required_modules": list(payload.get("required_modules") or []),
            "version": str(payload.get("version") or ""),
            "ingest_date": str(payload.get("ingest_date") or ""),
            "language": "/".join(LANGUAGE_LABELS.get(l, l)
                                 for l in languages),
            "languages": languages,
            "module_count": len(modules),
            "modules": [dict(m) for m in modules],
            "base_image": str(modules[0].get("base_image") or "")
                          if modules else "",
            "analysis": {
                "languages": "/".join(LANGUAGE_LABELS.get(l, l)
                                      for l in languages),
                "deploy_scope": payload.get("deploy_scope", "full"),
            },
            "missing_fields": missing,
        }


# ── 适配器:扫描规则库 ──

class RuleSource(JsonFileSource):
    """扫描规则库:按语言组织的检测规则(Scanner 的加载后端)。"""

    category = "rule_library"

    def query(self, project_id: str = "", **params) -> dict:
        language = params.get("language")
        rules_dir = self.data_dir / "rules"
        if language:
            path = rules_dir / f"{language}.json"
            if not path.exists():
                raise DataSourceError(f"未知扫描语言: {language}")
            payload = json.loads(path.read_text(encoding="utf-8"))
            rules = payload.get("rules") or []
        else:
            rules = []
            if rules_dir.exists():
                for path in sorted(rules_dir.glob("*.json")):
                    try:
                        payload = json.loads(path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        continue
                    rules.extend(payload.get("rules") or [])
        return {"project_id": str(project_id or ""), "language": language,
                "rules": rules, "rule_count": len(rules),
                "missing_fields": []}


# ── 适配器:运行环境库 ──

class EnvSource(JsonFileSource):
    """运行环境库:环境配置快照 + 审查基线(纯 eq 语义,无正则无代码注入)。"""

    category = "env_config_db"

    def query(self, project_id: str, **params) -> dict:
        path = self.data_dir / "envs" / f"{project_id}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise DataSourceError(f"项目 {project_id} 无运行环境配置") from None
        except json.JSONDecodeError as e:
            raise DataSourceError(f"环境配置损坏({project_id}): {e}") from None
        return {"project_id": str(project_id),
                "env": str(payload.get("env") or ""),
                "values": dict(payload.get("values") or {}),
                "checks": list(payload.get("checks") or []),
                "missing_fields": []}

    def review(self, project_id: str) -> dict:
        """动态运行环境安全性审查:逐项基线比对(实际值 == target 即命中)。"""
        doc = self.query(project_id)
        issues = []
        for check in doc.get("checks") or []:
            try:
                actual = dot_get(doc.get("values") or {}, check["metric"])
            except (KeyError, TypeError, IndexError, ValueError):
                actual = None
            if actual == check.get("target"):
                issues.append({
                    "check_id": str(check.get("check_id") or ""),
                    "name": str(check.get("name") or ""),
                    "severity": str(check.get("severity") or "low"),
                    "likelihood": check.get("likelihood"),
                    "risk_score": risk_score(
                        str(check.get("severity") or "low"),
                        check.get("likelihood"),
                        CodeAnalysisConfigs.ENV_WEIGHTS),
                    "metric": str(check.get("metric") or ""),
                    "actual": actual,
                    "fix": str(check.get("fix") or ""),
                })
        severity_order = {"critical": 4, "high": 3, "medium": 2, "low": 1}
        max_severity = (max((i["severity"] for i in issues),
                            key=lambda s: severity_order.get(s, 0))
                        if issues else None)
        return {"project_id": str(project_id), "env": doc.get("env"),
                "issues": issues, "issue_count": len(issues),
                "max_severity": max_severity,
                "analysis": {"issue_count": len(issues)},
                "missing_fields": doc.get("missing_fields") or []}


# ── 适配器:公司云平台(模拟) ──

class CloudPlatformSource(JsonFileSource):
    """公司云平台:制品注册表 + 部署/健康检查接口(模拟实现)。

    换真实云环境 = 实现同一接口契约(deploy/health/registry),业务零改动。
    """

    category = "cloud_platform"

    def __init__(self, data_dir: Path, id_seq: int = 1):
        super().__init__(data_dir)
        self._seq = id_seq
        self._deploys: dict[str, dict] = {}   # 模拟云端的部署单状态

    def query(self, project_id: str, **params) -> dict:
        entries = [e for e in self.registry_entries()
                   if e.get("asset_id") == project_id]
        return {"project_id": str(project_id), "platform": self.platform(),
                "entries": entries, "entry_count": len(entries),
                "missing_fields": ([] if entries
                                   else [f"cloud_platform:{project_id}"])}

    def platform(self) -> str:
        try:
            payload = self.load_json("cloud/registry.json")
            return str(payload.get("platform") or "公司云平台(模拟)")
        except DataSourceError:
            return "公司云平台(模拟)"

    def registry_entries(self) -> list[dict]:
        try:
            payload = self.load_json("cloud/registry.json")
        except DataSourceError:
            return []
        return list(payload.get("entries") or [])

    def registry_map(self) -> dict:
        """{(asset_id, module): entry},Deployer 的注册表输入。"""
        return {(e.get("asset_id"), e.get("module")): e
                for e in self.registry_entries()}

    def deploy(self, asset_id: str, modules: list[str],
               base_image: str | None = None) -> dict:
        """模拟云端部署:登记部署单并立即健康。"""
        deploy_id = f"DEP-{self._seq:04d}"
        self._seq += 1
        self._deploys[deploy_id] = {"status": "healthy", "modules": modules,
                                    "base_image": base_image or "",
                                    "detail": "部署完成,服务健康"}
        return {"deploy_id": deploy_id, "status": "healthy",
                "base_image": base_image or "", "modules": list(modules)}

    def health(self, deploy_id: str) -> dict:
        """模拟云端健康检查(可用性)。"""
        state = self._deploys.get(deploy_id)
        if state is None:
            return {"status": "unknown", "detail": "部署单不存在或已回收"}
        return {"status": state.get("status", "unknown"),
                "detail": state.get("detail", "")}


# ── 适配器:知识库(历史整改案例) ──

class KnowledgeBaseSource(JsonFileSource):
    """知识库:历史整改案例(批准归档即记忆,供后续检索)。"""

    category = "knowledge_base"

    def all_cases(self) -> list[dict]:
        """全部案例(仅 cases/ 顶层文件,archive/ 归档不重复加载)。"""
        cases: list[dict] = []
        root = self.data_dir / "cases"
        if not root.exists():
            return cases
        for path in sorted(root.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            cases.extend(payload.get("cases") or [])
        return cases

    def query(self, project_id: str, symptom: str = "", top_k: int = 3,
              **params) -> dict:
        """关键词/标签打分检索(项目命中 > 主风险命中 > 标签命中)。"""
        matches = []
        for case in self.all_cases():
            score = 0.0
            if case.get("project_id") == project_id:
                score += 2.0
            main = str(case.get("main_risk") or "")
            if main and main in symptom:
                score += 2.0
            for tag in case.get("tags") or []:
                if tag in symptom:
                    score += 1.0
                elif any(tok in symptom for tok in str(tag).split()):
                    score += 0.5
            if score > 0:
                matches.append({"case_id": case.get("case_id"),
                                "project_id": case.get("project_id"),
                                "main_risk": main,
                                "summary": case.get("summary"),
                                "actions": case.get("actions"),
                                "tags": case.get("tags"),
                                "score": round(score, 2)})
        matches.sort(key=lambda m: m["score"], reverse=True)
        return {"project_id": str(project_id), "symptom": symptom,
                "match_count": len(matches[:top_k]),
                "matches": matches[:top_k], "missing_fields": []}


# ── 门面 ──

class DataHub:
    """五类数据源门面:统一查询入口 + 业务捷径。"""

    def __init__(self, data_dir: Path | None = None):
        self.data_dir = data_dir or CodeAnalysisConfigs.DATA_DIR
        self.sources: dict[str, DataSource] = {
            "asset_db": AssetSource(self.data_dir),
            "rule_library": RuleSource(self.data_dir),
            "env_config_db": EnvSource(self.data_dir),
            "cloud_platform": CloudPlatformSource(self.data_dir),
            "knowledge_base": KnowledgeBaseSource(self.data_dir),
        }

    def query(self, category: str, project_id: str = "", **params) -> dict:
        """按类别查询;未知类别/数据不可用抛 DataSourceError。"""
        source = self.sources.get(category)
        if source is None:
            raise DataSourceError(f"未知数据源类别: {category}")
        try:
            return source.query(project_id, **params)
        except DataSourceError as e:
            raise DataSourceError(f"{e};{self._available_hint()}") from None

    def _available_hint(self) -> str:
        """数据环境提示:可用项目清单,帮助 LLM 自纠参数。"""
        projects = [a.get("project_id") for a in self.asset_source.all_assets()]
        return f"可用项目: {projects}" if projects else "无可用项目"

    # ── 业务捷径 ──

    @property
    def asset_source(self) -> AssetSource:
        return self.sources["asset_db"]

    @property
    def env_source(self) -> EnvSource:
        return self.sources["env_config_db"]

    @property
    def cloud_source(self) -> CloudPlatformSource:
        return self.sources["cloud_platform"]

    def asset(self, project_id: str) -> dict | None:
        """资产归一化字典;不存在返回 None(不抛异常)。"""
        try:
            return self.asset_source.query(project_id)
        except DataSourceError:
            return None

    def all_assets(self) -> list[dict]:
        return self.asset_source.all_assets()

    def env_review(self, project_id: str) -> dict:
        """动态运行环境审查结果(issues + 风险分)。"""
        return self.env_source.review(project_id)

    def registry_entries(self) -> list[dict]:
        return self.cloud_source.registry_entries()

    def search_cases(self, project_id: str, symptom: str = "",
                     top_k: int = 3) -> dict:
        return self.sources["knowledge_base"].query(
            project_id, symptom=symptom, top_k=top_k)

    def src_root(self, project_id: str) -> Path:
        """项目源码根目录(模块 local_dir 相对此路径)。"""
        return self.data_dir / "assets" / project_id
