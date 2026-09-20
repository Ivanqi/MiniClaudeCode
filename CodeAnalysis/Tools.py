"""代码安全分析工具集:tool_defs()/handlers() 双接口,可直接注册进 src.ToolRegistry。

9 个只读工具 + 2 个写工具:
- 只读: 资产盘点/详情、静态扫描、规则库查询、安全自评估、环境审查、
        部署验证复核、部署就绪状态、案例检索;
- 写:   deploy_asset(一键部署)、register_asset(代码入库登记)。

权限设计(与 Catering 的差异,见 ToolGuard 注释):
- 写工具对 LLM 探索模式默认不暴露(tool_defs 不含 + ToolGuard 拦截),
  需 CODEANALYSIS_ENABLE_WRITES=1;
- 工作流模式的 deploy 步骤是流水线正规步骤(人工启动即授权),
  handler 不因开关拒绝;register_asset 仅 LLM 使用,关闭时直接报错。

Handler 返回归一化 dict(含 missing_fields 显式标记),参数名与
JSON-schema 属性名一一对应(handler(**args) 分发的约定)。
"""
import json
from datetime import datetime

from src.BasicOper import BasicOper
from src.LlmClient import make_function_tool

from CodeAnalysis.Configs import CodeAnalysisConfigs
from CodeAnalysis.DataHub import (DataHub, DataSourceError, LANGUAGE_LABELS)
from CodeAnalysis.Deployer import Deployer
from CodeAnalysis.Metrics import overall_assessment, sha256_file
from CodeAnalysis.Scanner import Scanner, ScannerError

# 写工具集合(与 ToolGuard.WRITE_TOOLS 一致)
WRITE_TOOLS = {"deploy_asset", "register_asset"}


class CodeAnalysisTools:
    """代码安全分析工具集:数据/扫描/部署三引擎的薄门面。"""

    def __init__(self, hub: DataHub | None = None,
                 scanner: Scanner | None = None,
                 deployer: Deployer | None = None,
                 enable_writes: bool | None = None):
        self.hub = hub or DataHub()
        self.scanner = scanner or Scanner()
        self.deployer = deployer or Deployer(
            self.hub.cloud_source, self.hub.cloud_source.registry_map(),
            assets_dir=CodeAnalysisConfigs.ASSETS_DIR)
        self.enable_writes = (enable_writes if enable_writes is not None
                              else CodeAnalysisConfigs.ENABLE_WRITES)

    # ── 统一执行入口 ──

    def execute(self, name: str, args: dict) -> dict:
        """执行工具:异常归一化为 {error, missing_fields},结果恒为 dict。"""
        handler = self.handlers().get(name)
        try:
            result = BasicOper.call_tool_handler(handler, args, name)
        except (DataSourceError, ScannerError, ValueError, KeyError) as e:
            return {"error": str(e), "missing_fields": [str(e)]}
        if not isinstance(result, dict):
            return {"text": str(result)}
        return result

    # ── 只读 handler ──

    def run_inventory_assets(self) -> dict:
        """代码资产清单:入库项目 + 语言 + 版本概览。"""
        assets = self.hub.all_assets()
        return {
            "assets": [{"project_id": a["project_id"], "name": a["name"],
                        "language": a["language"], "version": a["version"],
                        "ingest_date": a["ingest_date"]}
                       for a in assets],
            "asset_count": len(assets),
            "analysis": {"projects": [a["project_id"] for a in assets]},
            "missing_fields": [],
        }

    def run_get_asset(self, project_id: str) -> dict:
        """资产详情:模块/语言/部署范围/制品清单。"""
        asset = self.hub.asset(project_id)
        if asset is None:
            raise DataSourceError(f"未知项目: {project_id}")
        if asset.get("missing_fields"):
            return {**asset, "error": f"资产字段缺失: "
                    f"{', '.join(asset['missing_fields'])}"}
        return asset

    def run_scan_static(self, project_id: str) -> dict:
        """多语言静态扫描:按资产语言加载规则,逐文件逐行匹配。"""
        asset = self.hub.asset(project_id)
        if asset is None:
            raise DataSourceError(f"未知项目: {project_id}")
        findings = self.scanner.scan_project(asset, self.hub.src_root(project_id))
        counts = {s: sum(1 for f in findings if f["severity"] == s)
                  for s in ("critical", "high", "medium", "low")}
        return {"project_id": project_id, "findings": findings,
                "total": len(findings),
                "critical_count": counts["critical"],
                "high_count": counts["high"],
                "medium_count": counts["medium"],
                "low_count": counts["low"],
                "analysis": {"languages": asset.get("language"),
                             "module_count": asset.get("module_count")},
                "missing_fields": []}

    def run_list_scan_rules(self, language: str | None = None) -> dict:
        """扫描规则库查询:规则 ID/严重度/CWE/OWASP 映射。"""
        payload = self.hub.query("rule_library", "",
                                 language=language) if language \
            else self.hub.query("rule_library", "")
        rules = payload.get("rules") or []
        return {"language": language or "all",
                "rules": [{"rule_id": r.get("rule_id"), "name": r.get("name"),
                           "severity": r.get("severity"),
                           "cwes": r.get("cwes"), "owasp": r.get("owasp")}
                          for r in rules],
                "rule_count": len(rules),
                "analysis": {"rule_count": len(rules)},
                "missing_fields": []}

    def run_assess_security(self, project_id: str) -> dict:
        """安全等级自评估:静态+环境风险 → 总分/等级(部署风险见部署步骤)。"""
        scan = self.run_scan_static(project_id)
        env = self.hub.env_review(project_id)
        assessment = overall_assessment(scan.get("findings") or [],
                                        env.get("issues") or [], [])
        return {"project_id": project_id,
                "score": assessment["risk_score"],
                "subtotals": assessment["subtotals"],
                "critical_count": assessment["critical_count"],
                "level": assessment["security_level"],
                "analysis": {"level": assessment["security_level"],
                             "score": assessment["risk_score"],
                             "vuln_count": scan.get("total") or 0,
                             "env_issue_count": env.get("issue_count") or 0},
                "missing_fields": []}

    def run_review_env(self, project_id: str) -> dict:
        """动态运行环境安全性审查:按环境基线逐项比对。"""
        env = self.hub.env_review(project_id)
        env["analysis"] = {"issue_count": env.get("issue_count") or 0,
                           "max_severity": env.get("max_severity")}
        return env

    def run_deploy_asset(self, project_id: str) -> dict:
        """一键部署(写工具):门禁 → 完整性 → 真实性 → 云部署 → 可用性。

        工作流模式的流水线正规步骤(人工启动即授权);探索模式下
        ToolGuard 仅在 CODEANALYSIS_ENABLE_WRITES=1 时放行。
        """
        asset = self.hub.asset(project_id)
        if asset is None:
            raise DataSourceError(f"未知项目: {project_id}")
        scan = self.run_scan_static(project_id)
        env = self.hub.env_review(project_id)
        assessment = overall_assessment(scan.get("findings") or [],
                                        env.get("issues") or [], [])
        record = self.deployer.deploy(asset, assessment)
        record["analysis"] = dict(record.get("analysis") or {})
        record["analysis"].update({"pre_deploy_level":
                                   assessment["security_level"]})
        return record

    def run_verify_deployment(self, project_id: str, deploy_id: str) -> dict:
        """部署验证复核(可用性/真实性/完整性),与部署记录相互独立。"""
        asset = self.hub.asset(project_id)
        if asset is None:
            raise DataSourceError(f"未知项目: {project_id}")
        return self.deployer.verify(asset, deploy_id)

    def run_deploy_status(self, project_id: str) -> dict:
        """部署就绪状态:模块是否登记/制品是否就位/校验和是否匹配。"""
        asset = self.hub.asset(project_id)
        if asset is None:
            raise DataSourceError(f"未知项目: {project_id}")
        registry = self.deployer.registry
        modules = []
        for m in asset.get("modules") or []:
            entry = registry.get((m.get("asset_id"), m.get("module")))
            artifact_path = (CodeAnalysisConfigs.ASSETS_DIR
                             / str(m.get("asset_id"))
                             / str(m.get("artifact") or ""))
            checksum = sha256_file(artifact_path)
            modules.append({
                "module": m.get("module"),
                "registered": entry is not None,
                "artifact_present": checksum is not None,
                "checksum_ok": bool(checksum and entry
                                    and checksum == entry.get("sha256")),
            })
        return {"project_id": project_id,
                "ready": all(mo["registered"] and mo["artifact_present"]
                             and mo["checksum_ok"] for mo in modules),
                "modules": modules,
                "analysis": {"module_count": len(modules)},
                "missing_fields": []}

    def run_search_cases(self, project_id: str, symptom: str,
                         top_k: int = 3) -> dict:
        """知识库案例检索:历史整改案例打分匹配。"""
        return self.hub.search_cases(project_id, symptom, top_k=top_k)

    # ── 写 handler ──

    def run_register_asset(self, project_id: str, name: str,
                           language: str, version: str) -> dict:
        """代码入库登记(写工具,仅 LLM 探索模式):建立资产目录与元数据。"""
        if not self.enable_writes:
            raise ValueError("写工具 register_asset 默认禁用"
                             "(需开启 CODEANALYSIS_ENABLE_WRITES)")
        if language not in LANGUAGE_LABELS:
            raise ValueError(f"不支持的开发语言: {language}"
                             f"(可选: {sorted(LANGUAGE_LABELS)})")
        project_dir = CodeAnalysisConfigs.ASSETS_DIR / project_id
        if project_dir.exists():
            raise ValueError(f"项目 {project_id} 已入库")
        payload = {
            "schema_version": CodeAnalysisConfigs.SCHEMA_VERSION,
            "project_id": project_id, "name": name, "owner": "",
            "deploy_scope": "full", "required_modules": [],
            "version": version,
            "ingest_date": datetime.now().strftime("%Y-%m-%d"),
            "assets": [{"asset_id": project_id, "module": "core",
                        "language": language, "local_dir": "src/core",
                        "artifact": "", "version": version,
                        "base_image": ""}],
        }
        (project_dir / "src" / "core").mkdir(parents=True, exist_ok=True)
        (project_dir / "asset.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8")
        return {"project_id": project_id, "registered": True,
                "asset_path": str(project_dir / "asset.json"),
                "missing_fields": []}

    # ── 工具注册接口(src.ToolRegistry 协议) ──

    def tool_defs(self) -> list[dict]:
        """工具定义(OpenAI function 格式);写工具仅写模式对 LLM 暴露。"""
        tools = [
            make_function_tool("inventory_assets", "列出已入库的代码资产清单(项目/语言/版本)",
                               {"type": "object", "properties": {}}),
            make_function_tool("get_asset", "查询某个代码资产的详情(模块/语言/部署范围)",
                               {"type": "object",
                                "properties": {"project_id": {"type": "string",
                                                              "description": "项目 ID,如 PJ-A-001"}},
                                "required": ["project_id"]}),
            make_function_tool("scan_static", "对项目源码执行多语言静态安全扫描(Java/C#/PHP/Python)",
                               {"type": "object",
                                "properties": {"project_id": {"type": "string"}},
                                "required": ["project_id"]}),
            make_function_tool("list_scan_rules", "查询扫描规则库(可指定语言)",
                               {"type": "object",
                                "properties": {"language": {"type": "string",
                                                            "description": "java/csharp/php/python,缺省为全部"}}}),
            make_function_tool("assess_security", "计算项目的安全等级自评估(风险分+等级)",
                               {"type": "object",
                                "properties": {"project_id": {"type": "string"}},
                                "required": ["project_id"]}),
            make_function_tool("review_env", "审查项目动态运行环境配置安全",
                               {"type": "object",
                                "properties": {"project_id": {"type": "string"}},
                                "required": ["project_id"]}),
            make_function_tool("verify_deployment", "部署验证复核:可用性/真实性/完整性",
                               {"type": "object",
                                "properties": {"project_id": {"type": "string"},
                                               "deploy_id": {"type": "string",
                                                             "description": "部署单号,如 DEP-0001"}},
                                "required": ["project_id", "deploy_id"]}),
            make_function_tool("deploy_status", "查询项目部署就绪状态(登记/制品/校验和)",
                               {"type": "object",
                                "properties": {"project_id": {"type": "string"}},
                                "required": ["project_id"]}),
            make_function_tool("search_cases", "检索历史整改案例(知识库)",
                               {"type": "object",
                                "properties": {"project_id": {"type": "string"},
                                               "symptom": {"type": "string"},
                                               "top_k": {"type": "integer"}},
                                "required": ["project_id", "symptom"]}),
        ]
        if self.enable_writes:
            tools.append(make_function_tool(
                "deploy_asset", "一键部署到公司云环境(门禁/完整性/真实性/可用性)",
                {"type": "object",
                 "properties": {"project_id": {"type": "string"}},
                 "required": ["project_id"]}))
            tools.append(make_function_tool(
                "register_asset", "代码入库登记(新建资产目录与元数据)",
                {"type": "object",
                 "properties": {"project_id": {"type": "string"},
                                "name": {"type": "string"},
                                "language": {"type": "string",
                                             "description": "java/csharp/php/python"},
                                "version": {"type": "string"}},
                 "required": ["project_id", "name", "language", "version"]}))
        return tools

    def handlers(self) -> dict:
        """工具名 → handler 方法(恒含写工具,供工作流与分析链校验)。"""
        return {"inventory_assets": self.run_inventory_assets,
                "get_asset": self.run_get_asset,
                "scan_static": self.run_scan_static,
                "list_scan_rules": self.run_list_scan_rules,
                "assess_security": self.run_assess_security,
                "review_env": self.run_review_env,
                "deploy_asset": self.run_deploy_asset,
                "verify_deployment": self.run_verify_deployment,
                "deploy_status": self.run_deploy_status,
                "search_cases": self.run_search_cases,
                "register_asset": self.run_register_asset}
