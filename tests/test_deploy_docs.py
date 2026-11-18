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
import shutil
import subprocess
import tempfile
import unittest
import uuid

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
        # Compile it both ways. A substring check on "{36}" passes a class that can never
        # fill a thread id (`[0-9a-f]{36}` has no dashes) and a wildcard width (`.{36}` matches
        # any 36 characters). The class the runbook uses is hex-or-dash, so a real id has to
        # match, and a path of the wrong width or alphabet has to be refused.
        try:
            route = re.compile(pattern)
        except re.error as error:
            problems.append(f"the reply location is not a valid regex: {error}")
        else:
            if route.fullmatch("/threads/" + str(uuid.uuid4()) + "/reply") is None:
                problems.append("the reply location does not match a thread id")
            if route.fullmatch("/threads/" + "a" * 35 + "/reply") is not None:
                problems.append("the reply location does not pin the id length, so it matches other paths")
            if route.fullmatch("/threads/" + "z" * 36 + "/reply") is not None:
                problems.append("the reply location matches 36 characters that cannot be in a thread id")
            # A class that matches on past the id, and one that makes the id optional, pass
            # every probe above: both accept a real id and refuse the wrong width and
            # alphabet. What they also accept is a path that is not a thread id at all.
            if route.fullmatch("/threads/" + str(uuid.uuid4()) + "x/reply") is not None:
                problems.append("the reply location accepts a path that runs on past the thread id")
            if route.fullmatch("/threads//reply") is not None:
                problems.append("the reply location accepts a request with no thread id")
    # The condition lines, not the words "return 405": `= POST` refuses the wrong requests and
    # `!= 0` inverts the gate, and both still leave the phrase in the block. The order is about
    # the status a caller gets, not about what it learns: 405 and 401 are both emitted from
    # inside a location that already matched the shape, so either one confirms the route. A
    # non-POST should be refused as a method, not invited to retry with a token.
    method_guard = "if ($request_method != POST) { return 405; }"
    gate_guard = "if ($wake_reply_has_auth = 0) { return 401; }"
    if method_guard not in block:
        problems.append(f"§6.5 does not carry {method_guard} as written")
    if gate_guard not in block:
        problems.append(f"§6.5 does not carry {gate_guard} as written")
    if method_guard in block and gate_guard in block and block.index(method_guard) > block.index(gate_guard):
        problems.append("§6.5 checks the credential before the method, so a non-POST gets a 401 instead of a 405")
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

    def test_a_class_that_cannot_match_a_thread_id_is_caught(self):
        # `[0-9a-f]{36}` still contains "{36}", so the substring check passed it, but a thread
        # id is dash-separated and this route would publish nothing.
        text = read().replace("[0-9a-fA-F-]{36}", "[0-9a-f]{36}")
        self.assertNotEqual(text, read(), "the runbook no longer uses the hex-or-dash id class")
        self.assertNotEqual(reply_problems(text), [])

    def test_a_wildcard_width_is_caught(self):
        # `.{36}` also contains "{36}" and matches any 36 characters, not a thread id.
        text = read().replace("[0-9a-fA-F-]{36}", ".{36}")
        self.assertNotEqual(text, read(), "the runbook no longer uses the hex-or-dash id class")
        self.assertNotEqual(reply_problems(text), [])

    def test_a_class_that_matches_on_past_the_id_is_caught(self):
        # `[0-9a-fA-F-]{36}.*` accepts a real id and refuses the wrong width and alphabet,
        # and also accepts `/threads/<uuid>x/reply`.
        text = read().replace("[0-9a-fA-F-]{36}", "[0-9a-fA-F-]{36}.*")
        self.assertNotEqual(text, read(), "the runbook no longer uses the hex-or-dash id class")
        self.assertIn("runs on past the thread id", " ".join(reply_problems(text)))

    def test_an_optional_thread_id_is_caught(self):
        # `([0-9a-fA-F-]{36})?` also accepts `/threads//reply`, which no handler answers.
        text = read().replace("[0-9a-fA-F-]{36}", "([0-9a-fA-F-]{36})?")
        self.assertNotEqual(text, read(), "the runbook no longer uses the hex-or-dash id class")
        self.assertIn("no thread id", " ".join(reply_problems(text)))

    def test_a_class_with_no_width_limit_is_caught(self):
        # `[0-9a-fA-F-]+` matches a single character, so the width probe is the only check
        # that refuses `/threads/a/reply`. Deleting it used to leave this suite green.
        text = read().replace("[0-9a-fA-F-]{36}", "[0-9a-fA-F-]+")
        self.assertNotEqual(text, read(), "the runbook no longer uses the hex-or-dash id class")
        self.assertIn("does not pin the id length", " ".join(reply_problems(text)))

    def test_an_uncompilable_class_is_caught(self):
        # Silencing the re.error branch made this report no problems at all, so a runbook
        # whose class does not compile shipped unaudited.
        text = read().replace("[0-9a-fA-F-]{36}", "[0-9a-fA-F-{36}")
        self.assertNotEqual(text, read(), "the runbook no longer uses the hex-or-dash id class")
        self.assertIn("not a valid regex", " ".join(reply_problems(text)))

    def test_the_method_guard_flipped_is_caught(self):
        # `= POST` keeps the words "return 405" in the block and refuses every legitimate reply.
        text = read().replace("if ($request_method != POST)", "if ($request_method = POST)")
        self.assertNotEqual(text, read(), "the runbook no longer shows the method guard as written")
        self.assertNotEqual(reply_problems(text), [])

    def test_the_credential_gate_inverted_is_caught(self):
        # `!= 0` keeps "return 401" and opens the route to every credential-less request.
        text = read().replace("if ($wake_reply_has_auth = 0)", "if ($wake_reply_has_auth != 0)")
        self.assertNotEqual(text, read(), "the runbook no longer shows the gate as written")
        self.assertNotEqual(reply_problems(text), [])

    def test_the_guards_are_pinned_in_the_order_the_runbook_states(self):
        block = read()
        swapped = block.replace(
            "        if ($request_method != POST) { return 405; }\n"
            "        if ($wake_reply_has_auth = 0) { return 401; }",
            "        if ($wake_reply_has_auth = 0) { return 401; }\n"
            "        if ($request_method != POST) { return 405; }",
        )
        self.assertNotEqual(swapped, block, "the runbook no longer shows the two guards on consecutive lines")
        self.assertNotEqual(reply_problems(swapped), [])

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


def cli_store_section(text):
    """The CLI-store section, from its heading to the next top-level one."""
    if "## The CLI store is a site setting" not in text:
        return ""
    start = text.index("## The CLI store is a site setting")
    end = text.index("\n## ", start + 1)
    return text[start:end]


def confirm_command(text):
    """The section's one `sh` block: the operator's check of the resolved store."""
    blocks = re.findall(r"```sh\n(.*?)```", cli_store_section(text), re.S)
    assert len(blocks) == 1, f"the CLI-store section shows {len(blocks)} sh blocks, expected one"
    return blocks[0].strip()


def cli_store_problems(text):
    """The order is the fix, and the runbook is the only place it is written down.

    The wrong order is silent: `wake --inbox` exits 0, `wake-inbox-io read` prints nothing,
    and the next append creates the default store - so a runbook that deploys first and
    writes the file afterwards is the failure, not a typo. The confirm command is checked
    here because it is prose until someone runs it.
    """
    if not cli_store_section(text):
        return ["the CLI-store section is gone from the runbook"]
    block = cli_store_section(text)
    problems = []
    if "**before** the first deploy" not in block:
        problems.append("the section no longer says the file is written before the deploy")
    if "no threads opened by human:unknown" not in block:
        problems.append("the section no longer shows the silent failure it exists to prevent")
    try:
        command = confirm_command(text)
    except AssertionError as error:
        return problems + [str(error)]
    if "/usr/local/bin" not in command:
        problems.append("the confirm command does not name the prefix the scripts live in")
    if "WAKE_STATE_DIR" not in command:
        problems.append("the confirm command does not print the resolved store")
    return problems


class CliStoreSectionTest(unittest.TestCase):
    def test_the_runbook_writes_the_site_file_before_the_deploy(self):
        self.assertEqual(cli_store_problems(read()), [])

    def test_the_confirm_command_prints_what_the_site_file_sets(self):
        """Run the runbook's own command: a wrong one makes the operator's check lie."""
        command = confirm_command(read())
        with tempfile.TemporaryDirectory() as tmp:
            prefix = os.path.join(tmp, "bin")
            os.mkdir(prefix)
            shutil.copy(os.path.join(ROOT, "bin", "wake_config.py"), prefix)
            home = os.path.join(tmp, "home")
            os.mkdir(home)
            site = os.path.join(tmp, "site.env")
            with open(site, "w", encoding="utf-8") as handle:
                handle.write("WAKE_STATE_DIR=/srv/site-store\n")
            run = subprocess.run(
                ["sh", "-c", command.replace("/usr/local/bin", prefix)],
                capture_output=True,
                text=True,
                env={
                    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                    "HOME": home,
                    "WAKE_SYSTEM_CONFIG": site,
                    "PYTHONDONTWRITEBYTECODE": "1",
                },
            )
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout.strip(), "/srv/site-store", run.stderr)

    def test_a_section_that_deploys_before_writing_the_file_is_caught(self):
        text = read().replace("Write that file **before** the first deploy", "Write that file after the first deploy")
        self.assertNotEqual(cli_store_problems(text), [])


if __name__ == "__main__":
    unittest.main()
