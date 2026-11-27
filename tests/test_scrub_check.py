"""Tests for scripts/scrub-check.sh: it must catch real secrets and ignore the noise in commit messages.

Each test builds a throwaway git repository, drops the real script into it, commits something, then runs it.
Run with the rest: python3 -m unittest discover -s tests
"""

import os
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
CI = os.path.join(ROOT, ".github", "workflows", "ci.yml")


def text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def ci_steps(block):
    """Parse a job's `steps:` from ci.yml: keys, inline values and block sections per step.

    Only the shapes these pins need: a `key: value` line, or a `key:` line that opens a block of
    deeper-indented lines. Anything else is left to the YAML parser CI itself uses.
    """
    steps = []
    step = None
    section = None
    for line in block:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if line.startswith("      - "):
            step = {"keys": [], "values": {}, "sections": {}}
            steps.append(step)
            section = None
            stripped = stripped[2:].strip()
        elif step is None:
            continue
        elif line.startswith("        ") and not line.startswith("         "):
            section = None
        else:
            if section is not None:
                step["sections"][section].append(stripped)
            continue
        key, _, rest = stripped.partition(":")
        key, rest = key.strip(), rest.strip()
        step["keys"].append(key)
        if rest:
            step["values"][key] = rest
        else:
            section = key
            step["sections"][key] = []
    return steps


class ScrubCheckTestCase(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix="scrub-test-")
        shutil.copytree(SCRIPTS, os.path.join(self.repo, "scripts"))
        self.git("init", "-q")
        self.git("config", "user.email", "tester@example.invalid")
        self.git("config", "user.name", "tester")
        self.write("README.md", "scratch repository\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "base")

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, capture_output=True, text=True)

    def write(self, name, text):
        with open(os.path.join(self.repo, name), "w") as f:
            f.write(text)

    def commit(self, msg):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg)

    def scan(self, flag="--history"):
        r = subprocess.run(
            ["sh", os.path.join(self.repo, "scripts", "scrub-check.sh"), flag],
            cwd=self.repo,
            capture_output=True,
            text=True,
        )
        return r.returncode, r.stdout + r.stderr

    def assert_clean(self):
        rc, out = self.scan()
        self.assertEqual(rc, 0, out)
        self.assertIn("clean", out)

    def assert_hit(self, label):
        rc, out = self.scan()
        self.assertEqual(rc, 1, out)
        self.assertIn(label, out)


class ScopeTests(ScrubCheckTestCase):
    """#163: CI scans the checked-out commit's history; a full clone also holds every other branch."""

    def plant_on_another_branch(self):
        main = self.git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        self.git("checkout", "-q", "-b", "unmerged")
        self.write("mail.txt", "write to " + "someone" + "@" + "gmail" + ".com\\n")
        self.commit("mail")
        self.git("checkout", "-q", main)
        self.git("rm", "-q", "-f", "--ignore-unmatch", "mail.txt")

    def test_history_reads_an_unmerged_branch(self):
        self.plant_on_another_branch()
        rc, out = self.scan("--history")
        self.assertEqual(rc, 1, out)
        self.assertIn("email addresses (history)", out)

    def test_history_head_ignores_an_unmerged_branch(self):
        self.plant_on_another_branch()
        rc, out = self.scan("--history-head")
        self.assertEqual(rc, 0, out)
        self.assertIn("clean", out)

    def test_history_head_still_reads_the_checked_out_history(self):
        self.write("tick", "x\\n")
        self.commit("oops\\n\\n" + "Bearer " + "abcdefghijklmnopqrstuvwxyz012345")
        rc, out = self.scan("--history-head")
        self.assertEqual(rc, 1, out)
        self.assertIn("token-like strings (history)", out)

    def test_ci_scans_the_checked_out_history_only(self):
        runs = [
            line.strip() for line in text(CI).splitlines() if line.strip().startswith("run: scripts/scrub-check.sh")
        ]
        self.assertEqual(runs, ["run: scripts/scrub-check.sh --history-head"])

    def test_a_shallow_clone_hides_the_history_from_the_scan(self):
        """Why the CI checkout keeps `fetch-depth: 0` (#164 nit 2): `--history-head` reads
        `git log -p HEAD`, and a depth-1 clone holds one commit, so the same hit that fails a full
        clone scans clean there. Pinned by test_ci_secret_scan_cannot_be_disarmed.
        """
        self.write("mail.txt", "write to " + "someone" + "@" + "gmail" + ".com\n")
        self.commit("mail")
        self.write("mail.txt", "nothing to see\n")
        self.commit("cleanup")
        rc, out = self.scan("--history-head")
        self.assertEqual(rc, 1, out)
        self.assertIn("email addresses (history)", out)
        clone = tempfile.mkdtemp(prefix="scrub-shallow-")
        self.addCleanup(shutil.rmtree, clone, True)
        r = subprocess.run(
            ["git", "clone", "-q", "--depth", "1", "file://" + self.repo, clone],
            capture_output=True,
            text=True,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        r = subprocess.run(
            ["sh", os.path.join(clone, "scripts", "scrub-check.sh"), "--history-head"],
            cwd=clone,
            capture_output=True,
            text=True,
        )
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("clean", r.stdout)

    def test_ci_secret_scan_cannot_be_disarmed(self):
        """#164 nit 3: the run line was pinned, the job was not. An `if:`, `continue-on-error:` or
        job `defaults` on secret-scan skips the only history scan in CI, or neutralizes it, while
        every test here stays green - the class #162 pinned for `backgrounded`. A top-level
        `defaults` (one line outside the job, swapping every run step's shell) is already refused
        by tests/test_ops_scripts.py::test_ci_runs_the_suite_as_a_background_job.
        """
        lines = text(CI).splitlines()
        self.assertIn("  secret-scan:", lines, "the secret-scan job is gone")
        start = lines.index("  secret-scan:")
        block = []
        for line in lines[start + 1 :]:
            if line.startswith("  ") and not line.startswith("   ") and line.rstrip().endswith(":"):
                break
            block.append(line)
        # Job keys: `defaults` swaps the shell of the job's run step, `if` skips the job and
        # `continue-on-error` reports a failed scan as neutral - none of them touches a test.
        keys = [
            line.strip().split(":", 1)[0]
            for line in block
            if line.startswith("    ")
            and not line.startswith("     ")
            and ":" in line
            and not line.strip().startswith("#")
        ]
        self.assertEqual(
            keys,
            ["runs-on", "steps"],
            f"job keys {keys}: only runs-on and steps are allowed - defaults, if or "
            "continue-on-error would let a scan that never runs report green",
        )
        # #172: the key list above freezes the name, not the value. `runs-on: self-hosted` kept
        # every assertion here green with the scan running on whatever that machine has - a
        # different git or grep, a global git config - so the image is pinned by value too.
        runner = [line.strip() for line in block if line.startswith("    runs-on:")]
        self.assertEqual(
            runner,
            ["runs-on: ubuntu-24.04"],
            f"the secret-scan runner is {runner}: the scan's verdict comes from the runner's git "
            "and grep, so the image is pinned - a floating tag or a self-hosted machine changes "
            "what the scan reads while this test stays green",
        )
        # A step is as easy to disarm as the job, and the key also parses off the step's dash line
        # (`- if: false`) or behind a YAML anchor (`- &a if: false`), so all three are refused.
        disarming = [
            line.strip()
            for line in block
            if not line.strip().startswith("#")
            and re.match(
                r"""^\s*(?:-\s+)?(?:&\S+\s+)?["']?(if|continue-on-error)["']?\s*:""",
                line,
                re.IGNORECASE,
            )
        ]
        self.assertEqual(
            disarming,
            [],
            f"a step key that can disarm the scan was added: {disarming} - a skipped or neutral "
            "step still reports green",
        )
        steps = ci_steps(block)
        self.assertEqual(
            len(steps),
            2,
            f"{len(steps)} steps: only the checkout and the scan - another step can change what the scan reads",
        )
        checkout, scan = steps
        self.assertEqual(
            checkout["keys"],
            ["uses", "with"],
            f"checkout step keys {checkout['keys']}: only uses and with are allowed - an if or "
            "continue-on-error here disarms the checkout the scan depends on",
        )
        self.assertRegex(
            checkout["values"].get("uses", ""),
            r"^actions/checkout@v\d+$",
            f"checkout action is {checkout['values'].get('uses')!r}",
        )
        # #164 nit 2: full history is load-bearing for --history-head. At the default depth (1)
        # the runner holds one commit, the history scan sees nothing and stays green.
        self.assertEqual(
            checkout["sections"].get("with"),
            ["fetch-depth: 0"],
            f"checkout with is {checkout['sections'].get('with')}: fetch-depth: 0 is the history "
            "the scan reads - a shallow clone scans one commit and passes",
        )
        self.assertEqual(
            scan["keys"],
            ["name", "run"],
            f"scan step keys {scan['keys']}: only name and run are allowed - shell replaces the "
            "interpreter, if skips the step and continue-on-error reports a failed scan as neutral",
        )
        self.assertEqual(
            scan["values"].get("name"),
            "Scan tracked files and history",
            f"the scan step is named {scan['values'].get('name')!r}: the job name and this step's "
            "name are what a reviewer and the run log read, so a rename is a deliberate edit",
        )
        self.assertEqual(
            scan["values"].get("run"),
            "scripts/scrub-check.sh --history-head",
            f"the scan runs {scan['values'].get('run')!r}: --history reads every branch and turns "
            "main red for text only an unmerged branch carries (#163)",
        )


class NoiseTests(ScrubCheckTestCase):
    def test_dependabot_trailer_is_ignored(self):
        self.write("tick", "x\n")
        self.commit(
            "chore(deps): bump actions/checkout from 5 to 6\n\nSigned-off-by: dependabot[bot] <support@github.com>"
        )
        self.assert_clean()

    def test_a_github_noreply_coauthor_trailer_is_ignored(self):
        self.write("tick", "x\n")
        self.commit("squash merge\n\nCo-authored-by: Hitori Bocchi <anxiety471@users.noreply.github.com>")
        self.assert_clean()

    # #248: the owner's own address is his public identity, and a squash merge copies his commit
    # identity into a co-author trailer, so every branch cut from main failed the scan on it.
    OWNER_MAIL = "hitoribocchi471" + "@" + "gmail" + ".com"

    def test_the_owner_address_in_a_squash_trailer_is_ignored(self):
        self.write("owners.txt", "Co-authored-by: Joshua M. Valderrama <" + self.OWNER_MAIL + ">\n")
        self.commit("squash merge\n\nCo-authored-by: Joshua M. Valderrama <" + self.OWNER_MAIL + ">")
        self.assert_clean()

    def test_a_live_address_after_the_owner_address_still_fails(self):
        # the owner mask anchors the end of the address, like the noreply mask: a longer live
        # domain after it is not his address and is still a hit
        self.write("mail.txt", self.OWNER_MAIL + "." + "acme-mail" + ".net" + "\n")
        self.commit("mail")
        self.assert_hit("email addresses (tracked files)")

    def test_the_owner_address_in_a_tracked_file_is_ignored(self):
        # the same mask runs on the tracked-files stage, and the pattern in the script does not hit itself
        self.write("mail.txt", "write to " + self.OWNER_MAIL + "\n")
        self.commit("mail")
        self.assert_clean()

    def test_a_live_address_with_a_longer_local_part_before_the_owner_address_fails(self):
        # a local part in front of it is a different mailbox, so the mask must not eat it
        self.write("mail.txt", "write to someone." + self.OWNER_MAIL + "\n")
        self.commit("mail")
        self.assert_hit("email addresses (tracked files)")

    def test_a_live_address_beside_the_owner_address_fails(self):
        self.write(
            "mail.txt",
            "Co-authored-by: A <" + self.OWNER_MAIL + "> " + "someone" + "@" + "acme-mail" + ".net" + "\n",
        )
        self.commit("mail")
        self.assert_hit("email addresses (tracked files)")

    def test_a_secret_sharing_a_line_with_the_owner_address_still_fails(self):
        # masked per match, not skipped as a whole line, so a key beside it is still seen
        self.write("tick", "x\n")
        self.commit("oops\n\nCo-authored-by: A <" + self.OWNER_MAIL + "> sk-" + "c" * 30)
        self.assert_hit("token-like strings (history)")

    # #253: an address at a `.trade` domain is one of the instance's own identities, not a person, and a
    # squash merge copies it into a co-author trailer the same way it copies the owner's address.
    BOT_MAIL = "potato" + "@" + "bots" + "." + "trade"

    def test_a_bot_mail_coauthor_trailer_is_ignored(self):
        self.write("agents.txt", "Co-authored-by: Potato <" + self.BOT_MAIL + ">" + "\n")
        self.commit("squash merge" + "\n" + "\n" + "Co-authored-by: Potato <" + self.BOT_MAIL + ">")
        self.assert_clean()

    def test_the_bot_mail_in_a_tracked_file_is_ignored(self):
        self.write("agents.txt", "the devops bot is " + self.BOT_MAIL + "\n")
        self.commit("agents")
        self.assert_clean()

    def test_a_live_address_after_the_bot_mail_still_fails(self):
        # the mask anchors the end of the domain, so a real mailbox on a longer domain is still a hit
        self.write("mail.txt", "write to " + self.BOT_MAIL + "." + "acme-mail" + ".net" + "\n")
        self.commit("mail")
        self.assert_hit("email addresses (tracked files)")

    def test_another_mailbox_beside_the_bot_mail_fails(self):
        self.write(
            "mail.txt",
            "Co-authored-by: A <" + self.BOT_MAIL + "> " + "someone" + "@" + "acme-mail" + ".net" + "\n",
        )
        self.commit("mail")
        self.assert_hit("email addresses (tracked files)")

    def test_a_secret_sharing_a_line_with_the_bot_mail_still_fails(self):
        # masked per match, not skipped as a whole line, so a key beside it is still seen
        self.write("tick", "x" + "\n")
        self.commit("oops" + "\n" + "\n" + "Co-authored-by: P <" + self.BOT_MAIL + "> sk-" + "c" * 30)
        self.assert_hit("token-like strings (history)")

    def test_a_live_address_after_a_github_noreply_name_still_fails(self):
        # the noreply domain is public only as a whole address: a longer live domain after it is not
        self.write("mail.txt", "write to " + "someone" + "@users.noreply.github.com." + "attacker-cloud.cloud\n")
        self.commit("mail")
        self.assert_hit("email addresses (tracked files)")

    def test_a_documentation_url_is_ignored(self):
        self.write("NOTES.md", "- [Changelog](https://github.com/actions/checkout/blob/main/CHANGELOG.md)\n")
        self.commit("notes")
        self.assert_clean()

    def test_a_reserved_test_address_is_not_a_secret(self):
        # RFC 2606 / RFC 6761 names cannot resolve or receive mail, so a fixture there is a known
        # fake: tests/test_publish_checklist.py commits with one of these. Both stages see it here,
        # because the file is tracked and the message is in history.
        self.write(
            "fixture.py",
            "user.email=simulation@example.invalid\n"
            "a=test@example.com\n"
            "b=test@example.net\n"
            "c=test@sub.example.org\n"
            "d=test@build.test\n"
            "e=test@name.example\n"
            "f=test@box.localhost\n",
        )
        self.commit("fixtures\n\nuser.email=simulation@example.invalid")
        self.assert_clean()


class SecretTests(ScrubCheckTestCase):
    def test_bearer_token_in_a_commit_message_fails(self):
        self.write("tick", "x\n")
        self.commit("oops\n\n" + "Bearer " + "abcdefghijklmnopqrstuvwxyz012345")
        self.assert_hit("token-like strings (history)")

    def test_long_hex_blob_in_a_file_fails(self):
        self.write("blob.txt", "deadbeef" * 8 + "\n")
        self.commit("blob")
        self.assert_hit("long blobs outside URLs (tracked files)")

    def test_api_key_in_a_file_fails(self):
        self.write("key.txt", "sk-" + "a" * 30 + "\n")
        self.commit("key")
        self.assert_hit("token-like strings (tracked files)")

    def test_email_in_a_file_fails(self):
        # .example.org is a reserved test name now, so a public address is what has to fail here
        self.write("mail.txt", "write to " + "someone" + "@" + "gmail" + ".com\n")
        self.commit("mail")
        self.assert_hit("email addresses (tracked files)")

    def test_a_private_use_name_is_still_a_secret(self):
        # .internal and .local can name real private infrastructure, so they are not test names
        self.write("mail.txt", "write to " + "someone" + "@" + "build" + ".internal\n")
        self.commit("mail")
        self.assert_hit("email addresses (tracked files)")

    def test_a_reserved_name_that_only_prefixes_a_live_domain_still_fails(self):
        self.write("mail.txt", "write to " + "someone" + "@" + "example.com." + "attacker-cloud.cloud\n")
        self.commit("mail")
        self.assert_hit("email addresses (tracked files)")

    def test_a_live_address_sharing_a_line_with_a_fixture_still_fails(self):
        self.write("mail.txt", "simulation@example.invalid and " + "someone" + "@" + "gmail" + ".com\n")
        self.commit("mail")
        self.assert_hit("email addresses (tracked files)")

    def test_a_secret_sharing_a_line_with_a_noreply_address_still_fails(self):
        # the address is masked per match, not skipped as a whole line, so the key beside it is seen
        self.write("tick", "x\n")
        self.commit("oops\n\nCo-authored-by: x <a@users.noreply.github.com> sk-" + "c" * 30)
        self.assert_hit("token-like strings (history)")

    def test_url_is_stripped_but_a_secret_next_to_it_still_fails(self):
        self.write(
            "mixed.txt", "see https://example.org/a/very/long/path/that/is/not/a/secret and sk-" + "b" * 30 + "\n"
        )
        self.commit("mixed")
        self.assert_hit("token-like strings (tracked files)")


class AllowlistTests(ScrubCheckTestCase):
    """The allowlist blanks its own fixed strings, not the whole line (#160)."""

    LIVE_MAIL = "someone" + "@" + "acme-mail" + ".net"
    LIVE_IP = "10.20." + "30.40"

    def test_a_live_address_beside_a_noreply_trailer_fails(self):
        self.write("mail.txt", "Co-authored-by: A <a@users.noreply.github.com> " + self.LIVE_MAIL + "\n")
        self.commit("mail")
        self.assert_hit("email addresses (tracked files)")

    def test_a_live_ip_beside_the_owner_name_fails(self):
        self.write("hosts.txt", "@Anxiety471 " + self.LIVE_IP + "\n")
        self.commit("hosts")
        self.assert_hit("IP addresses other than 127.0.0.1 (tracked files)")

    def test_a_key_beside_the_owner_name_fails(self):
        self.write("key.txt", "@Anxiety471 " + "AKIA" + "IOSFODNN7EXAMPLE" + "\n")
        self.commit("key")
        self.assert_hit("token-like strings (tracked files)")

    def test_a_live_secret_in_a_commit_body_beside_a_trailer_fails(self):
        self.write("tick", "x\n")
        self.commit(
            "plant\n\nSigned-off-by: dependabot[bot] <support@github.com> " + self.LIVE_MAIL + " " + self.LIVE_IP
        )
        self.assert_hit("email addresses (history)")

    def test_a_private_pattern_hit_beside_a_fake_still_fails(self):
        # .git/scrub-patterns.txt never lands in the repo, so this stage has no other test
        with open(os.path.join(self.repo, ".git", "scrub-patterns.txt"), "w") as f:
            f.write("SECRET-[0-9]{4}\n")
        self.write("cfg.txt", "@Anxiety471 SECRET-1234\n")
        self.commit("cfg")
        self.assert_hit("private patterns from .git/scrub-patterns.txt (tracked files)")

    ROUTABLE = "10." + "0.0.0"  # a routable quad, assembled so this file does not carry one
    ROUTES = ROUTABLE + "/8 via " + ROUTABLE + ", " + "20." + "0.0.0 and " + "100." + "0.0.0"

    def test_a_routable_address_around_the_wildcard_fake_still_fails(self):
        # 0.0.0.0 is blanked as a whole address only: a routable address that contains it is still seen
        self.write("routes.txt", self.ROUTES + "\n")
        self.commit("routes")
        rc, out = self.scan()
        self.assertEqual(rc, 1, out)
        self.assertIn("IP addresses other than 127.0.0.1 (tracked files)", out)
        # every quad survives masking: nothing here was blanked along with the wildcard
        self.assertIn(self.ROUTES, out)

    def test_a_line_of_known_fakes_is_still_ignored(self):
        # masking must not turn the fakes themselves into hits
        self.write(
            "fake.txt",
            "Bearer minted-bot-engineer-0002\n"
            "expect(parseHost('0.0.0.0')).toBe('0.0.0.0');\n"
            "desk-secret-desk-secret-desk-secret\n"
            "Copyright (c) 2026 Joshua M. Valderrama\n"
            "Co-authored-by: A <anxiety471@users.noreply.github.com>\n",
        )
        self.commit("fake")
        self.assert_clean()

    def test_a_line_of_known_fakes_in_a_commit_body_is_still_ignored(self):
        # the same masking runs on history: a commit body holding only fakes is clean there too
        self.write("tick", "x\n")
        self.commit(
            "fake body\n\n"
            "Signed-off-by: dependabot[bot] <support@github.com>\n"
            "Bearer minted-bot-engineer-0002\n"
            "expect(parseHost('0.0.0.0')).toBe('0.0.0.0');\n"
            "Copyright (c) 2026 Joshua M. Valderrama\n"
        )
        self.assert_clean()

    def test_a_private_pattern_matching_a_known_fake_is_still_ignored(self):
        # the private stage masks the fakes too: a pattern that only hits a known fake reports nothing
        with open(os.path.join(self.repo, ".git", "scrub-patterns.txt"), "w") as f:
            f.write("desk-secret\n")
        self.write("cfg.txt", "desk-secret-desk-secret-desk-secret\n")
        self.commit("cfg")
        self.assert_clean()


if __name__ == "__main__":
    unittest.main()
