"""一键部署模块(需求 4 与量化指标 1 的落地):与公司云环境联动的自动化部署。

部署流水线(每步失败即拦截,失败原因显式记录,绝不静默):
1. 安全门禁:  自评估等级 E(危险)禁止部署,需完成整改后重新评估;
2. 完整性:    制品 SHA-256 与云平台制品注册表比对,不一致判定疑似篡改;
3. 真实性:    制品签名/版本与注册表登记信息一致(来源可追溯);
4. 云部署:    调用云平台适配器执行部署(当前为模拟实现,换真实云平台
               = 替换 CloudPlatformSource 适配器,业务代码零改动);
5. 可用性:    部署后健康检查。

verify() 对部署结果做独立复核(重新计算校验和/签名/健康状态),
显式命名可用性/真实性/完整性三项,构成部署验证闭环。

部分部署(量化指标:工程技术一体化系统部分部署)由资产元数据
deploy_scope=partial + required_modules 声明,属合规场景而非失败。
"""
from pathlib import Path

from CodeAnalysis.Configs import CodeAnalysisConfigs
from CodeAnalysis.Metrics import sha256_file


class Deployer:
    """一键部署器:门禁 + 部署前校验 + 云部署 + 验证复核。"""

    def __init__(self, cloud, registry: dict, assets_dir: Path | None = None,
                 id_seq: int = 1):
        self.cloud = cloud                 # 云平台适配器(模拟)
        self.registry = dict(registry)     # {(asset_id, module): entry}
        self.assets_dir = assets_dir or CodeAnalysisConfigs.ASSETS_DIR
        self._seq = id_seq                 # 部署单号序列(可注入保证确定性)

    # ── 模块选择(全量 / 部分) ──

    @staticmethod
    def _select_modules(asset: dict) -> list[dict]:
        """按资产元数据选择待部署模块;partial 只部署 required_modules。"""
        modules = list(asset.get("modules") or [])
        if asset.get("deploy_scope") == "partial":
            required = set(asset.get("required_modules") or [])
            modules = [m for m in modules if m.get("module") in required]
        return modules

    # ── 部署 ──

    def deploy(self, asset: dict, assessment: dict) -> dict:
        """执行一键部署,返回自包含的部署记录 dict。

        status: deployed | blocked | failed(blocked/failed 时 verify 不执行)。
        """
        record = {
            "deploy_id": None,
            "status": "blocked",
            "blocked_reason": None,
            "partial": asset.get("deploy_scope") == "partial",
            "modules_deployed": 0,
            "requested_modules": [],
            # None = 未执行(前置校验拦截后不再继续),已执行项恒为 {passed, detail}
            "verification": {"integrity": None, "authenticity": None,
                             "availability": None},
            "risk_events": [],
            "cloud": {},
            "analysis": {},
        }
        # ① 安全门禁
        level = assessment.get("security_level")
        if level == CodeAnalysisConfigs.DEPLOY_GATE_LEVEL:
            record["blocked_reason"] = (f"安全门禁: 安全等级 {level}(危险)"
                                        "禁止部署,需完成整改后重新评估")
            record["analysis"] = {"gate": "rejected", "level": level}
            return record

        modules = self._select_modules(asset)
        record["requested_modules"] = [m.get("module") for m in modules]
        # ② 部署前校验:完整性(校验和)→ 真实性(签名/版本),失败即拦截
        for module in modules:
            entry = self.registry.get((module.get("asset_id"),
                                       module.get("module")))
            if entry is None:
                record = self._block(record, "authenticity", "high", 12.0,
                                     f"模块 {module.get('module')} 未在制品注册表登记")
                return record
            integrity_ok = self._check_integrity(module, entry)
            record["verification"]["integrity"] = integrity_ok
            if not integrity_ok.get("passed"):
                record = self._block(record, "integrity", "critical", 15.0,
                                     integrity_ok.get("detail", ""))
                return record
            authenticity_ok = self._check_authenticity(module, entry)
            record["verification"]["authenticity"] = authenticity_ok
            if not authenticity_ok.get("passed"):
                record = self._block(record, "authenticity", "high", 12.0,
                                     authenticity_ok.get("detail", ""))
                return record
        # ③ 云平台部署
        try:
            result = self.cloud.deploy(asset_id=asset.get("project_id"),
                                       modules=record["requested_modules"],
                                       base_image=asset.get("base_image"))
        except Exception as e:
            record = self._block(record, "availability", "high", 10.0,
                                 f"云平台部署调用失败: {type(e).__name__}: {e}")
            return record
        record["deploy_id"] = result.get("deploy_id")
        record["cloud"] = result
        # ④ 可用性:部署后健康检查
        try:
            health = self.cloud.health(record["deploy_id"])
        except Exception as e:
            health = {"status": "error", "detail": f"{type(e).__name__}: {e}"}
        record["verification"]["availability"] = {
            "passed": health.get("status") == "healthy",
            "detail": health.get("detail") or health.get("status"),
        }
        if not record["verification"]["availability"]["passed"]:
            record = self._block(record, "availability", "high", 10.0,
                                 f"部署后健康检查未通过: {health.get('status')}")
            record["status"] = "failed"   # 已部署但不健康(可用性未达标)
            return record
        record["status"] = "deployed"
        record["modules_deployed"] = len(modules)
        record["analysis"] = {"gate": "passed", "level": level,
                              "partial": record["partial"]}
        return record

    def _check_integrity(self, module: dict, entry: dict) -> dict:
        """完整性:制品 SHA-256 与注册表比对。"""
        artifact = self.assets_dir / str(module.get("asset_id")) \
            / str(module.get("artifact") or "")
        actual = sha256_file(artifact)
        expected = str(entry.get("sha256") or "")
        if actual is None:
            return {"passed": False, "detail": "制品文件缺失或不可读"}
        if actual != expected:
            return {"passed": False,
                    "detail": "制品 SHA-256 与注册表不一致(疑似构建产物被篡改)"}
        return {"passed": True, "detail": f"sha256 {actual[:16]}… 一致"}

    def _check_authenticity(self, module: dict, entry: dict) -> dict:
        """真实性:制品签名与版本与注册表登记信息一致。"""
        expected = (f"sig:CN={entry.get('cn')}:v{entry.get('version')}:"
                    f"{str(entry.get('sha256') or '')[:16]}")
        if str(entry.get("signature") or "") != expected:
            return {"passed": False,
                    "detail": "制品签名与注册表登记不一致(来源不可追溯)"}
        if str(module.get("version") or "") != str(entry.get("version") or ""):
            return {"passed": False,
                    "detail": f"模块版本 {module.get('version')} 与注册表 "
                              f"{entry.get('version')} 不一致"}
        return {"passed": True, "detail": f"签名有效({expected[:32]}…)"}

    def _block(self, record: dict, check: str, severity: str, weight: float,
               detail: str) -> dict:
        """拦截:记录失败原因 + 部署风险事件(风险分计入总评估)。"""
        record["status"] = "blocked"
        record["blocked_reason"] = f"{check}: {detail}"
        record["risk_events"].append({
            "check": check, "severity": severity, "weight": weight,
            "risk_score": weight,   # 部署失败为确定性事件,可能性 1.0
            "detail": detail,
        })
        return record

    # ── 部署后独立复核 ──

    def verify(self, asset: dict, deploy_id: str) -> dict:
        """部署验证复核:重新计算完整性/真实性/可用性,与部署记录相互独立。"""
        checks = {}
        modules = self._select_modules(asset)
        integrity_parts, authenticity_parts = [], []
        for module in modules:
            entry = self.registry.get((module.get("asset_id"),
                                       module.get("module")))
            if entry is None:
                integrity_parts.append(
                    {"passed": False,
                     "detail": f"模块 {module.get('module')} 未登记"})
                continue
            integrity_parts.append(self._check_integrity(module, entry))
            authenticity_parts.append(self._check_authenticity(module, entry))
        checks["integrity"] = {
            "passed": all(p.get("passed") for p in integrity_parts),
            "detail": "; ".join(p.get("detail", "") for p in integrity_parts),
        }
        checks["authenticity"] = {
            "passed": all(p.get("passed") for p in authenticity_parts),
            "detail": "; ".join(p.get("detail", "") for p in authenticity_parts),
        }
        try:
            health = self.cloud.health(deploy_id)
        except Exception as e:
            health = {"status": "error", "detail": f"{type(e).__name__}: {e}"}
        checks["availability"] = {
            "passed": health.get("status") == "healthy",
            "detail": health.get("detail") or health.get("status"),
        }
        return {"deploy_id": deploy_id,
                "all_passed": all(c["passed"] for c in checks.values()),
                "checks": checks,
                "analysis": {"modules_verified": len(modules)}}
