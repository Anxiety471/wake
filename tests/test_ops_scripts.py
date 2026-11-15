"""Tests for bin/ops: the operator helpers must stay publishable, and must still run.

scripts/scrub-check.sh already scans every tracked file for IP addresses, emails and token-like
strings. These tests cover the rest of CONTRIBUTING's rule for these files, which a secret scan
cannot see: a personal absolute path, an undocumented WAKE_* variable, a missing `# desc:` line,
a lost exec bit, or an install path that silently drops them.

Nothing here needs a network, a database or a real host. Run with the rest:
python3 -m unittest discover -s tests
"""

import os
import re
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OPS = os.path.join(ROOT, "bin", "ops")
NAMES = sorted(n for n in os.listdir(OPS) if not n.endswith(".md"))
DOCS = (os.path.join(OPS, "README.md"), os.path.join(ROOT, "config.example.env"))

# Paths and names that belong to one operator's machine, never to this repo.
PERSONAL = ("/home/", "/root/", "/Users/", "/tmp/pgbuild", "rakazo", "hitori", "Anxiety471")


def text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def read(name):
    return text(os.path.join(OPS, name))


def run(name, *args, env=None):
    """Run one helper with a scrubbed environment: never the caller's real config."""
    base = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": tempfile.mkdtemp(prefix="ops-home-")}
    base.update(env or {})
    return subprocess.run([os.path.join(OPS, name), *args], capture_output=True, text=True, env=base, timeout=60)


class OpsFilesTestCase(unittest.TestCase):
    def test_the_directory_is_not_empty(self):
        self.assertEqual(NAMES, ["wake-api-e2e", "wake-keys", "wake-pg", "wake-pingall"])

    def test_every_script_parses(self):
        for name in NAMES:
            with self.subTest(name=name):
                r = subprocess.run(["bash", "-n", os.path.join(OPS, name)], capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stderr)

    def test_every_script_is_executable(self):
        for name in NAMES:
            with self.subTest(name=name):
                self.assertTrue(os.stat(os.path.join(OPS, name)).st_mode & 0o111, f"{name} lost its exec bit")

    def test_every_script_has_a_one_line_desc(self):
        for name in NAMES:
            with self.subTest(name=name):
                lines = read(name).splitlines()
                self.assertTrue(lines[1].startswith("# desc: "), f"{name}: line 2 is not a '# desc:' line")
                self.assertLess(len(lines[1]), 300, f"{name}: the desc line is too long to print in a terminal")

    def test_no_script_carries_a_personal_path(self):
        for name in NAMES:
            body = read(name)
            for needle in PERSONAL:
                with self.subTest(name=name, needle=needle):
                    self.assertNotIn(needle, body, f"{name} carries {needle!r}")

    def test_every_wake_variable_is_documented(self):
        docs = "".join(text(p) for p in DOCS)
        for name in NAMES:
            for var in sorted(set(re.findall(r"\bWAKE_[A-Z0-9_]+", read(name)))):
                with self.subTest(name=name, var=var):
                    self.assertIn(var, docs, f"{name} reads {var} but bin/ops/README.md does not name it")

    def test_the_e2e_script_has_no_literal_secret(self):
        body = read("wake-api-e2e")
        self.assertIn("openssl rand", body, "the e2e secret must be generated, not written down")
        self.assertNotRegex(body, r"SECRET=[\"']?[A-Za-z0-9][A-Za-z0-9-]{15,}[\"']?\n", "a literal secret is back")

    def test_the_e2e_script_calls_the_pg_helper_through_its_variable(self):
        # A bare `wake-pg` would fail from a checkout that is not installed; "$PG" works either way.
        self.assertNotRegex(read("wake-api-e2e"), r"(?m)^wake-pg ", "call the helper through $PG")

    def test_the_e2e_script_refuses_a_missing_checkout(self):
        r = run("wake-api-e2e", env={"WAKE_REPO": tempfile.mkdtemp(prefix="not-a-checkout-")})
        self.assertEqual(r.returncode, 1)
        self.assertIn("no wake checkout", r.stderr)
        self.assertNotIn("ALL PASS", r.stdout)


class OpsBehaviourTestCase(unittest.TestCase):
    def test_wake_pg_prints_usage_for_an_unknown_command(self):
        r = run("wake-pg", "nonsense")
        self.assertEqual(r.returncode, 2)
        self.assertIn("usage: wake-pg", r.stderr)

    def test_wake_pg_refuses_a_database_name_that_is_not_plain(self):
        for bad in ("bad;name", "two words", ""):
            with self.subTest(bad=bad):
                r = run("wake-pg", "createdb", bad)
                self.assertEqual(r.returncode, 2)
                self.assertIn("bad database name", r.stderr)

    def test_wake_keys_refuses_a_missing_registry(self):
        missing = os.path.join(tempfile.mkdtemp(prefix="keys-"), "bots.tsv")
        r = run("wake-keys", env={"WAKE_REGISTRY": missing, "WAKE_KEY_DIR": tempfile.mkdtemp()})
        self.assertEqual(r.returncode, 1)
        self.assertIn("no registry", r.stderr)

    def test_wake_keys_reports_bots_with_no_key_and_writes_nothing(self):
        home = tempfile.mkdtemp(prefix="keys-")
        key_dir = os.path.join(home, "keys")
        empty_store = os.path.join(home, "host-keys")
        os.makedirs(empty_store)
        reg = os.path.join(home, "bots.tsv")
        with open(reg, "w", encoding="utf-8") as f:
            f.write("coordinator\tbot-coordinator-0001\tCoordinator\topen\tplan\n")
            f.write("vault\tbot-vault-0008\tVault\tprivate\tnotes\n")
        r = run(
            "wake-keys",
            env={"WAKE_REGISTRY": reg, "WAKE_KEY_DIR": key_dir, "WAKE_HOST_KEY_DIR": empty_store},
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("saved 0, skipped 0, unreadable 0, no key 2", r.stdout)
        self.assertEqual(os.listdir(key_dir), [])

    def test_wake_keys_copies_a_key_with_mode_600_and_never_prints_it(self):
        home = tempfile.mkdtemp(prefix="keys-")
        key_dir = os.path.join(home, "keys")
        store = os.path.join(home, "host-keys")
        os.makedirs(store)
        secret = "k" * 40
        with open(os.path.join(store, "bot-coordinator-0001.key"), "w", encoding="utf-8") as f:
            f.write(secret)
        reg = os.path.join(home, "bots.tsv")
        with open(reg, "w", encoding="utf-8") as f:
            f.write("coordinator\tbot-coordinator-0001\tCoordinator\topen\tplan\n")
        r = run("wake-keys", env={"WAKE_REGISTRY": reg, "WAKE_KEY_DIR": key_dir, "WAKE_HOST_KEY_DIR": store})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("ok    coordinator", r.stdout)
        self.assertNotIn(secret, r.stdout + r.stderr)
        saved = os.path.join(key_dir, "bot-coordinator-0001.key")
        self.assertEqual(oct(os.stat(saved).st_mode & 0o777), "0o600")
        self.assertEqual(sorted(os.listdir(key_dir)), ["bot-coordinator-0001.key"])

    def test_wake_pingall_refuses_an_empty_registry(self):
        home = tempfile.mkdtemp(prefix="ping-")
        reg = os.path.join(home, "bots.tsv")
        with open(reg, "w", encoding="utf-8") as f:
            f.write("vault\tbot-vault-0008\tVault\tprivate\tnotes\n")
        r = run("wake-pingall", env={"WAKE_REGISTRY": reg, "WAKE_PING_LOG_DIR": home})
        self.assertEqual(r.returncode, 1)
        self.assertIn("no open bots", r.stderr)


class OpsInstallTestCase(unittest.TestCase):
    def install(self, **env):
        prefix = tempfile.mkdtemp(prefix="ops-prefix-")
        base = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PREFIX": prefix, "HOME": tempfile.mkdtemp()}
        base.update(env)
        r = subprocess.run([os.path.join(ROOT, "install.sh")], capture_output=True, text=True, env=base)
        self.assertEqual(r.returncode, 0, r.stderr)
        return prefix, r

    def test_install_without_ops_leaves_them_out(self):
        prefix, _ = self.install()
        for name in NAMES:
            self.assertFalse(os.path.exists(os.path.join(prefix, "bin", name)), f"{name} installed without OPS=1")
        self.assertTrue(os.path.exists(os.path.join(prefix, "bin", "wake")))

    def test_install_with_ops_installs_every_helper_executable(self):
        prefix, r = self.install(OPS="1")
        self.assertIn("bin/ops helpers", r.stdout)
        for name in NAMES:
            path = os.path.join(prefix, "bin", name)
            with self.subTest(name=name):
                self.assertTrue(os.path.exists(path), f"{name} was not installed")
                self.assertTrue(os.stat(path).st_mode & 0o111, f"{name} installed without its exec bit")
        self.assertFalse(os.path.exists(os.path.join(prefix, "bin", "README.md")), "the README is not a command")


if __name__ == "__main__":
    unittest.main()
