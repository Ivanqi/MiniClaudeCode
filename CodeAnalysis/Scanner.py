"""多语言静态扫描引擎(自动化扫描技术研究的目标 2 落地)。

数据驱动: 扫描规则全部声明在 data/rules/{java,csharp,php,python}.json,
安全团队直接改 JSON 即可调整规则,无需改动代码;换真实扫描器(如
SAST 工具/SonarQube)时实现同一 find 契约即可。

引擎设计(确定性、防失控):
- 逐行正则匹配,模式禁止跨行(防灾难性回溯),编译结果 lru_cache;
- not_match 为"净化器排除"正则:同一行命中主模式但包含净化函数
  (如 htmlspecialchars/参数化绑定)时判定为安全,不报告;
- 文件 1MB / 行 2000 字符 / 每文件 50 条发现三重上限;
- 每条发现附风险分(severity × likelihood,复用 Metrics.risk_score)、
  CWE/OWASP 映射与修复建议,报告结论可回溯到具体文件与行号。
"""
import json
import re
from functools import lru_cache
from pathlib import Path

from CodeAnalysis.Configs import CodeAnalysisConfigs
from CodeAnalysis.Metrics import risk_score

# 规则 flag 白名单(防规则文件注入任意 re 标志)
FLAG_MAP = {"i": re.IGNORECASE, "m": re.MULTILINE}


class ScannerError(Exception):
    """扫描不可用(未知语言/规则库损坏)。"""


@lru_cache(maxsize=512)
def _compile(pattern: str, flags: int) -> re.Pattern:
    return re.compile(pattern, flags)


def _rule_flags(rule: dict) -> int:
    value = 0
    for flag in str(rule.get("flags") or ""):
        if flag in FLAG_MAP:
            value |= FLAG_MAP[flag]
    return value


class Scanner:
    """多语言静态扫描器:规则加载 + 逐文件逐行匹配。"""

    def __init__(self, rules_dir: Path | None = None):
        self.rules_dir = rules_dir or CodeAnalysisConfigs.RULES_DIR
        self._rule_cache: dict[str, list[dict]] = {}

    # ── 规则库 ──

    def load_rules(self, language: str) -> list[dict]:
        """加载某语言的规则集;未知语言抛 ScannerError。"""
        if language in self._rule_cache:
            return self._rule_cache[language]
        path = self.rules_dir / f"{language}.json"
        if not path.exists():
            raise ScannerError(f"未知扫描语言: {language}")
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            raise ScannerError(f"规则库损坏({path}): {e}") from None
        rules = payload.get("rules") or []
        self._rule_cache[language] = rules
        return rules

    def available_languages(self) -> list[str]:
        """规则库覆盖的语言列表。"""
        langs = []
        if self.rules_dir.exists():
            langs = sorted(p.stem for p in self.rules_dir.glob("*.json"))
        return langs

    def language_extensions(self, language: str) -> list[str]:
        """某语言规则的源文件扩展名(供源码文件发现)。"""
        try:
            payload = json.loads((self.rules_dir / f"{language}.json")
                                 .read_text())
        except (OSError, json.JSONDecodeError) as e:
            raise ScannerError(f"规则库损坏({language}): {e}") from None
        return list(payload.get("extensions") or [])

    # ── 扫描 ──

    def scan_file(self, path: Path, rules: list[dict],
                  rel_path: str | None = None) -> list[dict]:
        """扫描单个源码文件,返回发现列表(每条含文件/行号/证据行)。"""
        findings: list[dict] = []
        try:
            if path.stat().st_size > CodeAnalysisConfigs.MAX_FILE_BYTES:
                return findings
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return findings
        display = rel_path or str(path)
        for lineno, line in enumerate(text.splitlines(), 1):
            if len(line) > CodeAnalysisConfigs.MAX_LINE_CHARS:
                continue
            for rule in rules:
                if len(findings) >= CodeAnalysisConfigs.MAX_FINDINGS_PER_FILE:
                    return findings
                compiled = _compile(rule.get("pattern", ""),
                                    _rule_flags(rule))
                if not compiled.search(line):
                    continue
                not_match = rule.get("not_match")
                if not_match and _compile(not_match,
                                          _rule_flags(rule)).search(line):
                    continue   # 净化器排除:同一行已做安全处理
                findings.append(self._make_finding(rule, display, lineno,
                                                   line.strip()))
        return findings

    @staticmethod
    def _make_finding(rule: dict, path: str, line: int,
                      evidence: str) -> dict:
        """组装一条发现:风险分 + 标准映射 + 修复建议 + 证据行。"""
        likelihood = rule.get("likelihood")
        return {
            "rule_id": str(rule.get("rule_id") or ""),
            "name": str(rule.get("name") or ""),
            "severity": str(rule.get("severity") or "low"),
            "likelihood": likelihood,
            "risk_score": risk_score(str(rule.get("severity") or "low"),
                                     likelihood,
                                     CodeAnalysisConfigs.STATIC_WEIGHTS),
            "cwes": list(rule.get("cwes") or []),
            "owasp": str(rule.get("owasp") or ""),
            "iso27001": str(rule.get("iso27001") or ""),
            "file": path,
            "line": line,
            "evidence": evidence,
            "fix": str(rule.get("fix") or ""),
        }

    def scan_project(self, asset: dict, src_root: Path) -> list[dict]:
        """扫描一个资产的全部模块源码,返回合并发现列表。

        asset 为 DataHub 归一化的资产字典(含 modules 列表,
        每模块带 language 与 local_dir)。
        """
        findings: list[dict] = []
        for module in asset.get("modules") or []:
            language = str(module.get("language") or "")
            local_dir = str(module.get("local_dir") or "")
            try:
                rules = self.load_rules(language)
                extensions = self.language_extensions(language)
            except ScannerError:
                continue
            src_dir = src_root / local_dir
            if not src_dir.exists():
                continue
            files: list[Path] = []
            for ext in extensions:
                files.extend(src_dir.rglob(f"*{ext}"))
            for path in sorted(set(files), key=str):
                rel = str(path.relative_to(src_root))
                findings.extend(self.scan_file(path, rules, rel_path=rel))
        return findings
