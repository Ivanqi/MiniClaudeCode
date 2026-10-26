"""技能加载:扫描 skills 目录,解析 SKILL.md 的 frontmatter。

每个技能是一个包含 SKILL.md 的子目录,注册表缓存在内存中,
供 list_skills / load_skill 工具使用。
"""
from pathlib import Path

import yaml

from src.Configs import Configs
from src.LlmClient import make_function_tool


class SkillSystem:
    """技能系统:负责技能目录的扫描、列表与加载。"""

    def __init__(self, skills_dir: Path | None = None, strict: bool = True):
        self.skills_dir = skills_dir or Configs.skills_dir()
        self.strict = strict
        self.registry: dict[str, dict] = {}
        self.errors: list[str] = []
        self.scan_skills()

    @staticmethod
    def _parse_frontmatter(text: str) -> tuple[dict, str]:
        """解析 YAML frontmatter,返回 (元数据, 正文);坏格式按空元数据容错。"""
        if not text.startswith("---"):
            return {}, text
        parts = text.split("---", 2)
        if len(parts) < 3:
            return {}, text
        try:
            meta = yaml.safe_load(parts[1]) or {}
        except yaml.YAMLError:
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        return meta, parts[2].strip()

    def scan_skills(self):
        self.registry.clear()
        self.errors = []
        if not self.skills_dir.exists():
            return
        for directory in sorted(self.skills_dir.iterdir()):
            if not directory.is_dir():
                continue
            manifest = directory / "SKILL.md"
            if not manifest.exists():
                continue
            try:
                raw = manifest.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                self.errors.append(f"{manifest}: {exc}")
                continue
            meta, body = self._parse_frontmatter(raw)
            name = str(meta.get("name") or directory.name).strip()
            if not name:
                self.errors.append(f"{manifest}: empty skill name")
                continue
            if name in self.registry:
                message = (f"duplicate skill name '{name}' in "
                           f"{self.registry[name]['path']} and {manifest}")
                if self.strict:
                    raise ValueError(message)
                self.errors.append(message)
                continue
            desc = meta.get("description")
            if not desc:
                lines = [line.strip() for line in body.splitlines()
                         if line.strip()]
                first = next((line for line in lines
                              if not line.startswith("#")), "")
                if not first:
                    first = next((line for line in lines
                                  if line.startswith("#")), "")
                desc = first.lstrip("#").strip() or name
            try:
                version = int(meta.get("version", 1))
            except (TypeError, ValueError):
                version = 1
            self.registry[name] = {
                "name": name,
                "description": str(desc),
                "content": raw,
                "version": max(1, version),
                "updated_at": meta.get("updated_at"),
                "deprecated": bool(meta.get("deprecated", False)),
                "path": str(manifest),
            }

    def list_skills(self) -> str:
        if not self.registry:
            return "(no skills found)"
        lines = []
        for name in sorted(self.registry):
            skill = self.registry[name]
            suffix = " [deprecated]" if skill["deprecated"] else ""
            lines.append(f"- {skill['name']}: {skill['description']}{suffix}")
        return "\n".join(lines)

    def load_skill(self, name: str) -> str:
        skill = self.registry.get(name)
        if not skill:
            available = ", ".join(sorted(self.registry)) or "(none)"
            return f"Skill not found: {name}. Available: {available}"
        if skill["deprecated"]:
            return f"[deprecated skill: {name}]\n{skill['content']}"
        return skill["content"]

    # ── 工具层 ──

    def tool_defs(self) -> list[dict]:
        return [
            make_function_tool("load_skill", "Load the full content of a skill by name.",
                {"type": "object",
                 "properties": {"name": {"type": "string"}},
                 "required": ["name"]}),
        ]

    def handlers(self) -> dict:
        return {"load_skill": self.load_skill}


# 全局单例,导入时完成技能扫描。
SKILL_SYSTEM = SkillSystem()
