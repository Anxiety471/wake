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


def update_block(text):
    """§7, from its heading to the next top-level one."""
    start = text.index("## 7. Update")
    end = text.index("\n## ", start + 1)
    return text[start:end]


def rollback_problems(text):
    """What the §7 restore must say, and the one thing it must not.

    `pg_restore -c` drops only the objects the archive holds, so anything a later migration
    added survives the restore and the schema does not go back to the older shape. Dropping and
    recreating the database is the exact form, and it is also the destructive one: it discards
    everything written after the dump, which the runbook has to say out loud.
    """
    block = update_block(text)
    problems = []
    # The flag combination, not the two words: §7 itself explains why `pg_restore -c` alone is
    # not a rollback, and a guard that reads prose as a command would fail on its own advice.
    if "pg_restore -c -d" in block:
        problems.append("§7 restores with -c, which keeps objects the dump does not have")
    for needed in ("dropdb wake", "createdb -O wake wake", "pg_restore -d wake", "pg_restore -l"):
        if needed not in block:
            problems.append(f"§7 no longer says {needed!r}")
    if "discard" not in block.lower():
        problems.append("§7 does not say that the restore discards what was written after the dump")
    return problems


class RollbackRestoreTest(unittest.TestCase):
    def test_the_rollback_restores_the_dump_exactly(self):
        self.assertEqual(rollback_problems(read()), [])

    def test_each_part_of_the_exact_restore_is_required(self):
        # The prose can be rewritten; the steps cannot go missing, and the destructive part has
        # to stay in plain words next to them.
        for needed in ("dropdb wake", "createdb -O wake wake", "pg_restore -d wake", "pg_restore -l"):
            with self.subTest(removed=needed):
                self.assertNotEqual(rollback_problems(read().replace(needed, "removed")), [])

    def test_the_old_compress_restore_is_caught(self):
        # The shape this replaced: one `pg_restore -c` line and no drop.
        old = read().replace("sudo -u postgres pg_restore -d wake <", "sudo -u postgres pg_restore -c -d wake <")
        problems = rollback_problems(old)
        self.assertNotEqual(problems, [])
        self.assertTrue(any("-c" in problem for problem in problems), problems)

    def test_a_restore_that_forgets_the_data_loss_is_caught(self):
        text = read().replace("is discarded with the old", "goes away with the old")
        self.assertTrue(any("discard" in problem for problem in rollback_problems(text)))


def unpublish_block(text):
    """The unpublish step, from its heading to the next one."""
    start = text.index("### 6.3 Unpublish")
    end = text.index("\n### ", start + 1)
    return text[start:end]


def unpublish_problems(text):
    block = unpublish_block(text)
    problems = []
    if 'cp "/etc/nginx/sites-available/$BACKUP" "/etc/nginx/sites-available/$SITE"' not in block:
        problems.append("the rollback does not copy $BACKUP over $SITE")
    if "/sites-available/wake-mcp" in block:
        problems.append("the rollback hard-codes a site file name")
    return problems


class UnpublishRollbackTest(unittest.TestCase):
    def test_the_rollback_takes_both_paths_from_the_operators_values(self):
        """An incident rollback dies at the cp when the name it copies is not the name on the host."""
        self.assertEqual(unpublish_problems(read()), [])

    def test_a_hard_coded_site_file_name_is_caught(self):
        literal = read().replace(
            'cp "/etc/nginx/sites-available/$BACKUP" "/etc/nginx/sites-available/$SITE"',
            "cp /etc/nginx/sites-available/wake-mcp.bak-2026-11-15 /etc/nginx/sites-available/wake-mcp",
        )
        self.assertNotEqual(unpublish_problems(literal), [], "a hard-coded rollback passed")


if __name__ == "__main__":
    unittest.main()
