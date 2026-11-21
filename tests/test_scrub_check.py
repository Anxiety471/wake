"""Tests for scripts/scrub-check.sh: it must catch real secrets and ignore the noise in commit messages.

Each test builds a throwaway git repository, drops the real script into it, commits something, then runs it.
Run with the rest: python3 -m unittest discover -s tests
"""

import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")


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

    def scan(self):
        r = subprocess.run(
            ["sh", os.path.join(self.repo, "scripts", "scrub-check.sh"), "--history"],
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


if __name__ == "__main__":
    unittest.main()
