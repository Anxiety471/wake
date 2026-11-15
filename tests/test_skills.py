"""The agent skills must stay loadable, and must stay publishable.

A skill is loaded by its frontmatter, so a missing `name`, a `name` that does not match
its directory, or a missing `description` makes it invisible rather than wrong. These
tests also keep the same publishing rules the rest of the repository follows: no
personal absolute paths, no live hostnames, and no skill left out of the README table.
"""

import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILLS = os.path.join(ROOT, "skills")

PERSONAL = ("/home/", "/root/", "/Users/", "rakazo", "hitori", "Anxiety471")
HOSTNAME = re.compile(
    r"\b(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+"
    r"(?:com|net|org|io|dev|app|co|me|xyz|cloud|trade|internal|local|lan|corp|host|site|info|biz|gov|edu)\b"
)


def names():
    return sorted(n for n in os.listdir(SKILLS) if os.path.isdir(os.path.join(SKILLS, n)))


def text(name):
    with open(os.path.join(SKILLS, name, "SKILL.md"), encoding="utf-8") as f:
        return f.read()


def frontmatter(body):
    """The key/value pairs between the first two `---` lines."""
    parts = body.split("---")
    if len(parts) < 3 or parts[0].strip():
        return {}
    pairs = {}
    for line in parts[1].splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            pairs[key.strip()] = value.strip()
    return pairs


class SkillsTestCase(unittest.TestCase):
    def test_there_is_at_least_one_skill(self):
        self.assertTrue(names(), "skills/ is empty")

    def test_every_skill_has_a_frontmatter_that_will_load(self):
        for name in names():
            with self.subTest(name=name):
                meta = frontmatter(text(name))
                self.assertEqual(meta.get("name"), name, "the name must match the directory")
                self.assertGreater(len(meta.get("description", "")), 80, "a description drives when it loads")

    def test_every_skill_names_itself_the_way_the_frontmatter_does(self):
        for name in names():
            with self.subTest(name=name):
                self.assertRegex(text(name), r"(?m)^# ", "a skill starts with a heading")

    def test_no_skill_carries_a_personal_path_or_a_live_hostname(self):
        allowed = (".example.com", ".example.net", ".example.org")
        for name in names():
            body = text(name)
            for needle in PERSONAL:
                with self.subTest(name=name, needle=needle):
                    self.assertNotIn(needle, body)
            for hit in sorted({m.group(0) for m in HOSTNAME.finditer(body)}):
                with self.subTest(name=name, hit=hit):
                    self.assertTrue(hit in ("example.com",) or hit.endswith(allowed), f"{name} carries {hit!r}")

    def test_the_readme_lists_every_skill(self):
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as f:
            readme = f.read()
        for name in names():
            with self.subTest(name=name):
                self.assertIn(f"skills/{name}", readme, f"{name} is not in the README table")

    def test_every_ops_helper_is_named_in_a_skill(self):
        # A helper that no skill mentions is one an agent will not find. wake-wait landed in
        # bin/ops and in no skill until this check existed.
        ops = os.path.join(ROOT, "bin", "ops")
        helpers = sorted(n for n in os.listdir(ops) if not n.endswith(".md") and os.path.isfile(os.path.join(ops, n)))
        everywhere = "\n".join(text(name) for name in names())
        for helper in helpers:
            with self.subTest(helper=helper):
                self.assertIn(helper, everywhere, f"bin/ops/{helper} is not named in any skill")


if __name__ == "__main__":
    unittest.main()
