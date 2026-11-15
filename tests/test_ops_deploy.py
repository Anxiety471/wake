"""Behaviour tests for the four helpers that touch a copy of wake or a host.

The helpers ship code only: nothing here reaches a network, a database or a real
host. `ssh`, the host runner and the builder are stubs that record what they were
asked to do, so a remote install and a service deploy are both proven end to end
without one — and a secret is proven to travel through stdin, never through stdout
or a command line.
"""

import base64
import io
import os
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
        record(state, build)
        checkout = os.path.join(self.dir, "checkout")
        migrations = os.path.join(checkout, "apps", "api", "src", "migrations")
        os.makedirs(migrations)
        for name in ("001_init.sql", "002_threads.sql"):
            with open(os.path.join(migrations, name), "w", encoding="utf-8") as f:
                f.write("select 1;\n")
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
        record(state, build)
        checkout = os.path.join(self.dir, "checkout")
        migrations = os.path.join(checkout, "apps", "api", "src", "migrations")
        os.makedirs(migrations)
        with open(os.path.join(migrations, "a b.sql"), "w", encoding="utf-8") as f:
            f.write("select 1;\n")
        env = dict(self.env, WAKE_DEPLOY_STATE=state, WAKE_NODE="/usr/bin/node", WAKE_REPO=checkout)
        r = run("wake-host-deploy", "install", env=env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("not a migration name", r.stderr)

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

    def test_a_secret_with_a_newline_is_refused_before_the_host_is_touched(self):
        # REGISTRATION_SECRET=<value> is one line; a newline in the value writes a second one.
        r = run("wake-host-deploy", "env", env=dict(self.env, WAKE_REGISTRATION_SECRET="abc\ndef"))
        self.assertEqual(r.returncode, 1)
        self.assertIn("control character", r.stderr)
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


if __name__ == "__main__":
    unittest.main()
