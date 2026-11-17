"""Guards for `deploy/README.md`: the nginx snippets decide what the internet reaches.

The publish step is a single `location` block in a runbook, so nothing compiles it and
nothing runs it. A prefix match there (the first attempt) forwarded `/mcp/audit/prune`,
`/mcp/identity` and the identity rotate/revoke routes to the app as well, collapsing the
operator secret and the public token into one tier. The reply route has the same failure
mode one route further on: a prefix there publishes every thread in the store. These tests
read the file the operator pastes from, which is the only place that boundary is written
down, so a location that drifts in the runbook drifts on the next host.
"""

import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
README = os.path.join(ROOT, "deploy", "README.md")
UNITS = {"k": 1024, "m": 1024 * 1024, None: 1}
HEADING = "### 6.5 Public reply route"


def read():
    with open(README) as handle:
        return handle.read()


def nginx_blocks(text):
    return re.findall(r"```nginx\n(.*?)```", text, re.S)


def nginx_block(text, needle="= /mcp"):
    """The one nginx block that holds `needle`; asserts the runbook shows it exactly once."""
    blocks = [block for block in nginx_blocks(text) if needle in block]
    assert len(blocks) == 1, f"expected one nginx block with {needle!r}, found {len(blocks)}"
    return blocks[0]


def section(text, heading):
    """One `###` heading up to the next heading of the same or a higher level."""
    if heading not in text:
        return ""
    start = text.index(heading)
    ends = [pos for pos in (text.find("\n### ", start + 1), text.find("\n## ", start + 1)) if pos != -1]
    return text[start : min(ends)] if ends else text[start:]


def reply_blocks(text):
    return nginx_blocks(section(text, HEADING))


def reply_block(text):
    """The location block of §6.5: the second of its two nginx blocks."""
    blocks = reply_blocks(text)
    assert len(blocks) == 2, f"§6.5 shows {len(blocks)} nginx blocks, expected the map and the location"
    return blocks[1]


def reply_problems(text):
    """What §6.5 must publish, and every way it has gone wrong before.

    Returns problems rather than raising, so a runbook that dropped the location reports
    "no regex location" and "a prefix for /threads" instead of an IndexError.
    """
    problems = []
    blocks = nginx_blocks(text)
    if len(blocks) != 3:
        problems.append(f"expected the runbook's three audited nginx blocks, found {len(blocks)}")
    if HEADING not in text:
        return problems + ["§6.5 is gone from the runbook"]
    local = reply_blocks(text)
    if len(local) != 2:
        problems.append(f"§6.5 shows {len(local)} nginx blocks, expected the map and the location")
    gate = local[0] if local else ""
    block = local[1] if len(local) > 1 else ""

    locations = re.findall(r"^\s*location\s+(.*?)\s*\{?\s*$", block, re.M)
    if len(locations) != 1:
        problems.append(f"§6.5 publishes {len(locations)} locations, expected 1: {locations}")
    match = re.search(r'^\s*location\s+~\s+"([^"]+)"\s*\{', block, re.M)
    if match is None:
        problems.append("§6.5 no longer uses a quoted regex location")
    else:
        pattern = match.group(1)
        if not pattern.startswith("^/threads/"):
            problems.append("the reply location is not anchored to the start of the path")
        if not pattern.endswith("/reply$"):
            problems.append("the reply location does not end at /reply$")
        if "{36}" not in pattern:
            problems.append("the reply location does not pin the id length, so it matches other paths")
    if "return 405" not in block:
        problems.append("§6.5 does not refuse a non-POST with 405")
    if "return 401" not in block:
        problems.append("§6.5 does not refuse a credential-less request with 401")
    if "limit_except" in block:
        problems.append("§6.5 gates the method with limit_except, which runs after the 401 check")
    proxy = re.search(r"proxy_pass\s+(\S+);", block)
    if proxy is None or proxy.group(1) != "http://127.0.0.1:3001":
        problems.append("§6.5 does not proxy to the loopback API")
    ceiling = re.search(r"client_max_body_size\s+(\d+)([km])?;", block)
    if ceiling is None:
        problems.append("§6.5 does not bound the request body")
    else:
        size = int(ceiling.group(1)) * UNITS[ceiling.group(2)]
        if not 8 * 1024 <= size <= 64 * 1024:
            problems.append(f"§6.5 body ceiling {size} B is not between the app's 8 KiB and 64k")
    if "map " in block:
        problems.append("the map sits inside the server block; sites-enabled files are included in http")

    # The map that feeds the 401 gate names both credentials: a bot with its own agent
    # token answers this route too, so gating on the reply token alone refuses it.
    if not re.search(
        r'^map\s+"\$http_x_wake_reply_token\|\$http_authorization"\s+\$wake_reply_has_auth\s*\{',
        gate,
        re.M,
    ):
        problems.append("§6.5 no longer defines $wake_reply_has_auth from both headers")
    if not re.search(r'^\s*"\|"\s+0;', gate, re.M):
        problems.append("the map no longer maps the two-empty case to 0")
    if not re.search(r"^\s*default\s+1;", gate, re.M):
        problems.append("the map no longer defaults to 1")
    if "location" in gate:
        problems.append("the gate block carries a location; a map belongs at http level")

    # A prefix anywhere would publish the rest of /threads, whatever §6.5 says.
    if re.search(r"^\s*location\s+(?:\^~|~\*?|=\s*)?\s*/threads", text, re.M):
        problems.append("the runbook carries a prefix location for /threads")
    return problems


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


class ReplyRoutePublishTest(unittest.TestCase):
    def test_the_runbook_publishes_the_reply_route_and_only_that_route(self):
        self.assertEqual(reply_problems(read()), [])

    def test_the_runbook_shows_three_audited_blocks_and_no_fourth(self):
        blocks = nginx_blocks(read())
        self.assertEqual(len(blocks), 3)
        self.assertEqual(sum("= /mcp" in b for b in blocks), 1)
        self.assertEqual(sum("$wake_reply_has_auth" in b and "location" not in b for b in blocks), 1)
        self.assertEqual(sum("/reply" in b for b in blocks), 1)

    def test_a_prefix_location_for_threads_is_caught(self):
        """The first attempt at /mcp, one route further on: it publishes the whole store."""
        text = read().replace('location ~ "^/threads/[0-9a-fA-F-]{36}/reply$"', "location ^~ /threads/")
        problems = reply_problems(text)
        self.assertNotEqual(problems, [])

    def test_an_unanchored_route_is_caught(self):
        for old, new in (
            ('"^/threads/[0-9a-fA-F-]{36}/reply$"', '"/threads/[0-9a-fA-F-]{36}/reply$"'),
            ('"^/threads/[0-9a-fA-F-]{36}/reply$"', '"^/threads/[0-9a-fA-F-]{36}/reply"'),
            ('"^/threads/[0-9a-fA-F-]{36}/reply$"', '"^/threads/.+/reply$"'),
        ):
            with self.subTest(pattern=new):
                self.assertNotEqual(reply_problems(read().replace(old, new)), [])

    def test_a_route_without_the_credential_gate_is_caught(self):
        self.assertNotEqual(reply_problems(read().replace("return 401", "return 404")), [])
        self.assertNotEqual(reply_problems(read().replace("return 405", "return 404")), [])

    def test_a_gate_that_ignores_the_bearer_is_caught(self):
        # A bot with its own agent token answers this route too; gating on the reply
        # token alone would refuse it, and the map is the only place both are named.
        text = read().replace("$http_x_wake_reply_token|$http_authorization", "$http_x_wake_reply_token")
        self.assertNotEqual(reply_problems(text), [])

    def test_the_map_and_the_location_have_to_be_in_different_contexts(self):
        # sites-enabled files are included inside http{}: a map in the server block is a
        # config error, and one location pasted at http level silently does nothing.
        self.assertNotIn("map ", reply_block(read()))
        gate = [b for b in nginx_blocks(read()) if "map " in b]
        self.assertEqual(len(gate), 1)
        self.assertNotIn("location", gate[0])

    def test_the_body_ceiling_sits_between_the_apps_limit_and_a_spoolable_body(self):
        ceiling = re.search(r"client_max_body_size\s+(\d+)([km])?;", reply_block(read()))
        self.assertIsNotNone(ceiling)
        size = int(ceiling.group(1)) * UNITS[ceiling.group(2)]
        self.assertGreaterEqual(size, 8 * 1024)
        self.assertLessEqual(size, 64 * 1024)


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
