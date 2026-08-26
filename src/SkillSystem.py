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

    def __init__(self, skills_dir: Path | None = None):
        self.skills_dir = skills_dir or Configs.SKILLS_DIR
        self.registry: dict[str, dict] = {}
        self.scan_skills()

    @staticmethod
    def _parse_frontmatter(text: str) -> tuple[dict, str]:
        """解析 YAML frontmatter,返回 (元数据, 正文)。"""
        if not text.startswith("---"):
            return {}, text
        parts = text.split("---", 2)
        if len(parts) < 3:
            return {}, text
        try:
            meta = yaml.safe_load(parts[1]) or {}
        except yaml.YAMLError:
            meta = {}
        return meta, parts[2].strip()

    def scan_skills(self):
        self.registry.clear()
        if not self.skills_dir.exists():
            return
        for directory in sorted(self.skills_dir.iterdir()):
            if not directory.is_dir():
                continue
            manifest = directory / "SKILL.md"
            if not manifest.exists():
                continue
            raw = manifest.read_text()
            meta, _ = self._parse_frontmatter(raw)
            name = meta.get("name", directory.name)
            desc = meta.get("description", raw.split("\n")[0].lstrip("#").strip())
            self.registry[name] = {"name": name, "description": desc, "content": raw}

    def list_skills(self) -> str:
        if not self.registry:
            return "(no skills found)"
        return "\n".join(
            f"- {skill['name']}: {skill['description']}"
            for skill in self.registry.values())

    def load_skill(self, name: str) -> str:
        skill = self.registry.get(name)
        if not skill:
            available = ", ".join(self.registry.keys()) or "(none)"
            return f"Skill not found: {name}. Available: {available}"
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
