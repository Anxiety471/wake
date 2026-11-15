"""Guards for `deploy/README.md`: the nginx snippet decides what the internet reaches.

The publish step is a single `location` block in a runbook, so nothing compiles it and
nothing runs it. A prefix match there (the first attempt) forwarded `/mcp/audit/prune`,
`/mcp/identity` and the identity rotate/revoke routes to the app as well, collapsing the
operator secret and the public token into one tier. These tests read the file the operator
pastes from, which is the only place that boundary is written down.
"""

import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
README = os.path.join(ROOT, "deploy", "README.md")
UNITS = {"k": 1024, "m": 1024 * 1024, None: 1}


def read():
    with open(README) as handle:
        return handle.read()


def nginx_block(text):
    blocks = re.findall(r"```nginx\n(.*?)```", text, re.S)
    assert len(blocks) == 1, f"expected one nginx block, found {len(blocks)}"
    return blocks[0]


class PublishSnippetTest(unittest.TestCase):
    def test_publishes_mcp_by_exact_match_and_nothing_else(self):
        locations = re.findall(r"^\s*location\s+([^{]+?)\s*\{?\s*$", nginx_block(read()), re.M)
        self.assertEqual(locations, ["= /mcp"])

    def test_no_prefix_location_anywhere_in_the_runbook(self):
        # Catches the fix being applied to a prose example instead of the snippet.
        self.assertIsNone(re.search(r"^\s*location\s+/mcp", read(), re.M))

    def test_the_body_ceiling_stays_near_the_apps_own_limit(self):
        match = re.search(r"client_max_body_size\s+(\d+)([km])?;", nginx_block(read()))
        self.assertIsNotNone(match, "the snippet must bound the request body")
        size = int(match.group(1)) * UNITS[match.group(2)]
        # Below the app's 32 KiB a legitimate body meets nginx's HTML 413 instead of the
        # API's JSON; far above it, nginx spools an unauthenticated body to disk before
        # the app can answer 401.
        self.assertGreaterEqual(size, 32 * 1024)
        self.assertLessEqual(size, 64 * 1024)

    def test_every_cited_script_exists_and_the_install_covers_the_directory(self):
        body = read()
        self.assertIn("install -d -o root -g wake -m 755 /opt/wake/ops", body)
        self.assertIn("install -o root -g wake -m 755 deploy/ops/*.py /opt/wake/ops/", body)
        local = {name for name in os.listdir(os.path.join(ROOT, "deploy", "ops")) if name.endswith(".py")}
        cited = set(re.findall(r"/opt/wake/ops/([\w.-]+\.py)", body))
        self.assertEqual(cited - local, set())


class SectionNumberingTest(unittest.TestCase):
    def test_every_subsection_reference_names_a_numbered_heading(self):
        body = read()
        headings = set(re.findall(r"^###\s+(\d+\.\d+)\s", body, re.M))
        cited = set(re.findall(r"§(\d+\.\d+)", body))
        self.assertEqual(cited - headings, set())


if __name__ == "__main__":
    unittest.main()
