"""Every command wake sends is pinned here, so a change to one is a decision (#167).

The host gate performs only the shapes it knows and refuses everything else, and a refusal is
silent inside wake: the call fails, wake reads nothing, and `--inbox` answers from the slow
per-thread fallback instead of the store. `tests/test_gate_shapes.py` checks the allow-list, but
only against a gate *staged on the machine*, and CI never has one - so a pull request could change
a command shape and go green while every gated host quietly stopped using it. That is how #166
shipped a new store-wide read: the route had to be written by hand into the candidate gate, and
nothing in the repository would have gone red if it had not been.

Committing a gate to the repository would close that gap and contradict the design note that where
a gate lives is a property of one machine, which is a decision for the maintainer. This file takes
the other half: the shapes are pinned. A change to any command the client builds fails in CI until
the author edits PINNED in the same commit, and that edit is the review signal that the installed
gate needs a new route before the client ships.

The proof of a pin is its mutation table, not its passing run: a pin cannot fail on the state it
describes. #167's pull request mutates the store read, the append prologue, the register tail and
the inbox read one at a time and records what each one did.
"""

import json
import os
import re
import shutil
import unittest

try:  # `tests/run.sh` runs `discover -s tests`, which puts tests/ on sys.path
    from test_gate_shapes import exercise, exercise_inbox_io  # noqa: E402
    from test_wake import IS_ROOT, ROOT_SKIP, WakeTestCase, slurp, write_exe  # noqa: E402
except ImportError:  # `python3 -m unittest tests.test_gate_pins` from the repo root
    from tests.test_gate_shapes import exercise, exercise_inbox_io  # noqa: E402
    from tests.test_wake import IS_ROOT, ROOT_SKIP, WakeTestCase, slurp, write_exe  # noqa: E402

#: Every distinct command `bin/wake` and `bin/wake-inbox-io` send, normalized: `{T}` the thread
#: store, `{I}` the inbox, `{A}` its active-marker directory, `{K}` one thread id or inbox id, and
#: `{P}` a pid. One entry per shape, in sorted order.
#:
#: A change here means an installed gate will refuse the new shape until it is taught the route,
#: so the gate install belongs in the same change. Everything a shape carries that the gate's
#: literal has to reproduce - the `@@wake-file` / `@@wake-fail` markers below, the `chmod 660`
#: tail, the `2>/dev/null` on every read, the order of the append's two branches - is part of the
#: entry on purpose.
PINNED = (
    # wake.thread_append: both branches in one command, so the gate owns the choice.
    "mkdir -p {T} 2>/dev/null || sudo mkdir -p {T}; if [ -e {T}/{K}.jsonl ]; then "
    "dd of={T}/{K}.jsonl oflag=append conv=notrunc,nocreat status=none; "
    "else cat >> {T}/{K}.jsonl; fi",
    # wake.thread_read: --thread, --reply, and --inbox's per-thread fallback
    "cat {T}/{K}.jsonl 2>/dev/null",
    # wake.threads_all: the one store-wide read (#165)
    'for f in {T}/*.jsonl; do [ -f "$f" ] || continue; printf \'\\n@@wake-file %s\\n\' "${f##*/}"; '
    'cat "$f" || printf \'\\n@@wake-fail %s\\n\' "${f##*/}"; done 2>/dev/null',
    # wake.threads_all_one_by_one: the listing it asks for after a refused store read
    "ls {T} 2>/dev/null",
    # wake.store_dir_unreadable: the empty answer's confirmation, marked with each entry's type
    # so a `*.jsonl` directory or a dangling symlink is not read as an unreadable store (#186)
    "ls -F {T} 2>/dev/null",
    # wake.inbox_live
    "ls -t {A}/*.json 2>/dev/null | head -10",
    "cat {A}/{K}.json 2>/dev/null",
    "test -d /proc/{P}",
    # wake.inbox_push
    "mkdir -p {I} 2>/dev/null; touch {I}/{K}.jsonl; cat >> {I}/{K}.jsonl",
    # wake-inbox-io register: the `chmod 660` tail is matched literally by the gate
    "mkdir -p {A} 2>/dev/null; find {A} -name '*.json' -mmin +1440 -delete 2>/dev/null; "
    "touch {I}/{K}.jsonl {A}/{K}.json; chmod 660 {I}/{K}.jsonl {A}/{K}.json 2>/dev/null",
    # wake-inbox-io beat
    "cat > {A}/{K}.json",
    # wake-inbox-io read, when the store cannot be read here and the call goes to the host
    "cat {I}/{K}.jsonl 2>/dev/null",
)

#: The one shape this file cannot drive as root: with a uid that ignores file modes the inbox
#: stays locally readable, so the host call is never made. CI and `tests/run.sh` run as a normal
#: user; a root run leaves this entry out of the comparison instead of failing on it.
HOST_ONLY = ("cat {I}/{K}.jsonl 2>/dev/null",)


def normalize(commands, state):
    """The paths a run happened to use, replaced by the placeholders PINNED is written in.

    The state directory is per-run and a thread id carries the clock, so neither can appear in a
    pin. Longest path first: `{A}` lives inside `{I}`, which lives beside `{T}`.
    """
    out = []
    for cmd in commands:
        text = cmd
        for label, path in (
            ("{A}", os.path.join(state, "inbox", "active")),
            ("{I}", os.path.join(state, "inbox")),
            ("{T}", os.path.join(state, "threads")),
        ):
            text = text.replace(path, label)
        text = re.sub(
            r"(\{T\}|\{I\}|\{A\})/[A-Za-z0-9][A-Za-z0-9._-]*\.(jsonl|json)",
            lambda m: m.group(1) + "/{K}." + m.group(2),
            text,
        )
        text = re.sub(r"/proc/[0-9]+", "/proc/{P}", text)
        out.append(text)
    return out


def _session(test, log, name):
    """A stub host that records every command, and one live session marker pointing at an inbox."""
    hostops = os.path.join(test.fakebin, name)
    write_exe(
        hostops,
        f"""
        #!/bin/sh
        printf '%s\\n' "$1" >> {log}
        case "$1" in
            "for f in "*) printf 'refused by the gate\\n' >&2; exit 1 ;;
        esac
        exec bash -c "$1"
        """,
    )
    test.put_key("bot-engineer-0002")
    inbox = os.path.join(test.state, "inbox", "s1.jsonl")
    os.makedirs(os.path.join(test.state, "inbox", "active"), exist_ok=True)
    with open(os.path.join(test.state, "inbox", "active", "s1.json"), "w") as f:
        json.dump({"inbox": inbox, "pid": os.getpid()}, f)
    return hostops, inbox


def refused_store_read(test):
    """The shapes a gated host produces when its gate refuses the store-wide read.

    A gate that predates #165 refuses the one call with a message on stderr, which wake reads as
    `bridge_failed`: the store read returns None and `--inbox` lists and reads one thread per call.
    Those two shapes are what every host with the installed v7 gate sends today, so they are
    pinned too. Driven by refusing that command rather than by an unreadable file, so the result
    depends on no file mode and no uid.
    """
    log = os.path.join(test.tmp, "hostops-refused.log")
    hostops, inbox = _session(test, log, "hostops-refused")
    env = {"WAKE_HOSTOPS": hostops}
    r = test.wake("--ask", "engineer", "refused store read", env=dict(env, WAKE_INBOX=inbox, WAKE_SOURCE="skill:agent"))
    test.assertEqual(r.returncode, 0, r.stderr)
    open(log, "w").close()

    r = test.wake("--inbox", env=dict(env, WAKE_INBOX=inbox, WAKE_SOURCE="skill:agent"))
    test.assertEqual(r.returncode, 0, r.stdout + r.stderr)
    return slurp(log).splitlines()


def inbox_read_over_the_host(test):
    """`wake-inbox-io read` when the store is readable to the host but not to this process.

    Every other shape reaches the host by itself; this one takes the store being unreadable here,
    which is a file mode, so it is driven with the directory at 000 and skipped as root.
    """
    log = os.path.join(test.tmp, "hostops-inbox-read.log")
    hostops, _inbox = _session(test, log, "hostops-inbox-read")
    base = os.path.join(test.state, "inbox")
    os.makedirs(base, exist_ok=True)
    open(log, "w").close()
    os.chmod(base, 0o000)
    try:
        r = test.run_bin("wake-inbox-io", "read", os.path.join(base, "abc123.jsonl"), env={"WAKE_HOSTOPS": hostops})
    finally:
        os.chmod(base, 0o700)
    test.assertEqual(r.returncode, 0, r.stderr)
    return slurp(log).splitlines()


def empty_store_confirmation(test):
    """The empty answer's confirmation, which is the one shape a store with threads never builds.

    `--inbox` asks it only when nothing came back (#168), so the pinned marked listing is reached
    here or not at all: a store with no entries, driven through a stub host that runs everything.
    """
    log = os.path.join(test.tmp, "hostops-empty-store.log")
    hostops, _inbox = _session(test, log, "hostops-empty-store")
    store = os.path.join(test.state, "threads")
    shutil.rmtree(store, ignore_errors=True)
    os.makedirs(store)
    open(log, "w").close()
    r = test.wake("--inbox", env={"WAKE_HOSTOPS": hostops, "WAKE_SOURCE": "bot:engineer"})
    test.assertEqual(r.returncode, 0, r.stdout + r.stderr)
    return slurp(log).splitlines()


def every_shape(test):
    """Every command the two programs built, in the order they were seen."""
    commands = (
        list(exercise(test)) + list(exercise_inbox_io(test)) + refused_store_read(test) + empty_store_confirmation(test)
    )
    if not IS_ROOT:
        commands += inbox_read_over_the_host(test)
    return normalize(commands, test.state)


def expected():
    """PINNED, less the shapes this run cannot drive."""
    return [c for c in PINNED if not (IS_ROOT and c in HOST_ONLY)]


class ShapePinTests(WakeTestCase):
    def test_every_command_wake_sends_is_pinned(self):
        built = sorted(set(every_shape(self)))
        want = sorted(expected())
        if built != want:
            self.fail(
                "the commands wake sends no longer match PINNED, so an installed gate will refuse a "
                "shape it was never taught and wake will fall back without saying so.\n"
                + "".join(f"  added:   {c}\n" for c in built if c not in want)
                + "".join(f"  removed: {c}\n" for c in want if c not in built)
                + "Update PINNED in this file in the same commit, and stage the gate route in the "
                "same change: gate first, client second (#89)."
            )

    def test_every_pinned_shape_is_built_by_a_real_run(self):
        """A pin no run reaches is a rule nobody keeps, and a stale one is worse than none: each
        entry has to be produced by the run above, not just be plausible."""
        built = set(every_shape(self))
        unreached = [c for c in expected() if c not in built]
        self.assertEqual(unreached, [], "pinned shapes no run built:\n" + "\n".join(unreached))

    def test_the_store_read_still_carries_both_markers(self):
        """The gate's store-read literal is written by hand from this text, and the failure marker
        is what keeps a short inbox from hiding a reply: without it a file that cannot be read
        produces neither a record nor an error, and `--inbox` answers as if the thread never
        existed (#166). Both markers are inside the pinned entry; this test names why.
        """
        store = [c for c in PINNED if c.startswith("for f in ")]
        self.assertEqual(len(store), 1, store)
        self.assertIn("@@wake-file", store[0])
        self.assertIn("@@wake-fail", store[0])

    def test_the_fallback_is_part_of_the_pin(self):
        """A gated host that has not been taught the store-wide read still has to work, so the
        listing and the per-thread read it uses instead are pinned like everything else."""
        shapes = normalize(refused_store_read(self), self.state)
        self.assertIn("ls {T} 2>/dev/null", shapes)
        self.assertIn("cat {T}/{K}.jsonl 2>/dev/null", shapes)

    @unittest.skipIf(IS_ROOT, ROOT_SKIP)
    def test_the_host_inbox_read_is_pinned(self):
        shapes = normalize(inbox_read_over_the_host(self), self.state)
        self.assertEqual(shapes, list(HOST_ONLY))

    @unittest.skipIf(IS_ROOT, ROOT_SKIP)
    def test_an_unreadable_thread_file_still_asks_for_the_same_shapes(self):
        """The unreadable-file path (#166) changes what wake prints, not what it sends: the store
        read, the listing and the per-thread read. Proven so a later fix that adds a call of its
        own cannot slip past the pin."""
        log = os.path.join(self.tmp, "hostops-unreadable.log")
        hostops, inbox = _session(self, log, "hostops-unreadable")
        env = {"WAKE_HOSTOPS": hostops}
        r = self.wake("--ask", "engineer", "hidden file", env=dict(env, WAKE_INBOX=inbox, WAKE_SOURCE="skill:agent"))
        self.assertEqual(r.returncode, 0, r.stderr)
        thread = os.path.join(self.state, "threads", os.listdir(os.path.join(self.state, "threads"))[0])
        # tearDown chmods 600 before it removes the tree, so this needs no cleanup of its own
        os.chmod(thread, 0o000)
        open(log, "w").close()
        r = self.wake("--inbox", env=dict(env, WAKE_INBOX=inbox, WAKE_SOURCE="skill:agent"))
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("cannot read the thread store", r.stderr)
        for shape in normalize(slurp(log).splitlines(), self.state):
            self.assertIn(shape, PINNED, f"{shape} is not pinned")


if __name__ == "__main__":
    unittest.main()
