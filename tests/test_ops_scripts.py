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
import signal
import subprocess
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
    base = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": tempfile.mkdtemp(prefix="ops-home-")}
    base.update(env or {})
    return subprocess.run([os.path.join(OPS, name), *args], capture_output=True, text=True, env=base, timeout=60)


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

    def test_the_e2e_script_refuses_a_missing_checkout(self):
        r = run("wake-api-e2e", env={"WAKE_REPO": tempfile.mkdtemp(prefix="not-a-checkout-")})
        self.assertEqual(r.returncode, 1)
        self.assertIn("no wake checkout", r.stderr)
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

    def test_wake_pingall_refuses_an_empty_registry(self):
        home = tempfile.mkdtemp(prefix="ping-")
        reg = os.path.join(home, "bots.tsv")
        with open(reg, "w", encoding="utf-8") as f:
            f.write("vault\tbot-vault-0008\tVault\tprivate\tnotes\n")
        r = run("wake-pingall", env={"WAKE_REGISTRY": reg, "WAKE_PING_LOG_DIR": home})
        self.assertEqual(r.returncode, 1)
        self.assertIn("no open bots", r.stderr)

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


class WakeWaitTestCase(unittest.TestCase):
    """wake-wait blocks on one thread. Its only interface is `wake --thread`, so a stub stands in
    for the CLI here: the real store is exercised by tests/test_wake.py."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="wait-")
        self.bin = os.path.join(self.home, "bin")
        os.makedirs(self.bin)
        self.calls = os.path.join(self.home, "calls.log")
        self.stub("exit 0")

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
        )
        try:
            time.sleep(1.5)  # one poll has taken its baseline and is between polls
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
