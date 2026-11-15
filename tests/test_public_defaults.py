"""Every host named in the tracked tree, in a URL or written bare, is an example or the loopback.

The repository is public. A single real hostname in a doc, a default or a test
publishes someone's infrastructure, and a default is the worst place for one: it
is what an operator inherits without reading anything.

This test does not try to recognise a private name. It allowlists the hosts a
public repository is allowed to name, so a new real one fails here instead of
shipping. Add an entry only when the repository is meant to name that host.

The walker asks git which files are tracked, not the filesystem: a dot-directory
like .github is where a runner label or a deploy target gets named, and an
untracked note in someone's working copy is not part of the repository.
"""

import os
import re
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Hosts the tree may name: the loopback, the example domains, and the places the
#: project is actually published (its own repository, the versioning spec).
ALLOWED = {
    "127.0.0.1",
    "localhost",
    "example.com",
    "example.org",
    "example.net",
    "github.com",
    "semver.org",
}

#: A host that cannot be a public name: reserved test suffixes.
ALLOWED_SUFFIXES = (".invalid", ".example", ".example.com", ".example.org", ".internal")

#: Binary or generated files a text scan cannot read.
SKIP_SUFFIXES = (".png", ".gif", ".jpg", ".jpeg", ".ico", ".pdf", ".pyc", ".woff", ".woff2")
SKIP_NAMES = {"bun.lock", "package-lock.json", "uv.lock"}

#: The one file that cannot be scanned: to prove the guard bites, it has to hold a
#: host that must fail. A test below pins this set to exactly this file, so the
#: exclusion cannot quietly grow.
SKIP_PATHS = {"tests/test_public_defaults.py"}

# An http(s) URL's host: a user:token@ prefix is skipped rather than read as the host.
# A protocol-relative //host needs no pattern of its own - HOSTNAME below sees its name.
URL = re.compile(r"https?://(?:[^/@\s]*@)?(\[[0-9a-fA-F:]+\]|[A-Za-z0-9._-]+)")
HOSTNAME = re.compile(r"\b[A-Za-z0-9-]+\.(?:com|org|net|trade|io|ph|gov|edu)\b")


def allowed(host):
    name = host.strip("[]").lower()
    if name in ALLOWED or name.endswith(ALLOWED_SUFFIXES):
        return True
    # A single label cannot be a public domain; it is a local alias or a placeholder.
    return "." not in name


def read(path):
    with open(path, encoding="utf-8", errors="ignore") as handle:
        return handle.read()


def tracked_files(root=ROOT):
    """Every tracked text file, as (relative path, text). Git decides the list."""
    try:
        listing = subprocess.run(
            ["git", "-C", root, "ls-files", "-z"],
            check=True,
            capture_output=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as error:  # pragma: no cover
        raise unittest.SkipTest(f"not a git checkout, cannot list tracked files: {error}") from error

    for raw in listing.split(b"\0"):
        if not raw:
            continue
        rel = raw.decode("utf-8", "surrogateescape")
        if rel in SKIP_PATHS or os.path.basename(rel) in SKIP_NAMES or rel.endswith(SKIP_SUFFIXES):
            continue
        try:
            yield rel, read(os.path.join(root, rel))
        except (UnicodeDecodeError, OSError):
            continue


def hosts_in(text):
    """Every host the text names: inside a URL, or a bare name with a public suffix."""
    return URL.findall(text) + HOSTNAME.findall(text)


def offenders_in(text):
    return [host for host in hosts_in(text) if not allowed(host)]


class PublicHostTests(unittest.TestCase):
    def test_no_url_names_a_host_outside_the_allowlist(self):
        found = [f"{rel}: {host}" for rel, text in tracked_files() for host in offenders_in(text)]
        self.assertEqual(found, [], "a real host leaked into the tree")

    def test_only_this_file_is_excluded_from_the_scan(self):
        self.assertEqual(SKIP_PATHS, {"tests/test_public_defaults.py"})

    def test_workflow_files_are_scanned_too(self):
        names = [rel for rel, _ in tracked_files()]
        self.assertIn(".github/workflows/ci.yml", names, "the walker skipped a dot-directory")

    def test_the_deploy_verify_default_is_an_example(self):
        body = read(os.path.join(ROOT, "deploy", "ops", "mcp-verify.py"))
        default = re.search(r'URL = sys\.argv\[1\] if len\(sys\.argv\) > 1 else "(https?://[^"]+)"', body)
        self.assertIsNotNone(default, "mcp-verify.py lost its default URL line")
        self.assertIn("example.com", default.group(1))

    def test_the_config_example_sends_to_an_example_host(self):
        body = read(os.path.join(ROOT, "config.example.env"))
        host = re.search(r"^WAKE_API_BASE=https?://([A-Za-z0-9._-]+)", body, re.M)
        self.assertIsNotNone(host, "WAKE_API_BASE is no longer a URL in the example config")
        self.assertTrue(host.group(1).lower().endswith(("example.com", "example.org", "example.net")), host.group(1))

    def test_a_bare_hostname_is_scanned_in_every_tracked_file(self):
        # A URL-only scan left a plain sentence naming a host green, and outside deploy/
        # the bare-name pattern never ran. Both shapes are inputs to the same scan now.
        # The userinfo sample is assembled: written whole, this repository's own scrub reads
        # a user:secret@host as an email address.
        userinfo = "See https://ops" + ":token@" + "bots.acme-corp.io/mcp."
        for sample in (
            "See bots.acme-corp.io for the MCP endpoint.",
            userinfo,
            "See //bots.acme-corp.io/mcp.",
        ):
            with self.subTest(sample=sample):
                self.assertTrue(offenders_in(sample), f"{sample} would slip past")
        self.assertEqual(offenders_in("See example.com, github.com and semver.org."), [])

    def test_the_scanner_would_catch_a_real_host(self):
        # A guard that cannot fail is not a guard.
        for sample in ("https://bots.acme-corp.io/mcp", "https://h.example-hospital.gov.ph", "bots.acme-corp.io"):
            with self.subTest(sample=sample):
                hosts = URL.findall(sample) or HOSTNAME.findall(sample)
                self.assertTrue(hosts, sample)
                self.assertFalse(all(allowed(host) for host in hosts), f"{sample} would slip past")


if __name__ == "__main__":
    unittest.main()
