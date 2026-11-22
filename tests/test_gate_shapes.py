"""The host gate only performs the exact command shapes wake sends.

`rakazo-wake-gate` refuses anything else, so a shape that drifts inside wake turns
into a silent no-op: the call fails, wake reads nothing, and an inbox delivery
disappears. These tests run wake's real builders against a host stub that executes
them, capture every command, and check each one against the gate allow-list when a
gate is staged on this computer (`WAKE_GATE`, default the newest pinned
`rakazo-wake-gate.vN` in the ops-review staging directory, because the bare name
there is restaged under a running check).

Without a staged gate the allow-list check is skipped (CI has no gate), but the
shape invariants below still run: the append never changes a mode and every read
silences stderr, which is what the gate's read routes match on.

The half CI can check is `tests/test_gate_pins.py`: every command wake builds is
pinned there, so a shape that changes fails the suite even with no gate staged, and
the diff that updates the pin is the review signal for a new gate route (#167).
"""

import contextlib
import io
import json
import os
import re
import shutil
import stat
import sys
import syslog
import tempfile
import types
import unittest
from unittest import mock

try:  # `tests/run.sh` runs `discover -s tests`, which puts tests/ on sys.path
    from test_wake import WakeTestCase, slurp, write_exe  # noqa: E402
except ImportError:  # `python3 -m unittest tests.test_gate_shapes` from the repo root
    from tests.test_wake import WakeTestCase, slurp, write_exe  # noqa: E402

GATE_STAGE = os.environ.get("WAKE_GATE_STAGE", "")


def staged_gate():
    """The gate to check against: the newest pinned `rakazo-wake-gate.vN` in the staging
    directory, else the bare name. The bare file is restaged under a running check, so a
    pinned copy is what the assertions can rely on.

    Empty when no staging directory is configured: where a gate lives is a property of
    one machine, never of the repository."""
    if not GATE_STAGE:
        return ""
    best = None
    try:
        names = os.listdir(GATE_STAGE)
    except OSError:
        names = []
    for name in names:
        m = re.fullmatch(r"rakazo-wake-gate\.v(\d+)", name)
        if m and (best is None or int(m.group(1)) > best[0]):
            best = (int(m.group(1)), name)
    if best:
        return os.path.join(GATE_STAGE, best[1])
    return os.path.join(GATE_STAGE, "rakazo-wake-gate")


GATE_DEFAULT = staged_gate()


def exercise(test):
    """Drive ask, thread, reply and inbox through a host stub that runs every command,
    and return the commands that actually reached the host."""
    log = os.path.join(test.tmp, "hostops.log")
    hostops = os.path.join(test.fakebin, "hostops")
    write_exe(
        hostops,
        f"""
        #!/bin/sh
        printf '%s\\n' "$1" >> {log}
        exec bash -c "$1"
        """,
    )
    test.put_key("bot-engineer-0002")
    inbox = os.path.join(test.state, "inbox", "s1.jsonl")
    os.makedirs(os.path.join(test.state, "inbox", "active"))
    with open(os.path.join(test.state, "inbox", "active", "s1.json"), "w") as f:
        json.dump({"inbox": inbox, "pid": os.getpid()}, f)

    env = {"WAKE_HOSTOPS": hostops}
    # An empty store is the one place the client asks whether the store can be entered at all,
    # and it asks with `ls -F <store>`: the marked listing #186 added and gate v10 routes. Ask
    # before the first thread exists so the allow-list check covers that shape too, otherwise a
    # gate without the route (v9) passes every test here while the client silently falls back to
    # the plain listing that is wrong on a host with a `*.jsonl` directory in the store.
    os.makedirs(os.path.join(test.state, "threads"), exist_ok=True)  # an installed store, still empty
    r = test.wake("--inbox", env=dict(env, WAKE_SOURCE="skill:agent"))
    test.assertEqual(r.returncode, 0, r.stderr)
    test.assertIn("no threads opened by skill:agent", r.stdout)

    r = test.wake("--ask", "engineer", "gate shapes", env=dict(env, WAKE_INBOX=inbox, WAKE_SOURCE="skill:agent"))
    test.assertEqual(r.returncode, 0, r.stderr)
    tid = re.search(r"thread (\S+) opened", r.stdout).group(1)

    r = test.wake("--thread", tid, env=env)
    test.assertEqual(r.returncode, 0, r.stderr)
    test.assertIn("gate shapes", r.stdout)

    r = test.wake("--reply", tid, "done", env=dict(env, WAKE_SOURCE="bot:engineer"))
    test.assertEqual(r.returncode, 0, r.stderr)
    test.assertIn("delivered into the owner's session", r.stdout)

    r = test.wake("--inbox", env=dict(env, WAKE_SOURCE="skill:agent"))
    test.assertEqual(r.returncode, 0, r.stderr)

    return slurp(log).splitlines()


def exercise_inbox_io(test):
    """Drive register, beat and read through a host stub that runs every command, and return
    the commands that reached the host. The inbox directory is removed before each call so
    `sh()` takes the host path: it prefers the local path whenever the directory is writable.
    """
    log = os.path.join(test.tmp, "hostops-inbox.log")
    hostops = os.path.join(test.fakebin, "hostops-inbox")
    write_exe(
        hostops,
        f"""
        #!/bin/sh
        printf '%s\\n' "$1" >> {log}
        exec bash -c "$1"
        """,
    )
    base = os.path.join(test.state, "inbox")
    env = {"WAKE_HOSTOPS": hostops}
    shutil.rmtree(base, ignore_errors=True)
    r = test.run_bin("wake-inbox-io", "register", "abc123", '{"pid": 1}', env=env)
    test.assertEqual(r.returncode, 0, r.stderr)
    box = r.stdout.strip()
    test.assertEqual(box, os.path.join(base, "abc123.jsonl"))
    # A guest shell has the directory but cannot write it: registering is what made it.
    # Deleting it here would ask the host to write a marker into a missing directory, which a
    # real guest never does, and which `wake-inbox-io` now reports as the failure it is.
    os.chmod(base, 0o500)
    try:
        r = test.run_bin("wake-inbox-io", "beat", "abc123", '{"pid": 2}', env=env)
        test.assertEqual(r.returncode, 0, r.stderr)
        r = test.run_bin("wake-inbox-io", "read", box, env=env)
        test.assertEqual(r.returncode, 0, r.stderr)
    finally:
        os.chmod(base, 0o700)
    return slurp(log).splitlines()


class CommandShapeTests(WakeTestCase):
    def test_append_never_changes_modes_and_reads_silence_stderr(self):
        cmds = exercise(self)
        self.assertTrue(any(".jsonl" in c for c in cmds), cmds)
        for c in cmds:
            # a thread id is four hex digits, so "1777" or "666" can appear in a name: only chmod matters
            self.assertNotIn("chmod", c)
            if c.startswith("cat "):
                self.assertTrue(c.endswith("2>/dev/null"), c)


class StagedGateCase(WakeTestCase):
    """Base for the tests that need a gate artifact staged on this machine.

    Where a gate lives is a property of one machine, so the repository has no opinion: without
    `WAKE_GATE` (one artifact) or `WAKE_GATE_STAGE` (the staging directory) these tests skip.
    """

    def setUp(self):
        super().setUp()
        self.gate_path = os.environ.get("WAKE_GATE") or GATE_DEFAULT
        if not self.gate_path or not os.path.exists(self.gate_path):
            where = self.gate_path or "no path configured"
            self.skipTest(f"no staged gate ({where}): set WAKE_GATE or WAKE_GATE_STAGE to check wake's commands")

    def load_gate(self):
        mod = types.ModuleType("wake_gate")
        mod.__file__ = self.gate_path
        with open(self.gate_path, encoding="utf-8") as f:
            exec(compile(f.read(), self.gate_path, "exec"), mod.__dict__)  # noqa: S102 - the staged gate
        if not hasattr(mod, "ROUTES"):
            self.skipTest("staged gate is the old pattern list (no ROUTES/match); re-stage the allow-list gate")
        threads = os.path.join(self.state, "threads")
        inbox = os.path.join(self.state, "inbox")
        mod.THREADS, mod.INBOX = threads, inbox
        mod.ACTIVE = os.path.join(inbox, "active")
        mod._TEMPLATES = [
            (label, tpl.replace("{T}", threads).replace("{A}", mod.ACTIVE).replace("{I}", inbox))
            for label, tpl in mod.ROUTES
        ]
        return mod


class GateAllowListTests(StagedGateCase):
    def test_every_command_wake_sends_is_allow_listed(self):
        gate = self.load_gate()
        cmds = exercise(self)
        # The marked listing is the shape this test exists to protect (#186): assert the driver
        # still reaches it, so deleting the empty-store `--inbox` cannot turn a missing route back
        # into a silent pass.
        self.assertTrue(
            any(c.startswith("ls -F ") for c in cmds),
            "exercise() no longer reaches store_dir_unreadable(); the allow-list check would miss "
            "the `ls -F` route: " + repr(cmds),
        )
        refused = [c for c in cmds if gate.match(c) is None]
        self.assertEqual(refused, [], "the gate would refuse these wake commands:\n" + "\n".join(refused))

    def test_every_inbox_io_command_is_allow_listed(self):
        gate = self.load_gate()
        refused = [c for c in exercise_inbox_io(self) if gate.match(c) is None]
        self.assertEqual(refused, [], "the gate would refuse these wake-inbox-io commands:\n" + "\n".join(refused))


def _chmod_if_there(path, mode):
    """Restore a store dir's mode, tolerating a state directory `tearDown` already removed."""
    try:
        os.chmod(path, mode)
    except FileNotFoundError:
        pass


class GateStoreListingTests(StagedGateCase):
    """A store dir the gate cannot list is refused by name, never answered as an empty store.

    Every answer the gate gives is built from a listing, and `list_dir()` / `list_active()` turn a
    lost directory into `[]`: a store that cannot be read then answers "no threads" with rc 0, and
    the caller cannot tell it from a quiet morning (#168). v8 holds that line only by accident -
    each handler that lists the dir lets the opendir's EACCES escape as an `OSError`, which `run()`
    prints as `io error: Permission denied` with no path, and the routes that do not touch the
    broken dir answer normally - so the guarantee sits in three handlers, not in the check, and one
    `except FileNotFoundError: return []` around the same call would drop it. A gate from v11 on
    proves listability in `_check_store()` before any handler runs and names the dir it cannot read;
    v9 and v10 do not carry it, and v10 is what that check has to be added to (v10 is newer than v9
    and also carries the `ls -F` route #186 needs, so v11 = v10 + this check is the one that ships).

    Against a v8 or v10 gate these tests fail. That is the point: the gate is installed before the client
    that relies on it, so the staging directory shows the order the two artifacts ship in.
    """

    ROUTES = ("thread list", "thread store read", "inbox active list")

    def store(self):
        """The three dirs the loaded gate points at, emptied, and restored to 0755 on cleanup.

        `WakeTestCase` deletes the state directory in `tearDown`, and cleanups run after it, so the
        restore has to tolerate a path that is already gone.
        """
        gate = self.load_gate()
        for d in (gate.THREADS, gate.INBOX, gate.ACTIVE):
            os.makedirs(d, exist_ok=True)
            self.addCleanup(_chmod_if_there, d, 0o755)
            for name in os.listdir(d):
                os.unlink(os.path.join(d, name))
        return gate

    def answer(self, gate, label):
        """Run one allow-listed route through the gate's own `run()`, as sshd would."""
        cmd = next(t for lab, t in gate._TEMPLATES if lab == label)
        out, err = io.BytesIO(), io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = gate.run(cmd, b"", out)
        return rc, out.getvalue(), err.getvalue()

    @unittest.skipUnless(os.geteuid() != 0, "root lists a 0300 dir, so the refusal cannot be measured as root")
    def test_an_unlistable_store_dir_is_refused_by_name(self):
        gate = self.store()
        for d in (gate.THREADS, gate.INBOX, gate.ACTIVE):
            os.chmod(d, 0o300)  # stat-able and searchable, so every check before the listing passes
            try:
                for label in self.ROUTES:
                    rc, body, err = self.answer(gate, label)
                    self.assertEqual(rc, 1, f"{label} answered {rc} with {d} unlistable: {body!r}")
                    self.assertEqual(body, b"", f"{label} answered data from an unlistable store")
                    self.assertIn("store path cannot be listed: " + d, err, label)
            finally:
                os.chmod(d, 0o755)

    def test_a_readable_store_still_answers(self):
        """No over-refusal: the check must not cost a working store its answers."""
        gate = self.store()
        with open(os.path.join(gate.THREADS, "11141808-0a61.jsonl"), "w") as f:
            f.write('{"kind":"ask"}\n')
        rc, body, err = self.answer(gate, "thread list")
        self.assertEqual((rc, err), (0, ""), err)
        self.assertIn(b"11141808-0a61.jsonl", body)
        rc, body, err = self.answer(gate, "thread store read")
        self.assertEqual((rc, err), (0, ""), err)
        self.assertTrue(body.startswith(b"\n@@wake-file 11141808-0a61.jsonl\n"), body)
        rc, body, err = self.answer(gate, "inbox active list")
        self.assertEqual((rc, body, err), (0, b"", ""), "an idle session list is quiet, not refused")

    def test_a_quiet_store_stays_quiet(self):
        """An empty store is the one answer rc 0 is for, and it is the one a broken store must not
        be able to imitate."""
        gate = self.store()
        for label in self.ROUTES:
            rc, body, err = self.answer(gate, label)
            self.assertEqual((rc, body, err), (0, b"", ""), label)


class InboxIoShapeTests(WakeTestCase):
    def test_register_keeps_the_mode_tail_the_gate_matches_literally(self):
        """The gate's inbox-register route is an exact literal that includes the chmod tail, so
        dropping it refuses registration on a gated host instead of fixing anything. The tail
        asks for `660`, and it is still not what sets the mode: the store inbox dir is 2770, and
        the gate creates both files at 660 itself. `rakazo-wake-gate.v7` accepts this tail;
        against v6 the allow-list test above fails, which is the order the two artifacts ship
        in: gate first, client second.
        """
        cmds = exercise_inbox_io(self)
        reg = [c for c in cmds if "chmod " in c]
        self.assertEqual(len(reg), 1, cmds)
        self.assertRegex(reg[0], r"; chmod 660 \S+\.jsonl \S+\.json 2>/dev/null$")
        self.assertTrue(reg[0].startswith("mkdir -p "), reg[0])

    def test_register_leaves_both_files_at_660(self):
        """The tail runs for real on an ungated host, where the registering process owns both
        files, so the mode it asks for is the mode they get. `666` made an inbox that anyone the
        directory admits could append to; `660` is owner plus group `wake`, which is every
        writer a 2770 parent admits. This is the assertion that fails on the old tail.
        """
        base = os.path.join(self.state, "inbox")
        shutil.rmtree(base, ignore_errors=True)
        r = self.run_bin("wake-inbox-io", "register", "abc123", '{"pid": 1}', env={"WAKE_HOSTOPS": ""})
        self.assertEqual(r.returncode, 0, r.stderr)
        for path in (os.path.join(base, "abc123.jsonl"), os.path.join(base, "active", "abc123.json")):
            self.assertTrue(os.path.exists(path), path)
            mode = stat.S_IMODE(os.stat(path).st_mode)
            self.assertEqual(mode, 0o660, f"{path} is {mode:o}, not 660")


class GateAppendOwnershipTests(StagedGateCase):
    """An append to an existing store file must not ask the kernel to create it.

    The host sets `fs.protected_regular = 2` and the store is `3770 root:wake` (setgid + sticky),
    so an `O_CREAT` open in that sticky, group-writable directory is refused with `EACCES` when
    the file belongs to neither the caller nor the directory's owner. The gate runs as `wakeops`
    and the store belongs to `root`, so a thread file a host-side client created as `hitori` -
    `_host_run` runs the shell directly when there is no `WAKE_HOSTOPS` - was un-appendable:
    reads answered normally, every append was refused as a bare `errno=13`, and nine messages
    were lost between 2026-11-19 and 2026-11-23 (#221). The flags are what this test can hold on
    any machine: the ownership that triggers the refusal cannot be set up by a single-user test,
    and CI runs as one user, so the test asserts the open never asks to create an existing file.

    The `ENOENT` retry on both writers is deliberately not pinned: it only shortens a race that
    already existed (the file vanishing between `lstat` and `open`), and no single-user test can
    lose that race on demand.
    """

    APPEND = (
        "mkdir -p {T} 2>/dev/null || sudo mkdir -p {T}; "
        "if [ -e {T}/{K}.jsonl ]; then dd of={T}/{K}.jsonl "
        "oflag=append conv=notrunc,nocreat status=none; else cat >> {T}/{K}.jsonl; fi"
    )

    def store(self):
        gate = self.load_gate()
        for d in (gate.THREADS, gate.INBOX, gate.ACTIVE):
            os.makedirs(d, exist_ok=True)
        path = os.path.join(gate.THREADS, "11141808-0a61.jsonl")
        cmd = self.APPEND.replace("{T}", gate.THREADS).replace("{K}", "11141808-0a61")
        return gate, path, cmd

    def open_flags(self, call):
        """The file flags `call()` used, via a recording `os.open` the loaded gate also sees."""
        seen = []
        real_open = os.open

        def record(path, flags, *args, **kwargs):
            seen.append((path, flags))
            return real_open(path, flags, *args, **kwargs)

        with mock.patch.object(os, "open", record):
            call()
        return seen

    def test_an_append_to_an_existing_file_does_not_pass_o_creat(self):
        gate, path, cmd = self.store()
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"kind":"ask"}\n')
        seen = self.open_flags(lambda: gate.run(cmd, b'{"kind":"reply"}\n', io.BytesIO()))
        written = [(p, fl) for p, fl in seen if p == path]
        self.assertEqual(len(written), 1, f"expected one open of {path}: {seen}")
        self.assertFalse(
            written[0][1] & os.O_CREAT,
            "the append asks the kernel to create a file that already exists, which a store file "
            "owned by another user answers with EACCES (#221)",
        )
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), '{"kind":"ask"}\n{"kind":"reply"}\n')

    def test_a_missing_thread_file_is_still_created(self):
        """The fix must not stop the gate from opening a new thread: no over-refusal."""
        gate, path, cmd = self.store()
        self.assertFalse(os.path.exists(path))
        seen = self.open_flags(lambda: gate.run(cmd, b'{"kind":"ask"}\n', io.BytesIO()))
        written = [(p, fl) for p, fl in seen if p == path]
        self.assertEqual(len(written), 1, seen)
        self.assertTrue(written[0][1] & os.O_CREAT, "a missing thread file is opened without O_CREAT")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o660, "a new store file is not 0660")

    def test_a_refused_append_names_the_key_and_the_target(self):
        """A bare errno said a message was lost, never which thread (#301)."""
        gate, path, cmd = self.store()
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"kind":"ask"}\n')
        real_open = os.open

        def refuse(path_, flags, *args, **kwargs):
            if path_ == path and flags & os.O_WRONLY:
                raise PermissionError(13, "Permission denied", path_)
            return real_open(path_, flags, *args, **kwargs)

        err = io.StringIO()
        with mock.patch.object(os, "open", refuse), contextlib.redirect_stderr(err):
            rc = gate.run(cmd, b'{"kind":"reply"}\n', io.BytesIO())
        message = err.getvalue()
        self.assertEqual(rc, 1, message)
        self.assertIn("errno=13", message)
        self.assertIn("key=11141808-0a61", message)
        self.assertIn("target=" + path, message)
        self.assertNotIn("(None)", message)

    def marker_store(self):
        """The marker route's state: an existing `{A}/{K}.json` and the command that writes it."""
        gate = self.load_gate()
        for d in (gate.THREADS, gate.INBOX, gate.ACTIVE):
            os.makedirs(d, exist_ok=True)
        path = os.path.join(gate.ACTIVE, "11141808-0a61.json")
        return gate, path, "cat > " + path

    def test_a_marker_write_to_an_existing_file_does_not_pass_o_creat(self):
        """The same open on the other route v12 changed (#313).

        `inbox marker write` goes through `write_bytes`, not `append_bytes`, and the store's
        `inbox/active` is 2770 without the sticky bit, so `fs.protected_regular` cannot fire there
        today. Hardening that directory would bring back the silent loss, so the flag contract is
        held on this route too rather than left to the one directory that happens to be sticky.
        """
        gate, path, cmd = self.marker_store()
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"pid": 1}\n')
        seen = self.open_flags(lambda: gate.run(cmd, b'{"pid": 2}', io.BytesIO()))
        written = [(p, fl) for p, fl in seen if p == path]
        self.assertEqual(len(written), 1, f"expected one open of {path}: {seen}")
        self.assertFalse(
            written[0][1] & os.O_CREAT,
            "the marker write asks the kernel to create a file that already exists, which a "
            "hardened `inbox/active` answers with EACCES (#313)",
        )
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), '{"pid": 2}')

    def test_a_missing_marker_file_is_still_created(self):
        """No over-refusal: a first beat for a key must still create its marker at 0660."""
        gate, path, cmd = self.marker_store()
        self.assertFalse(os.path.exists(path))
        seen = self.open_flags(lambda: gate.run(cmd, b'{"pid": 2}', io.BytesIO()))
        written = [(p, fl) for p, fl in seen if p == path]
        self.assertEqual(len(written), 1, seen)
        self.assertTrue(written[0][1] & os.O_CREAT, "a missing marker is opened without O_CREAT")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o660, "a new marker is not 0660")

    def refuse_open(self, path, exc):
        """An `os.open` that raises `exc` for the store file and passes every other open through."""
        real_open = os.open

        def refuse(path_, flags, *args, **kwargs):
            if path_ == path and flags & os.O_WRONLY:
                raise exc
            return real_open(path_, flags, *args, **kwargs)

        return refuse

    def test_a_refusal_with_no_strerror_says_unknown_error(self):
        """`e.strerror or "unknown error"` (#294): a bare `OSError(13)` carries `strerror is None`."""
        gate, path, cmd = self.store()
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"kind":"ask"}\n')
        err = io.StringIO()
        with mock.patch.object(os, "open", self.refuse_open(path, OSError(13))), contextlib.redirect_stderr(err):
            rc = gate.run(cmd, b'{"kind":"reply"}\n', io.BytesIO())
        message = err.getvalue()
        self.assertEqual(rc, 1, message)
        self.assertIn("unknown error", message, "a None strerror is not named as such")
        self.assertNotIn("(None)", message)

    def test_a_refusal_reaches_the_journal_with_the_same_three_fields(self):
        """The CHANGELOG sells a journal line naming the thread; only stderr was pinned (#313)."""
        gate, path, cmd = self.store()
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"kind":"ask"}\n')
        logged = []
        with (
            mock.patch.object(syslog, "syslog", lambda fn, msg: logged.append(msg)),
            mock.patch.object(os, "open", self.refuse_open(path, PermissionError(13, "Permission denied", path))),
        ):
            rc = gate.run(cmd, b'{"kind":"reply"}\n', io.BytesIO())
        self.assertEqual(rc, 1)
        refusals = [m for m in logged if " refuse " in m]
        self.assertTrue(refusals, f"the refusal was not audited at all: {logged}")
        detail = refusals[-1]
        self.assertIn("errno=13", detail)
        self.assertIn("key=11141808-0a61", detail)
        self.assertIn("target=" + path, detail)


class StagedGatePickTests(unittest.TestCase):
    def test_pinned_gate_wins_over_the_bare_name_and_versions_compare_as_numbers(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        for name in (
            "rakazo-wake-gate",
            "rakazo-wake-gate.v2",
            "rakazo-wake-gate.v10",
            "rakazo-wake-gate.v9.bak",
        ):
            open(os.path.join(tmp, name), "w").close()
        with mock.patch.object(sys.modules[__name__], "GATE_STAGE", tmp):
            self.assertEqual(staged_gate(), os.path.join(tmp, "rakazo-wake-gate.v10"))

    def test_bare_name_is_the_fallback_when_nothing_is_pinned(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        open(os.path.join(tmp, "rakazo-wake-gate"), "w").close()
        with mock.patch.object(sys.modules[__name__], "GATE_STAGE", tmp):
            self.assertEqual(staged_gate(), os.path.join(tmp, "rakazo-wake-gate"))

    def test_missing_staging_directory_does_not_raise(self):
        with mock.patch.object(sys.modules[__name__], "GATE_STAGE", os.path.join(tempfile.mkdtemp(), "gone")):
            self.assertTrue(staged_gate().endswith("rakazo-wake-gate"))

    def test_no_staging_directory_configured_means_no_gate(self):
        # Where a gate is staged is a property of one machine, so the repository has no
        # opinion: without WAKE_GATE_STAGE (or WAKE_GATE) the allow-list tests skip.
        with mock.patch.object(sys.modules[__name__], "GATE_STAGE", ""):
            self.assertEqual(staged_gate(), "")


if __name__ == "__main__":
    unittest.main()
