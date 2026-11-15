"""Every URL in the tracked tree points at an example host or the loopback.

The repository is public. A single real hostname in a doc, a default or a test
publishes someone's infrastructure, and a default is the worst place for one: it
is what an operator inherits without reading anything.

This test does not try to recognise a private name. It allowlists the hosts a
public repository is allowed to name, so a new real one fails here instead of
shipping. Add an entry only when the repository is meant to name that host.
"""

import glob
import os
import re
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

SKIP_DIRS = {".git", "node_modules", "target", "__pycache__", ".ruff_cache", "dist", "build"}
SKIP_FILES = {"bun.lock", "package-lock.json"}

URL = re.compile(r"https?://(\[[0-9a-fA-F:]+\]|[A-Za-z0-9._-]+)")
HOSTNAME = re.compile(r"\b[A-Za-z0-9-]+\.(?:com|org|net|trade|io)\b")


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
    for path in glob.glob(os.path.join(root, "**", "*"), recursive=True):
        rel = os.path.relpath(path, root)
        if any(part in SKIP_DIRS for part in rel.split(os.sep)):
            continue
        if os.path.basename(rel) in SKIP_FILES or os.path.isdir(path):
            continue
        if not os.path.isfile(path) or path.endswith((".png", ".gif", ".pyc")):
            continue
        try:
            yield rel, read(path)
        except (UnicodeDecodeError, OSError):
            continue


class PublicHostTests(unittest.TestCase):
    def test_no_url_names_a_host_outside_the_allowlist(self):
        offenders = [
            f"{rel}: {host}" for rel, text in tracked_files() for host in URL.findall(text) if not allowed(host)
        ]
        self.assertEqual(offenders, [], "a real host leaked into the tree")

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

    def test_no_file_in_deploy_names_a_hostname_outside_the_allowlist(self):
        for path in glob.glob(os.path.join(ROOT, "deploy", "**", "*"), recursive=True):
            if os.path.isdir(path) or path.endswith((".png", ".gif", ".pyc")):
                continue
            for name in HOSTNAME.findall(read(path)):
                self.assertTrue(allowed(name), f"{os.path.relpath(path, ROOT)} names {name}")


if __name__ == "__main__":
    unittest.main()
