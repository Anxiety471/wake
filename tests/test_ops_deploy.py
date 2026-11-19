"""Behaviour tests for the four helpers that touch a copy of wake or a host.

The helpers ship code only: nothing here reaches a network, a database or a real
host. `ssh`, the host runner and the builder are stubs that record what they were
asked to do, so a remote install and a service deploy are both proven end to end
without one — and a secret is proven to travel through stdin, never through stdout
or a command line.
"""

import base64
import contextlib
import io
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OPS = os.path.join(ROOT, "bin", "ops")


def run(name, *args, env=None):
    base = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": tempfile.mkdtemp(prefix="ops-home-"),
        "PYTHONDONTWRITEBYTECODE": "1",  # a helper that imports a module must not cache it in the tree
    }
    base.update(env or {})
    return subprocess.run([os.path.join(OPS, name), *args], capture_output=True, text=True, env=base, timeout=120)


def write(path, body, executable=False):
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    if executable:
        os.chmod(path, 0o755)
    return path


def load_source(name, path):
    """Execute a script from disk as a module without writing a bytecode cache.

    SourceFileLoader writes `<dir>/__pycache__` beside the file it loads, which puts
    bin/ops/__pycache__ into the checkout; compile() plus exec writes nothing but the
    module object.
    """
    module = types.ModuleType(name)
    module.__file__ = path
    with open(path, encoding="utf-8") as f:
        exec(compile(f.read(), path, "exec"), module.__dict__)  # noqa: S102 - the file under test
    return module


def load_host_deploy():
    """Execute bin/ops/wake-host-deploy as a module, to test one pure function of it."""
    return load_source("wake_host_deploy", os.path.join(OPS, "wake-host-deploy"))


def make_build(directory, body="export const built = 1\n"):
    """A build directory shaped like the one `build` makes: private, with index.mjs."""
    build = os.path.join(directory, "wake-build-mine")
    os.makedirs(build)
    os.chmod(build, 0o700)
    with open(os.path.join(build, "index.mjs"), "w", encoding="utf-8") as f:
        f.write(body)
    return build


def record(state, build):
    os.makedirs(state, exist_ok=True)
    with open(os.path.join(state, "last-build"), "w", encoding="utf-8") as f:
        f.write(build + "\n")


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

    def test_an_ssh_prefix_tilde_belongs_to_the_remote_shell(self):
        # expanduser() ran once for every target, so `ssh:DEST` with the default prefix was
        # installed into this machine's home path on the remote.
        env = {"HOME": "/home/someone-else"}
        r = run("wake-deploy", "ssh:test-host", "--dry-run", env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('"$HOME"/.local/bin', r.stdout)
        self.assertNotIn("/home/someone-else", r.stdout, "this machine's home was sent to the remote")

    def test_a_local_prefix_still_expands_here(self):
        env = {"HOME": self.dir}
        r = run("wake-deploy", "local", "--dry-run", env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(os.path.join(self.dir, ".local", "bin"), r.stdout)

    def test_a_prefix_for_another_users_home_is_refused_on_an_ssh_target(self):
        # shell_path quoted it, so ~root/.local/bin was created as a relative path under $HOME on
        # the remote and verify_ssh blessed it, because it built the same quoted string.
        r = run("wake-deploy", "ssh:test-host", "--prefix", "~root/.local/bin", "--dry-run")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("another user's home", r.stderr)
        self.assertNotIn("~root", r.stdout, "an unresolvable ~user path was sent to the remote")

    def test_a_local_prefix_for_another_users_home_still_expands_here(self):
        r = run("wake-deploy", "local", "--prefix", "~root/.local/bin", "--dry-run")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("~root", r.stdout, "a local ~user path was left unexpanded")

    def test_a_literal_prefix_this_machine_cannot_expand_is_refused(self):
        # expanduser leaves an unknown ~user alone, and install_local would create a literal
        # "~no-such-user" directory under the working directory.
        r = run("wake-deploy", "local", "--prefix", "~no-such-user-wake/bin", "--dry-run")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("cannot resolve", r.stderr)


class HostDeployTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="hostdeploy-")
        self.log = os.path.join(self.dir, "host.log")
        self.hostops = write(
            os.path.join(self.dir, "hostops"),
            "#!/bin/sh\n{\n  printf 'CMD: %s\\n' \"$1\"\n  cat\n  printf '\\n--\\n'\n} >> \"$STUB_LOG\"\n",
            executable=True,
        )
        self.secret = "regsecret-0123456789-0123456789abc"
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

    def test_help_says_which_transport_wins(self):
        # transport_kind() made WAKE_HOSTOPS win when both are set, and deploy/README.md says so,
        # but the text an operator reads at 2 a.m. still offered "one is required" (#129).
        r = run("wake-host-deploy", "--help", env=self.env)
        self.assertEqual(r.returncode, 0)
        self.assertIn("WAKE_HOSTOPS wins when both", r.stdout)

    def test_the_user_step_makes_the_group_before_the_account(self):
        # On a clean host, `useradd --gid wake` fails with "group 'wake' does not exist", so the
        # documented order stopped at its second step and nothing after it could run.
        r = run("wake-host-deploy", "user", env=self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        log = self.logged()
        self.assertIn("getent group wake >/dev/null || sudo -n groupadd --system wake", log)
        self.assertLess(log.index("groupadd"), log.index("useradd"), "the group must exist first")

    def test_install_refuses_a_build_it_did_not_make(self):
        # install used to take the newest /tmp/wake-build-* by mtime. A planted directory won.
        state = os.path.join(self.dir, "state")
        planted = os.path.join(self.dir, "wake-build-planted")
        os.makedirs(planted)
        with open(os.path.join(planted, "index.mjs"), "w", encoding="utf-8") as f:
            f.write("planted payload")
        env = dict(self.env, WAKE_DEPLOY_STATE=state, TMPDIR=self.dir)
        r = run("wake-host-deploy", "install", env=env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("no build recorded", r.stderr)
        self.assertNotIn("planted payload", self.logged())

    def test_install_takes_the_recorded_build(self):
        state = os.path.join(self.dir, "state")
        build = make_build(self.dir, "export const mine = 1\n")
        record(state, build)
        env = dict(self.env, WAKE_DEPLOY_STATE=state, WAKE_NODE="/usr/bin/node", WAKE_REPO=self.dir)
        r = run("wake-host-deploy", "install", env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("export const mine = 1", self.logged())

    def test_install_refuses_a_recorded_build_that_is_not_private(self):
        state = os.path.join(self.dir, "state")
        build = make_build(self.dir)
        os.chmod(build, 0o755)
        record(state, build)
        env = dict(self.env, WAKE_DEPLOY_STATE=state, WAKE_NODE="/usr/bin/node")
        r = run("wake-host-deploy", "install", env=env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("not a private directory", r.stderr)

    def test_a_value_read_never_takes_the_hosts_stderr_with_it(self):
        # host() returns stdout and stderr together, so a warning from a wrapper around the runner
        # became the value: `getent passwd` found nothing and reported "user: exists", so the step
        # skipped useradd and the install it guards failed later, further from the cause (#117).
        noisy = write(
            os.path.join(self.dir, "hostops-noisy"),
            "#!/bin/sh\n"
            "{ printf 'CMD: %s\\n' \"$1\"; cat; printf '\\n--\\n'; } >> \"$STUB_LOG\"\n"
            'case "$1" in\n'
            "  'getent passwd '*) echo 'Warning: Permanently added example to the list of known hosts.' >&2 ;;\n"
            "esac\n"
            "exit 0\n",
            executable=True,
        )
        r = run("wake-host-deploy", "user", env=dict(self.env, WAKE_HOSTOPS=noisy))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("user: exists", r.stdout, "stderr was read as an account")
        self.assertIn("useradd", self.logged(), "the account was never created")
        self.assertNotIn("Permanently added", r.stdout)

    def test_a_warning_on_stderr_never_lands_in_the_environment_file(self):
        # The same splice wrote the warning into api.env: the existing file is read for its keys,
        # and everything read back was merged into what gets installed (#117).
        noisy = write(
            os.path.join(self.dir, "hostops-warn-env"),
            "#!/bin/sh\n"
            "{ printf 'CMD: %s\\n' \"$1\"; cat; printf '\\n--\\n'; } >> \"$STUB_LOG\"\n"
            'case "$1" in\n'
            "  *'cat /etc/wake/api.env'*)\n"
            "      printf 'WAKE_PORT=3001\\n'\n"
            "      printf '__wake_env:read__\\n'\n"
            "      echo 'Warning: Permanently added example to the list of known hosts.' >&2 ;;\n"
            "esac\n"
            "exit 0\n",
            executable=True,
        )
        r = run("wake-host-deploy", "env", env=dict(self.env, WAKE_HOSTOPS=noisy))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        log = self.logged()
        self.assertIn("WAKE_PORT=3001", log, "the file's own keys were dropped")
        self.assertNotIn("Permanently added", log, "a warning was written into api.env")

    def test_dry_run_install_needs_no_build_and_no_node(self):
        # Both branches used to exit 1: no build found, and `command -v node` empty under dry-run.
        env = dict(self.env, WAKE_DEPLOY_STATE=os.path.join(self.dir, "state"))
        r = run("wake-host-deploy", "install", "--dry-run", env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("<build>/index.mjs", r.stdout)
        self.assertFalse(os.path.exists(self.log), "dry run reached the host")

    def test_migrations_arrive_as_one_stream_with_no_tmp_staging(self):
        state = os.path.join(self.dir, "state")
        build = make_build(self.dir)
        migrations = os.path.join(build, "migrations")
        os.makedirs(migrations)
        for name in ("001_init.sql", "002_threads.sql"):
            with open(os.path.join(migrations, name), "w", encoding="utf-8") as f:
                f.write("select 1;\n")
        record(state, build)
        # A checkout is named and still ignored: install ships the build's copy, not os.listdir.
        checkout = os.path.join(self.dir, "checkout")
        os.makedirs(os.path.join(checkout, "apps", "api", "src", "migrations"))
        env = dict(self.env, WAKE_DEPLOY_STATE=state, WAKE_NODE="/usr/bin/node", WAKE_REPO=checkout)
        r = run("wake-host-deploy", "install", env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        log = self.logged()
        self.assertNotIn("/tmp/wake-", log, "a /tmp staging name is back")
        self.assertIn("base64 -d | sudo -n tar -x --no-same-owner -C /opt/wake/migrations", log)
        self.assertIn('case " 001_init.sql 002_threads.sql " in', log, "stale migrations are not pruned")
        payload = log.split("base64 -d | sudo -n tar -x --no-same-owner -C /opt/wake/migrations", 1)[1]
        payload = payload.split("\n--\n", 1)[0]
        stream = base64.b64decode(payload.strip())
        with tarfile.open(fileobj=io.BytesIO(stream)) as tar:
            self.assertEqual(sorted(tar.getnames()), ["001_init.sql", "002_threads.sql"])

    def test_install_refuses_a_migration_name_it_cannot_trust(self):
        state = os.path.join(self.dir, "state")
        build = make_build(self.dir)
        migrations = os.path.join(build, "migrations")
        os.makedirs(migrations)
        with open(os.path.join(migrations, "a b.sql"), "w", encoding="utf-8") as f:
            f.write("select 1;\n")
        record(state, build)
        env = dict(self.env, WAKE_DEPLOY_STATE=state, WAKE_NODE="/usr/bin/node")
        r = run("wake-host-deploy", "install", env=env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("not a migration name", r.stderr)

    def test_a_host_without_node_gets_the_message_that_names_wake_node(self):
        # host_out() exited first with "FAILED: command -v node", so the one line that says what to
        # do could never be printed (#134).
        silent = write(os.path.join(self.dir, "hostops-no-node"), "#!/bin/sh\nexit 1\n", executable=True)
        state = os.path.join(self.dir, "state")
        record(state, make_build(self.dir))
        r = run("wake-host-deploy", "install", env=dict(self.env, WAKE_HOSTOPS=silent, WAKE_DEPLOY_STATE=state))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("set WAKE_NODE", r.stdout + r.stderr)
        self.assertNotIn("FAILED: command -v node", r.stdout + r.stderr)

    def test_a_refused_env_read_stops_before_the_rewrite(self):
        # `cat FILE 2>/dev/null || true` answered "empty" for a refused read, so the rewrite dropped
        # every key the file carried (#131).
        stub = write(
            os.path.join(self.dir, "hostops-unreadable"),
            "#!/bin/sh\ncase $1 in *__wake_env:unreadable__*) printf '__wake_env:unreadable__\\n' ;; esac\n",
            executable=True,
        )
        r = run("wake-host-deploy", "env", env=dict(self.env, WAKE_HOSTOPS=stub))
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("could not be read", r.stderr)
        self.assertNotIn("tee /etc/wake/api.env.new", r.stderr)

    def test_an_env_file_that_is_read_keeps_its_keys(self):
        stub = write(
            os.path.join(self.dir, "hostops-read"),
            "#!/bin/sh\n"
            "case $1 in\n"
            "  *__wake_env:read__*) printf 'WAKE_RATE_LIMIT=5\\n__wake_env:read__\\n' ;;\n"
            '  *) printf \'CMD: %s\\n\' "$1" >> "$STUB_LOG"; cat >> "$STUB_LOG" ;;\n'
            "esac\n",
            executable=True,
        )
        r = run("wake-host-deploy", "env", env=dict(self.env, WAKE_HOSTOPS=stub))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("WAKE_RATE_LIMIT=5", self.logged(), "a key the file carried was dropped")

    def test_env_merge_keeps_what_the_operator_added(self):
        merge = load_host_deploy().merge_env
        existing = "DATABASE_URL=postgres://old\nWAKE_RATE_LIMIT=5\nWAKE_MCP_ENABLED=true\n# a note the operator left\n"
        merged = merge(existing, {"DATABASE_URL": "postgres://new", "WAKE_MCP_ENABLED": "false"})
        self.assertIn("DATABASE_URL=postgres://new", merged)
        self.assertIn("WAKE_RATE_LIMIT=5", merged)
        self.assertIn("WAKE_MCP_ENABLED=false", merged)
        self.assertEqual(merged.count("WAKE_MCP_ENABLED="), 1)
        self.assertIn("# a note the operator left", merged)

    def test_env_writes_mcp_off_unless_the_operator_says_on(self):
        r = run("wake-host-deploy", "env", env=self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("WAKE_MCP_ENABLED=false", self.logged())
        r = run("wake-host-deploy", "env", env=dict(self.env, WAKE_MCP_ENABLED="true"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("WAKE_MCP_ENABLED=true", self.logged().split("WAKE_MCP_ENABLED=false")[-1])

    def test_the_env_file_is_written_through_stdin_and_never_printed(self):
        r = run("wake-host-deploy", "env", env=self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn(self.secret, r.stdout + r.stderr)
        self.assertNotIn(self.password, r.stdout + r.stderr)
        self.assertIn("REGISTRATION_SECRET=" + self.secret, self.logged())
        self.assertIn("-m 640 /dev/null /etc/wake/api.env.new", self.logged(), "the env file is not 0640")
        self.assertIn("mv -f /etc/wake/api.env.new /etc/wake/api.env", self.logged())

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

    def test_a_secret_with_a_newline_is_refused_before_the_host_is_touched(self):
        # REGISTRATION_SECRET=<value> is one line; a newline in the value writes a second one.
        r = run("wake-host-deploy", "env", env=dict(self.env, WAKE_REGISTRATION_SECRET="abc\ndef"))
        self.assertEqual(r.returncode, 1)
        self.assertIn("control character", r.stderr)
        self.assertFalse(self.logged(), "a host command ran before the secret was checked")

    def test_env_refuses_a_registration_secret_the_api_would_not_boot_with(self):
        # The API enforces a 32-character floor, so a shorter value writes a file that cannot start.
        r = run("wake-host-deploy", "env", env=dict(self.env, WAKE_REGISTRATION_SECRET="short-secret"))
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("32 characters", r.stderr)
        self.assertFalse(self.logged(), "a host command ran before the secret was checked")

    def test_env_refuses_a_password_that_the_db_step_would_refuse(self):
        # env built DATABASE_URL from any value, while db refused a non-alphanumeric one, so the
        # two steps could not both succeed with the same password.
        for bad in ("abc\ndef", "pass-word"):
            r = run("wake-host-deploy", "env", env=dict(self.env, WAKE_DB_PASSWORD=bad))
            self.assertEqual(r.returncode, 1, bad)
            self.assertIn("non-alphanumerics", r.stderr)
            self.assertFalse(self.logged(), "a host command ran before the password was checked")

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

    def test_shape_check_says_it_executes_the_gate(self):
        # exec_module runs the gate in this process: "nothing here runs a host command" was true of
        # hosts and false of the artifact.
        with open(os.path.join(OPS, "wake-shape-check"), encoding="utf-8") as f:
            body = f.read()
        self.assertIn("with your privileges", body)
        r = run("wake-shape-check", "--help", env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("usage: wake-shape-check", r.stdout)

    def test_a_managed_value_with_a_newline_is_refused_before_the_host_is_touched(self):
        # merge_env checked nothing: WAKE_MCP_ENABLED accepted "true\nINJECTED_EVIL=1" and the
        # second line went into api.env.
        bad = "true\nINJECTED_EVIL=1"
        r = run("wake-host-deploy", "env", env=dict(self.env, WAKE_MCP_ENABLED=bad))
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("control character", r.stderr)
        self.assertNotIn("INJECTED_EVIL", r.stdout + r.stderr)
        self.assertFalse(self.logged(), "a host command ran before the value was checked")

    def test_env_is_staged_so_a_failed_write_keeps_the_live_file(self):
        # The step truncated api.env with `install /dev/null` before the tee, so a write that
        # failed or was interrupted left the service with an empty environment file.
        r = run("wake-host-deploy", "env", env=self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        log = self.logged()
        staged = "install -o root -g wake -m 640 /dev/null /etc/wake/api.env.new"
        self.assertIn(staged, log, "the write is not staged beside the live file")
        self.assertIn("tee /etc/wake/api.env.new", log)
        self.assertNotIn("tee /etc/wake/api.env >", log, "the live file is written in place")
        self.assertLess(log.index(staged), log.index("tee /etc/wake/api.env.new"))
        self.assertLess(
            log.index("tee /etc/wake/api.env.new"),
            log.index("mv -f /etc/wake/api.env.new /etc/wake/api.env"),
            "the staged file must be renamed after it is written",
        )

    def test_install_stages_every_file_as_the_privileged_user(self):
        # /opt is root-owned and the operator is not root, so `cat > /opt/wake.index.mjs` was
        # refused with Permission denied and the install died before it started (#109). This stub
        # refuses exactly what the kernel refuses there; the fix writes through the privileged
        # user, so it never sends those shapes.
        strict = write(
            os.path.join(self.dir, "hostops-strict"),
            "#!/bin/sh\n"
            'case "$1" in\n'
            '  "cat > /opt/"*|"rm -f /opt/"*) echo "Permission denied" >&2; exit 1 ;;\n'
            "esac\n"
            "{ printf 'CMD: %s\\n' \"$1\"; cat; printf '\\n--\\n'; } >> \"$STUB_LOG\"\n",
            executable=True,
        )
        state = os.path.join(self.dir, "state")
        build = make_build(self.dir)
        record(state, build)
        env = dict(self.env, WAKE_HOSTOPS=strict, WAKE_DEPLOY_STATE=state, WAKE_NODE="/usr/bin/node")
        r = run("wake-host-deploy", "install", env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        log = self.logged()
        self.assertIn("sudo -n tee /opt/wake.index.mjs", log)
        self.assertNotIn("cat > /opt/wake.index.mjs", log)
        self.assertIn("&& sudo -n rm -f /opt/wake.index.mjs", log)

    def test_a_build_that_carries_no_migrations_never_runs_the_empty_glob(self):
        # A real shell fails `chmod 644 <dir>/*.sql` when the glob matches nothing, and that ran
        # after index.mjs had already been replaced: a build with no migrations reported FAILED
        # for a host it had just installed (#106). The stub fails that exact glob, as the host does.
        glob = write(
            os.path.join(self.dir, "hostops-noglob"),
            "#!/bin/sh\n"
            'case "$1" in\n'
            '  *"chmod 644 /opt/wake/migrations/*.sql"*)'
            " echo \"chmod: cannot access '/opt/wake/migrations/*.sql': No such file or directory\" >&2;"
            " exit 1 ;;\n"
            "esac\n"
            "{ printf 'CMD: %s\\n' \"$1\"; cat; printf '\\n--\\n'; } >> \"$STUB_LOG\"\n",
            executable=True,
        )
        state = os.path.join(self.dir, "state")
        build = make_build(self.dir)  # no migrations/ directory: the build carries none
        record(state, build)
        env = dict(self.env, WAKE_HOSTOPS=glob, WAKE_DEPLOY_STATE=state, WAKE_NODE="/usr/bin/node")
        r = run("wake-host-deploy", "install", env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("migrations: 0", r.stdout)
        self.assertNotIn("chmod 644 /opt/wake/migrations/*.sql", self.logged())

    def test_the_node_read_ignores_a_warning_on_stderr(self):
        # install looks the host's node up with `command -v node`; a warning on stderr was spliced
        # into the path handed to install(1), which then said "cannot stat" and stopped the deploy
        # on a host that had node all along (#129). Every other install test sets WAKE_NODE, so the
        # fallback branch had no test at all.
        noisy = write(
            os.path.join(self.dir, "hostops-node"),
            "#!/bin/sh\n"
            "{ printf 'CMD: %s\\n' \"$1\"; cat; printf '\\n--\\n'; } >> \"$STUB_LOG\"\n"
            'case "$1" in\n'
            "  'command -v node')\n"
            "      printf '/usr/bin/node\\n'\n"
            "      echo 'Warning: Permanently added example to the list of known hosts.' >&2 ;;\n"
            "esac\n"
            "exit 0\n",
            executable=True,
        )
        state = os.path.join(self.dir, "state")
        build = make_build(self.dir)
        record(state, build)
        r = run("wake-host-deploy", "install", env=dict(self.env, WAKE_HOSTOPS=noisy, WAKE_DEPLOY_STATE=state))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        log = self.logged()
        self.assertIn("install -m 755 /usr/bin/node /opt/wake/node", log)
        self.assertNotIn("Permanently added", log, log[:400])

    def test_the_installed_commit_is_a_value_not_a_block(self):
        # `status` prints the commit beside the installed file, and with stderr spliced in the
        # value an operator reads back carried the transport's own notice (#129).
        noisy = write(
            os.path.join(self.dir, "hostops-commit"),
            "#!/bin/sh\n"
            "{ printf 'CMD: %s\\n' \"$1\"; cat; printf '\\n--\\n'; } >> \"$STUB_LOG\"\n"
            'case "$1" in\n'
            "  'cat /opt/wake/index.commit 2>/dev/null || true')\n"
            "      printf 'abc1234\\n'\n"
            "      echo 'Warning: Permanently added example to the list of known hosts.' >&2 ;;\n"
            "esac\n"
            "exit 0\n",
            executable=True,
        )
        r = run("wake-host-deploy", "status", env=dict(self.env, WAKE_HOSTOPS=noisy))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("installed commit: abc1234", r.stdout)
        self.assertNotIn("Permanently added", r.stdout)

    def test_the_database_probes_are_values_not_blocks(self):
        # The two psql probes are compared and printed, never read by a human: a NOTICE on stderr
        # made `present` differ from "1", so createdb ran against an existing database, and the
        # owner line carried the notice (#129).
        noisy = write(
            os.path.join(self.dir, "hostops-psql"),
            "#!/bin/sh\n"
            "{ printf 'CMD: %s\\n' \"$1\"; cat; printf '\\n--\\n'; } >> \"$STUB_LOG\"\n"
            'case "$1" in\n'
            "  *'select 1 from pg_database'*)\n"
            "      printf '1\\n'\n"
            "      echo 'NOTICE: relation does not exist, skipping' >&2 ;;\n"
            "  *'pg_get_userbyid'*)\n"
            "      printf 'wake\\n'\n"
            "      echo 'NOTICE: relation does not exist, skipping' >&2 ;;\n"
            "esac\n"
            "exit 0\n",
            executable=True,
        )
        r = run("wake-host-deploy", "db", env=dict(self.env, WAKE_HOSTOPS=noisy))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("db: already present", r.stdout, "a spliced probe value ran createdb anyway")
        self.assertNotIn("createdb", self.logged())
        self.assertIn("owner: wake", r.stdout)
        self.assertNotIn("NOTICE", r.stdout)


class ScpTransportTestCase(unittest.TestCase):
    """The ssh transport stages in the operator's /tmp before install(1) moves the bytes.

    Every other deploy test sets WAKE_HOSTOPS, so the `scp` half of put() was never driven by the
    suite: the line that names the staging file could be reverted and the suite stayed green (#117).
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="scpstage-")
        self.bin = os.path.join(self.dir, "bin")
        os.makedirs(self.bin)
        self.log = os.path.join(self.dir, "host.log")
        self.env = {
            "PATH": self.bin + ":" + os.environ.get("PATH", "/usr/bin:/bin"),
            "WAKE_HOST_SSH": "ops@example",
            "WAKE_SUDO": "",  # the commands name their own user; sudo belongs to the real host
            "WAKE_NODE": "/usr/bin/node",
            "STUB_LOG": self.log,
        }
        self.ssh(warning=None)
        # scp records where it was told to write and the mode that path had before the write, then
        # writes through it the way scp does: an existing file keeps its mode.
        write(
            os.path.join(self.bin, "scp"),
            "#!/bin/sh\n"
            # The last two arguments are the local file and user@host:path; the rest are options.
            'for a in "$@"; do from=$to; to=$a; done\n'
            "dest=${to#*:}\n"
            'if [ "$(printf %s "$dest" | wc -l)" -gt 0 ]; then\n'
            '  printf "SCP REFUSED a staged path that carries a newline\\n" >> "$STUB_LOG"\n'
            "  exit 1\n"
            "fi\n"
            'mode=$(stat -c %a "$dest" 2>/dev/null || echo none)\n'
            'printf "SCP %s mode=%s file_mode=%s\\n" "$dest" "$mode" "$(stat -c %a "$from")" >> "$STUB_LOG"\n'
            'cp "$from" "$dest" && exit 0\n'
            "exit 1\n",
            executable=True,
        )

    def ssh(self, warning, silent=False):
        """A host that answers `mktemp` the way mktemp does: creates the file 0600 and prints it.

        `warning` is what ssh itself writes to stderr (a host-key notice), which the helper reads
        back together with stdout. `silent` is a transport that exits 0 and names no file — a
        wrapper that swallows `mktemp`'s failure. A `mktemp` that fails exits non-zero and
        `host()` stops the run before put() can ask it for a name.
        """
        body = "#!/bin/sh\nfor last; do :; done\n"
        if warning:
            body += 'printf "%s\\n" "$WAKE_SSH_WARNING" >&2\n'
        body += 'printf "SSH: %s\\n" "$last" >> "$STUB_LOG"\ncase "$last" in\n  "mktemp "*)\n'
        if not silent:
            body += (
                "      set -- $last; shift\n"
                '      p=$(mktemp "$1") || exit 1\n'
                '      chmod 600 "$p"\n'
                '      printf "%s\\n" "$p"\n'
            )
        body += "      ;;\n"
        # The fake host also runs the cleanup the real command carries: `install … && rm -f` is
        # logged only (never executed against /opt), and the trailing rm removes the staged file the
        # way the operator's host does. While the stub swallowed that chain, every run of this class
        # left a real /tmp/.wake-deploy.* file behind - a leak a green CI never sees (#129).
        body += '  *"rm -f "*) p=${last##*rm -f }; rm -f "$p" ;;\n'
        body += "esac\n"
        path = os.path.join(self.bin, "ssh")
        write(path, body, executable=True)
        if warning:
            self.env["WAKE_SSH_WARNING"] = warning
        return path

    def logged(self):
        with open(self.log, encoding="utf-8") as f:
            return f.read()

    def install(self, name):
        state = os.path.join(self.dir, name + "-state")
        build = make_build(os.path.join(self.dir, name))
        record(state, build)
        return run("wake-host-deploy", "install", env=dict(self.env, WAKE_DEPLOY_STATE=state))

    def run_install(self, env=None):
        state = os.path.join(self.dir, "state")
        build = make_build(os.path.join(self.dir, "build"))
        record(state, build)
        return run("wake-host-deploy", "install", env=dict(self.env, WAKE_DEPLOY_STATE=state, **(env or {})))

    def staged(self):
        return [line.split()[1] for line in self.logged().splitlines() if line.startswith("SCP ")]

    def test_the_staged_name_is_created_by_the_host_and_differs_every_time(self):
        # A staged name built from the pid and the basename is predictable: any local account on
        # the host can pre-create that path and have the operator's scp write through it (#117).
        for name in ("first", "second"):
            r = self.install(name)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        log = self.logged()
        paths = self.staged()
        self.assertEqual(len(paths), 2, log)
        self.assertNotEqual(paths[0], paths[1], "a staged name that repeats is guessable")
        for path in paths:
            self.assertNotRegex(path, r"/tmp/wake-put-", "the staged name is derived, not created")
        # Each file already existed, private, before scp wrote into it.
        for line in log.splitlines():
            if line.startswith("SCP "):
                self.assertIn("mode=600", line, line)
        # And install(1) moves the very file scp wrote. The mode on that intermediate is
        # incidental (root reads it, and the bundle the service runs is installed from it next),
        # so this pins the move, not the mode.
        self.assertRegex(log, rf"install -o root -g wake -m \d+ {re.escape(paths[0])} /opt/wake\.index\.mjs")
        # The mode that decides whether the service can read its bundle is on the destination.
        self.assertIn("install -o root -g wake -m 644 /opt/wake.index.mjs /opt/wake/index.mjs", log)

    def test_a_host_notice_on_stderr_never_becomes_part_of_the_staged_path(self):
        # host() returns stdout and stderr together, so a host-key notice is spliced into the name
        # scp is handed: it writes a file whose name holds the notice, install then fails with
        # "No such file or directory" and both staged files are left behind (#117).
        self.ssh(warning="Warning: Permanently added example to the list of known hosts.")
        r = self.install("noisy")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        log = self.logged()
        self.assertNotIn("REFUSED", log, "a staged path carried the host's stderr")
        self.assertNotIn("Warning", log, log[:400])
        self.assertEqual(len(self.staged()), 1, log)

    def test_a_refused_install_does_not_leave_the_staged_file_behind(self):
        # install and the cleanup were one `&&` chain, so a refused install left the operator's
        # copy of the bundle in the host's /tmp, 0600 and collected by nothing (#117).
        bindir = os.path.join(self.dir, "bin-fail")
        os.makedirs(bindir)
        write(
            os.path.join(bindir, "ssh"),
            "#!/bin/sh\n"
            "for last; do :; done\n"
            'printf "SSH: %s\\n" "$last" >> "$STUB_LOG"\n'
            'case "$last" in\n'
            '  "mktemp "*)\n'
            '      set -- $last; shift; p=$(mktemp "$1") || exit 1; chmod 600 "$p"; printf "%s\\n" "$p" ;;\n'
            '  "rm -f "*)\n'
            '      set -- $last; shift; rm -f "$@" ;;\n'
            '  "install -o root"*)\n'
            '      echo "install: cannot create regular file: Permission denied" >&2; exit 1 ;;\n'
            "esac\n",
            executable=True,
        )
        write(
            os.path.join(bindir, "scp"),
            '#!/bin/sh\nfor a in "$@"; do from=$to; to=$a; done\ncp "$from" "${to#*:}"\n',
            executable=True,
        )
        env = dict(self.env, PATH=bindir + ":" + os.environ.get("PATH", "/usr/bin:/bin"))
        r = self.run_install(env)
        self.assertNotEqual(r.returncode, 0, "a refused install reported success")
        log = self.logged()
        staged = re.search(r"SSH: install -o root -g wake -m \d+ (\S+) /opt/wake\.index\.mjs", log)
        self.assertIsNotNone(staged, log)
        self.assertFalse(os.path.exists(staged.group(1)), "the staged copy was left in /tmp")
        self.assertIn(f"SSH: rm -f {staged.group(1)}", log, "the cleanup never ran")

    def test_one_transport_decides_where_the_bytes_go(self):
        # put() picked scp from WAKE_HOST_SSH while host() preferred WAKE_HOSTOPS, so with both set
        # the bytes travelled a different route than every command around them (#117).
        runner = write(
            os.path.join(self.dir, "hostops-both"),
            "#!/bin/sh\n{ printf 'CMD: %s\\n' \"$1\"; cat; printf '\\n--\\n'; } >> \"$STUB_LOG\"\n",
            executable=True,
        )
        env = dict(self.env, WAKE_HOSTOPS=runner)
        r = self.run_install(env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        log = self.logged()
        self.assertNotIn("SCP ", log, "scp staged the bytes while every command went through the runner")
        self.assertIn("CMD: install -o root -g wake -m 644 /dev/null /opt/wake.index.mjs", log)

    def test_a_stderr_only_answer_is_not_a_staging_file(self):
        # The old read took the first line of stdout and stderr together, which is the same thing
        # until stdout is empty: a wrapper that warns and names nothing then handed scp the warning
        # as a path. Only a value read that ignores stderr stops here (#117).
        bindir = os.path.join(self.dir, "bin-warn")
        os.makedirs(bindir)
        write(
            os.path.join(bindir, "ssh"),
            "#!/bin/sh\n"
            "for last; do :; done\n"
            'printf "SSH: %s\\n" "$last" >> "$STUB_LOG"\n'
            'case "$last" in\n'
            '  "mktemp "*)'
            " echo 'Warning: Permanently added example to the list of known hosts.' >&2 ;;\n"
            "esac\n",
            executable=True,
        )
        write(
            os.path.join(bindir, "scp"),
            '#!/bin/sh\nfor a in "$@"; do from=$to; to=$a; done\nprintf "SCP %s\\n" "${to#*:}" >> "$STUB_LOG"\n',
            executable=True,
        )
        env = dict(self.env, PATH=bindir + ":" + os.environ.get("PATH", "/usr/bin:/bin"))
        r = self.run_install(env)
        self.assertNotEqual(r.returncode, 0, "a deploy went ahead with a warning as its path")
        self.assertIn("did not name a staging file", r.stdout + r.stderr)
        self.assertNotIn("SCP ", self.logged(), "scp was handed the warning as a path")

    def test_a_host_that_names_nothing_stops_before_scp(self):
        # A transport can exit 0 and name no file — a `WAKE_HOST_SSH` wrapper that swallows the
        # failure. scp would then be handed an empty path and write into the operator's own
        # directory, so put() has to stop first (#117).
        self.ssh(warning=None, silent=True)
        r = self.install("silent")
        self.assertNotEqual(r.returncode, 0, "a deploy went ahead with no staged file")
        self.assertIn("did not name a staging file", r.stdout + r.stderr)
        self.assertNotIn("SCP ", self.logged(), "scp was handed no staged path")

    def test_the_harness_removes_the_staging_file_it_created(self):
        # The fake host answered `mktemp` and swallowed `install … && rm -f`, so one run of this
        # class left a real /tmp/.wake-deploy.* behind for every install it drove (#129).
        r = self.install("leak")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        paths = self.staged()
        self.assertEqual(len(paths), 1, self.logged())
        self.assertFalse(os.path.exists(paths[0]), "the fake host left the staged file behind")

    def test_a_local_scp_that_is_missing_still_removes_the_staged_file(self):
        # subprocess.run raises FileNotFoundError when scp is not installed, which is not a
        # SystemExit: the cleanup never ran and the bundle stayed in the host's /tmp (#134).
        only = os.path.join(self.dir, "no-scp")
        os.makedirs(only)
        # A PATH with what the helper and the fake host need and no scp: too narrow and the helper's
        # own `#!/usr/bin/env python3` cannot start, which fails the test for the wrong reason.
        for tool in ("python3", "mktemp", "chmod", "rm", "cat"):
            real = shutil.which(tool)
            self.assertIsNotNone(real, f"{tool} is missing from this machine")
            os.symlink(real, os.path.join(only, tool))
        os.symlink(os.path.join(self.bin, "ssh"), os.path.join(only, "ssh"))
        r = self.run_install(env={"PATH": only})
        self.assertNotEqual(r.returncode, 0, "a missing scp reported success")
        self.assertIn("No such file or directory", r.stdout + r.stderr)
        cleaned = [line.split("rm -f ", 1)[1] for line in self.logged().splitlines() if "rm -f " in line]
        self.assertEqual(len(cleaned), 1, self.logged())
        self.assertFalse(os.path.exists(cleaned[0]), "the staged file was stranded when scp was missing")

    def test_a_failed_scp_does_not_leave_its_half_written_file_behind(self):
        # scp truncates the staged file before the transfer ends, so a connection that drops
        # mid-copy leaves half a bundle in the host's /tmp. put() checked scp's status and exited
        # before its cleanup, so nothing collected it (#129).
        write(
            os.path.join(self.bin, "scp"),
            "#!/bin/sh\n"
            'for a in "$@"; do from=$to; to=$a; done\n'
            "p=${to#*:}\n"
            'printf "half a bundle" > "$p"\n'
            'printf "SCP %s mode=written\\n" "$p" >> "$STUB_LOG"\n'
            'echo "lost connection" >&2\n'
            "exit 1\n",
            executable=True,
        )
        r = self.run_install()
        self.assertNotEqual(r.returncode, 0, "a failed scp reported success")
        self.assertIn("scp failed", r.stdout + r.stderr)
        paths = self.staged()
        self.assertEqual(len(paths), 1, self.logged())
        self.assertFalse(os.path.exists(paths[0]), "scp's half-written file was left in /tmp")
        self.assertIn(f"SSH: rm -f {paths[0]}", self.logged(), "the cleanup never ran")


class BuildStateTestCase(unittest.TestCase):
    """The files that remember a build: nobody else's directory, and a dry run never dies."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="state-")
        self.state = os.path.join(self.dir, "state")
        self.deploy = load_host_deploy()
        self.deploy.STATE_DIR = self.state

    def record(self, path):
        os.makedirs(self.state, exist_ok=True)
        with open(os.path.join(self.state, "last-build"), "w", encoding="utf-8") as f:
            f.write(path + "\n")

    def test_a_state_directory_it_creates_is_private(self):
        self.deploy.record_build(make_build(self.dir))
        self.assertEqual(oct(os.stat(self.state).st_mode & 0o777), "0o700")
        self.assertEqual(oct(os.stat(os.path.join(self.state, "last-build")).st_mode & 0o777), "0o600")

    def test_a_state_directory_it_did_not_create_is_refused_not_chmodded(self):
        # record_build chmodded whatever STATE_DIR named, so a shared directory this operator
        # owns but did not make - WAKE_DEPLOY_STATE=/tmp, or /srv/shared/state - came back 0700
        # and stopped working for everyone else using it.
        os.makedirs(self.state)
        os.chmod(self.state, 0o755)
        with self.assertRaises(SystemExit) as caught:
            self.deploy.record_build(make_build(self.dir))
        self.assertIn("not a private directory of yours", str(caught.exception))
        self.assertEqual(oct(os.stat(self.state).st_mode & 0o777), "0o755", "chmodded a directory it did not create")
        self.assertFalse(os.path.exists(os.path.join(self.state, "last-build")), "wrote a pointer into it anyway")

    def test_a_state_path_that_is_a_file_is_named(self):
        # STATE_DIR naming a regular file used to raise a raw FileExistsError traceback.
        with open(self.state, "w", encoding="utf-8") as f:
            f.write("not a directory\n")
        os.chmod(self.state, 0o600)
        with self.assertRaises(SystemExit) as caught:
            self.deploy.record_build(make_build(self.dir))
        self.assertIn("is not a directory", str(caught.exception))
        self.assertEqual(oct(os.stat(self.state).st_mode & 0o777), "0o600", "chmodded a file it did not create")

    def test_a_dry_run_names_a_stale_build_instead_of_dying(self):
        gone = os.path.join(self.dir, "gone")
        self.record(gone)
        self.deploy.DRY = True
        note = io.StringIO()
        with contextlib.redirect_stderr(note):
            got = self.deploy.build_dir()
        self.assertEqual(got, gone, "the plan stops naming the build the operator recorded")
        self.assertIn("a real run stops here", note.getvalue())

    def test_a_stale_build_still_stops_a_real_install(self):
        self.record(os.path.join(self.dir, "gone"))
        self.deploy.DRY = False
        with self.assertRaises(SystemExit) as caught:
            self.deploy.build_dir()
        self.assertIn("is gone: run the build step again", str(caught.exception))


class NoBytecodeInTheTreeTests(unittest.TestCase):
    """A loader import wrote `bin/ops/__pycache__` into the working tree.

    A clean checkout then carries an untracked directory, and anything that lists
    `bin/ops` to find the helpers sees an entry that is not one.
    """

    def bytecode(self, directory):
        found = set()
        for root, _dirs, files in os.walk(directory):
            found.update(os.path.join(root, name) for name in files if name.endswith((".pyc", ".pyo")))
        return found

    def test_loading_the_helper_writes_no_bytecode(self):
        before = self.bytecode(OPS)
        load_host_deploy()
        self.assertEqual(self.bytecode(OPS) - before, set(), "loading the helper wrote bytecode into bin/ops")

    def test_the_shape_check_writes_no_bytecode_beside_the_gate(self):
        gate_dir = tempfile.mkdtemp(prefix="gate-")
        gate = write(os.path.join(gate_dir, "gate.py"), "ROUTES = []\n\n\ndef match(command):\n    return None\n")
        load_source("wake_shape_check", os.path.join(OPS, "wake-shape-check")).load_gate(gate)
        self.assertEqual(sorted(os.listdir(gate_dir)), ["gate.py"], "loading the gate wrote beside it")


BUILDER_STUB = """#!/bin/sh
for a in "$@"; do
  case "$a" in --outfile=*) out="${a#--outfile=}" ;; esac
done
printf 'export const built = 1\\n' > "$out"
"""

# A builder that commits while it runs: the artifact then belongs to no commit anyone recorded.
SHIFTY_BUILDER_STUB = """#!/bin/sh
printf 'serve(); // shifted during the build\\n' > apps/api/src/index.ts
git add -A
git -c user.name=t -c user.email=t@example.invalid commit -q -m 'shifted during the build'
for a in "$@"; do
  case "$a" in --outfile=*) out="${a#--outfile=}" ;; esac
done
printf 'export const built = 1\\n' > "$out"
"""


class BuildRefTestCase(unittest.TestCase):
    """`build` shipped whatever the shared clone had checked out, and never said which ref.

    Measured for #75: the clone sat on an unmerged pull request's tree and `install` would
    have put that tree on the host without recording it. These tests drive the real `git` in
    a throwaway repository, because the guard is about what the checkout actually is.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="buildref-")
        self.log = os.path.join(self.dir, "host.log")
        self.hostops = write(
            os.path.join(self.dir, "hostops"),
            "#!/bin/sh\n{\n  printf 'CMD: %s\\n' \"$1\"\n  cat\n  printf '\\n--\\n'\n} >> \"$STUB_LOG\"\n",
            executable=True,
        )
        self.builder = write(os.path.join(self.dir, "builder"), BUILDER_STUB, executable=True)
        self.repo = os.path.join(self.dir, "repo")
        os.makedirs(os.path.join(self.repo, "apps", "api", "src"))
        write(os.path.join(self.repo, "apps", "api", "src", "index.ts"), "serve();\n")
        self.git("-c", "init.defaultBranch=main", "init", "-q")
        self.git("add", "-A")
        self.git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "one")
        self.main = self.git("rev-parse", "HEAD")
        # The remote-tracking ref a real clone has; `git fetch` has no remote here, which the
        # build reports and then compares against this one.
        self.git("update-ref", "refs/remotes/origin/main", self.main)
        self.env = {
            "WAKE_HOSTOPS": self.hostops,
            "STUB_LOG": self.log,
            "WAKE_REPO": self.repo,
            "WAKE_BUILDER": self.builder,
            "WAKE_DEPLOY_STATE": os.path.join(self.dir, "state"),
        }

    def git(self, *args):
        r = subprocess.run(["git", "-C", self.repo, *args], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def logged(self):
        try:
            with open(self.log, encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return ""

    def commit(self, body, message):
        write(os.path.join(self.repo, "apps", "api", "src", "index.ts"), body)
        self.git("add", "-A")
        self.git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def test_a_dirty_checkout_is_refused_and_names_the_file(self):
        # The built bytes would be a commit plus edits nobody can name afterwards.
        write(os.path.join(self.repo, "apps", "api", "src", "index.ts"), "serve(); // edited\n")
        r = run("wake-host-deploy", "build", env=self.env)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("has uncommitted changes", r.stderr)
        self.assertIn("apps/api/src/index.ts", r.stderr)
        self.assertNotIn("size:", r.stdout, "the builder ran on a tree that is not a commit")

    def test_a_head_that_is_not_origin_main_is_refused_with_both_ids(self):
        self.git("checkout", "-q", "-b", "feature")
        head = self.commit("serve(); // feature\n", "feature")
        r = run("wake-host-deploy", "build", env=self.env)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(head[:12], r.stderr, "the refusal must name the commit it found")
        self.assertIn(self.main[:12], r.stderr, "the refusal must name origin/main")

    def test_the_override_builds_another_commit_and_says_so(self):
        self.git("checkout", "-q", "-b", "hotfix")
        head = self.commit("serve(); // hotfix\n", "hotfix")
        r = run("wake-host-deploy", "build", env=dict(self.env, WAKE_ALLOW_ANY_REF="1"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(head, r.stdout, "the artifact must carry the commit it was built from")
        self.assertIn("WAKE_ALLOW_ANY_REF", r.stderr, "an overridden build must say what it did")

    def test_the_override_is_off_when_it_says_off(self):
        # It was a blacklist: "off" was not in it, so the documented default turned the override on.
        self.git("checkout", "-q", "-b", "hotfix")
        head = self.commit("serve(); // hotfix\n", "hotfix")
        for value in ("off", "OFF", "none", "disabled", "2"):
            r = run("wake-host-deploy", "build", env=dict(self.env, WAKE_ALLOW_ANY_REF=value))
            self.assertEqual(r.returncode, 1, f"{value!r}: {r.stdout}{r.stderr}")
            self.assertIn(head[:12], r.stderr, f"{value!r} did not name the commit it refused")
            self.assertNotIn("size:", r.stdout, f"{value!r} built anyway")

    def test_untracked_work_hidden_by_a_gitconfig_is_still_refused(self):
        # `git status --porcelain` honours status.showUntrackedFiles=no, so untracked work
        # disappeared from the check and the build shipped it under main's name.
        write(os.path.join(self.repo, "apps", "api", "src", "evil.ts"), "evil();\n")
        home = tempfile.mkdtemp(prefix="gitconfig-home-")
        write(os.path.join(home, ".gitconfig"), "[status]\n\tshowUntrackedFiles = no\n")
        r = run("wake-host-deploy", "build", env=dict(self.env, HOME=home))
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("evil.ts", r.stderr, "the hidden file is not named")
        self.assertNotIn("size:", r.stdout)

    def test_a_tree_that_moves_while_it_is_built_is_not_recorded(self):
        # build_ref read HEAD, the builder ran, then the commit was recorded: nothing looked in
        # between, so a builder that commits leaves an artifact stamped with the wrong commit.
        self.builder = write(os.path.join(self.dir, "shifty-builder"), SHIFTY_BUILDER_STUB, executable=True)
        env = dict(self.env, WAKE_BUILDER=self.builder, TMPDIR=self.dir)
        r = run("wake-host-deploy", "build", env=env)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("moved from", r.stderr)
        self.assertIn(self.main[:12], r.stderr, "the refusal must name the commit it started from")
        moved = self.git("rev-parse", "HEAD")
        self.assertIn(moved[:12], r.stderr, "the refusal must name the commit it ended on")
        self.assertNotIn("size:", r.stdout)
        self.assertFalse(
            os.path.exists(os.path.join(env["WAKE_DEPLOY_STATE"], "last-build")),
            "a moved tree was recorded anyway",
        )
        self.assertEqual(
            [n for n in os.listdir(self.dir) if n.startswith("wake-build-") and n != "wake-build-mine"],
            [],
            "the half-built artifact was left in the temp directory",
        )

    def test_install_ships_the_migrations_the_build_carried(self):
        # install printed the build's commit and then tarred the live checkout: an uncommitted
        # .sql added after the build ran against the live database at the next boot.
        migrations = os.path.join(self.repo, "apps", "api", "src", "migrations")
        os.makedirs(migrations)
        write(os.path.join(migrations, "0001_built.sql"), "select 1;\n")
        self.git("add", "-A")
        self.git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "migrations")
        self.main = self.git("rev-parse", "HEAD")
        self.git("update-ref", "refs/remotes/origin/main", self.main)
        r = run("wake-host-deploy", "build", env=self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("migrations: 1", r.stdout)
        # The checkout moves on after the build: one more migration, and the built one edited.
        write(os.path.join(migrations, "0002_uncommitted.sql"), "drop table users;\n")
        write(os.path.join(migrations, "0001_built.sql"), "drop table users; -- edited\n")
        env = dict(self.env, WAKE_NODE="/usr/bin/node")
        r = run("wake-host-deploy", "install", env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("install: commit " + self.main, r.stdout)
        log = self.logged()
        self.assertIn('case " 0001_built.sql " in', log, "the build's migration is not the one shipped")
        payload = log.split("base64 -d | sudo -n tar -x --no-same-owner -C /opt/wake/migrations", 1)[1]
        payload = payload.split("\n--\n", 1)[0]
        with tarfile.open(fileobj=io.BytesIO(base64.b64decode(payload.strip()))) as tar:
            self.assertEqual(sorted(tar.getnames()), ["0001_built.sql"], "an uncommitted migration was shipped")
            self.assertEqual(tar.extractfile("0001_built.sql").read(), b"select 1;\n", "the edited bytes were shipped")

    def test_a_build_with_no_migrations_never_globs_the_migrations_directory(self):
        # #106: the host's migrations directory is empty on a first install, so a glob there
        # stays a literal, chmod exits 1, and install says FAILED after index.mjs landed.
        r = run("wake-host-deploy", "build", env=self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("migrations: 0", r.stdout)
        r = run("wake-host-deploy", "install", env=dict(self.env, WAKE_NODE="/usr/bin/node"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("migrations: 0 (this build carries none", r.stdout)
        log = self.logged()
        self.assertNotIn("*.sql", log, "install still globs a directory it did not fill")
        self.assertIn(
            "chown -R root:wake /opt/wake/migrations", log, "the directory is still handed to the service group"
        )
        self.assertNotIn("chmod", log, "install chmods files that are not there")

    def test_the_shipped_migrations_are_chmodded_by_name(self):
        # The positive control for #106: with migrations carried, each one is named, so a
        # directory that holds something else cannot be reached by the same command.
        migrations = os.path.join(self.repo, "apps", "api", "src", "migrations")
        os.makedirs(migrations)
        write(os.path.join(migrations, "0001_built.sql"), "select 1;\n")
        self.git("add", "-A")
        self.git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "migrations")
        self.main = self.git("rev-parse", "HEAD")
        self.git("update-ref", "refs/remotes/origin/main", self.main)
        r = run("wake-host-deploy", "build", env=self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r = run("wake-host-deploy", "install", env=dict(self.env, WAKE_NODE="/usr/bin/node"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        log = self.logged()
        self.assertIn("chmod 644 0001_built.sql", log)
        self.assertNotIn("chmod 644 /opt/wake/migrations/*.sql", log)

    def test_main_is_built_and_the_commit_reaches_install_and_status(self):
        r = run("wake-host-deploy", "build", env=self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("commit: " + self.main, r.stdout)
        env = dict(self.env, WAKE_NODE="/usr/bin/node")
        r = run("wake-host-deploy", "install", env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("install: commit " + self.main, r.stdout)
        # The commit is written beside the installed file, so status can answer about the host
        # rather than about what this checkout last built.
        log = self.logged()
        self.assertIn("sudo -n tee /opt/wake.commit", log)
        self.assertIn(self.main, log.split("sudo -n tee /opt/wake.commit", 1)[1].split("\n--\n", 1)[0])
        self.assertIn(
            "install -o root -g wake -m 644 /opt/wake.commit /opt/wake/index.commit", log, "the record is not installed"
        )

    def test_a_non_root_operator_stages_and_cleans_up_through_sudo(self):
        # #109: /opt is root-owned, so an unprivileged `cat >` there is a permission error and an
        # unprivileged `rm` cannot remove what install(1) placed. Both must go through priv().
        r = run("wake-host-deploy", "build", env=self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r = run("wake-host-deploy", "install", env=dict(self.env, WAKE_NODE="/usr/bin/node"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        log = self.logged()
        for needed in (
            "sudo -n install -o root -g wake -m 644 /dev/null /opt/wake.index.mjs",
            "sudo -n tee /opt/wake.index.mjs",
            "sudo -n rm -f /opt/wake.index.mjs",
            "sudo -n install -o root -g wake -m 644 /dev/null /opt/wake.commit",
            "sudo -n rm -f /opt/wake.commit",
        ):
            self.assertIn(needed, log)
        self.assertNotIn("cat > /opt/wake", log, "a staging file is written without sudo")
        self.assertNotIn("&& rm -f /opt/wake", log, "the cleanup is not privileged")

    def test_the_root_operator_gets_no_sudo_prefix(self):
        # WAKE_SUDO="" is the root case; priv() has to disappear rather than emit a bare space.
        r = run("wake-host-deploy", "build", env=self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r = run("wake-host-deploy", "install", env=dict(self.env, WAKE_NODE="/usr/bin/node", WAKE_SUDO=""))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        log = self.logged()
        self.assertIn("install -o root -g wake -m 644 /dev/null /opt/wake.index.mjs", log)
        self.assertIn("tee /opt/wake.index.mjs", log)
        self.assertNotIn("sudo", log)

    def test_a_dry_run_prints_the_staging_step_without_touching_the_host(self):
        r = run("wake-host-deploy", "build", env=self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r = run("wake-host-deploy", "install", "--dry-run", env=dict(self.env, WAKE_NODE="/usr/bin/node"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("put ", r.stdout)
        self.assertEqual(self.logged(), "", "a dry run wrote to the host")

    def test_status_prints_the_commit_beside_the_installed_file(self):
        # The old line printed the last build's commit from the state directory: build B after
        # installing A said B while the host ran A. This reads the host instead.
        state = os.path.join(self.dir, "state")
        build = make_build(self.dir)
        record(state, build)
        answering = write(
            os.path.join(self.dir, "hostops-answer"),
            '#!/bin/sh\ncase "$1" in *index.commit*) printf \'%s\\n\' "$STUB_COMMIT" ;; esac\nexit 0\n',
            executable=True,
        )
        env = dict(self.env, WAKE_HOSTOPS=answering, STUB_COMMIT="cafe1234", WAKE_DEPLOY_STATE=state)
        r = run("wake-host-deploy", "status", env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("installed commit: cafe1234", r.stdout)
        self.assertNotIn("build commit:", r.stdout, "status still answers about the checkout")

    def test_a_checkout_that_is_not_git_is_refused_unless_overridden(self):
        # A tarball or a copied tree has no ref at all, so the build cannot be named.
        plain = os.path.join(self.dir, "plain")
        os.makedirs(os.path.join(plain, "apps", "api", "src"))
        write(os.path.join(plain, "apps", "api", "src", "index.ts"), "serve();\n")
        env = dict(self.env, WAKE_REPO=plain)
        r = run("wake-host-deploy", "build", env=env)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("not a git checkout", r.stderr)
        self.assertIn("WAKE_ALLOW_ANY_REF", r.stderr, "the refusal must name the way out")
        r = run("wake-host-deploy", "build", env=dict(env, WAKE_ALLOW_ANY_REF="1"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
