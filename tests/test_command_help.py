"""Every command answers --help, and every command is documented.

CONTRIBUTING asks a script in `bin/` to carry a one-line `# desc:` header, a row in a README and a
test. This is the test a script cannot pass by existing: it runs each command with `--help` in an
empty environment and requires its usage on stdout with exit 0, before anything else in the
command can run.

Two of them used to do real work here: `wake-api-e2e --help` ran the whole end-to-end suite and
`wake-pingall --help` asked a bot named `--help` a question. A help path is also the only place an
operator sees a command's exit codes and environment, so the check is on the bytes the command
prints, not on whether it survived.
"""

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "bin"
OPS = CORE / "ops"


def commands():
    """Every executable command in bin/ and bin/ops, in a stable order."""
    found = []
    for directory in (CORE, OPS):
        for path in sorted(directory.iterdir()):
            if path.is_file() and path.suffix != ".py" and os.access(path, os.X_OK):
                found.append(path)
    return found


def environment(home):
    """The smallest environment a command can run in: no wake config, no registry, no token."""
    return {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(home),
        "TMPDIR": str(home),
        "LANG": "C.UTF-8",
        # bin/wake imports bin/wake_config.py: without this the check caches it in the checkout
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def problem(path, flag="--help"):
    """None when the command answers `flag` with its usage on stdout and exit 0, else why not."""
    with tempfile.TemporaryDirectory() as home:
        try:
            r = subprocess.run(
                [str(path), flag],
                capture_output=True,
                text=True,
                timeout=120,
                stdin=subprocess.DEVNULL,
                cwd=home,
                env=environment(home),
            )
        except subprocess.TimeoutExpired:
            return f"{flag} did not return within 120s"
    if r.returncode != 0:
        said = (r.stderr or r.stdout or "").strip()
        return f"{flag} exited {r.returncode}, not 0: {said[:200]}"
    lines = (r.stdout or "").strip().splitlines()
    if not lines:
        return f"{flag} printed nothing on stdout"
    expected = "usage: " + path.name
    if not lines[0].startswith(expected):
        return f"{flag} printed {lines[0][:80]!r}, not {expected!r}"
    return None


class CommandHelpTestCase(unittest.TestCase):
    def test_every_command_answers_help_with_its_usage(self):
        for path in commands():
            with self.subTest(command=path.name):
                self.assertIsNone(
                    problem(path),
                    f"{path.relative_to(ROOT)} does not answer --help with its usage and exit 0",
                )

    def test_every_command_answers_the_short_form_too(self):
        for path in commands():
            with self.subTest(command=path.name):
                self.assertIsNone(problem(path, "-h"), f"{path.name} does not answer -h")

    def test_every_command_carries_a_desc_line(self):
        for path in commands():
            with self.subTest(command=path.name):
                head = "\n".join(path.read_text(errors="replace").splitlines()[:3])
                match = re.search(r"(?m)^# desc: (\S.{19,})$", head)
                self.assertIsNotNone(
                    match,
                    f"{path.relative_to(ROOT)} has no '# desc: <what it does>' line",
                )

    def test_every_command_is_named_in_a_readme(self):
        readmes = {"core": (ROOT / "README.md").read_text(), "ops": (OPS / "README.md").read_text()}
        for path in commands():
            where = "ops" if path.parent == OPS else "core"
            with self.subTest(command=path.name):
                self.assertIn(
                    "`" + path.name + "`",
                    readmes[where],
                    f"{path.name} is not named in the {where} README",
                )

    def test_the_installer_answers_help_without_installing_anything(self):
        """`./install.sh --help` must be read, not run: an installer that installs on --help is a trap."""
        with tempfile.TemporaryDirectory() as home:
            prefix = Path(home) / "prefix"
            r = subprocess.run(
                [str(ROOT / "install.sh"), "--help"],
                capture_output=True,
                text=True,
                timeout=60,
                stdin=subprocess.DEVNULL,
                cwd=home,
                env=dict(environment(home), PREFIX=str(prefix)),
            )
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue(r.stdout.startswith("usage: ./install.sh"), f"--help printed {r.stdout[:80]!r}")
            self.assertFalse(prefix.exists(), "--help created the install prefix")

    def test_a_helper_that_takes_no_option_refuses_one(self):
        """--help is the only unknown flag that is answered; another one is a mistake, not a no-op."""
        for name in ("wake-keys", "wake-api-e2e"):
            path = OPS / name
            with tempfile.TemporaryDirectory() as home:
                r = subprocess.run(
                    [str(path), "--bogus"],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    stdin=subprocess.DEVNULL,
                    cwd=home,
                    env=environment(home),
                )
            self.assertEqual(r.returncode, 2, f"{name} accepted --bogus instead of refusing it: {r.stdout[:120]}")
            self.assertIn("--help", r.stderr, f"{name} refused --bogus without pointing at --help")

    def test_the_check_rejects_a_command_that_ignores_help(self):
        """The assertions above must fail on a command with no help path, or they prove nothing."""
        with tempfile.TemporaryDirectory() as tmp:
            silent = Path(tmp) / "selftest-silent"
            silent.write_text("#!/bin/sh\nexit 0\n")
            silent.chmod(0o755)
            self.assertIsNotNone(problem(silent), "a silent command passed")
            usage_but_failed = Path(tmp) / "selftest-usage-but-failed"
            usage_but_failed.write_text("#!/bin/sh\nprintf 'usage: selftest-usage-but-failed\\n'\nexit 1\n")
            usage_but_failed.chmod(0o755)
            self.assertIsNotNone(problem(usage_but_failed), "a failing help path passed")
            wrong_text = Path(tmp) / "selftest-wrong-text"
            wrong_text.write_text("#!/bin/sh\nprintf 'PASS initialize\\n'\n")
            wrong_text.chmod(0o755)
            self.assertIsNotNone(problem(wrong_text), "a command that printed work output passed")


if __name__ == "__main__":
    unittest.main()
