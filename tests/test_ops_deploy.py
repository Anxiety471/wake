"""Behaviour tests for the four helpers that touch a copy of wake or a host.

The helpers ship code only: nothing here reaches a network, a database or a real
host. `ssh`, the host runner and the builder are stubs that record what they were
asked to do, so a remote install and a service deploy are both proven end to end
without one — and a secret is proven to travel through stdin, never through stdout
or a command line.
"""

import os
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OPS = os.path.join(ROOT, "bin", "ops")


def run(name, *args, env=None):
    base = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": tempfile.mkdtemp(prefix="ops-home-")}
    base.update(env or {})
    return subprocess.run([os.path.join(OPS, name), *args], capture_output=True, text=True, env=base, timeout=120)


def write(path, body, executable=False):
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    if executable:
        os.chmod(path, 0o755)
    return path


class GateCheckTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="gate-")
        self.gate = write(os.path.join(self.dir, "gate.py"), "def match(cmd):\n    return ('ok',)\n")

    def step(self, name, body):
        return write(os.path.join(self.dir, name), body, executable=True)

    def test_usage_without_arguments(self):
        r = run("wake-gate-check")
        self.assertEqual(r.returncode, 2)
        self.assertIn("usage: wake-gate-check", r.stderr)

    def test_a_missing_gate_is_bad_input(self):
        r = run("wake-gate-check", os.path.join(self.dir, "absent"))
        self.assertEqual(r.returncode, 2)
        self.assertIn("no gate at", r.stderr)

    def test_every_configured_step_runs_and_passes(self):
        ok = self.step("ok.py", "import sys\nprint('checked', sys.argv[1].endswith('.py'))\n")
        r = run("wake-gate-check", self.gate, "--selftest", ok, "--harness", ok)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("OK: every configured step passed", r.stdout)
        self.assertRegex(r.stdout, r"(?m)^[0-9a-f]{64}$")
        self.assertIn("bytes", r.stdout)
        self.assertIn("checked True", r.stdout)

    def test_a_failing_step_fails_the_check(self):
        bad = self.step("bad.py", "import sys\nprint('nope')\nsys.exit(3)\n")
        r = run("wake-gate-check", self.gate, "--harness", bad)
        self.assertEqual(r.returncode, 1)
        self.assertIn("a configured step did not pass", r.stderr)
        self.assertIn("exited 3", r.stderr)

    def test_the_e2e_runs_in_a_copy_and_gets_the_gate(self):
        repo = tempfile.mkdtemp(prefix="repo-")
        os.makedirs(os.path.join(repo, "tests"))
        write(os.path.join(repo, "tests", "test_keep.py"), "")
        e2e = write(
            os.path.join(self.dir, "test_probe.py"),
            "import os, unittest\n"
            "class Probe(unittest.TestCase):\n"
            "    def test_gate_is_a_file(self):\n"
            "        self.assertTrue(os.path.isfile(os.environ.get('WAKE_GATE', '')))\n",
        )
        r = run("wake-gate-check", self.gate, "--e2e", e2e, "--repo", repo)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("OK: every configured step passed", r.stdout)
        self.assertEqual(os.listdir(os.path.join(repo, "tests")), ["test_keep.py"], "the checkout was edited")


class ShapeCheckTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="shape-")
        self.wake = os.path.join(ROOT, "bin", "wake")

    def gate(self, name, body):
        return write(os.path.join(self.dir, name + ".py"), body)

    def test_usage_without_arguments(self):
        r = run("wake-shape-check")
        self.assertEqual(r.returncode, 2)
        self.assertIn("usage: wake-shape-check", r.stderr)

    def test_a_missing_gate_is_bad_input(self):
        r = run("wake-shape-check", self.wake)
        self.assertEqual(r.returncode, 2)
        self.assertIn("no gate", r.stderr)

    def test_a_permissive_gate_allows_every_command(self):
        allowed = self.gate("allow", "def match(cmd):\n    return ('ok',)\n")
        r = run("wake-shape-check", self.wake, allowed)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("ALLOW", r.stdout)
        self.assertIn(", 0 refused", r.stdout)

    def test_a_refusing_gate_fails_the_check(self):
        refused = self.gate("refuse", "def match(cmd):\n    return None\n")
        r = run("wake-shape-check", self.wake, refused)
        self.assertEqual(r.returncode, 1)
        self.assertIn("REFUSE", r.stdout)

    def test_a_file_without_the_builders_checks_nothing_and_says_so(self):
        target = write(os.path.join(self.dir, "plain"), "value = 1\n")
        allowed = self.gate("allow", "def match(cmd):\n    return ('ok',)\n")
        r = run("wake-shape-check", target, allowed)
        self.assertEqual(r.returncode, 1)
        self.assertIn("built no commands", r.stderr)


class DeployTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="deploy-")
        self.prefix = os.path.join(self.dir, "bin")
        self.ssh = write(
            os.path.join(self.dir, "ssh"),
            "#!/bin/sh\n"
            "while [ $# -gt 0 ]; do\n"
            '  case "$1" in\n'
            "    -o) shift 2 ;;\n"
            "    -q) shift ;;\n"
            "    *) break ;;\n"
            "  esac\n"
            "done\n"
            "shift\n"
            'exec sh -c "$*"\n',
            executable=True,
        )

    def test_a_local_install_lands_every_file_with_its_mode_and_is_verified(self):
        r = run("wake-deploy", "local", "--prefix", self.prefix)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("verified", r.stdout)
        wake = os.path.join(self.prefix, "wake")
        self.assertTrue(os.path.exists(wake))
        self.assertEqual(oct(os.stat(wake).st_mode & 0o777), "0o755")
        self.assertEqual(oct(os.stat(os.path.join(self.prefix, "wake_config.py")).st_mode & 0o777), "0o644")
        self.assertTrue(os.path.exists(os.path.join(self.prefix, "wake-pingall")), "the ops helpers are part of it")

    def test_an_ssh_target_is_installed_and_verified(self):
        env = {"PATH": self.dir + ":" + os.environ.get("PATH", "/usr/bin:/bin")}
        r = run("wake-deploy", "ssh:test-host", "--prefix", self.prefix, env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("test-host: verified", r.stdout)
        self.assertTrue(os.path.exists(os.path.join(self.prefix, "wake")))
        self.assertEqual(oct(os.stat(os.path.join(self.prefix, "wake")).st_mode & 0o777), "0o755")

    def test_dry_run_writes_nothing(self):
        r = run("wake-deploy", "local", "--prefix", self.prefix, "--dry-run")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("install -m 755", r.stdout)
        self.assertFalse(os.path.exists(self.prefix), "dry run created the prefix")

    def test_a_target_that_is_neither_local_nor_ssh_is_refused(self):
        r = run("wake-deploy", "some-host")
        self.assertEqual(r.returncode, 2)
        self.assertIn("`local` or `ssh:DEST`", r.stderr)


class HostDeployTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="hostdeploy-")
        self.log = os.path.join(self.dir, "host.log")
        self.hostops = write(
            os.path.join(self.dir, "hostops"),
            "#!/bin/sh\n{\n  printf 'CMD: %s\\n' \"$1\"\n  cat\n  printf '\\n--\\n'\n} >> \"$STUB_LOG\"\n",
            executable=True,
        )
        self.secret = "regsecret-0123456789"
        self.password = "dbpassword123"
        self.env = {
            "WAKE_HOSTOPS": self.hostops,
            "STUB_LOG": self.log,
            "WAKE_DB_PASSWORD": self.password,
            "WAKE_REGISTRATION_SECRET": self.secret,
        }

    def logged(self):
        try:
            with open(self.log, encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return ""

    def test_no_transport_is_bad_input(self):
        r = run("wake-host-deploy", "status")
        self.assertEqual(r.returncode, 2)
        self.assertIn("WAKE_HOSTOPS", r.stderr)

    def test_an_unknown_step_is_refused(self):
        r = run("wake-host-deploy", "drop-everything", env=self.env)
        self.assertEqual(r.returncode, 2)
        self.assertIn("unknown step", r.stderr)

    def test_help_lists_every_step(self):
        r = run("wake-host-deploy", "--help", env=self.env)
        self.assertEqual(r.returncode, 0)
        for step in ("db", "user", "build", "install", "env", "unit", "status", "log"):
            self.assertIn(step, r.stdout)

    def test_the_env_file_is_written_through_stdin_and_never_printed(self):
        r = run("wake-host-deploy", "env", env=self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn(self.secret, r.stdout + r.stderr)
        self.assertNotIn(self.password, r.stdout + r.stderr)
        self.assertIn("REGISTRATION_SECRET=" + self.secret, self.logged())
        self.assertIn("640 /etc/wake/api.env", self.logged())

    def test_the_db_step_creates_the_role_with_the_password_from_stdin(self):
        r = run("wake-host-deploy", "db", env=self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn(self.password, r.stdout + r.stderr)
        self.assertIn("CREATE ROLE wake LOGIN PASSWORD '" + self.password + "'", self.logged())

    def test_the_db_step_refuses_a_password_that_needs_quoting(self):
        env = dict(self.env, WAKE_DB_PASSWORD="not-alnum")
        r = run("wake-host-deploy", "db", env=env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("non-alphanumerics", r.stderr)

    def test_the_db_step_refuses_a_role_that_is_not_an_identifier(self):
        env = dict(self.env, WAKE_DB_ROLE="wake; drop role postgres")
        r = run("wake-host-deploy", "db", env=env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("plain identifier", r.stderr)

    def test_the_unit_runs_the_installed_copy_and_hardens_it(self):
        r = run("wake-host-deploy", "unit", env=self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        unit = self.logged()
        self.assertIn("ExecStart=/opt/wake/node /opt/wake/index.mjs", unit)
        self.assertIn("ProtectSystem=strict", unit)
        self.assertIn("User=wake", unit)
        self.assertNotIn(self.secret, r.stdout + r.stderr)

    def test_dry_run_prints_commands_and_no_secret(self):
        r = run("wake-host-deploy", "env", "--dry-run", env=self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("install -d -o root -g wake -m 750 /etc/wake", r.stdout)
        self.assertNotIn(self.secret, r.stdout + r.stderr)
        self.assertFalse(os.path.exists(self.log), "dry run reached the host")


if __name__ == "__main__":
    unittest.main()
