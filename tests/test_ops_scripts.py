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
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OPS = os.path.join(ROOT, "bin", "ops")
NAMES = sorted(n for n in os.listdir(OPS) if not n.endswith(".md") and os.path.isfile(os.path.join(OPS, n)))


def shebang(name):
    with open(os.path.join(OPS, name), encoding="utf-8") as f:
        return f.readline().strip()


def shell(name):
    """The helper is a shell script, so shellcheck applies to it."""
    return "bash" in shebang(name) or shebang(name).endswith("/sh")


DOCS = (os.path.join(OPS, "README.md"), os.path.join(ROOT, "config.example.env"))

# Paths and names that belong to one operator's machine, never to this repo.
PERSONAL = ("/home/", "/root/", "/Users/", "/tmp/pgbuild", "rakazo", "hitori", "Anxiety471")

# A real hostname, which CONTRIBUTING keeps out of these files the same way it keeps out an IP.
# The TLD list is deliberately short: a placeholder (example.com) is allowed, a live name is not.
HOSTNAME = re.compile(
    r"\b(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+"
    r"(?:com|net|org|io|dev|app|co|me|xyz|cloud|trade|internal|local|lan|corp|host|site|info|biz|gov|edu)\b"
)


def text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def read(name):
    return text(os.path.join(OPS, name))


def run(name, *args, env=None):
    """Run one helper with a scrubbed environment: never the caller's real config."""
    base = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": tempfile.mkdtemp(prefix="ops-home-"),
        # A machine that has /etc/wake/config.env must not decide what a helper resolves here.
        "WAKE_SYSTEM_CONFIG": "",
    }
    base.update(env or {})
    return subprocess.run([os.path.join(OPS, name), *args], capture_output=True, text=True, env=base, timeout=60)


def e2e_path(openssl):
    """A PATH holding what wake-api-e2e checks for, with openssl absent, silent, or real.

    The real tools are symlinked rather than faked, because the script under test uses some of
    them (mktemp, rm) before it reaches its own checks; bun, jq and curl only have to exist.
    """
    home = tempfile.mkdtemp(prefix="ops-path-")
    bindir = os.path.join(home, "bin")
    os.makedirs(bindir)
    for tool in ("dirname", "mktemp", "rm"):
        real = shutil.which(tool)
        assert real is not None, f"{tool} is missing from this machine"
        os.symlink(real, os.path.join(bindir, tool))
    os.symlink(sys.executable, os.path.join(bindir, "python3"))
    # bun, jq and curl only have to exist; wake-pg is checked before the secret is minted, so
    # the silent-openssl case needs it on the PATH too, and it is never run here.
    for tool in ("bun", "jq", "curl", "wake-pg"):
        path = os.path.join(bindir, tool)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\nexit 0\n")
        os.chmod(path, 0o755)
    if openssl == "silent":
        path = os.path.join(bindir, "openssl")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\nexit 0\n")
        os.chmod(path, 0o755)
    elif openssl == "real" and shutil.which("openssl") is not None:
        os.symlink(shutil.which("openssl"), os.path.join(bindir, "openssl"))
    return bindir


def fake_wake(home):
    """A stand-in `wake` on PATH, so a test never reaches the real CLI and can see the asks."""
    bindir = os.path.join(home, "bin")
    os.makedirs(bindir, exist_ok=True)
    calls = os.path.join(home, "calls")
    path = os.path.join(bindir, "wake")
    with open(path, "w", encoding="utf-8") as f:
        f.write("#!/bin/sh\nprintf '%s\\n' \"$*\" >> " + shlex.quote(calls) + "\nexit 0\n")
    os.chmod(path, 0o755)
    return bindir, calls


class OpsFilesTestCase(unittest.TestCase):
    def test_the_directory_is_not_empty(self):
        self.assertEqual(
            NAMES,
            [
                "wake-api-e2e",
                "wake-deploy",
                "wake-gate-check",
                "wake-host-deploy",
                "wake-keys",
                "wake-pg",
                "wake-pingall",
                "wake-shape-check",
                "wake-wait",
            ],
        )

    def test_every_script_parses(self):
        for name in NAMES:
            path = os.path.join(OPS, name)
            with self.subTest(name=name):
                if shell(name):
                    r = subprocess.run(["bash", "-n", path], capture_output=True, text=True)
                    self.assertEqual(r.returncode, 0, r.stderr)
                else:
                    with open(path, encoding="utf-8") as f:
                        compile(f.read(), path, "exec")

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

    def test_the_e2e_bridge_step_does_not_count_tools(self):
        # The bridge appends wake_fleet whenever this machine's registry is
        # readable, so an exact count is true only on a bare runner; the step
        # failed for a correct bridge on the operator's box (issue #77). CI never
        # runs this script (it needs Postgres and bun), so pin the shape here.
        self.assertNotRegex(read("wake-api-e2e"), r"\.\[1\]\.result\.tools\s*\|\s*length")

    def test_the_e2e_bridge_step_reads_the_tool_names(self):
        # The negative pin above names one shape. These are the shapes that would slip past
        # it (#81): `.[-2].result.tools | length`, `map(.name) | length == 10`, or a rewrite
        # that re-indexes the replies. What the step has to do is read the names and compare
        # them to the ten the server ships, so that is what is asserted here.
        script = read("wake-api-e2e")
        self.assertRegex(
            script,
            r"\[\.\[1\]\.result\.tools\[\]\.name\]\s*as\s*\$n",
            "the bridge step no longer collects the tool names",
        )
        self.assertIn('["wake_ask","wake_reply"', script, "the expected name list is gone")

    def test_the_e2e_script_refuses_a_missing_checkout(self):
        r = run("wake-api-e2e", env={"WAKE_REPO": tempfile.mkdtemp(prefix="not-a-checkout-")})
        self.assertEqual(r.returncode, 1)
        self.assertIn("no wake checkout", r.stderr)
        self.assertNotIn("ALL PASS", r.stdout)

    def test_the_e2e_script_refuses_a_short_secret_before_starting_anything(self):
        # The API exits 1 on a short secret, and the health wait that followed spent 20
        # seconds before it said so. The length is checked before Postgres is started, and
        # before the dependency check: the PATH is the one e2e_path builds, not the developer's,
        # so this passes on a box with bun installed and on a runner with none, which is where
        # the ordering was found to be wrong.
        r = run("wake-api-e2e", env={"WAKE_E2E_SECRET": "a" * 31, "PATH": e2e_path("real")})
        self.assertEqual(r.returncode, 1)
        self.assertIn("at least 32", r.stderr)
        self.assertNotIn("ALL PASS", r.stdout)

    def test_the_e2e_script_names_a_missing_openssl_instead_of_blaming_the_secret(self):
        # The secret was minted above the dependency check, so a host without openssl died with
        # a bare "openssl: command not found" and exit 127, and one that printed nothing carried
        # on to the length check as "WAKE_E2E_SECRET is 0 characters" - blaming a variable the
        # caller never set. Both are the missing or silent tool's fault, and the run must say so.
        cases = (
            ("absent", "openssl not found on PATH", "WAKE_E2E_SECRET is"),
            ("silent", "openssl rand -hex 24 printed 0 characters", None),
        )
        for openssl, expected, absent in cases:
            with self.subTest(openssl=openssl):
                r = run("wake-api-e2e", env={"PATH": e2e_path(openssl)})
                self.assertEqual(r.returncode, 1, r.stderr)
                self.assertIn(expected, r.stderr)
                if absent is not None:
                    self.assertNotIn(absent, r.stderr)
                self.assertNotIn("ALL PASS", r.stdout)

    def test_the_registry_default_does_not_follow_the_key_dir(self):
        # bin/wake_config.py derives the registry from WAKE_CONFIG_DIR. A helper that put it under
        # WAKE_KEY_DIR said "no registry" while `wake --list` listed the bots fine.
        for name in ("wake-keys", "wake-pingall"):
            with self.subTest(name=name):
                self.assertIn("REG=${WAKE_REGISTRY:-$CFG/bots.tsv}", read(name))

    def test_the_e2e_script_keeps_its_files_in_a_private_temp_dir(self):
        body = read("wake-api-e2e")
        self.assertIn("umask 077", body, "files must be private from the moment they are created")
        self.assertIn("mktemp -d", body, "each run needs its own directory")
        self.assertRegex(body, r'(?m)^  rm -rf "\$WORK"$', "the directory must be removed on exit")
        self.assertNotRegex(
            body, r"/tmp/e2e-", "a fixed /tmp name is back: world-readable, never removed, a symlink target"
        )
        self.assertNotRegex(body, r"/tmp/wake-e2e-", "a fixed /tmp name is back")

    def test_no_file_carries_a_real_hostname(self):
        # Placeholders are fine (docs use bots.example.com); a live-looking name is not.
        allowed = (".example.com", ".example.net", ".example.org")
        exact = {"example.com", "example.net", "example.org", "semver.org"}
        paths = [os.path.join(OPS, n) for n in NAMES] + [os.path.join(OPS, "README.md")] + list(DOCS)
        for path in paths:
            body = text(path)
            for hit in sorted({m.group(0) for m in HOSTNAME.finditer(body)}):
                with self.subTest(path=os.path.basename(path), hit=hit):
                    self.assertTrue(
                        hit in exact or hit.endswith(allowed),
                        f"{os.path.basename(path)} carries the hostname {hit!r}",
                    )

    def test_the_suite_runner_keeps_bytecode_out_of_the_tree(self):
        """bin/wake imports bin/wake_config.py, so a suite run would otherwise cache bytecode in
        the checkout; tests/test_wake.py passes the same setting to every child it spawns.
        """
        with open(os.path.join(ROOT, "tests", "run.sh"), encoding="utf-8") as f:
            runner = f.read()
        self.assertIn("PYTHONDONTWRITEBYTECODE=1", runner)

    def test_ci_runs_the_suite_as_a_background_job(self):
        """#142: a foreground-only suite cannot see a test that depends on the shell's SIGINT
        disposition - deleting the interrupt test's preexec_fn stayed green. The job pinned here is
        the only runner that reproduces a backgrounded suite, so losing it has to fail a test.
        """
        lines = text(os.path.join(ROOT, ".github/workflows/ci.yml")).splitlines()
        self.assertIn("  backgrounded:", lines, "the backgrounded suite job is gone")
        start = lines.index("  backgrounded:")
        block = []
        for line in lines[start + 1 :]:
            if line.startswith("  ") and not line.startswith("   ") and line.rstrip().endswith(":"):
                break
            block.append(line)
        commands = [line.strip() for line in block]
        self.assertIn("tests/run.sh &", commands, "the job no longer starts the suite as a background job")
        self.assertIn("wait $!", commands, "a bare wait reports zero for a failed suite; wait on the pid")
        self.assertNotIn("wait", commands, "a bare wait reports zero for a failed suite; wait on the pid")
        # The job has to be able to fail. continue-on-error makes GitHub report a failed suite as
        # neutral and `if` skips the job outright, so a one-line edit could re-create the
        # blindness #142 was filed about while this test stayed green (#152).
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
            ["name", "runs-on", "steps"],
            f"job keys {keys}: only name, runs-on and steps are allowed - continue-on-error or if "
            "would let a failed suite report green",
        )
        # A step is as easy to disarm as the job, and neither key touches the job's list: a
        # step-level continue-on-error makes GitHub report the failed step as neutral, and a false
        # `if` skips the step, so the suite never runs while this test stays green (measured by
        # mutation at c106c8c, #152 review). The key also parses off the step's dash line
        # (`- if: false` above name:, measured by mutation at 814de56) and behind a YAML anchor
        # (`- &a if: false`, measured at 7c4cecd), so the optional dash and the anchor in front
        # of the key are part of the shape: the step indent, the dash line and the anchored key
        # are all refused.
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
            f"a step key that can disarm this check was added: {disarming} - the suite would fail "
            "or be skipped and the job would still report green",
        )

    def test_the_hostname_guard_is_not_a_no_op(self):
        for sample in ("bots.example.com", "vps01.example.com", "wake.internal", "h.some-hospital.example.org"):
            with self.subTest(sample=sample):
                self.assertRegex(sample, HOSTNAME)

    def test_ci_and_readme_shellcheck_every_bash_helper(self):
        # One line in ci.yml and the same line in the README, finding the bash helpers by their
        # shebang: the hand-kept list it replaced dropped wake-wait the moment one was added.
        ci = [
            line for line in text(os.path.join(ROOT, ".github/workflows/ci.yml")).splitlines() if "shellcheck " in line
        ]
        dev = [
            line
            for line in text(os.path.join(ROOT, "README.md")).splitlines()
            if line.strip().startswith("shellcheck ")
        ]
        self.assertEqual(len(ci), 1, "expected one shellcheck line in ci.yml")
        self.assertEqual(len(dev), 1, "expected one shellcheck line in README.md")
        self.assertEqual(
            ci[0].split("shellcheck", 1)[1].strip(),
            dev[0].split("shellcheck", 1)[1].strip(),
            "ci.yml and README name different targets",
        )
        self.assertIn("grep -lE", ci[0], "a hand-kept target list is back: filter by shebang instead")
        picked = subprocess.run(
            ["bash", "-c", "grep -lE '^#!/(usr/)?bin/(env )?(ba)?sh' bin/ops/wake-* bin/wake*"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(picked.returncode, 0, picked.stderr)
        found = sorted(p for p in picked.stdout.split() if p.startswith("bin/ops/"))
        self.assertEqual(
            sorted("bin/ops/" + name for name in NAMES if shell(name)),
            found,
            "the shebang filter and the bash helpers disagree",
        )
        for name in NAMES:
            if shell(name):
                continue
            with self.subTest(name=name):
                self.assertNotIn("bin/ops/" + name, found, f"{name} is Python, so shellcheck would fail on it")


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

    def test_wake_pingall_skips_a_bad_alias_and_still_pings_the_rest(self):
        # An alias becomes a file name, so ../x must not run - but one bad alias must not hold the
        # batch hostage either. It is named and skipped, the rest go out, and the exit code says one
        # was refused.
        home = tempfile.mkdtemp(prefix="ping-")
        bindir = os.path.join(home, "bin")
        os.makedirs(bindir)
        asks = os.path.join(home, "asks")
        fake = os.path.join(bindir, "wake")
        with open(fake, "w", encoding="utf-8") as f:
            f.write('#!/bin/sh\nprintf "%s\\n" "$*" >> ' + shlex.quote(asks) + "\n")
        os.chmod(fake, 0o755)
        logs = os.path.join(home, "logs")
        env = {
            "PATH": bindir + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"),
            "WAKE_PING_LOG_DIR": logs,
        }
        r = run("wake-pingall", "bad.alias", "good-bot", env=env)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("bad.alias", r.stderr)
        self.assertIn("skipped", r.stderr)
        self.assertIn("started: good-bot", r.stdout)
        self.assertNotIn("bad.alias", r.stdout)
        self.assertEqual(sorted(os.listdir(logs)), ["good-bot.log"])
        for _ in range(40):  # the asks are started in the background
            if os.path.exists(asks):
                break
            time.sleep(0.05)
        with open(asks, encoding="utf-8") as f:
            sent = f.read()
        self.assertIn("--ask good-bot", sent)
        self.assertNotIn("bad.alias", sent)

    def test_wake_pingall_refuses_an_empty_registry(self):
        home = tempfile.mkdtemp(prefix="ping-")
        reg = os.path.join(home, "bots.tsv")
        with open(reg, "w", encoding="utf-8") as f:
            f.write("vault\tbot-vault-0008\tVault\tprivate\tnotes\n")
        r = run("wake-pingall", env={"WAKE_REGISTRY": reg, "WAKE_PING_LOG_DIR": home})
        self.assertEqual(r.returncode, 1)
        self.assertIn("no open bots", r.stderr)

    def test_wake_pingall_refuses_an_alias_that_is_not_a_bot_name(self):
        # An alias is both an argument (`wake --ask <alias>`) and a file name
        # ($LOG_DIR/<alias>.log), so `../escape` wrote a log one level up, `a b` asked two bots
        # called a and b, and "" started nothing while printing "started:".
        home = tempfile.mkdtemp(prefix="ping-")
        bindir, calls = fake_wake(home)
        logs = os.path.join(home, "logs")
        for alias in ("../escape", ".hidden", "a/b", "/abs", "--", "-x", "", "   ", "a b"):
            with self.subTest(alias=alias):
                r = run(
                    "wake-pingall",
                    alias,
                    env={
                        "PATH": bindir + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"),
                        "WAKE_PING_LOG_DIR": logs,
                    },
                )
                self.assertEqual(r.returncode, 2, r.stderr)
                self.assertIn("bad bot alias", r.stderr)
                self.assertNotIn("started:", r.stdout)
                self.assertFalse(os.path.exists(os.path.join(home, "escape.log")))
                self.assertFalse(os.path.exists(calls), "a refused alias reached wake")
        # The log dir is made before the loop, so it may exist; nothing may be inside it.
        self.assertEqual(os.listdir(logs) if os.path.isdir(logs) else [], [], "a refused alias was pinged")

    def test_wake_pingall_skips_a_registry_alias_that_would_escape_the_log_dir(self):
        # The registry is where an alias is least looked at. #39 keeps one bad alias from holding
        # the batch hostage, so the good one still goes out and the exit code says one was refused.
        home = tempfile.mkdtemp(prefix="ping-")
        bindir, _calls = fake_wake(home)
        reg = os.path.join(home, "bots.tsv")
        with open(reg, "w", encoding="utf-8") as f:
            f.write("ops\tbot-ops-0001\tOps\topen\tplan\n../escape\tbot-x-0002\tX\topen\tplan\n")
        logs = os.path.join(home, "logs")
        r = run(
            "wake-pingall",
            env={
                "PATH": bindir + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"),
                "WAKE_REGISTRY": reg,
                "WAKE_PING_LOG_DIR": logs,
            },
        )
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("../escape", r.stderr)
        self.assertIn("started: ops", r.stdout)
        self.assertFalse(os.path.exists(os.path.join(home, "escape.log")))
        self.assertEqual(sorted(os.listdir(logs)), ["ops.log"], "the good alias did not run")

    def test_wake_pingall_asks_a_valid_alias_and_keeps_its_log_inside_the_log_dir(self):
        home = tempfile.mkdtemp(prefix="ping-")
        stub = os.path.join(home, "bin")
        os.makedirs(stub)
        calls = os.path.join(home, "calls")
        with open(os.path.join(stub, "wake"), "w", encoding="utf-8") as f:
            f.write("#!/bin/bash\nprintf '%s\\n' \"$*\" >> " + shlex.quote(calls) + "\n")
        os.chmod(os.path.join(stub, "wake"), 0o755)
        logs = os.path.join(home, "logs")
        r = run(
            "wake-pingall",
            "ops-bot",
            env={
                "PATH": stub + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"),
                "WAKE_PING_LOG_DIR": logs,
            },
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        log = os.path.join(logs, "ops-bot.log")
        deadline = time.time() + 5
        while time.time() < deadline and not (os.path.exists(log) and os.path.exists(calls)):
            time.sleep(0.05)
        self.assertTrue(os.path.exists(log), "the log belongs inside WAKE_PING_LOG_DIR")
        self.assertEqual(sorted(os.listdir(logs)), ["ops-bot.log"])
        with open(calls, encoding="utf-8") as f:
            self.assertIn("--ask ops-bot", f.read())

    def test_wake_keys_finds_the_registry_when_the_key_dir_moves(self):
        home = tempfile.mkdtemp(prefix="cfg-")
        cfg = os.path.join(home, "cfg")
        keys = os.path.join(home, "keys")
        store = os.path.join(home, "host-keys")
        for d in (cfg, keys, store):
            os.makedirs(d)
        with open(os.path.join(cfg, "bots.tsv"), "w", encoding="utf-8") as f:
            f.write("coordinator\tbot-coordinator-0001\tCoordinator\topen\tplan\n")
        r = run("wake-keys", env={"WAKE_CONFIG_DIR": cfg, "WAKE_KEY_DIR": keys, "WAKE_HOST_KEY_DIR": store})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("no registry", r.stderr)
        self.assertIn("none  coordinator", r.stdout)

    def test_wake_keys_resolves_the_store_from_the_config_files(self):
        # wake-keys read the environment only, so on a host whose store is in the site file it said
        # "no key" for every bot and exited 0 (#135 F6).
        home = tempfile.mkdtemp(prefix="keys-")
        cfg = os.path.join(home, "cfg")
        os.makedirs(cfg)
        store = os.path.join(home, "host-keys")
        os.makedirs(store)
        with open(os.path.join(store, "bot-alpha-0001.key"), "w", encoding="utf-8") as f:
            f.write("k" * 40)
        site = os.path.join(home, "config.env")
        with open(site, "w", encoding="utf-8") as f:
            f.write(f"WAKE_STATE_DIR={store}\n")
        reg = os.path.join(cfg, "bots.tsv")
        with open(reg, "w", encoding="utf-8") as f:
            f.write("alpha\tbot-alpha-0001\tAlpha\topen\tplan\n")
        r = run(
            "wake-keys",
            env={
                "WAKE_CONFIG_DIR": cfg,
                "WAKE_KEY_DIR": os.path.join(home, "keys"),
                "WAKE_SYSTEM_CONFIG": site,
            },
        )
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("ok    alpha", r.stdout, "the store in the site file was not read")

    def test_wake_keys_takes_every_location_from_the_config_files(self):
        """Only the store was asked of the resolver; the other three were environment-only.

        A site file that names WAKE_KEY_DIR and WAKE_REGISTRY is the deploy shape on a host, so
        `wake-keys` said "no registry" there while the CLI it sits beside read all three (#135).
        """
        home = tempfile.mkdtemp(prefix="keys-")
        cfg = os.path.join(home, "cfg")
        store = os.path.join(home, "host-keys")
        keys = os.path.join(home, "saved-keys")
        for d in (cfg, store):
            os.makedirs(d)
        with open(os.path.join(store, "bot-alpha-0001.key"), "w", encoding="utf-8") as f:
            f.write("k" * 40)
        reg = os.path.join(cfg, "bots.tsv")
        with open(reg, "w", encoding="utf-8") as f:
            f.write("alpha\tbot-alpha-0001\tAlpha\topen\tplan\n")
        site = os.path.join(home, "config.env")
        with open(site, "w", encoding="utf-8") as f:
            f.write(f"WAKE_STATE_DIR={store}\nWAKE_KEY_DIR={keys}\nWAKE_REGISTRY={reg}\n")
        r = run("wake-keys", env={"HOME": home, "WAKE_SYSTEM_CONFIG": site})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("ok    alpha", r.stdout)
        self.assertEqual(os.listdir(keys), ["bot-alpha-0001.key"], "the key dir came from nowhere")

    def test_wake_keys_reads_an_emptied_field_in_place(self):
        """A tab is IFS whitespace, so `IFS=$'\t' read` collapsed an empty field.

        A site file naming `WAKE_KEY_DIR=` made the host store the key dir and the registry the
        store, and the helper then said "no registry" while the file named a readable one (#152).
        """
        home = tempfile.mkdtemp(prefix="shift-")
        cfg = os.path.join(home, "cfg")
        store = os.path.join(home, "host-keys")
        for d in (cfg, store):
            os.makedirs(d)
        with open(os.path.join(store, "bot-alpha-0001.key"), "w", encoding="utf-8") as f:
            f.write("k" * 40)
        reg = os.path.join(home, "registry.tsv")
        with open(reg, "w", encoding="utf-8") as f:
            f.write("alpha\tbot-alpha-0001\tAlpha\topen\tplan\n")
        site = os.path.join(home, "config.env")
        with open(site, "w", encoding="utf-8") as f:
            # The empty field is first, so every later value shifts one place left without the fix.
            f.write(f"WAKE_STATE_DIR={store}\nWAKE_KEY_DIR=\nWAKE_REGISTRY={reg}\n")
        r = run("wake-keys", env={"HOME": home, "WAKE_CONFIG_DIR": cfg, "WAKE_SYSTEM_CONFIG": site})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("ok    alpha", r.stdout, "an emptied field moved the registry out from under the helper")
        self.assertEqual(os.listdir(cfg), ["bot-alpha-0001.key"], "the key was saved somewhere else")

    def test_wake_keys_uses_wake_hostops_from_the_config_files(self):
        """WAKE_HOSTOPS was environment-only while wake_config.load() reads it from the files.

        With it only in the site file the bridge was never called and every bot printed
        "none (no key in the host store)" - a false "no key", the one answer the help text says
        cannot happen (#152).
        """
        home = tempfile.mkdtemp(prefix="hostops-")
        cfg = os.path.join(home, "cfg")
        os.makedirs(cfg)
        reg = os.path.join(cfg, "bots.tsv")
        with open(reg, "w", encoding="utf-8") as f:
            f.write("alpha\tbot-alpha-0001\tAlpha\topen\tplan\n")
        bridge = os.path.join(home, "bridge")
        with open(bridge, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$BRIDGE_LOG\"\nprintf 'test-key\\n'\n")
        os.chmod(bridge, 0o755)
        log = os.path.join(home, "bridge.log")
        site = os.path.join(home, "config.env")
        with open(site, "w", encoding="utf-8") as f:
            f.write(f"WAKE_KEY_DIR={os.path.join(home, 'keys')}\nWAKE_HOSTOPS={bridge}\n")
        # WAKE_CONFIG_DIR is read from the environment by config_dir(); the bridge comes from the file.
        r = run("wake-keys", env={"HOME": home, "WAKE_CONFIG_DIR": cfg, "WAKE_SYSTEM_CONFIG": site, "BRIDGE_LOG": log})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("ok    alpha", r.stdout, "the bridge named in the config file was never called")
        self.assertNotIn("none ", r.stdout, "a key on the host was reported as absent")
        with open(log, encoding="utf-8") as f:
            self.assertIn("sudo cat /var/lib/wake/bot-alpha-0001.key", f.read())

    def test_wake_keys_refuses_a_registry_the_config_emptied(self):
        """An empty WAKE_REGISTRY was silently replaced by $CFG/bots.tsv.

        That is the helper disagreeing with bin/wake about which file is the registry, and the
        message named a path the config had not asked for (#152 nit).
        """
        home = tempfile.mkdtemp(prefix="emptyreg-")
        cfg = os.path.join(home, "cfg")
        os.makedirs(cfg)
        with open(os.path.join(cfg, "bots.tsv"), "w", encoding="utf-8") as f:
            f.write("alpha\tbot-alpha-0001\tAlpha\topen\tplan\n")
        site = os.path.join(home, "config.env")
        with open(site, "w", encoding="utf-8") as f:
            f.write("WAKE_REGISTRY=\n")
        r = run("wake-keys", env={"HOME": home, "WAKE_CONFIG_DIR": cfg, "WAKE_SYSTEM_CONFIG": site})
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("WAKE_REGISTRY names an empty value", r.stderr, "an emptied registry was replaced silently")
        self.assertNotIn("alpha", r.stdout + r.stderr, "a registry the config did not name was read")

    def test_wake_keys_does_not_resolve_for_help(self):
        """The helper imported wake_config.py to print its usage text (#135 review).

        A run of `--help` has no reason to start an interpreter or read a config file, and on a
        machine where python3 is somewhere unexpected it must still answer.
        """
        home = tempfile.mkdtemp(prefix="help-")
        found = os.path.join(home, "resolved")
        for tool in ("cat", "dirname"):
            real = shutil.which(tool)
            self.assertIsNotNone(real, f"{tool} is missing from this machine")
            os.symlink(real, os.path.join(home, tool))
        with open(os.path.join(home, "python3"), "w", encoding="utf-8") as f:
            f.write(f"#!/bin/sh\nprintf resolved > {shlex.quote(found)}\nexit 1\n")
        os.chmod(os.path.join(home, "python3"), 0o755)
        r = subprocess.run(
            [os.path.join(OPS, "wake-keys"), "--help"],
            capture_output=True,
            text=True,
            env={"PATH": home, "HOME": home, "WAKE_SYSTEM_CONFIG": ""},
            timeout=60,
        )
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("usage: wake-keys", r.stdout)
        self.assertFalse(os.path.exists(found), "--help started the resolver")

    def test_wake_keys_falls_back_to_the_default_store_without_the_resolver(self):
        """A bare copy of bin/ops has no wake_config.py beside it; the built-in default stays.

        The fallback was unpinned: deleting it left the suite green, and a bare copy would then
        ask the host for `/<bot-id>.key` (#135).
        """
        home = tempfile.mkdtemp(prefix="bare-")
        bare = os.path.join(home, "ops")
        os.makedirs(bare)
        shutil.copy(os.path.join(OPS, "wake-keys"), os.path.join(bare, "wake-keys"))
        os.chmod(os.path.join(bare, "wake-keys"), 0o755)
        calls = os.path.join(home, "calls")
        hostops = os.path.join(home, "hostops")
        with open(hostops, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\nprintf '%s\\n' \"$*\" >> " + shlex.quote(calls) + "\nexit 255\n")
        os.chmod(hostops, 0o755)
        reg = os.path.join(home, "bots.tsv")
        with open(reg, "w", encoding="utf-8") as f:
            f.write("alpha\tbot-alpha-0001\tAlpha\topen\tplan\n")
        r = subprocess.run(
            [os.path.join(bare, "wake-keys")],
            capture_output=True,
            text=True,
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": home,
                "WAKE_SYSTEM_CONFIG": "",
                "WAKE_REGISTRY": reg,
                "WAKE_KEY_DIR": os.path.join(home, "keys"),
                "WAKE_HOSTOPS": hostops,
            },
            timeout=60,
        )
        self.assertNotEqual(r.returncode, 0, "a refused host fetch reported success")
        with open(calls, encoding="utf-8") as f:
            asked = f.read()
        self.assertIn("sudo cat /var/lib/wake/bot-alpha-0001.key", asked, "the default store is gone")

    def test_the_runner_switches_the_site_file_off(self):
        # A site file carrying WAKE_MCP_TOKEN failed three bridge tests on the reviewer's host; the
        # runner has to switch that layer off itself (#135 F5).
        body = text(os.path.join(ROOT, "tests", "run.sh"))
        self.assertRegex(body, r"(?m)^\s+WAKE_SYSTEM_CONFIG=\s*\\$", "tests/run.sh does not switch the site file off")

    def test_wake_keys_reports_a_failed_host_fetch_as_an_error(self):
        home = tempfile.mkdtemp(prefix="keys-")
        reg = os.path.join(home, "bots.tsv")
        with open(reg, "w", encoding="utf-8") as f:
            f.write("coordinator\tbot-coordinator-0001\tCoordinator\topen\tplan\n")
        hostops = os.path.join(home, "hostops")
        with open(hostops, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\necho 'ssh: connect to host: Connection refused' >&2\nexit 255\n")
        os.chmod(hostops, 0o755)
        r = run(
            "wake-keys",
            env={"WAKE_REGISTRY": reg, "WAKE_KEY_DIR": os.path.join(home, "keys"), "WAKE_HOSTOPS": hostops},
        )
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("err   coordinator", r.stdout)
        self.assertIn("Connection refused", r.stdout)
        self.assertNotIn("none  coordinator", r.stdout)


def _sigint_default():
    """Give the child a definite SIGINT disposition before exec.

    Python leaves an inherited SIG_IGN alone, and a child that starts with SIGINT ignored keeps
    it ignored (bash refuses to trap such a signal), so the test would time out wherever the
    runner itself was started as a background job. Reset in the child, not in the test process,
    so the suite's own signal handling is untouched.
    """
    signal.signal(signal.SIGINT, signal.SIG_DFL)


class WakeWaitTestCase(unittest.TestCase):
    """wake-wait blocks on one thread. Its only interface is `wake --thread`, so a stub stands in
    for the CLI here: the real store is exercised by tests/test_wake.py."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="wait-")
        self.bin = os.path.join(self.home, "bin")
        os.makedirs(self.bin)
        self.calls = os.path.join(self.home, "calls.log")
        self.stub("exit 0")

    def tearDown(self):
        # Every run used to leave /tmp/wait-* behind: 14,079 of them in one container after
        # an afternoon of runs, enough that any tool scanning /tmp walked all of them (#142).
        shutil.rmtree(self.home, ignore_errors=True)

    def stub(self, body):
        """A fake `wake` that records its arguments, then runs `body` (indented one level)."""
        path = os.path.join(self.bin, "wake")
        with open(path, "w", encoding="utf-8") as f:
            f.write('#!/bin/sh\nprintf "%s\\n" "$*" >> ' + self.calls + "\n" + textwrap.dedent(body).lstrip())
        os.chmod(path, 0o755)

    def stub_rounds(self, rounds):
        """A thread that grows: `rounds[i]` is what poll i+1 sees, and the last round repeats.

        This is how a reply arrives late without racing the poll: the script counts its own calls.
        """
        counter = os.path.join(self.home, "poll")
        body = ["n=$(cat " + counter + " 2>/dev/null || echo 0)", "n=$((n+1)); echo $n > " + counter, "case $n in"]
        for i, lines in enumerate(rounds, start=1):
            body.append("    " + (str(i) if i < len(rounds) else str(i) + "*") + ")")
            for line in lines:
                body.append("        printf '%s\\n' " + shlex.quote(line))
            body.append("        ;;")
        body.append("    *)")
        for line in rounds[-1]:
            body.append("        printf '%s\\n' " + shlex.quote(line))
        body.append("        ;;")
        body.append("esac")
        self.stub("\n".join(body))

    def wait(self, *args, env=None):
        base = {"PATH": self.bin + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"), "HOME": self.home}
        base.update(env or {})
        return subprocess.run(
            [os.path.join(OPS, "wake-wait"), *args], capture_output=True, text=True, env=base, timeout=60
        )

    def called(self):
        if not os.path.exists(self.calls):
            return []
        with open(self.calls, encoding="utf-8") as f:
            return f.read().splitlines()

    # ---- the answer ----------------------------------------------------------------

    def test_a_new_reply_is_printed_and_exits_0(self):
        ask = "[2026-11-15T12:00] human:tester (ask): what is the eta"
        reply = "[2026-11-15T12:01] bot-engineer (reply): tomorrow"
        self.stub_rounds([[ask], [ask, reply]])
        r = self.wait("t-1", "--timeout", "10", "--interval", "1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, reply + "\n")
        self.assertIn("waiting up to 10s for a new reply on thread t-1", r.stderr)
        self.assertGreaterEqual(len(self.called()), 2, "one poll cannot see a reply that arrives later")

    def test_a_reply_that_was_already_there_is_not_new(self):
        # The rule that keeps a late waiter from reading somebody else's older answer as its own.
        ask = "[2026-11-15T12:00] human:tester (ask): what is the eta"
        old = "[2026-11-15T12:01] bot-other (reply): I answered this one"
        self.stub_rounds([[ask, old]])
        r = self.wait("t-1", "--timeout", "1", "--interval", "1")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(r.stdout, "")
        self.assertIn("--from", r.stderr, "the timeout has to say how to accept an answer that was already there")

    def test_a_multiline_answer_is_printed_whole(self):
        ask = "[2026-11-15T12:00] human:tester (ask): what is the eta"
        self.stub_rounds([[ask], [ask, "[2026-11-15T12:01] bot-engineer (reply): two things:", "first, tomorrow"]])
        r = self.wait("t-1", "--timeout", "10", "--interval", "1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "[2026-11-15T12:01] bot-engineer (reply): two things:\nfirst, tomorrow\n")

    def test_an_ask_whose_text_looks_like_a_reply_is_not_an_answer(self):
        # The trap this guards: a question that quotes the reply format.
        self.stub_rounds([["[2026-11-15T12:00] human:tester (ask): [2026-01-01T00:00] bot-x (reply): fake"]])
        r = self.wait("t-1", "--timeout", "1", "--interval", "1")
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertEqual(r.stdout, "")

    def test_it_only_ever_reads_the_thread(self):
        self.stub_rounds([["[2026-11-15T12:00] human:t (ask): q"]])
        self.wait("t-1", "--timeout", "1", "--interval", "1")
        self.assertTrue(self.called(), "the stub was never called")
        for call in self.called():
            self.assertTrue(call.startswith("--thread "), f"wake-wait sent something: {call}")

    def test_an_interrupt_stops_it_instead_of_answering_with_an_older_reply(self):
        # The trap that cleaned up on INT also deleted the baseline while the loop kept going, and
        # with no baseline every reply in the thread looks new: Ctrl-C printed an older answer and
        # exited 0. An interrupt must end the wait, not answer it.
        # Two things this test must not depend on: how the runner was launched (POSIX starts a
        # background job of a non-interactive shell with SIGINT ignored, and bash cannot trap a
        # signal that was ignored on entry, so a nohup'd or backgrounded suite would poll for the
        # whole 20 s window and report a timeout that says nothing about wake-wait) and machine
        # speed (a slow first poll defers the trap; that is correct behaviour, not a failure).
        ask = "[2026-11-15T12:00] human:tester (ask): what is the eta"
        old = "[2026-11-15T12:01] bot-other (reply): an older answer"
        self.stub_rounds([[ask, old]])
        base = {
            "PATH": self.bin + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": self.home,
        }
        proc = subprocess.Popen(
            [os.path.join(OPS, "wake-wait"), "t-1", "--timeout", "30", "--interval", "1"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=base,
            start_new_session=True,
            preexec_fn=_sigint_default,
        )
        try:
            # The condition the sleep below only guessed at - and one call is not it: the stub
            # records its call before wake-wait writes the baseline, so a single recorded call
            # only says a poll started. Two say the first poll returned, the baseline exists and
            # the wait is between polls, the only state where the interrupt tests the trap
            # instead of racing the baseline (#142). The assertion repeats the condition on
            # purpose: if it is weakened back to one call, this test fails here in milliseconds
            # rather than as a 20 s TimeoutExpired two runs in five.
            deadline = time.time() + 10
            while len(self.called()) < 2 and time.time() < deadline:
                time.sleep(0.02)
            self.assertGreaterEqual(
                len(self.called()), 2, "the interrupt would race the baseline: the stub answered once"
            )
            proc.send_signal(signal.SIGINT)
            out, err = proc.communicate(timeout=20)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
        self.assertEqual(proc.returncode, 130, out + err)
        self.assertEqual(out, "", "an interrupt printed an answer")

    # ---- --from --------------------------------------------------------------------

    def test_from_accepts_an_answer_that_predates_the_wait(self):
        ask = "[2026-11-15T12:00] human:tester (ask): what is the eta"
        reply = "[2026-11-15T12:01] bot-engineer (reply): tomorrow"
        self.stub_rounds([[ask, reply]])
        r = self.wait("t-1", "--from", "engineer", "--timeout", "5")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, reply + "\n")
        self.assertIn("for a reply from engineer", r.stderr)
        self.assertEqual(len(self.called()), 1, "an answer that is already there needs no second poll")

    def test_from_ignores_another_bots_reply(self):
        ask = "[2026-11-15T12:00] human:tester (ask): what is the eta"
        other = "[2026-11-15T12:01] bot-other (reply): not your answer"
        self.stub_rounds([[ask, other]])
        r = self.wait("t-1", "--from", "engineer", "--timeout", "1", "--interval", "1")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(r.stdout, "")
        self.assertIn("no reply from engineer", r.stderr)

    def test_from_takes_the_source_prefix_and_the_equals_form(self):
        ask = "[2026-11-15T12:00] human:tester (ask): what is the eta"
        reply = "[2026-11-15T12:01] bot-engineer (reply): tomorrow"
        for form in (["--from", "bot:engineer"], ["--from=bot:engineer"], ["--from=ENGINEER"]):
            with self.subTest(form=form):
                self.stub_rounds([[ask, reply]])
                r = self.wait("t-1", *form, "--timeout=5")
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(r.stdout, reply + "\n")

    def test_from_can_come_from_the_environment(self):
        ask = "[2026-11-15T12:00] human:tester (ask): what is the eta"
        reply = "[2026-11-15T12:01] bot-engineer (reply): tomorrow"
        self.stub_rounds([[ask, reply]])
        r = self.wait("t-1", "--timeout", "5", env={"WAKE_WAIT_FROM": "engineer", "WAKE_WAIT_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, reply + "\n")
        self.assertEqual(r.stderr, "")

    def test_an_empty_from_is_refused_instead_of_switching_the_rule(self):
        # An empty alias used to mean "any new reply" silently, and a blank one waited the whole
        # timeout for a bot whose name is three spaces. The flags are the only place this is
        # refused; an empty WAKE_WAIT_FROM is the ordinary unset case and stays legal.
        for form in (["--from="], ["--from", ""], ["--from=   "], ["--from", "   "]):
            with self.subTest(form=form):
                self.stub_rounds([["[2026-11-15T12:00] human:tester (ask): what is the eta"]])
                r = self.wait("t-1", *form, "--timeout", "1")
                self.assertEqual(r.returncode, 1, r.stderr)
                self.assertIn("--from needs a bot alias", r.stderr)
                self.assertEqual(self.called(), [], "a refused --from still went to the thread store")

    def test_an_empty_from_in_the_environment_is_the_unset_case(self):
        ask = "[2026-11-15T12:00] human:tester (ask): what is the eta"
        reply = "[2026-11-15T12:01] bot-engineer (reply): tomorrow"
        self.stub_rounds([[ask], [ask, reply]])
        r = self.wait("t-1", "--timeout", "5", "--interval", "1", env={"WAKE_WAIT_FROM": "", "WAKE_WAIT_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, reply + "\n")

    def test_a_blank_from_in_the_environment_is_refused_too(self):
        # The variable is the third way in and was never checked: FROM=three spaces made
        # [ -n "$FROM" ] true, so it waited the whole timeout for a bot with that name.
        self.stub_rounds([["[2026-11-15T12:00] human:tester (ask): what is the eta"]])
        r = self.wait("t-1", "--timeout", "1", env={"WAKE_WAIT_FROM": "   ", "WAKE_WAIT_QUIET": "1"})
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("WAKE_WAIT_FROM", r.stderr)
        self.assertEqual(self.called(), [], "a blank WAKE_WAIT_FROM still went to the thread store")

    def test_the_default_timeout_is_180s_and_the_variable_sets_it(self):
        # Proved without waiting: a --from answer that is already there returns at once, so the
        # progress line is the only thing under test.
        ask = "[2026-11-15T12:00] human:tester (ask): what is the eta"
        reply = "[2026-11-15T12:01] bot-engineer (reply): tomorrow"
        self.stub_rounds([[ask, reply]])
        r = self.wait("t-1", "--from", "engineer")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("waiting up to 180s", r.stderr)
        self.stub_rounds([[ask, reply]])
        r = self.wait("t-1", "--from", "engineer", env={"WAKE_WAIT_TIMEOUT": "7"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("waiting up to 7s", r.stderr)

    def test_the_two_reply_patterns_cannot_drift(self):
        # The pattern is written twice: once for grep (the baseline snapshot) and once inside the
        # awk program, because a -v value is unescaped by gawk and left alone by mawk. If the two
        # ever diverge, "new reply" and "the reply to print" stop meaning the same thing.
        body = read("wake-wait")
        self.assertNotIn("-v reply=", body, "a pattern through -v is unescaped by gawk and kept by mawk")
        from_grep = re.search(r"(?m)^REPLY=.(.+).$", body)
        in_awk = re.search(r"\$0 !~ /(.+?)/\) next", body)
        self.assertIsNotNone(from_grep, "the REPLY pattern is gone")
        self.assertIsNotNone(in_awk, "the pattern inside the awk program is gone")
        self.assertEqual(in_awk.group(1), from_grep.group(1), "the grep and awk patterns drifted")

    # ---- timeout and errors --------------------------------------------------------

    def test_it_times_out_with_2_when_only_the_ask_is_there(self):
        self.stub_rounds([["[2026-11-15T12:00] human:tester (ask): what is the eta"]])
        r = self.wait("t-1", "--timeout", "1", "--interval", "1")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(r.stdout, "")
        self.assertIn("no new reply on thread t-1 after 1s", r.stderr)
        self.assertIn("wake --thread t-1", r.stderr)

    def test_a_zero_timeout_polls_once_and_times_out(self):
        self.stub_rounds([["[2026-11-15T12:00] human:t (ask): q"]])
        r = self.wait("t-1", "--timeout", "0")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(len(self.called()), 1)

    def test_the_quiet_flag_silences_the_progress_line(self):
        ask = "[2026-11-15T12:00] human:tester (ask): what is the eta"
        self.stub_rounds([[ask], [ask, "[2026-11-15T12:01] bot-engineer (reply): tomorrow"]])
        r = self.wait("t-1", "--timeout", "5", "--interval", "1", env={"WAKE_WAIT_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")

    def test_a_thread_that_was_never_opened_is_an_error(self):
        self.stub(
            """
            >&2 echo 'no thread t-9'
            exit 1
            """
        )
        r = self.wait("t-9", "--timeout", "5")
        self.assertEqual(r.returncode, 1)
        self.assertIn("was never opened", r.stderr)
        self.assertEqual(len(self.called()), 1, "a thread that never existed must not be polled")

    def test_an_unreadable_store_is_an_error_without_polling(self):
        self.stub(
            """
            >&2 echo 'cannot read the thread store at /nowhere: check WAKE_HOSTOPS (guest) and that the directory is readable.'
            exit 1
            """
        )
        r = self.wait("t-1", "--timeout", "30", "--interval", "1")
        self.assertEqual(r.returncode, 1)
        self.assertIn("cannot read the thread store", r.stderr)
        self.assertEqual(len(self.called()), 1)

    def test_any_other_failure_is_an_error(self):
        self.stub(
            """
            >&2 echo 'wake: boom'
            exit 1
            """
        )
        r = self.wait("t-1", "--timeout", "5")
        self.assertEqual(r.returncode, 1)
        self.assertIn("boom", r.stderr)

    def test_a_missing_wake_is_a_clear_error(self):
        base = {"PATH": self.home, "HOME": self.home}  # no wake on PATH
        r = subprocess.run(
            [os.path.join(OPS, "wake-wait"), "t-1"], capture_output=True, text=True, env=base, timeout=60
        )
        self.assertEqual(r.returncode, 1)
        self.assertIn("wake not found on PATH", r.stderr)

    # ---- usage ---------------------------------------------------------------------

    def test_help_prints_the_usage_and_the_exit_codes(self):
        r = self.wait("--help")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("usage: wake-wait <thread-id> [--timeout SECONDS] [--interval N] [--from BOT]", r.stdout)
        for code in ("exit 0", "2  no answer", "1  error"):
            self.assertIn(code, r.stdout)
        self.assertEqual(self.called(), [], "--help must not touch wake")

    def test_a_thread_id_with_a_path_is_refused_before_anything_runs(self):
        # The timeout is small on purpose: if the guard is ever dropped, this fails in a second
        # instead of polling for the default 180.
        for bad in ("../escape", "/etc/passwd", ""):
            with self.subTest(bad=bad):
                r = self.wait(bad, "--timeout", "1", "--interval", "1")
                self.assertEqual(r.returncode, 1, r.stderr)
                self.assertNotIn("Traceback", r.stderr)
        self.assertEqual(self.called(), [], "a rejected id must never reach wake")

    def test_two_ids_is_usage(self):
        r = self.wait("t-1", "t-2")
        self.assertEqual(r.returncode, 1)
        self.assertIn("usage: wake-wait", r.stderr)

    def test_an_unknown_option_is_refused(self):
        r = self.wait("t-1", "--soon")
        self.assertEqual(r.returncode, 1)
        self.assertIn("unknown option --soon", r.stderr)

    def test_a_zero_interval_is_refused(self):
        r = self.wait("t-1", "--interval", "0")
        self.assertEqual(r.returncode, 1)
        self.assertIn("interval must be a positive number", r.stderr)

    def test_an_option_without_its_value_is_refused(self):
        for flag in ("--timeout", "--interval", "--from"):
            with self.subTest(flag=flag):
                r = self.wait("t-1", flag)
                self.assertEqual(r.returncode, 1)
                self.assertIn(flag + " needs", r.stderr)

    def test_a_timeout_that_is_not_a_number_is_refused(self):
        r = self.wait("t-1", "--timeout", "soon")
        self.assertEqual(r.returncode, 1)
        self.assertIn("number of seconds", r.stderr)


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
