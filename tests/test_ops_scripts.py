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

try:  # `tests/run.sh` runs `discover -s tests`, which puts tests/ itself on sys.path
    from test_scrub_check import ci_steps  # noqa: E402
except ImportError:  # `python3 -m unittest tests.test_ops_scripts` from the repo root
    from tests.test_scrub_check import ci_steps  # noqa: E402

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


def run_bodies(block):
    """Every `run:` body in a job block, read off the raw lines, in order.

    ci_steps() keeps a `key: value` line and drops the lines under a `run: |`, so a body that is
    not one line is invisible to it. The lint and api pins both read bodies here: one reader, so
    the two cannot drift into reading different shapes of the same workflow.
    """
    runs = []
    i = 0
    while i < len(block):
        line = block[i]
        i += 1
        if not line.startswith("        run: "):
            continue
        payload = line[len("        run: ") :]
        if payload != "|":
            runs.append(payload)
            continue
        lines_of_block = []
        while i < len(block) and (not block[i].strip() or block[i].startswith("         ")):
            lines_of_block.append(block[i].strip())
            i += 1
        while lines_of_block and not lines_of_block[-1]:
            lines_of_block.pop()
        runs.append(lines_of_block)
    return runs


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
        # A workflow-level block sits one line outside this job and can still disarm it:
        # `defaults: {run: {shell: '/bin/true {0}'}}` applies to every run step in the file, so the
        # backgrounded step would exec /bin/true and the suite never start, with every guard below
        # green (measured at 88aa476, #141). Pin the file's top-level keys, so a new block here has
        # to be added in the same change as this pin.
        top = [
            line.split(":", 1)[0]
            for line in lines
            if line and not line.startswith((" ", "\t", "#", "-")) and ":" in line
        ]
        # Sorted: swapping two of the four keys changes nothing about the workflow, so a reorder
        # must not read as a fifth key. The injury is the added top-level block, and the message has
        # to name which key is the stranger instead of listing the whole file back.
        allowed = ["jobs", "name", "on", "permissions"]
        if sorted(top) != allowed:
            unexpected = [key for key in top if key not in allowed]
            missing = [key for key in allowed if key not in top]
            repeated = sorted({key for key in top if top.count(key) > 1})
            parts = []
            if unexpected:
                parts.append("unexpected " + ", ".join(unexpected))
            if missing:
                parts.append("missing " + ", ".join(missing))
            if repeated:
                parts.append("repeated " + ", ".join(repeated))
            self.fail(
                f"workflow keys {top}: {'; '.join(parts)} - only name, on, permissions and jobs are "
                "allowed, because a top-level defaults block swaps the shell for the backgrounded "
                "job's run step"
            )
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
        # The run body is another way in and it is not a key: `exit 0` as the first line under
        # `run: |` left every guard above green and the job green with the suite never started
        # (measured by mutation at 88aa476, #141). Pin the body to the two commands, and the run
        # step's own keys to run and env, so `shell:` - which replaces the interpreter - and any
        # other step key are refused by name too.
        step = "      - name: Run tests as a background job"
        self.assertIn(step, block, "the run step is gone or renamed")
        step_keys = []
        sections = {}
        section = None
        for line in block[block.index(step) + 1 :]:
            if line.startswith("      - "):
                break
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if line.startswith("        ") and not line.startswith("         "):
                section = None
                if ":" in line:
                    key, rest = stripped.split(":", 1)
                    rest = rest.strip()
                    step_keys.append(key)
                    if not rest or rest.startswith(("|", ">")):
                        section = key
                        sections[key] = []
                continue
            if section is not None:
                sections[section].append(stripped)
        self.assertEqual(
            sections.get("run"),
            ["tests/run.sh &", "wait $!"],
            f"the run body is {sections.get('run')}: the suite is the only command the step may "
            "run, backgrounded and waited on - an `exit 0` above it leaves the job green without "
            "the suite",
        )
        self.assertEqual(
            step_keys,
            ["run", "env"],
            f"run step keys {step_keys}: only run and env are allowed - shell, if or "
            "continue-on-error disarms the step",
        )
        # env is not a free pass either: PYTHON picks the interpreter tests/run.sh execs, so
        # `PYTHON: /bin/true` runs nothing and exits 0 - the suite never starts and the job is
        # green (measured at 88aa476, #141). A map written inline (`env: {PYTHON: python}`)
        # leaves no block to read, so it fails on the empty list.
        self.assertEqual(
            sections.get("env"),
            ["PYTHON: python"],
            f"the run step env is {sections.get('env')}: PYTHON picks the interpreter the suite "
            "runs under, so any other value here can pass the step without running it",
        )

    def test_ci_runs_on_push_and_pull_request(self):
        """#172: `on` was pinned to exist, not to what it holds. `on: workflow_dispatch:` alone kept
        all 100 tests green, and a workflow that no push or pull request starts runs nothing at all:
        GitHub shows an absent check rather than a green one, so the secret scan is off and nothing
        in the suite notices. An inline `on: [push, pull_request]` leaves no block to read and fails
        on the empty list, as the env pin above does for an inline map.
        """
        lines = text(os.path.join(ROOT, ".github/workflows/ci.yml")).splitlines()
        inline = [line.strip() for line in lines if line.startswith("on:") and line.strip() != "on:"]
        self.assertEqual(
            inline,
            [],
            f"the triggers are written inline ({inline}): the block form is pinned, so a change to "
            "them has to be a change to this pin",
        )
        self.assertIn("on:", lines, "the workflow has no top-level `on:` key")
        triggers = []
        for line in lines[lines.index("on:") + 1 :]:
            if line and not line.startswith((" ", "\t")):
                break
            if line.strip() and not line.strip().startswith("#"):
                triggers.append(line.strip())
        self.assertEqual(
            triggers,
            ["push:", "pull_request:"],
            f"the workflow triggers are {triggers}: every push and every pull request has to start "
            "CI, and anything narrower - workflow_dispatch alone, a `branches:` filter, a "
            "`pull_request` with `types:` - leaves commits that no check ever sees. A trigger added "
            "on purpose goes in this list in the same commit",
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

    def test_ci_lint_job_runs_on_a_pinned_image(self):
        """#174: `runs-on: self-hosted` on lint kept every test green while the verdict came from
        that machine's shellcheck and python. A key list freezes the name, not the value, so the
        value is pinned here as the secret-scan job's is in test_scrub_check; the list itself is
        test_ci_lint_job_keys_cannot_disarm_it."""
        lines = text(os.path.join(ROOT, ".github/workflows/ci.yml")).splitlines()
        self.assertIn("  lint:", lines, "the lint job is gone")
        start = lines.index("  lint:")
        block = []
        for line in lines[start + 1 :]:
            if line.startswith("  ") and not line.startswith("   ") and line.strip():
                break
            block.append(line)
        runner = [line.strip() for line in block if line.startswith("    runs-on:")]
        self.assertEqual(
            runner,
            ["runs-on: ubuntu-24.04"],
            f"the lint runner is {runner}: the verdict comes from the runner's shellcheck and "
            "python, so the image is pinned - a floating tag or a self-hosted machine changes "
            "what lint reads while the suite stays green",
        )

    def test_ci_lint_job_keys_cannot_disarm_it(self):
        """#178: the value pin above reads `runs-on` and nothing else, so `lint` can be disarmed
        with that line untouched - measured at 06c2c75, full suite green under each of a job
        `container:`, `if: false`, `continue-on-error: true`, a job `defaults` that swaps the run
        shell, a step `if: false` and a step `continue-on-error: true`. `lint` is one of the eight
        required checks on main, so a skipped or neutral lint is a gate that can never fail. This
        is the pin `backgrounded` and `secret-scan` already carry."""
        lines = text(os.path.join(ROOT, ".github/workflows/ci.yml")).splitlines()
        self.assertIn("  lint:", lines, "the lint job is gone")
        start = lines.index("  lint:")
        block = []
        for line in lines[start + 1 :]:
            if line.startswith("  ") and not line.startswith("   ") and line.strip():
                break
            block.append(line)
        # Job keys: `if` skips the job, `continue-on-error` reports a failed lint as neutral,
        # `defaults` swaps the shell of its run steps and `container` runs every step in another
        # image while `runs-on` keeps its pinned value. None of them touches a test.
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
            f"job keys {keys}: only runs-on and steps are allowed - if, continue-on-error, "
            "defaults or container would let a lint that never ran report green",
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
            f"a step key that can disarm lint was added: {disarming} - a skipped or neutral step still reports green",
        )
        # The key list and the refusal above pin the shape of the job and of every step, not what
        # a step runs: `run: "true"` swapped into `ruff check` keeps all of it green and lint then
        # checks nothing (measured at 06c2c75). The names and the run lines are pinned by value, as
        # the secret-scan step's are, so a change to what lint reads is a deliberate edit here.
        steps = ci_steps(block)
        named = [step["values"].get("name") or step["values"].get("uses") for step in steps]
        self.assertEqual(
            named,
            [
                "actions/checkout@v7",
                "actions/setup-python@v6",
                "Install ruff",
                "ruff check",
                "ruff format --check",
                "shellcheck",
                "compileall",
                "CHANGELOG.md deletions",
            ],
            f"the lint steps are {named}: a step added, dropped or renamed changes what lint reads",
        )
        # A step carries keys of its own and the job-level list above cannot see them:
        # `working-directory: /tmp` on `ruff check` runs `ruff check .` in an empty directory, which
        # exits 0 with a warning, and `shell: /bin/true {0}` on the same step runs nothing at all -
        # either one keeps every assertion above green (measured at 06c2c75 while #181 was reviewed).
        # Each step's keys are allowlisted here, as `secret-scan` allowlists checkout's `uses`/`with`
        # and the scan's `name`/`run` (#181).
        allowed = {
            "actions/checkout@v7": ["uses"],
            "actions/setup-python@v6": ["uses", "with"],
            "Install ruff": ["name", "run"],
            "ruff check": ["name", "run"],
            "ruff format --check": ["name", "run"],
            "shellcheck": ["name", "run"],
            "compileall": ["name", "run"],
            # #199: the step's `env` is the revision it compares against, and
            # tests/test_changelog_guard.py pins that value.
            "CHANGELOG.md deletions": ["name", "env", "run"],
        }
        for step, label in zip(steps, named):
            with self.subTest(step=label):
                self.assertIn(label, allowed, f"an unexpected lint step {label!r} was added")
                self.assertEqual(
                    step["keys"],
                    allowed[label],
                    f"{label} carries {step['keys']}: only {allowed[label]} are allowed - a step "
                    "shell, a working-directory or an `env` decides what the command reads without "
                    "changing a single pinned value",
                )
        # The one `with:` block in this job decides which interpreter runs ruff and shellcheck,
        # and nothing above reads it: `python-version: "3.13"` -> `"3.9"` keeps both lint pins green
        # (measured at b7c73da). The image is pinned by value on `runs-on`; the interpreter is the
        # same kind of decision, so it is pinned by value too, as `secret-scan` pins checkout's
        # `fetch-depth: 0`.
        setup = steps[1]
        self.assertRegex(
            setup["values"].get("uses", ""),
            r"^actions/setup-python@v\d+$",
            f"setup-python action is {setup['values'].get('uses')!r}",
        )
        # The list below is exact on purpose: `'3.13'` in single quotes or an added `cache: pip`
        # fails it, and a Python bump edits this line and ci.yml in the same commit - the pair is
        # what says which interpreter ruff and shellcheck run on, so neither moves quietly alone.
        self.assertEqual(
            setup["sections"].get("with"),
            ['python-version: "3.13"'],
            f"setup-python with is {setup['sections'].get('with')}: the interpreter is what runs "
            "ruff and shellcheck, so a different minor fails here instead of changing what lint reads",
        )
        # What each step runs is pinned by value too, read off the raw lines: `run: "true"` swapped
        # into `ruff check` keeps every pin above green while lint checks nothing (measured at
        # 06c2c75), and so does a ruff version that is not the one the suite was proved on. A
        # `run: |` opens a block the step parser above does not keep, so its lines are read here.
        runs = run_bodies(block)
        self.assertEqual(
            runs,
            [
                "python -m pip install ruff==0.17.5",
                "ruff check .",
                "ruff format --check .",
                "shellcheck install.sh tests/run.sh scripts/scrub-check.sh "
                "scripts/changelog-guard.sh "
                "$(grep -lE '^#!/(usr/)?bin/(env )?(ba)?sh' bin/ops/wake-* bin/wake*)",
                [
                    "python -m compileall -q bin tests",
                    "for f in bin/wake bin/wake-hook bin/wake-inbox-io bin/wake-mcp bin/wake-run; do",
                    'python -m py_compile "$f"',
                    "done",
                ],
                # #218: the push arm reads the tip the ref had before the push, so a merge
                # commit that drops a line is judged on `main` instead of passed as "not a pull
                # request"; test_changelog_guard.py executes this block.
                [
                    'base="$BASE_SHA"',
                    'if [ -z "$base" ]; then',
                    "# Only a push to the default branch is judged here. A branch push is left to its pull",
                    "# request, which compares the base it merges into; naming both the ref and the default",
                    "# branch keeps the skip visible in the log, and makes a rename read as itself. No",
                    "# default branch in the event means no way to know which ref is judged: refuse rather",
                    "# than fall back to `main`, which is the skip this arm exists to stop.",
                    'if [ -z "$DEFAULT_BRANCH" ]; then',
                    'echo "changelog-guard: the event names no default branch; refusing to guess which ref is judged" >&2',
                    "exit 1",
                    "fi",
                    'case "$REF" in',
                    '"refs/heads/$DEFAULT_BRANCH") ;;',
                    "*)",
                    'echo "changelog-guard: $REF is not refs/heads/$DEFAULT_BRANCH, the default branch; a branch push is judged by its pull request"',
                    "exit 0",
                    ";;",
                    "esac",
                    'base="$BEFORE_SHA"',
                    "# A ref that did not exist before reports an all-zero tip. A commit sha cannot be all",
                    '# zeros, so "every digit is 0" is what says nothing sits behind it; a push that reports',
                    "# no tip at all says the same thing in a different way, and says it with its own wording.",
                    'case "$base" in',
                    '"")',
                    'echo "changelog-guard: the push reports no previous tip; nothing to compare"',
                    "exit 0",
                    ";;",
                    "*[!0]*) ;;",
                    "*)",
                    'echo "changelog-guard: a new ref has no previous tip; nothing to compare"',
                    "exit 0",
                    ";;",
                    "esac",
                    "fi",
                    "# Fetched by sha, so the lint job keeps its default shallow checkout. A tip that cannot be",
                    "# fetched fails the step instead of passing: a pass would report a comparison that never",
                    "# ran, which is the hole this arm closes.",
                    'if ! git fetch --no-tags --depth=1 origin "$base"; then',
                    'echo "changelog-guard: cannot fetch $base; refusing to pass without comparing it" >&2',
                    "exit 1",
                    "fi",
                    'scripts/changelog-guard.sh "$base"',
                ],
            ],
            f"lint runs {runs}: a command that checks nothing reports green, and so does a ruff "
            "version that is not the one the suite was proved on",
        )

    def test_ci_api_job_runs_the_linter_config_control_right_after_the_inline_pin(self):
        """#260: `test_the_shipped_config_still_reports_an_unused_parameter` skips in every job but
        `api (bun)`, which is the one job with node_modules. The step that runs its module is
        pinned by value here, the way the inline pin step is: its position right after
        "Inline suppressions bite", its name, its env and its run string. Each assertion names the
        value that moved, so a deleted, renamed, moved or unarmed step fails with that name.
        """
        lines = text(os.path.join(ROOT, ".github/workflows/ci.yml")).splitlines()
        self.assertIn("  api:", lines, "the api (bun) job is gone")
        start = lines.index("  api:")
        block = []
        for line in lines[start + 1 :]:
            if line.startswith("  ") and not line.startswith("   ") and line.strip():
                break
            block.append(line)
        steps = ci_steps(block)
        named = [step["values"].get("name") or step["values"].get("uses") for step in steps]
        self.assertIn(
            "Linter configs bite",
            named,
            f"the api steps are {named}: no step named 'Linter configs bite' is in the api job, so "
            "test_linter_configs' control skips in every job (#260)",
        )
        self.assertIn(
            "Inline suppressions bite",
            named,
            f"the api steps are {named}: the step 'Inline suppressions bite' is gone, so the new "
            "linter-config step has no position to be pinned against (#260)",
        )
        position = named.index("Inline suppressions bite") + 1
        self.assertEqual(
            named.index("Linter configs bite"),
            position,
            f"the api steps are {named}: 'Linter configs bite' is at index "
            f"{named.index('Linter configs bite')}, pinned to {position}, immediately after "
            "'Inline suppressions bite' (#260)",
        )
        step = steps[position]
        self.assertEqual(
            step["keys"],
            ["name", "env", "run"],
            f"'Linter configs bite' carries {step['keys']}: pinned to name, env and run, and shell or "
            "if or continue-on-error would let the control skip or report neutral (#260)",
        )
        self.assertEqual(
            step["sections"].get("env"),
            ['WAKE_REQUIRE_LINTERS: "1"'],
            f"'Linter configs bite' env is {step['sections'].get('env')}, pinned to "
            '[\'WAKE_REQUIRE_LINTERS: "1"\']: without it a missing eslint skips the control and the step '
            "stays green (#260)",
        )
        self.assertEqual(
            step["values"].get("run"),
            "python3 tests/run_module_strict.py test_linter_configs",
            f"'Linter configs bite' runs {step['values'].get('run')!r}, pinned to "
            "'python3 tests/run_module_strict.py test_linter_configs': the module through the strict "
            "runner, so a skip fails the step (#260)",
        )

    def test_ci_api_job_runs_the_inline_pin_where_eslint_is_installed(self):
        """#216: the inline pin's control never ran in CI. `LinterBitesTest` proves that one
        `eslint-disable` line takes `eslint apps` from rc 1 to rc 0, and a `prettier-ignore` the
        formatter half of the same job - and it skips where `node_modules` is absent, which is
        every job but `api (bun)`. Measured at ce29cf01, job 137300777854: both tests reported
        `skipped 'eslint is not installed here: run bun install --frozen-lockfile'` and the suite
        `OK (skipped=10)`, so a change that stops the linter biting - or that mis-wires the
        fixture - leaves every check green while the pin guards a behaviour that is no longer
        there. `api (bun)` is the one job with the lockfile installed, so the step is pinned here
        by value, as the lint job's steps are (#181).
        """
        lines = text(os.path.join(ROOT, ".github/workflows/ci.yml")).splitlines()
        self.assertIn("  api:", lines, "the api (bun) job is gone")
        start = lines.index("  api:")
        block = []
        for line in lines[start + 1 :]:
            if line.startswith("  ") and not line.startswith("   ") and line.strip():
                break
            block.append(line)
        # The job's own keys, the shape #178 measured on `lint` at 06c2c75 and this test missed:
        # a job `defaults: run: shell:` replaces the shell of every run step below it - Install,
        # Typecheck, Lint, Test, Build, the e2e step and this pin - so the whole job exits 0
        # without running, and `container:` runs them in another image while `runs-on` keeps its
        # pinned value. Neither touches a line the assertions below read. `services` and the job
        # `env` are what `api` legitimately carries; their child keys sit one level deeper and are
        # not job keys, as they already are not for `lint`.
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
            ["name", "runs-on", "services", "env", "steps"],
            f"api job keys {keys}: only these are allowed - `if`, `continue-on-error`, `defaults` "
            "or `container` would let a job that never ran, or ran another image, report green "
            "with the inline pin inside it (#178)",
        )
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
            f"a job or step key that can disarm the api job was added: {disarming} - `if` skips "
            "the job or the step and `continue-on-error` reports it neutral, so it reports green "
            "without running. A step that legitimately needs `if:` (a pull-request-only e2e, say) "
            "fails here on purpose: allow it deliberately rather than letting the pin go quiet",
        )
        steps = ci_steps(block)
        named = [step["values"].get("name") or step["values"].get("uses") for step in steps]
        self.assertIn(
            "Inline suppressions bite",
            named,
            f"the api steps are {named}: the pin's control skips without node_modules, so this "
            "job is the only place LinterBitesTest can run (#216)",
        )
        pin = steps[named.index("Inline suppressions bite")]
        self.assertEqual(
            pin["keys"],
            ["name", "env", "run"],
            f"the inline pin step carries {pin['keys']}: only name, env and run are allowed - "
            "shell replaces the interpreter, if skips the step and continue-on-error reports it "
            "neutral",
        )
        self.assertEqual(
            pin["values"].get("run"),
            "python3 tests/run_module_strict.py test_inline_suppressions",
            f"the inline pin runs {pin['values'].get('run')!r}: the whole module, or the scan "
            "arms run without the control beside them, and it must run through the strict "
            "runner - `python3 -m unittest` exits 0 on `OK (skipped=1)`, so one "
            "`@unittest.skipIf` on an arm restores the silent green this step closes (#216)",
        )
        self.assertTrue(
            os.path.isfile(os.path.join(ROOT, "tests/run_module_strict.py")),
            "tests/run_module_strict.py is gone: the pin step names a runner that refuses a skip",
        )
        # Without this variable the two tests go back to skipping where node_modules is missing,
        # which is the silent green #216 measured. With it, the step fails instead.
        self.assertEqual(
            pin["sections"].get("env"),
            ['WAKE_REQUIRE_LINTERS: "1"'],
            f"the inline pin step env is {pin['sections'].get('env')}: WAKE_REQUIRE_LINTERS turns "
            "a missing linter into a failure, so this step cannot skip the control and pass",
        )
        # Order is load-bearing: the control needs the lockfile installed, and the step above it
        # is what installs it. A step moved above Install skips for the reason #216 names.
        self.assertLess(
            named.index("Install"),
            named.index("Inline suppressions bite"),
            f"the api steps are {named}: the pin runs before bun install --frozen-lockfile, so "
            "its control skips for the reason this test exists",
        )
        install = steps[named.index("Install")]
        self.assertEqual(
            install["values"].get("run"),
            "bun install --frozen-lockfile",
            f"the install step runs {install['values'].get('run')!r}: the pin's control reads "
            "node_modules/eslint, so the lockfile install is what makes it runnable",
        )
        # Every command this job runs is pinned by value here (#231). #216 pinned the job's keys,
        # the pin step's own body, its env and the install, but `Typecheck`, `Lint`, `Test`,
        # `Build` and the e2e step were matched by name alone: `run: "true"` swapped into `Test`
        # and `Lint`, or into `Typecheck` or `Build`, left this module at 75 OK with the JS suite
        # and the linter never run, and an e2e block stripped of its last line stayed green too -
        # measured at ebbb5906 in a full clone. A `run:` body is a decision like `uses` or
        # `python-version` is, so every one of them is pinned, and the step list is compared whole:
        # a step this job gains otherwise enters it unpinned and runs whatever it likes.
        allowed_keys = {
            "actions/checkout@v7": ["uses"],
            "oven-sh/setup-bun@v2": ["uses", "with"],
            "actions/setup-node@v7": ["uses", "with"],
            "Install": ["name", "run"],
            "actions/setup-python@v6": ["uses", "with"],
            "Inline suppressions bite": ["name", "env", "run"],
            "Linter configs bite": ["name", "env", "run"],
            "Typecheck": ["name", "run"],
            "Lint": ["name", "run"],
            "Test": ["name", "run"],
            "Build": ["name", "run"],
            "End-to-end threads over HTTP": ["name", "env", "run"],
        }
        # The step list is compared whole, `uses` steps included: reading only the run steps left
        # every `uses` step outside the pin, so `- uses: actions/cache@v4` added after `Build` kept
        # this module at 75 OK (measured at c39ecc9), and so did a swapped action version - the
        # same gap the lint pin closed by listing its `uses` steps by name.
        labels = [step["values"].get("name") or step["values"].get("uses") for step in steps]
        self.assertEqual(
            labels,
            list(allowed_keys),
            f"the api job's steps are {labels}, and this pin knows {list(allowed_keys)}: a step "
            "added here runs something no pin has measured, and one renamed, dropped or reordered "
            "changes what the job proves",
        )
        for step in steps:
            label = step["values"].get("name") or step["values"].get("uses")
            with self.subTest(step=label):
                self.assertEqual(
                    step["keys"],
                    allowed_keys[label],
                    f"{label} carries {step['keys']}: only {allowed_keys[label]} are allowed - a "
                    "`shell:`, a `working-directory:` or an added `env:` decides what the command "
                    "reads without changing a single value pinned below",
                )
        # What a `uses` step installs is a decision like a body is, and nothing above reads it:
        # `bun-version: 1.4.4` -> `1.4.3`, `node-version: "24"` -> `"22"` and
        # `python-version: "3.13"` -> `"3.12"` each kept this module green while changing the
        # toolchain every pinned body below runs on (measured at c39ecc9) - bun runs the JS suite
        # and `bun run lint`, node is in this job because eslint 10 needs `util.styleText`, and
        # python3 runs the inline pin. Each `with:` is pinned by value, as the lint job's
        # interpreter is.
        for label, expected in (
            ("oven-sh/setup-bun@v2", ["bun-version: 1.4.4"]),
            ("actions/setup-node@v7", ['node-version: "24"']),
            ("actions/setup-python@v6", ['python-version: "3.13"']),
        ):
            with self.subTest(step=label):
                with_section = steps[labels.index(label)]["sections"].get("with")
                self.assertEqual(
                    with_section,
                    expected,
                    f"{label} with is {with_section}: the toolchain is what every pinned body "
                    "runs on, so a different version fails here instead of quietly changing what "
                    "the job proves",
                )
        # What each step runs, read off the raw lines: ci_steps() keeps a `key: value` line and
        # drops the lines under a `run: |`, so the e2e block is read here, as the lint job's
        # bodies are read in test_ci_lint_job_keys_cannot_disarm_it.
        runs = run_bodies(block)
        self.assertEqual(
            runs,
            [
                "bun install --frozen-lockfile",
                "python3 tests/run_module_strict.py test_inline_suppressions",
                "python3 tests/run_module_strict.py test_linter_configs",
                "bun run typecheck",
                "bun run lint",
                "bun run test",
                "bun run build",
                # The e2e step creates a database of its own and then runs the script against it;
                # both halves are what the job proves, so both are pinned.
                [
                    'bun -e \'import { Client } from "pg";',
                    "const client = new Client({ connectionString: "
                    '"postgres://postgres:postgres@127.0.0.1:5432/postgres" });',
                    "await client.connect();",
                    'await client.query("DROP DATABASE IF EXISTS wake_e2e_test");',
                    'await client.query("CREATE DATABASE wake_e2e_test");',
                    "await client.end();'",
                    "bun scripts/e2e-threads.ts",
                ],
            ],
            f"the api job runs {runs}: a step whose body is not this one reports green without "
            'proving anything - `run: "true"` in `Test` and `Lint` left this module at 75 OK, and '
            "so did an e2e block with its last line removed (#231)",
        )
        # The e2e step proves two things at once: its block creates a database and its env is what
        # the script connects to. The block was pinned and the env was not, so
        # `DATABASE_URL: .../wake_test` kept this module at 75 OK while the script ran against the
        # suite's own database - the one thing the comment on the step says it must not share
        # (measured at c39ecc9). The env is pinned by value, and the name in it is compared with
        # the name the block creates, so the halves cannot drift apart.
        e2e = steps[labels.index("End-to-end threads over HTTP")]
        self.assertEqual(
            e2e["sections"].get("env"),
            ["DATABASE_URL: postgres://postgres:postgres@127.0.0.1:5432/wake_e2e_test"],
            f"the e2e step env is {e2e['sections'].get('env')}: the script registers agents and "
            "expects its own thread list, so it must not share wake_test with the suite above, and "
            "it must be the database the step itself creates",
        )
        creates = [line for line in run_bodies(block)[-1] if "CREATE DATABASE" in line]
        self.assertEqual(
            len(creates),
            1,
            f"the e2e block creates {creates}: one database, named once, or the env above can "
            "name a database nothing made",
        )
        created = re.search(r"CREATE DATABASE (\w+)", creates[0]).group(1)
        url = e2e["sections"]["env"][0].split("DATABASE_URL: ", 1)[1]
        self.assertEqual(
            url.rsplit("/", 1)[-1],
            created,
            f"the e2e step connects to {url} but creates {created}: the script would run against "
            "a database this step never made",
        )
        # The interpreter is the runner's default unless it is pinned, and a runner image bump
        # would then change what the pin reads while every value here stays the same.
        setups = [step for step in steps if (step["values"].get("uses") or "").startswith("actions/setup-python@")]
        self.assertEqual(
            len(setups),
            1,
            f"{len(setups)} setup-python steps: the pin needs exactly one pinned interpreter",
        )
        self.assertEqual(
            setups[0]["values"].get("uses"),
            "actions/setup-python@v6",
            f"setup-python action is {setups[0]['values'].get('uses')!r}: pinned by value like "
            "the runner, because `@v5` is a downgrade this pin would otherwise not notice",
        )
        self.assertEqual(
            setups[0]["sections"].get("with"),
            ['python-version: "3.13"'],
            f"setup-python with is {setups[0]['sections'].get('with')}: the interpreter is what "
            "runs the pin, so a different minor fails here instead of changing what it reads",
        )

    def test_ci_api_job_environment_services_and_runner_are_pinned(self):
        """#255: the api job's steps are pinned by value above, but its job-level values were not.
        `TEST_DATABASE_URL` is the connection the suite uses, and the postgres service image and
        ports decide what it connects to and which postgres the tests run on; `runs-on` decides
        the runner. Measured on Dr. Eggbot's review of #232 (6045430138): each of these edited with
        every step pin still green at 75 OK. The job's key list is pinned by the test above; this one
        pins what each of those keys holds, as the lint job's `runs-on` is pinned.
        """
        lines = text(os.path.join(ROOT, ".github/workflows/ci.yml")).splitlines()
        self.assertIn("  api:", lines, "the api (bun) job is gone")
        start = lines.index("  api:")
        block = []
        for line in lines[start + 1 :]:
            if line.startswith("  ") and not line.startswith("   ") and line.strip():
                break
            block.append(line)

        def job_section(key):
            """The non-comment lines under a job-level `key:`, indent kept, so a nested key can be told
            from a child of it."""
            found, body = False, []
            for line in block:
                if line.startswith("    ") and not line.startswith("     ") and line.strip():
                    found = line.strip() == f"{key}:"
                    continue
                if found and line.strip() and not line.strip().startswith("#"):
                    body.append(line.rstrip())
            return body

        runner = [line.strip() for line in block if line.startswith("    runs-on:")]
        self.assertEqual(
            runner,
            ["runs-on: ubuntu-24.04"],
            f"api job runs-on moved: {runner}: the runner decides what the api job runs on, so a "
            "floating or self-hosted image changes what it proves while every step stays green",
        )

        env = [line.strip() for line in job_section("env")]
        self.assertEqual(
            env,
            ["TEST_DATABASE_URL: postgres://postgres:postgres@127.0.0.1:5432/wake_test"],
            f"api job env moved: {env}: TEST_DATABASE_URL is the database the suite connects to, "
            "so pointing it elsewhere changes what the job proves without touching a step",
        )

        # The services block, one level down: a service key is one indent deeper than the service name, so
        # a key added to the postgres service is named in the failure rather than read as a second service.
        services = job_section("services")
        service_names = [
            line.strip() for line in services if line.startswith("      ") and not line.startswith("       ")
        ]
        self.assertEqual(
            service_names,
            ["postgres:"],
            f"api job services are {service_names}: the suite runs against one postgres service, "
            "and a second service or a renamed one changes what the job connects to",
        )
        pg = {}
        current = None
        for line in services:
            if line.startswith("      ") and not line.startswith("       "):
                continue
            if line.startswith("        ") and not line.startswith("         "):
                key, _, rest = line.strip().partition(":")
                current = key
                pg[key] = [rest.strip()] if rest.strip() else []
            elif current is not None:
                pg[current].append(line.strip())
        self.assertEqual(
            list(pg),
            ["image", "env", "ports", "options"],
            f"api job services.postgres keys are {list(pg)}: a key added to the service changes the "
            "database the tests run against, so each one is listed here",
        )
        self.assertEqual(
            pg["image"],
            ["postgres:17"],
            f"api job services.postgres.image moved: {pg['image']}: the image decides the postgres "
            "version the suite runs on, so a different tag changes what the job proves",
        )
        self.assertEqual(
            pg["env"],
            ["POSTGRES_USER: postgres", "POSTGRES_PASSWORD: postgres", "POSTGRES_DB: wake_test"],
            f"api job services.postgres.env moved: {pg['env']}: these create the user, password and "
            "database that TEST_DATABASE_URL above names",
        )
        self.assertEqual(
            pg["ports"],
            ["- 5432:5432"],
            f"api job services.postgres.ports moved: {pg['ports']}: the host port is the one "
            "TEST_DATABASE_URL connects to",
        )
        self.assertEqual(
            pg["options"],
            [
                ">-",
                '--health-cmd "pg_isready -U postgres -d wake_test"',
                "--health-interval 5s",
                "--health-timeout 5s",
                "--health-retries 10",
            ],
            f"api job services.postgres.options moved: {pg['options']}: the health check decides when "
            "the suite starts against the database, so a changed probe changes when it runs",
        )


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
        # signal that was ignored on entry, so a nohup'd or backgrounded suite runs the wait's own
        # window out and then fails on what that window means) and machine speed (a slow first poll
        # defers the trap; that is correct behaviour, not a failure).
        ask = "[2026-11-15T12:00] human:tester (ask): what is the eta"
        old = "[2026-11-15T12:01] bot-other (reply): an older answer"
        self.stub_rounds([[ask, old]])
        # Wake-wait's own window. The exit after the interrupt is bounded by it, so the wait below
        # is derived from this number rather than from a second, drifting one.
        window = 30
        base = {
            "PATH": self.bin + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": self.home,
        }
        proc = subprocess.Popen(
            [os.path.join(OPS, "wake-wait"), "t-1", "--timeout", str(window), "--interval", "1"],
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
            while len(self.called()) < 2 and proc.poll() is None and time.time() < deadline:
                time.sleep(0.02)
            self.assertGreaterEqual(
                len(self.called()), 2, "the interrupt would race the baseline: the stub answered once"
            )
            proc.send_signal(signal.SIGINT)
            # A trap that never fires leaves the loop at its own deadline, so the process cannot
            # outlive the window by more than the poll it is inside: waiting on the exit is bounded
            # too, with ten seconds of headroom for a slow runner. A bare communicate(timeout=20)
            # instead raised TimeoutExpired whenever the exit landed past 20 s, which is a fact about
            # the clock and not about the trap (#223); this says which condition failed.
            leave_by = time.time() + window + 10
            while proc.poll() is None and time.time() < leave_by:
                time.sleep(0.05)
            self.assertIsNotNone(
                proc.poll(),
                f"wake-wait was still polling {window + 10}s after the interrupt; an interrupt must end the wait",
            )
            out, err = proc.communicate()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
        self.assertEqual(proc.returncode, 130, "the interrupt did not end the wait: " + out + err)
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
