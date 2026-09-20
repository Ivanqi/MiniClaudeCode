"""样本数据生成器(确定性、幂等):资产源码/制品、环境配置、云注册表、规则片段、期望值。

运行: python CodeAnalysis/gen_fixtures.py

设计(与 Catering/gen_fixtures.py 同构):
- 全部字节级确定性(无时间戳、无随机),重复生成结果一致(幂等);
- 4 个样本项目对应量化指标的覆盖系统: 科技管理系统(Java,全量部署)、
  工程技术一体化系统(C#,部分部署)、二级单位质量管理系统(PHP,制品被篡改)、
  数据分析工具集(Python,零命中对照组);
- 植入漏洞只在"场景相关"的文件中;其余规则由 data/snippets/ 的
  planted/clean 片段对保证全覆盖(每条规则一个片段对);
- expected_assessments.json 由内联独立重实现的风险公式计算,
  与 Metrics 生产实现交叉验证;
- 生成时自校验: 扫描结果 == 期望漏洞清单、风险模型 == 独立重实现、
  篡改制品校验和不匹配持续存在、片段对命中/不命中正确。
"""
import hashlib
import json
from pathlib import Path

from CodeAnalysis.Configs import CodeAnalysisConfigs
from CodeAnalysis.DataHub import AssetSource
from CodeAnalysis.Metrics import overall_assessment
from CodeAnalysis.Scanner import Scanner

DATA_DIR = CodeAnalysisConfigs.DATA_DIR
INGEST_DATE = "2026-09-18"

# ════════════════════════════════════════════════════════════════════
# 1. 扫描规则库(数据驱动,安全团队直接改这里 → 重新生成即可)
# ════════════════════════════════════════════════════════════════════

RULES = {
    "java": {
        "extensions": [".java"],
        "rules": [
            {"rule_id": "CA-JAVA-SQLI-001", "name": "JDBC SQL 字符串拼接",
             "severity": "high", "likelihood": 0.9,
             "pattern": r"[\"']\s*(?:SELECT|INSERT|UPDATE|DELETE)\b[^\"'\n]{0,300}[\"']\s*\+\s*[\w\"']",
             "flags": "i", "not_match": "",
             "cwes": ["CWE-89"], "owasp": "A03:2021",
             "iso27001": "A.8.28",
             "evidence_hint": "字符串拼接构造 SQL 语句",
             "fix": "改用 PreparedStatement 参数化查询"},
            {"rule_id": "CA-JAVA-CRYPTO-002", "name": "DES/ECB 等弱加密算法",
             "severity": "medium", "likelihood": 0.7,
             "pattern": r"Cipher\s*\.\s*getInstance\s*\(\s*\"(?:DES|DESede|RC2|RC4)",
             "flags": "i", "not_match": "",
             "cwes": ["CWE-327"], "owasp": "A02:2021",
             "iso27001": "A.8.24",
             "evidence_hint": "使用已淘汰的对称加密算法",
             "fix": "改用 AES-GCM 等强加密算法"},
            {"rule_id": "CA-JAVA-LOG-003", "name": "日志记录敏感字段",
             "severity": "medium", "likelihood": 0.6,
             "pattern": r"log(?:ger)?\s*\.\s*(?:info|debug|warn|error)\s*\([^)\n]{0,140}?(?:password|passwd|pwd|idCard|cardNo)\b",
             "flags": "i", "not_match": "",
             "cwes": ["CWE-532"], "owasp": "A09:2021",
             "iso27001": "A.8.12",
             "evidence_hint": "日志输出含明文口令/证件号等敏感字段",
             "fix": "日志脱敏,禁止输出口令/证件号等敏感字段"},
        ],
    },
    "csharp": {
        "extensions": [".cs"],
        "rules": [
            {"rule_id": "CA-CS-SQLI-001", "name": "SqlCommand SQL 字符串拼接",
             "severity": "high", "likelihood": 0.8,
             "pattern": r"[\"']\s*(?:SELECT|INSERT|UPDATE|DELETE)\b[^\"'\n]{0,300}[\"']\s*\+\s*[\w\"']",
             "flags": "i", "not_match": "",
             "cwes": ["CWE-89"], "owasp": "A03:2021",
             "iso27001": "A.8.28",
             "evidence_hint": "字符串拼接构造 SQL 语句",
             "fix": "改用参数化查询(SqlCommand 参数集合)"},
            {"rule_id": "CA-CS-CRYPTO-001", "name": "MD5 弱哈希用于口令散列",
             "severity": "high", "likelihood": 0.8,
             "pattern": r"\bMD5\s*\.\s*(?:Create|ComputeHash|create)\s*\(|\bnew\s+MD5CryptoServiceProvider\b",
             "flags": "i", "not_match": "",
             "cwes": ["CWE-328"], "owasp": "A02:2021",
             "iso27001": "A.8.24",
             "evidence_hint": "使用 MD5 计算口令散列",
             "fix": "口令散列改用加盐慢哈希(bcrypt/argon2)"},
            {"rule_id": "CA-CS-DESER-002", "name": "BinaryFormatter 不安全反序列化",
             "severity": "high", "likelihood": 0.7,
             "pattern": r"\bnew\s+BinaryFormatter\s*\(|\bBinaryFormatter\s+\w+\s*=\s*new\b",
             "flags": "i", "not_match": "",
             "cwes": ["CWE-502"], "owasp": "A08:2021",
             "iso27001": "A.8.26",
             "evidence_hint": "使用 BinaryFormatter 反序列化不受信数据",
             "fix": "改用 DataContractJsonSerializer/System.Text.Json 并校验输入"},
            {"rule_id": "CA-CS-EXPOSE-003", "name": "异常堆栈直接返回前端",
             "severity": "medium", "likelihood": 0.5,
             "pattern": r"return\s+[^;\n]{0,90}\b(?:ex|e)\s*\.\s*(?:Message|StackTrace|ToString)",
             "flags": "i", "not_match": "",
             "cwes": ["CWE-209"], "owasp": "A05:2021",
             "iso27001": "A.8.4",
             "evidence_hint": "异常信息(堆栈)直接返回前端泄露内部实现",
             "fix": "统一错误处理,前端返回通用错误文案"},
        ],
    },
    "php": {
        "extensions": [".php"],
        "rules": [
            {"rule_id": "CA-PHP-SQLI-001", "name": "mysqli SQL 字符串拼接",
             "severity": "high", "likelihood": 0.8,
             "pattern": r"[\"']\s*(?:SELECT|INSERT|UPDATE|DELETE)\b[^\"'\n]{0,300}[\"']\s*\.\s*\$_\w+",
             "flags": "i", "not_match": "",
             "cwes": ["CWE-89"], "owasp": "A03:2021",
             "iso27001": "A.8.28",
             "evidence_hint": "字符串拼接构造 SQL 语句",
             "fix": "改用 PDO/mysqli 预处理语句"},
            {"rule_id": "CA-PHP-XSS-001", "name": "反射型 XSS(直接输出用户输入)",
             "severity": "high", "likelihood": 0.8,
             "pattern": r"\b(?:echo|print)\b[^\n;]{0,160}?\$_(?:GET|POST|REQUEST)\b",
             "flags": "i",
             "not_match": r"htmlspecialchars|htmlentities|esc_html|esc_attr|urlencode",
             "cwes": ["CWE-79"], "owasp": "A03:2021",
             "iso27001": "A.8.28",
             "evidence_hint": "直接输出未编码的用户输入",
             "fix": "输出前用 htmlspecialchars 编码"},
            {"rule_id": "CA-PHP-INCLUDE-002", "name": "动态 include 用户输入",
             "severity": "medium", "likelihood": 0.5,
             "pattern": r"\b(?:include|require)(?:_once)?\s*\(?\s*\$_(?:GET|POST|REQUEST|COOKIE)\b",
             "flags": "i", "not_match": "",
             "cwes": ["CWE-98"], "owasp": "A03:2021",
             "iso27001": "A.8.28",
             "evidence_hint": "include 路径来自用户输入",
             "fix": "使用固定视图路径白名单"},
            {"rule_id": "CA-PHP-CODE-003", "name": "eval/assert 代码执行",
             "severity": "critical", "likelihood": 0.9,
             "pattern": r"\b(?:eval|assert)\s*\(\s*\$_(?:GET|POST|REQUEST|COOKIE)|\bcall_user_func\s*\(\s*\$_(?:GET|POST|REQUEST)|\bcreate_function\s*\(",
             "flags": "i", "not_match": "",
             "cwes": ["CWE-95"], "owasp": "A03:2021",
             "iso27001": "A.8.28",
             "evidence_hint": "用户输入进入代码执行函数",
             "fix": "删除动态代码执行,改为固定逻辑"},
            {"rule_id": "CA-PHP-DESER-004", "name": "unserialize 反序列化用户输入",
             "severity": "low", "likelihood": 0.3,
             "pattern": r"\bunserialize\s*\(\s*\$_(?:GET|POST|REQUEST|COOKIE)",
             "flags": "i", "not_match": "json_decode",
             "cwes": ["CWE-502"], "owasp": "A08:2021",
             "iso27001": "A.8.26",
             "evidence_hint": "用户输入直接进入 unserialize",
             "fix": "改用 json_decode 并限制 allowed_classes"},
        ],
    },
    "python": {
        "extensions": [".py"],
        "rules": [
            {"rule_id": "CA-PY-EVAL-001", "name": "eval/exec 执行外部输入",
             "severity": "critical", "likelihood": 0.9,
             "pattern": r"\b(?:eval|exec)\s*\(\s*(?:request\.(?:args|form|values|GET|POST|json)\.?(?:get)?\s*\(|input\s*\()",
             "flags": "i", "not_match": "",
             "cwes": ["CWE-95"], "owasp": "A03:2021",
             "iso27001": "A.8.28",
             "evidence_hint": "用户输入进入 eval/exec",
             "fix": "改用 ast.literal_eval 或解析器"},
            {"rule_id": "CA-PY-CMD-002", "name": "subprocess shell=True 命令注入",
             "severity": "critical", "likelihood": 0.9,
             "pattern": r"\bsubprocess\s*\.\s*(?:Popen|run|call|check_output)\s*\([^)\n]{0,120}shell\s*=\s*True",
             "flags": "i", "not_match": "",
             "cwes": ["CWE-78"], "owasp": "A03:2021",
             "iso27001": "A.8.28",
             "evidence_hint": "shell=True 拼接外部输入构造命令",
             "fix": "参数列表调用并移除 shell=True"},
            {"rule_id": "CA-PY-YAML-003", "name": "yaml.load 未指定 SafeLoader",
             "severity": "high", "likelihood": 0.6,
             "pattern": r"\byaml\.load\s*\((?![^)\n]{0,80}(?:Safe|CSafe)Loader)[^)\n]{0,80}\)",
             "flags": "", "not_match": "",
             "cwes": ["CWE-502"], "owasp": "A08:2021",
             "iso27001": "A.8.26",
             "evidence_hint": "yaml.load 未使用安全加载器",
             "fix": "改用 yaml.safe_load 或显式 SafeLoader"},
            {"rule_id": "CA-PY-SECRET-004", "name": "源码明文硬编码密钥",
             "severity": "low", "likelihood": 0.5,
             "pattern": r"\b(?:SECRET_KEY|API_KEY|PASSWORD|TOKEN)\s*=\s*[\"'][^\"'\s]{8,}[\"']",
             "flags": "i", "not_match": r"os\.environ|getenv",
             "cwes": ["CWE-798"], "owasp": "A07:2021",
             "iso27001": "A.8.1",
             "evidence_hint": "密钥/令牌以明文硬编码在源码中",
             "fix": "改用环境变量或密钥管理服务读取"},
        ],
    },
}

# ════════════════════════════════════════════════════════════════════
# 2. 规则精度测试片段(每条规则 planted/clean 一对,落盘 snippets/)
# ════════════════════════════════════════════════════════════════════

SNIPPETS = {
    "CA-JAVA-SQLI-001": (
        'String sql = "SELECT * FROM users WHERE id = " + uid;',
        'PreparedStatement ps = con.prepareStatement("SELECT * FROM users WHERE id = ?");'),
    "CA-JAVA-CRYPTO-002": (
        'return Cipher.getInstance("DES/ECB/PKCS5Padding");',
        'return Cipher.getInstance("AES/GCM/NoPadding");'),
    "CA-JAVA-LOG-003": (
        'LOGGER.info("user login pwd=" + password);',
        'LOGGER.info("user login ok, userId=" + userId);'),
    "CA-CS-SQLI-001": (
        'var cmd = new SqlCommand("SELECT * FROM Orders WHERE Cust = " + custId, conn);',
        'var cmd = new SqlCommand("SELECT * FROM Orders WHERE Cust = @id", conn);'),
    "CA-CS-CRYPTO-001": (
        'var h = MD5.Create();',
        'var h = SHA256.Create();'),
    "CA-CS-DESER-002": (
        'var fmt = new BinaryFormatter();',
        'var fmt = new System.Runtime.Serialization.Json.DataContractJsonSerializer(typeof(object));'),
    "CA-CS-EXPOSE-003": (
        'catch (Exception ex) { return ex.ToString(); }',
        'catch (Exception ex) { return "操作失败"; }'),
    "CA-PHP-SQLI-001": (
        '$sql = "SELECT * FROM users WHERE name = " . $_POST["n"];',
        '$stmt = $conn->prepare("SELECT * FROM users WHERE name = ?");'),
    "CA-PHP-XSS-001": (
        'echo "<div>" . $_GET["q"] . "</div>";',
        'echo "<div>" . htmlspecialchars($_GET["q"], ENT_QUOTES, "UTF-8") . "</div>";'),
    "CA-PHP-INCLUDE-002": (
        'include($_GET["page"] . ".php");',
        'include __DIR__ . "/views/home.php";'),
    "CA-PHP-CODE-003": (
        '@eval($_GET["code"]);',
        'if (password_verify($_POST["password"], $row["hash"])) { }'),
    "CA-PHP-DESER-004": (
        '$obj = unserialize($_COOKIE["state"]);',
        '$obj = json_decode($_COOKIE["state"], true);'),
    "CA-PY-EVAL-001": (
        'result = eval(request.args.get("expr"))',
        'result = ast.literal_eval(request.args.get("expr"))'),
    "CA-PY-CMD-002": (
        'subprocess.run(cmd, shell=True)',
        'subprocess.run(["ls", "-l"], check=True)'),
    "CA-PY-YAML-003": (
        'cfg = yaml.load(open("app.yaml").read())',
        'cfg = yaml.safe_load(text)'),
    "CA-PY-SECRET-004": (
        'API_KEY = "ak_live_9f3c81d2e4b7"',
        'API_KEY = os.environ.get("API_KEY", "")'),
}

# ════════════════════════════════════════════════════════════════════
# 3. 4 个样本项目的源码(植入漏洞只出现在场景相关文件中)
# ════════════════════════════════════════════════════════════════════

SOURCE_FILES = {
    "PJ-A-001": {
        "src/core/UserService.java": '''package com.techmgmt.user;

import java.sql.*;
import javax.crypto.Cipher;

public class UserService {
    private Connection con;

    // 安全实现:参数化查询(对照)
    public String findByIdSafe(String uid) throws Exception {
        PreparedStatement ps = con.prepareStatement("SELECT * FROM users WHERE id = ?");
        ps.setString(1, uid);
        return ps.executeQuery().toString();
    }

    // 隐患:SQL 字符串拼接(CA-JAVA-SQLI-001)
    public String findByIdLegacy(String uid) throws Exception {
        Statement st = con.createStatement();
        String sql = "SELECT * FROM users WHERE id = " + uid;
        return st.executeQuery(sql).toString();
    }

    // 安全实现:AES-GCM 强加密(对照)
    private Cipher strongCipher() throws Exception {
        return Cipher.getInstance("AES/GCM/NoPadding");
    }

    // 隐患:DES/ECB 弱加密(CA-JAVA-CRYPTO-002)
    private Cipher weakCipher() throws Exception {
        return Cipher.getInstance("DES/ECB/PKCS5Padding");
    }
}
''',
        "src/core/AuditLogger.java": '''package com.techmgmt.audit;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

public class AuditLogger {
    private static final Logger LOGGER = LoggerFactory.getLogger(AuditLogger.class);

    // 安全实现:不记录敏感字段(对照)
    public void logLoginOk(String userId) {
        LOGGER.info("user login ok, userId=" + userId);
    }

    // 隐患:日志记录明文口令(CA-JAVA-LOG-003)
    public void logLoginLegacy(String password) {
        LOGGER.info("user login pwd=" + password);
    }
}
''',
    },
    "PJ-B-002": {
        "src/api/OrderService.cs": '''using System.Data.SqlClient;
using System.Security.Cryptography;

public class OrderService
{
    private SqlConnection conn;

    // 安全实现:参数化查询(对照)
    public object GetOrderSafe(string custId)
    {
        using (var cmd = new SqlCommand("SELECT * FROM Orders WHERE Cust = @id", conn))
        {
            cmd.Parameters.AddWithValue("@id", custId);
            return cmd.ExecuteScalar();
        }
    }

    // 安全实现:SHA-256(对照)
    public byte[] HashSafe(byte[] data)
    {
        var h = SHA256.Create();
        return h.ComputeHash(data);
    }

    // 隐患:MD5 弱哈希(CA-CS-CRYPTO-001)
    public byte[] HashLegacy(byte[] data)
    {
        var h = MD5.Create();
        return h.ComputeHash(data);
    }
}
''',
        "src/api/ReportController.cs": '''using System;

public class ReportController
{
    // 安全实现:JSON 序列化(对照)
    public void LoadSafe(System.IO.Stream stream)
    {
        var fmt = new System.Runtime.Serialization.Json.DataContractJsonSerializer(typeof(object));
    }

    // 隐患:BinaryFormatter 反序列化(CA-CS-DESER-002)
    public void LoadLegacy(System.IO.Stream stream)
    {
        var fmt = new BinaryFormatter();
        fmt.Deserialize(stream);
    }

    private string Render() { return "report"; }

    // 安全实现:统一错误处理(对照)
    public string RenderSafe()
    {
        try { return Render(); }
        catch (Exception ex) { return "操作失败"; }
    }

    // 隐患:异常堆栈直接返前端(CA-CS-EXPOSE-003)
    public string RenderLegacy()
    {
        try { return Render(); }
        catch (Exception ex) { return ex.ToString(); }
    }
}
''',
        "src/batch/BatchJob.cs": '''using System;
using System.Security.Cryptography;

public class BatchJob
{
    // 批量任务模块:仅做日志与哈希校验(安全实现对照)
    public string DigestFile(byte[] data)
    {
        var h = SHA256.Create();
        return Convert.ToHexString(h.ComputeHash(data));
    }

    public string Name() { return "batch"; }
}
''',
    },
    "PJ-C-003": {
        "src/web/user.php": '''<?php
// 用户模块(全部安全实现,供规则精度对照)
$conn = new mysqli("localhost", "user", "pass", "db");

// 安全实现:预处理语句(对照)
$stmt = $conn->prepare("SELECT * FROM users WHERE name = ?");
$stmt->bind_param("s", $_POST["name"]);
$stmt->execute();

// 安全实现:口令校验(对照)
$row = ["hash" => ""];
if (password_verify($_POST["password"], $row["hash"])) { }

// 安全实现:JSON 反序列化(对照)
$obj = json_decode($_COOKIE["state"], true);
''',
        "src/web/view.php": '''<?php
// 视图模块:植入 XSS 与动态 include 两处隐患

// 安全实现:输出编码(对照)
echo "<div>" . htmlspecialchars($_GET["q"], ENT_QUOTES, "UTF-8") . "</div>";

// 隐患:反射型 XSS(CA-PHP-XSS-001)
echo "<div>" . $_GET["q"] . "</div>";

// 安全实现:固定视图路径(对照)
include __DIR__ . "/views/home.php";

// 隐患:动态 include(CA-PHP-INCLUDE-002)
include($_GET["page"] . ".php");
''',
    },
    "PJ-D-004": {
        "src/pipeline/data_pipeline.py": '''import ast
import os
import subprocess

import yaml

# 安全实现:literal_eval(对照)
def safe_expr(expr):
    return ast.literal_eval(expr)

# 安全实现:无 shell 调用(对照)
def safe_list():
    return subprocess.run(["ls", "-l"], check=True)

# 安全实现:SafeLoader(对照)
def safe_yaml(text):
    return yaml.safe_load(text)

# 安全实现:环境变量读取(对照)
API_KEY = os.environ.get("API_KEY", "")
''',
    },
}

# ════════════════════════════════════════════════════════════════════
# 4. 资产元数据(asset.json)
# ════════════════════════════════════════════════════════════════════

ASSET_META = {
    "PJ-A-001": {
        "name": "科技管理系统", "owner": "科技管理部",
        "deploy_scope": "full", "required_modules": [],
        "version": "1.4.0",
        "modules": [{"asset_id": "PJ-A-001", "module": "core",
                     "language": "java", "local_dir": "src/core",
                     "artifact": "artifacts/core-1.4.0.jar",
                     "version": "1.4.0",
                     "base_image": "openjdk:17.0.9-slim"}],
    },
    "PJ-B-002": {
        "name": "工程技术一体化系统", "owner": "工程管理部",
        "deploy_scope": "partial", "required_modules": ["api", "web"],
        "version": "3.2.0",
        "modules": [{"asset_id": "PJ-B-002", "module": "api",
                     "language": "csharp", "local_dir": "src/api",
                     "artifact": "artifacts/api-3.2.0.dll",
                     "version": "3.2.0",
                     "base_image": "mcr.microsoft.com/dotnet/aspnet:8.0"},
                    {"asset_id": "PJ-B-002", "module": "web",
                     "language": "csharp", "local_dir": "src/web",
                     "artifact": "artifacts/web-3.2.0.zip",
                     "version": "3.2.0",
                     "base_image": "mcr.microsoft.com/dotnet/aspnet:8.0"},
                    {"asset_id": "PJ-B-002", "module": "batch",
                     "language": "csharp", "local_dir": "src/batch",
                     "artifact": "", "version": "3.2.0",
                     "base_image": ""}],
    },
    "PJ-C-003": {
        "name": "二级单位质量管理系统", "owner": "质量检验中心",
        "deploy_scope": "full", "required_modules": [],
        "version": "2.1.0",
        "modules": [{"asset_id": "PJ-C-003", "module": "quality-web",
                     "language": "php", "local_dir": "src/web",
                     "artifact": "artifacts/quality-web-2.1.0.zip",
                     "version": "2.1.0",
                     "base_image": "php:8.2-fpm-alpine"}],
    },
    "PJ-D-004": {
        "name": "数据分析工具集", "owner": "数据中心",
        "deploy_scope": "full", "required_modules": [],
        "version": "0.9.1",
        "modules": [{"asset_id": "PJ-D-004", "module": "pipeline",
                     "language": "python", "local_dir": "src/pipeline",
                     "artifact": "artifacts/pipeline-0.9.1.whl",
                     "version": "0.9.1",
                     "base_image": "python:3.11-slim"}],
    },
}

# 制品注册表补充信息(签名 CN 等);篡改项目:PJ-C-003
ARTIFACT_CN = {
    ("PJ-A-001", "core"): "tech-mgmt",
    ("PJ-B-002", "api"): "eng-integration",
    ("PJ-B-002", "web"): "eng-integration",
    ("PJ-C-003", "quality-web"): "quality-mgmt",
    ("PJ-D-004", "pipeline"): "data-tools",
}
TAMPERED_PROJECTS = {"PJ-C-003"}   # 制品被篡改:注册表存原哈希,文件写变异字节

# ════════════════════════════════════════════════════════════════════
# 5. 运行环境配置(envs/)与审查基线
# ════════════════════════════════════════════════════════════════════

ENV_CONFIGS = {
    "PJ-A-001": {
        "env": "prod",
        "values": {"app": {"debug_mode": True},
                   "cloud": {"credentials_hardcoded": True},
                   "host": {"base_image_outdated": True}},
        "checks": [
            {"check_id": "CA-ENV-DEBUG-001", "name": "生产环境开启调试模式",
             "severity": "critical", "likelihood": 1.0,
             "metric": "app.debug_mode", "target": True,
             "fix": "生产环境关闭 debug_mode"},
            {"check_id": "CA-ENV-CRED-002", "name": "运行环境硬编码云平台凭证",
             "severity": "high", "likelihood": 1.0,
             "metric": "cloud.credentials_hardcoded", "target": True,
             "fix": "改用密钥管理服务注入凭证"},
            {"check_id": "CA-ENV-IMAGE-003", "name": "基础镜像过期(含已知 CVE)",
             "severity": "high", "likelihood": 0.8,
             "metric": "host.base_image_outdated", "target": True,
             "fix": "升级基础镜像并纳入镜像扫描"},
        ],
    },
    "PJ-B-002": {
        "env": "prod",
        "values": {"tls": {"min_version": "1.0"}},
        "checks": [
            {"check_id": "CA-ENV-TLS-001", "name": "允许 TLS 1.0 协议",
             "severity": "low", "likelihood": 0.5,
             "metric": "tls.min_version", "target": "1.0",
             "fix": "禁用 TLS 1.0/1.1,最低 TLS 1.2"},
        ],
    },
    "PJ-C-003": {
        "env": "prod",
        "values": {"debug": {"phpinfo_endpoint": True},
                   "session": {"cookie_secure": False}},
        "checks": [
            {"check_id": "CA-ENV-EXPOSE-001", "name": "生产暴露 phpinfo/调试端点",
             "severity": "medium", "likelihood": 0.7,
             "metric": "debug.phpinfo_endpoint", "target": True,
             "fix": "移除 phpinfo/调试端点"},
            {"check_id": "CA-ENV-COOKIE-002", "name": "Session Cookie 未加固",
             "severity": "low", "likelihood": 0.5,
             "metric": "session.cookie_secure", "target": False,
             "fix": "开启 cookie_secure 与 httponly"},
        ],
    },
    "PJ-D-004": {"env": "prod", "values": {}, "checks": []},
}

# 各项目植入的漏洞规则(severity/likelihood 与 RULES 一致,期望值由此推导)
EXPECTED_VULNS = {
    "PJ-A-001": [("CA-JAVA-SQLI-001", "high", 0.9),
                 ("CA-JAVA-CRYPTO-002", "medium", 0.7),
                 ("CA-JAVA-LOG-003", "medium", 0.6)],
    "PJ-B-002": [("CA-CS-CRYPTO-001", "high", 0.8),
                 ("CA-CS-DESER-002", "high", 0.7),
                 ("CA-CS-EXPOSE-003", "medium", 0.5)],
    "PJ-C-003": [("CA-PHP-XSS-001", "high", 0.8),
                 ("CA-PHP-INCLUDE-002", "medium", 0.5)],
    "PJ-D-004": [],
}

# ════════════════════════════════════════════════════════════════════
# 6. 生成逻辑
# ════════════════════════════════════════════════════════════════════

def artifact_bytes(asset_id: str, module: str, version: str) -> bytes:
    """制品字节:确定性内容(幂等生成,哈希可复现)。"""
    return f"{asset_id}:{module}:{version}:artifact\n".encode() * 64


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _signature(cn: str, version: str, digest: str) -> str:
    return f"sig:CN={cn}:v{version}:{digest[:16]}"


def _level_inline(score: float, criticals: int) -> str:
    """安全等级独立重实现(与 Metrics.classify_level 交叉验证用)。"""
    if score <= 0:
        base = "A"
    elif score < 10:
        base = "B"
    elif score < 25:
        base = "C"
    elif score < 45:
        base = "D"
    else:
        base = "E"
    rank = {"A": 4, "B": 3, "C": 2, "D": 1, "E": 0}
    reverse = {4: "A", 3: "B", 2: "C", 1: "D", 0: "E"}
    cap = {0: None, 1: 2, 2: 1}.get(criticals, 0)
    if cap is None:
        return base
    return reverse[min(rank[base], cap)]


def independent_expected() -> dict:
    """内联独立重实现:静态+环境+部署子项 → 总分/等级/主风险(交叉验证基准)。"""
    static_w = {"critical": 10.0, "high": 7.0, "medium": 4.0, "low": 1.0}
    env_w = {"critical": 8.0, "high": 5.0, "medium": 2.0, "low": 1.0}
    projects = {}
    for pid, vulns in EXPECTED_VULNS.items():
        static = round(sum(static_w[sev] * like for _, sev, like in vulns), 4)
        env = round(sum(env_w[c["severity"]] * c["likelihood"]
                        for c in ENV_CONFIGS[pid]["checks"]), 4)
        deploy = 15.0 if pid in TAMPERED_PROJECTS else 0.0
        score = round(static + env + deploy, 4)
        criticals = sum(1 for _, sev, _ in vulns if sev == "critical") \
            + sum(1 for c in ENV_CONFIGS[pid]["checks"]
                  if c["severity"] == "critical") \
            + (1 if pid in TAMPERED_PROJECTS else 0)
        subtotals = {"static_vuln": static, "env_config": env,
                     "deploy_integrity": deploy}
        vals = {k: subtotals.get(k, 0.0)
                for k in ("static_vuln", "env_config", "deploy_integrity")}
        if all(v <= 0 for v in vals.values()):
            main, main_score = "none", None
        else:
            main = max(vals, key=lambda k: vals[k])
            main_score = vals[main]
        projects[pid] = {
            "static_vuln": static, "env_config": env,
            "deploy_integrity": deploy, "risk_score": score,
            "critical_count": criticals,
            "level": _level_inline(score, criticals),
            "main_risk": main, "main_risk_score": main_score,
            "expected_vuln_ids": [v[0] for v in vulns],
        }
    return projects


def _write_json(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8")


def _write_text(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def generate() -> dict:
    """生成全部样本数据并自校验,返回期望值字典。"""
    # ── 规则库 + 片段 ──
    for language, payload in RULES.items():
        _write_json(DATA_DIR / "rules" / f"{language}.json",
                    {"schema_version": CodeAnalysisConfigs.SCHEMA_VERSION,
                     "language": language, **payload})
    for rule_id, (planted, clean) in SNIPPETS.items():
        _write_text(DATA_DIR / "snippets" / f"{rule_id}_planted.txt",
                    planted + "\n")
        _write_text(DATA_DIR / "snippets" / f"{rule_id}_clean.txt",
                    clean + "\n")

    # ── 资产:源码 + asset.json + 制品 ──
    registry_entries = []
    for pid, meta in ASSET_META.items():
        for rel_path, content in SOURCE_FILES[pid].items():
            _write_text(DATA_DIR / "assets" / pid / rel_path, content)
        _write_json(DATA_DIR / "assets" / pid / "asset.json",
                    {"schema_version": CodeAnalysisConfigs.SCHEMA_VERSION,
                     "project_id": pid, "name": meta["name"],
                     "owner": meta["owner"],
                     "deploy_scope": meta["deploy_scope"],
                     "required_modules": meta["required_modules"],
                     "version": meta["version"], "ingest_date": INGEST_DATE,
                     "assets": meta["modules"]})
        for module in meta["modules"]:
            artifact = module.get("artifact")
            if not artifact:
                continue
            original = artifact_bytes(pid, module["module"], module["version"])
            digest = _sha256_bytes(original)
            cn = ARTIFACT_CN[(pid, module["module"])]
            registry_entries.append({
                "asset_id": pid, "module": module["module"],
                "version": module["version"], "artifact": artifact,
                "sha256": digest,
                "signature": _signature(cn, module["version"], digest),
                "cn": cn, "base_image": module["base_image"],
            })
            # 制品落盘:C 项目写变异字节(模拟构建产物被篡改)
            if pid in TAMPERED_PROJECTS:
                _write_bytes(DATA_DIR / "assets" / pid / artifact,
                             original + b"tampered\n")
            else:
                _write_bytes(DATA_DIR / "assets" / pid / artifact, original)

    # ── 云平台制品注册表 ──
    _write_json(DATA_DIR / "cloud" / "registry.json",
                {"schema_version": CodeAnalysisConfigs.SCHEMA_VERSION,
                 "platform": "公司云平台(模拟)",
                 "registry_version": "2026.09",
                 "entries": registry_entries})

    # ── 运行环境配置 ──
    for pid, payload in ENV_CONFIGS.items():
        _write_json(DATA_DIR / "envs" / f"{pid}.json",
                    {"schema_version": CodeAnalysisConfigs.SCHEMA_VERSION,
                     "project_id": pid, **payload})

    # ── 期望值(独立重实现) ──
    expected = independent_expected()
    _write_json(DATA_DIR / "expected_assessments.json",
                {"schema_version": CodeAnalysisConfigs.SCHEMA_VERSION,
                 "projects": expected})

    _assert_invariants(expected)
    return expected


def _write_bytes(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _assert_invariants(expected: dict):
    """生成后自校验:扫描结果/风险模型/篡改持久性/片段精度。"""
    scanner = Scanner(CodeAnalysisConfigs.RULES_DIR)
    # ① 扫描结果与期望漏洞清单一致(无多无漏)
    for pid, exp in expected.items():
        asset = AssetSource.normalize(
            json.loads((DATA_DIR / "assets" / pid / "asset.json")
                       .read_text(encoding="utf-8")), pid)
        findings = scanner.scan_project(asset, DATA_DIR / "assets" / pid)
        got_ids = sorted(f["rule_id"] for f in findings)
        want_ids = sorted(exp["expected_vuln_ids"])
        assert got_ids == want_ids, \
            f"{pid} 扫描结果与期望不符: {got_ids} != {want_ids}"
    # ② 风险模型与独立重实现一致(静态+环境+部署)
    from CodeAnalysis.DataHub import EnvSource
    from CodeAnalysis.Deployer import Deployer
    from CodeAnalysis.DataHub import CloudPlatformSource
    env_source = EnvSource(DATA_DIR)
    cloud = CloudPlatformSource(DATA_DIR)
    deployer = Deployer(cloud, cloud.registry_map(),
                        assets_dir=DATA_DIR / "assets")
    for pid, exp in expected.items():
        asset = AssetSource.normalize(
            json.loads((DATA_DIR / "assets" / pid / "asset.json")
                       .read_text(encoding="utf-8")), pid)
        findings = scanner.scan_project(asset, DATA_DIR / "assets" / pid)
        issues = env_source.review(pid)["issues"]
        scan = {"findings": findings}
        # 部署风险事件:预演部署,取被拦截时的风险事件
        pre = overall_assessment(findings, issues, [])
        record = deployer.deploy(asset, pre)
        deploy_events = record.get("risk_events") or []
        got = overall_assessment(findings, issues, deploy_events)
        assert abs(got["risk_score"] - exp["risk_score"]) < 1e-9, \
            f"{pid} 总分不符: {got['risk_score']} != {exp['risk_score']}"
        assert got["security_level"] == exp["level"], \
            f"{pid} 等级不符: {got['security_level']} != {exp['level']}"
        assert got["main_risk"] == exp["main_risk"], \
            f"{pid} 主风险不符: {got['main_risk']} != {exp['main_risk']}"
    # ③ 篡改制品:注册表哈希与制品文件哈希持续不匹配
    registry = {tuple(e[k] for k in ("asset_id", "module")): e
                for e in cloud.registry_entries()}
    for (asset_id, module), entry in registry.items():
        if asset_id not in TAMPERED_PROJECTS:
            continue
        import hashlib as _hashlib
        path = (DATA_DIR / "assets" / asset_id / entry["artifact"])
        actual = _hashlib.sha256(path.read_bytes()).hexdigest()
        assert actual != entry["sha256"], f"{asset_id} 制品篡改断言失效"
    # ④ 片段精度:planted 命中、clean 不命中
    for rule_id, (planted, clean) in SNIPPETS.items():
        rule = _find_rule(rule_id)
        planted_hits = scanner.scan_file(
            DATA_DIR / "snippets" / f"{rule_id}_planted.txt", [rule],
            rel_path=f"{rule_id}_planted.txt")
        clean_hits = scanner.scan_file(
            DATA_DIR / "snippets" / f"{rule_id}_clean.txt", [rule],
            rel_path=f"{rule_id}_clean.txt")
        assert len(planted_hits) == 1, f"{rule_id} planted 应命中 1 次"
        assert len(clean_hits) == 0, f"{rule_id} clean 误报 {len(clean_hits)} 次"


def _find_rule(rule_id: str) -> dict:
    for payload in RULES.values():
        for rule in payload["rules"]:
            if rule["rule_id"] == rule_id:
                return rule
    raise KeyError(f"未知规则: {rule_id}")


if __name__ == "__main__":
    result = generate()
    print(f"样本数据已生成: {len(result)} 个项目,"
          f"{len(SNIPPETS)} 条规则片段对,自校验通过。")
