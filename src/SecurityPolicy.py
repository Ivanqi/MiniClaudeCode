"""安全策略:敏感路径、控制面、secret 脱敏与 bash 命令分析。

这里是纯函数层,不做 I/O、不弹确认:
- classify_path 判断文件工具能否读写某个路径;
- redact 在工具结果/审计/消息落盘前替换密钥;
- child_env 给 bash 子进程一个不含 API Key 的环境;
- analyze_bash 用 shlex 解析命令,识别危险/网络/破坏性操作。

注意:命令黑名单不是安全边界,真正的边界是 Sandbox(bwrap)。
"""
import fnmatch
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

ALLOW = "allow"
DENY = "deny"
CONFIRM = "confirm"


@dataclass(frozen=True)
class PathDecision:
    verdict: str
    reason: str = ""
    rel_path: str = ""


@dataclass(frozen=True)
class BashDecision:
    verdict: str
    reason: str = ""
    read_only: bool = False
    needs_network: bool = False


# ── 路径模式 ──

SENSITIVE_BASENAMES = (
    "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.keystore",
    "id_rsa", "id_rsa.*", "id_ed25519", "id_ed25519.*",
    "id_ecdsa", "id_ecdsa.*", "id_dsa", "id_dsa.*",
    "credentials", "credentials.*", "secrets", "secrets.*",
    ".netrc", ".git-credentials",
)
SENSITIVE_PATHS = (".ssh/*", ".ssh/**", ".git/config", ".git/hooks",
                   ".git/hooks/**")
ENV_TEMPLATE_ALLOW = {".env.example", ".env.sample", ".env.template"}

CONTROL_PLANE_PATTERNS = (
    "main.py",
    "src/AgentLoop.py",
    "src/BasicOper.py",
    "src/HookSystem.py",
    "src/Configs.py",
    "src/ToolRegistry.py",
    "src/ToolPolicy.py",
    "src/ToolDispatcher.py",
    "src/SecurityPolicy.py",
    "src/ApprovalGate.py",
    "src/AuditLog.py",
    "src/Sandbox.py",
    "src/FileLock.py",
    "src/SubagentSystem.py",
    "src/TeammateSystem.py",
    "src/BackgroundTaskSystem.py",
    "src/MemoryStore.py",
    "src/TranscriptStore.py",
    "src/Retention.py",
    "src/Memories.py",
    "src/SkillSystem.py",
    "src/Roles.py",
    "src/TeamMetrics.py",
    "src/LeadMonitor.py",
    "src/ReviewSystem.py",
    "src/IntegrationSystem.py",
    "Catering/Guardrails.py",
    "Catering/AnomalyAgent.py",
    "Catering/Cases.py",
    "Catering/data/cases/**",
    "skills/**",
    "roles.json",
    ".claude/**",
)

PROTECTED_STATE_PATTERNS = (
    ".audit/**",
    ".backups/**",
    ".tasks/**",
    ".mailboxes/**",
    ".scheduled_tasks.json",
    ".transcripts/**",
    ".task_outputs/**",
    ".training_data/**",
    ".memory/**",
    ".worktrees/**",
    ".team/**",
    ".git/**",
)

MAX_PATH_CHARS = 4096


def _matches(rel: str, patterns) -> bool:
    return any(fnmatch.fnmatch(rel, pattern) for pattern in patterns)


def _is_sensitive_basename(name: str) -> bool:
    if name in ENV_TEMPLATE_ALLOW:
        return False
    if name == ".env" or name.startswith(".env."):
        return True
    return any(fnmatch.fnmatch(name, pattern)
               for pattern in SENSITIVE_BASENAMES)


def _is_sensitive_rel(rel: str) -> bool:
    parts = PurePosixPath(rel).parts
    if parts and _is_sensitive_basename(parts[-1]):
        return True
    return _matches(rel, SENSITIVE_PATHS)


def classify_path(raw: str, root: Path, write: bool = False) -> PathDecision:
    """判断文件路径是否可读/可写。

    返回 DENY / CONFIRM / ALLOW;CONFIRM 只在写控制面时出现。
    """
    if not isinstance(raw, str) or not raw.strip():
        return PathDecision(DENY, "empty path")
    if len(raw) > MAX_PATH_CHARS:
        return PathDecision(DENY, "path too long")
    if "\x00" in raw:
        return PathDecision(DENY, "path contains NUL byte")
    try:
        base = Path(root).resolve()
        path = Path(raw)
        if not path.is_absolute():
            path = base / path
        resolved = path.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        return PathDecision(DENY, f"cannot resolve path: {exc}")
    if not resolved.is_relative_to(base):
        return PathDecision(DENY, f"path escapes workspace: {raw}")
    rel = resolved.relative_to(base).as_posix()
    if write and _matches(rel, PROTECTED_STATE_PATTERNS):
        return PathDecision(DENY, f"protected state path: {rel}", rel)
    if _is_sensitive_rel(rel):
        return PathDecision(DENY, f"sensitive file: {rel}", rel)
    if _matches(rel, CONTROL_PLANE_PATTERNS):
        if write:
            return PathDecision(
                CONFIRM, f"control-plane write requires confirmation: {rel}", rel)
        return PathDecision(ALLOW, "control-plane read", rel)
    return PathDecision(ALLOW, "", rel)


# ── secret 脱敏 ──

_REDACT_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]+"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(
        r"(?i)\b([A-Z0-9_]*(?:API_KEY|ACCESS_KEY|SECRET_KEY|AUTH_TOKEN|"
        r"ACCESS_TOKEN|CLIENT_SECRET|PASSWORD|PASSWD))\s*[:=]\s*"
        r"(['\"]?)([^\s'\"]{6,})\2"),
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?"
        r"-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
    re.compile(r"(?i)(https?://[^/\s:@]+:)[^@\s/]+@"),
)


def redact(text) -> str:
    """替换常见密钥格式;非字符串先转 str。"""
    if text is None:
        return ""
    text = text if isinstance(text, str) else str(text)
    text = _REDACT_PATTERNS[0].sub("[REDACTED]", text)
    text = _REDACT_PATTERNS[1].sub("Bearer [REDACTED]", text)
    text = _REDACT_PATTERNS[2].sub("[REDACTED]", text)
    text = _REDACT_PATTERNS[3].sub("[REDACTED]", text)
    text = _REDACT_PATTERNS[4].sub(
        lambda m: f"{m.group(1)}=[REDACTED]", text)
    text = _REDACT_PATTERNS[5].sub("[REDACTED PRIVATE KEY]", text)
    text = _REDACT_PATTERNS[6].sub(r"\1[REDACTED]@", text)
    return text


def redact_obj(value):
    """递归脱敏 dict/list 中的字符串(转录、轨迹、审计用)。"""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [redact_obj(item) for item in value]
    if isinstance(value, dict):
        return {key: redact_obj(item) for key, item in value.items()}
    return value


# ── 子进程环境 ──

_SAFE_ENV_KEYS = {
    "PATH", "HOME", "LANG", "TERM", "TMPDIR", "USER", "LOGNAME", "SHELL",
    "PWD", "OLDPWD", "VIRTUAL_ENV", "CONDA_PREFIX", "PYTHONPATH",
    "LD_LIBRARY_PATH", "TZ", "COLUMNS", "LINES",
}
_SECRET_ENV_RE = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH)",
                            re.IGNORECASE)


def child_env(base: dict | None = None) -> dict:
    """只保留非敏感环境变量,避免 API Key 泄漏给 bash 子进程。"""
    env = {}
    for key, value in (os.environ if base is None else base).items():
        if key in _SAFE_ENV_KEYS or key.startswith("LC_"):
            env[key] = value
        elif _SECRET_ENV_RE.search(key):
            continue
    return env


# ── bash 分析 ──

_HARD_DENY_COMMANDS = {
    "sudo", "su", "doas", "shutdown", "reboot", "poweroff", "halt",
    "fdisk", "parted", "mount", "umount", "iptables", "nft", "systemctl",
    "service", "chroot", "setcap",
}
_NETWORK_COMMANDS = {
    "curl", "wget", "nc", "netcat", "telnet", "ssh", "scp", "sftp",
    "rsync", "ftp", "socat", "ping", "nslookup", "dig", "host",
}
_PACKAGE_COMMANDS = {
    "apt", "apt-get", "dnf", "yum", "apk", "pacman", "brew", "pip",
    "pip3", "npm", "pnpm", "yarn", "gem", "cargo", "go",
}
_DESTRUCTIVE_COMMANDS = {
    "rm", "mv", "cp", "tee", "chmod", "chown", "truncate", "shred",
    "dd", "kill", "pkill", "killall",
}
_READ_ONLY_COMMANDS = {
    "ls", "pwd", "cat", "head", "tail", "grep", "rg", "wc", "file",
    "stat", "du", "df", "tree", "sort", "uniq", "cut", "tr", "basename",
    "dirname", "realpath", "readlink", "which", "type", "echo", "printf",
    "date", "printenv",
}
_OPERATORS = {";", "&&", "||", "|", "&", "\n"}
_REDIRECTS = {">", ">>", "<", ">&", "&>"}
_WRITE_FLAGS = {"-i", "--in-place", "-delete", "-exec", "-execdir", "-ok"}


def _split_segments(tokens: list[str]) -> list[list[str]]:
    segments, current = [], []
    for token in tokens:
        if token in _OPERATORS:
            if current:
                segments.append(current)
                current = []
        else:
            current.append(token)
    if current:
        segments.append(current)
    return segments


def _command_name(token: str) -> str:
    return PurePosixPath(token).name


def _token_path(token: str, root: Path) -> Path | None:
    """把 token 解释为路径;明显不是路径(URL/选项)时返回 None。"""
    if not token or token.startswith("-") or token in _OPERATORS:
        return None
    if "://" in token or token.startswith(("$", "`", "@")):
        return None
    try:
        path = Path(os.path.expanduser(token)) if token.startswith("~") \
            else Path(token)
        if not path.is_absolute():
            path = root / path
        return path.resolve()
    except (OSError, RuntimeError, ValueError):
        return None


_FILE_COMMANDS = {
    "cat", "head", "tail", "less", "more", "grep", "rg", "sed", "awk",
    "cp", "mv", "rm", "chmod", "chown", "git", "python", "python3",
    "node", "ruby", "perl", "php", "tar", "zip", "unzip", "curl", "wget",
    "source",
}


def _looks_pathlike(token: str) -> bool:
    return "/" in token or token.startswith(".") or bool(PurePosixPath(token).suffix)


def _contains_sensitive_token(tokens: list[str], command: str) -> str | None:
    for token in tokens:
        if token.startswith("-") or token in _REDIRECTS:
            continue
        name = PurePosixPath(token).name
        rel = token.lstrip("./")
        if name in ENV_TEMPLATE_ALLOW:
            continue
        # 路径形态的 token 一律检查;裸文件名只在文件类命令里检查,
        # 避免把 `echo secrets` 这类普通文本误判为读密钥。
        if _looks_pathlike(token):
            if _is_sensitive_basename(name) or _is_sensitive_rel(rel):
                return token
        elif command in _FILE_COMMANDS and _is_sensitive_basename(name):
            return token
    return None


def _git_subcommand(args: list[str]) -> str:
    """跳过 git 全局选项,返回子命令。"""
    index = 0
    while index < len(args):
        token = args[index]
        if token in ("-C", "-c", "--git-dir", "--work-tree"):
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        return token
    return ""


def _analyze_segment(tokens: list[str], root: Path,
                     restrict_paths: bool) -> BashDecision:
    if not tokens:
        return BashDecision(ALLOW, "", read_only=True)
    if any(token in {"(", ")"} for token in tokens):
        return BashDecision(CONFIRM, "subshell is not analyzed")

    # 跳过环境变量赋值与前缀包装器
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", token):
            index += 1
            continue
        if _command_name(token) in {"env", "nohup", "command", "exec", "time"}:
            index += 1
            continue
        if _command_name(token) == "timeout":
            index += 2  # timeout <seconds> cmd
            continue
        break
    if index >= len(tokens):
        return BashDecision(ALLOW, "", read_only=True)
    command = _command_name(tokens[index])
    args = tokens[index + 1:]

    # 嵌套 shell / 解释器 -c:递归分析
    if command in {"sh", "bash", "zsh", "dash", "ksh"}:
        if "-c" in args:
            pos = args.index("-c")
            nested = " ".join(args[pos + 1:])
            return analyze_bash(nested, root, restrict_paths=restrict_paths)
        return BashDecision(CONFIRM, "shell reading stdin is not analyzed")
    if command == "xargs":
        return _analyze_segment(args, root, restrict_paths)
    if command in {"python", "python3", "node", "ruby", "perl", "php"} \
            and any(flag in args for flag in ("-c", "-e", "-r")):
        return BashDecision(CONFIRM, f"{command} inline code requires confirmation")

    if command in _HARD_DENY_COMMANDS or command.startswith("mkfs."):
        return BashDecision(DENY, f"privileged/destructive command: {command}")
    if re.search(r":\s*\(\s*\)\s*\{", " ".join(tokens)):
        return BashDecision(DENY, "fork bomb pattern")
    if command == "dd" and any(arg.startswith("of=/dev/") for arg in args):
        return BashDecision(DENY, "dd writing to a device")
    if "--no-preserve-root" in args:
        return BashDecision(DENY, "--no-preserve-root is not allowed")
    if command == "rm" and any(arg in {"/", "/*", "~", "~/*", "$HOME",
                                       "$HOME/*"} for arg in args):
        return BashDecision(DENY, "rm targeting root or home")

    sensitive = _contains_sensitive_token(tokens, command)
    if sensitive:
        return BashDecision(DENY, f"sensitive path argument: {sensitive}")

    if restrict_paths and any("$" in token for token in tokens):
        return BashDecision(
            DENY, "variable expansion is not allowed in read-only bash")
    paths = [p for p in (_token_path(token, root) for token in tokens)
             if p is not None]
    if restrict_paths:
        for path in paths:
            if not path.is_relative_to(root):
                return BashDecision(
                    DENY, f"read-only bash path escapes workspace: {path}")

    needs_network = False
    confirm_reasons = []

    if command in _NETWORK_COMMANDS:
        needs_network = True
        confirm_reasons.append(f"network command: {command}")
    if command in _PACKAGE_COMMANDS:
        if command in {"pip", "pip3", "npm", "pnpm", "yarn", "gem", "cargo", "go"} \
                and not any(arg in {"install", "add", "update", "upgrade"}
                            for arg in args):
            pass
        else:
            needs_network = True
            confirm_reasons.append(f"package command: {command}")
    if command == "git":
        sub = _git_subcommand(args)
        if sub in {"push", "pull", "fetch", "clone", "submodule", "remote"}:
            needs_network = True
            confirm_reasons.append(f"network git: {sub}")
        if sub in {"reset", "clean", "restore", "checkout"} and \
                any(arg in {"--hard", "-f", "-fd", "-fdx", "--force"}
                    for arg in args):
            confirm_reasons.append(f"destructive git: {sub}")
    if command in {"docker", "podman", "kubectl", "terraform", "aws", "gcloud", "az"}:
        confirm_reasons.append(f"infrastructure command: {command}")
    if command in _DESTRUCTIVE_COMMANDS:
        if command == "dd" and not any(arg.startswith("of=") for arg in args):
            pass
        else:
            confirm_reasons.append(f"destructive command: {command}")
    if command in {"sed", "perl", "awk"} and any(flag in args for flag in _WRITE_FLAGS):
        confirm_reasons.append(f"in-place edit: {command}")
    if command == "find" and any(flag in args for flag in _WRITE_FLAGS):
        confirm_reasons.append("find with -delete/-exec")
    if command in {"chmod", "chown"} and any(arg in {"/", "/*"} for arg in args):
        return BashDecision(DENY, f"{command} on root")

    # 重定向:敏感目标拒绝;写控制面/状态面确认;工作区外确认
    for pos, token in enumerate(tokens):
        if token not in _REDIRECTS:
            continue
        target = tokens[pos + 1] if pos + 1 < len(tokens) else ""
        if not target or target.isdigit():
            continue  # fd duplication like 2>&1
        decision = classify_path(target, root, write=True)
        if decision.verdict == DENY:
            return BashDecision(DENY, decision.reason)
        if decision.verdict == CONFIRM:
            confirm_reasons.append(decision.reason)

    for path in paths:
        if not path.is_relative_to(root):
            continue
        rel = path.relative_to(root).as_posix()
        if _matches(rel, CONTROL_PLANE_PATTERNS) and \
                any(token in _REDIRECTS for token in tokens):
            confirm_reasons.append(f"control-plane redirect: {rel}")

    if confirm_reasons:
        return BashDecision(CONFIRM, "; ".join(confirm_reasons),
                            needs_network=needs_network)

    read_only = command in _READ_ONLY_COMMANDS
    if command == "git":
        sub = _git_subcommand(args)
        read_only = sub in {"status", "diff", "log", "show", "branch",
                            "rev-parse", "ls-files", "blame"}
    if command in {"sed", "awk"}:
        read_only = not any(flag in args for flag in _WRITE_FLAGS)
    if command == "find":
        read_only = not any(flag in args for flag in _WRITE_FLAGS)
    if any(token in _REDIRECTS for token in tokens):
        read_only = False
    return BashDecision(ALLOW, "", read_only=read_only)


def _combine(results: list[BashDecision]) -> BashDecision:
    deny = next((r for r in results if r.verdict == DENY), None)
    if deny:
        return deny
    confirm = [r for r in results if r.verdict == CONFIRM]
    if confirm:
        return BashDecision(
            CONFIRM, "; ".join(dict.fromkeys(r.reason for r in confirm)),
            needs_network=any(r.needs_network for r in confirm))
    return BashDecision(
        ALLOW, "", read_only=all(r.read_only for r in results),
        needs_network=any(r.needs_network for r in results))


def analyze_bash(command: str, root: Path,
                 restrict_paths: bool = False) -> BashDecision:
    """分析 shell 命令;restrict_paths=True 时禁止访问工作区外路径。"""
    if not isinstance(command, str) or not command.strip():
        return BashDecision(ALLOW, "", read_only=True)
    if "\x00" in command:
        return BashDecision(DENY, "command contains NUL byte")
    if re.search(r"\$\(|`", command):
        return BashDecision(CONFIRM, "command substitution requires confirmation")
    lines = [line for line in command.splitlines() if line.strip()]
    if len(lines) > 1:
        return _combine([analyze_bash(line, root, restrict_paths=restrict_paths)
                         for line in lines])
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError as exc:
        return BashDecision(CONFIRM, f"cannot parse command: {exc}")
    segments = _split_segments(tokens)
    if not segments:
        return BashDecision(ALLOW, "", read_only=True)
    results = [_analyze_segment(segment, Path(root).resolve(), restrict_paths)
               for segment in segments]
    combined = _combine(results)
    if combined.verdict == ALLOW:
        has_pipe = any(token in {"|", "&", "&&", "||"} for token in tokens)
        # 管道本身可接受;后台 & 保守视为非只读
        if "&" in tokens:
            combined = BashDecision(ALLOW, "", read_only=False)
        elif has_pipe:
            combined = BashDecision(ALLOW, "",
                                    read_only=combined.read_only)
    return combined


def is_read_only_bash(command: str, root: Path) -> bool:
    decision = analyze_bash(command, root, restrict_paths=True)
    return decision.verdict == ALLOW and decision.read_only


# ── 名称/ID 校验 ──

_AGENT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_TASK_ID_RE = re.compile(r"^task_[A-Za-z0-9_]+$")


def validate_agent_name(name: str) -> str | None:
    """返回错误描述或 None;禁止路径分隔符与 ..。

    注意:mailbox 的 "lead" 是合法收件人,保留名检查放在
    TeammateSystem.spawn 里,而不是这个通用校验器。
    """
    if not isinstance(name, str) or not name:
        return "agent name cannot be empty"
    if name in (".", "..") or not _AGENT_NAME_RE.match(name):
        return (f"invalid agent name '{name}': only letters, digits, "
                "dots, underscores, dashes (1-64 chars)")
    return None


def validate_task_id(task_id: str) -> str | None:
    if not isinstance(task_id, str) or not _TASK_ID_RE.match(task_id):
        return f"invalid task id '{task_id}'"
    return None
