"""SkillSystem 测试:同名严格报错、版本/弃用元数据、坏 frontmatter。"""
import tempfile
import unittest
from pathlib import Path

from src.SkillSystem import SkillSystem


def write_skill(root: Path, dirname: str, frontmatter: str, body: str = "body"):
    directory = root / dirname
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        f"---\n{frontmatter}\n---\n# {dirname}\n{body}\n")


class TestSkillSystem(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def test_parses_version_updated_at_and_deprecated(self):
        write_skill(self.root, "demo",
                    "name: demo\ndescription: demo skill\nversion: 3\n"
                    "updated_at: 2026-01-01\ndeprecated: true")
        skills = SkillSystem(self.root)
        skill = skills.registry["demo"]
        self.assertEqual(skill["version"], 3)
        self.assertEqual(str(skill["updated_at"]), "2026-01-01")
        self.assertTrue(skill["deprecated"])
        self.assertIn("[deprecated]", skills.list_skills())
        self.assertIn("[deprecated skill: demo]", skills.load_skill("demo"))

    def test_duplicate_name_raises(self):
        write_skill(self.root, "a", "name: same\ndescription: first")
        write_skill(self.root, "b", "name: same\ndescription: second")
        with self.assertRaises(ValueError):
            SkillSystem(self.root)

    def test_non_strict_keeps_first_and_records_error(self):
        write_skill(self.root, "a", "name: same\ndescription: first")
        write_skill(self.root, "b", "name: same\ndescription: second")
        skills = SkillSystem(self.root, strict=False)
        self.assertEqual(skills.registry["same"]["description"], "first")
        self.assertTrue(skills.errors)
        self.assertIn("duplicate skill name", skills.errors[0])

    def test_malformed_frontmatter_is_tolerated(self):
        write_skill(self.root, "odd", "- not\n- a mapping", body="Useful body")
        skills = SkillSystem(self.root)
        self.assertIn("odd", skills.registry)
        self.assertIn("Useful body", skills.registry["odd"]["description"])

    def test_invalid_version_defaults_to_one(self):
        write_skill(self.root, "v", "name: v\nversion: nope")
        self.assertEqual(SkillSystem(self.root).registry["v"]["version"], 1)

    def test_missing_dir(self):
        skills = SkillSystem(self.root / "missing")
        self.assertEqual(skills.registry, {})
        self.assertIn("no skills found", skills.list_skills())

    def test_catalog_sorted(self):
        write_skill(self.root, "z", "name: zebra\ndescription: z")
        write_skill(self.root, "a", "name: apple\ndescription: a")
        catalog = SkillSystem(self.root).list_skills()
        self.assertLess(catalog.index("apple"), catalog.index("zebra"))


if __name__ == "__main__":
    unittest.main()
