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
"""

import importlib.machinery
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from test_wake import WakeTestCase, slurp, write_exe  # noqa: E402

GATE_STAGE = "/home/rakazo/shared/ops-review"


def staged_gate():
    """The gate to check against: the newest pinned `rakazo-wake-gate.vN` in the staging
    directory, else the bare name. The bare file is restaged under a running check, so a
    pinned copy is what the assertions can rely on."""
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


class CommandShapeTests(WakeTestCase):
    def test_append_never_changes_modes_and_reads_silence_stderr(self):
        cmds = exercise(self)
        self.assertTrue(any(".jsonl" in c for c in cmds), cmds)
        for c in cmds:
            # a thread id is four hex digits, so "1777" or "666" can appear in a name: only chmod matters
            self.assertNotIn("chmod", c)
            if c.startswith("cat "):
                self.assertTrue(c.endswith("2>/dev/null"), c)


class GateAllowListTests(WakeTestCase):
    def setUp(self):
        super().setUp()
        self.gate_path = os.environ.get("WAKE_GATE", GATE_DEFAULT)
        if not os.path.exists(self.gate_path):
            self.skipTest(f"no staged gate at {self.gate_path}: set WAKE_GATE to check wake's commands against it")

    def load_gate(self):
        loader = importlib.machinery.SourceFileLoader("wake_gate", self.gate_path)
        spec = importlib.util.spec_from_loader("wake_gate", loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
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

    def test_every_command_wake_sends_is_allow_listed(self):
        gate = self.load_gate()
        refused = [c for c in exercise(self) if gate.match(c) is None]
        self.assertEqual(refused, [], "the gate would refuse these wake commands:\n" + "\n".join(refused))


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


if __name__ == "__main__":
    unittest.main()
