"""Inline suppressions: the directive a linter reads out of the very file it is checking.

#185 pinned the config files a linter loads on its own (`pyproject.toml`, a `.ruff.toml`, a
`.shellcheckrc`). A suppression comment is the same trust boundary one level in: it needs no new
file, it sits in the source CI already lints, and every linter honours it. Measured at `e4ca592`
(ruff 0.17.5, shellcheck 0.11.0), clean clone, with a real `F821`/`SC2154` planted as the error:

    `# ruff: noqa` as a standalone     ->  `ruff check .` "All checks passed!", rc 0
    comment line, anywhere in the          `ruff format --check .` rc 0
    file, with a real F821 further         the whole suite `489 tests ... OK (skipped=3)`
    down

    a bare `# noqa` trailing that      ->  `ruff check .` rc 0 (rc 1 with the comment removed)
    same offending line

    `# shellcheck disable=SC2154`      ->  the shipped shellcheck line, rc 1 -> rc 0
    on the line above a real SC2154
    in `scripts/scrub-check.sh`

    `# fmt: off` at line 1             ->  `ruff format --check .` rc 0 with the file misformatted

    `# yapf: disable` at line 1        ->  `ruff format --check .` rc 0 with the file misformatted
                                           (rc 1 with the comment removed)

    `# isort: skip_file`,              ->  `ruff check .` rc 0 with a real I001 in the file
    `# ruff: isort: skip_file`,            (`# isort: skip_file` and `# ruff: isort: skip_file` are
    `# isort: off` with no `on`            the same directive to ruff; `# isort: off` exempts the
                                           rest of the file when no `on` closes the block)

    `# ruff: disable[F821]` /          ->  `ruff check .` rc 0 with a real F821 further down
    `# ruff: ignore[F821]`                 (rc 1 with the comment removed)

The rule is not a ban: the tree legitimately uses code-scoped suppressions, and a pin that refused
them would be turned off rather than obeyed. It is green on the tree today (no file-level form, no
bare `noqa`, no `# fmt:` / `# yapf:` / `# isort:` / `# ruff: disable[` at all, and the only two
`# shellcheck disable=` both name one code and carry a justification), and it still closes every arm
above:

1. a `noqa` must name the code it covers - `# noqa: E402` passes, a bare `# noqa` and a malformed
   `# noqa:` are refused;
2. the file-level `# ruff: noqa` / `# flake8: noqa` forms are refused outright: they silence the
   codes they name - or every rule - for the whole file, far from the line that needed them;
3. a `# shellcheck disable=` must name exactly one code and carry a justification comment, as the
   two shipped ones do (`# shellcheck disable=SC2016  # the script is quoted for ...`);
4. a formatter directive is refused outright: it names no code, and `# fmt: off` or
   `# yapf: disable` at line 1 exempts a whole file from `ruff format --check .`;
5. an isort directive is refused outright: `skip_file` exempts the whole file's import order,
   `off` exempts everything below it until an `on` that may never come, and `skip` exempts one
   import line, and none of them names a code. Written as `# isort: ...` or `# ruff: isort: ...`,
   the same directive to ruff;
6. a `# ruff: disable[CODE]` / `enable[CODE]` / `ignore[CODE]` rule switch is refused: it turns a
   rule off for everything below it and names no line, so the finding it hides is not on the
   comment's own line to review.

Spelled `noq[a]` in this file's own regexes so the patterns do not trip ruff's
"invalid `# noqa` directive" warning on the scanner that refuses them.

Python comments are read with `tokenize`, so the same text inside a docstring or a string literal
is not a directive - ruff does not honour it there either (measured). Shell files are read line by
line and a directive has to begin its line, which is shellcheck's own placement rule: SC1126
already refuses a trailing one, so nothing can hide a disable at the end of a command.

Run with the rest: `python3 -m unittest discover -s tests`
"""

import io
import os
import re
import shutil
import subprocess
import tempfile
import tokenize
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: The line marker, and everything after it: `: E402`, `: E402 - why`, or nothing at all.
NOQA = re.compile(r"#\s*noq[a](?P<rest>.*)$", re.IGNORECASE)

#: The file-level form. Ruff matches it anywhere in a comment, even after other text.
FILE_NOQA = re.compile(r"#\s*(?:ruff|flake8)\s*:\s*noq[a]", re.IGNORECASE)

#: `# shellcheck disable=SC2016  # why` - `codes` stops at whitespace or a second `#`.
SHELLCHECK_DISABLE = re.compile(r"^#\s*shellcheck\s+disable=(?P<codes>[^\s#]*)(?P<rest>.*)$")

#: A lint code as both tools spell it: letters then digits (`E402`, `BLE001`, `SC2154`).
CODE = re.compile(r"[A-Z][A-Z0-9]*[0-9]")

#: A shellcheck code, the only shape its `disable=` accepts.
SHELLCHECK_CODE = re.compile(r"SC[0-9]{4}")

#: A formatter directive, as ruff's formatter honours it: the fmt and yapf spellings drive the same
#: switch. There is no code to name, so the only pinned count is zero.
FMT_DIRECTIVE = re.compile(r"#\s*(?:fmt|yapf)\s*:\s*(?:off|on|skip|disable|enable)\b", re.IGNORECASE)

#: An import-order directive. `skip_file` exempts the whole file, `off` the rest of it, `skip` one
#: line; ruff reads it with or without the `ruff:` prefix.
ISORT_DIRECTIVE = re.compile(r"#\s*(?:ruff\s*:\s*)?isort\s*:\s*(?:skip_file|skip|off|on)\b", re.IGNORECASE)

#: A file- or block-wide rule switch, the bracket form ruff added for one rule at a time.
RULE_SWITCH = re.compile(r"#\s*ruff\s*:\s*(?:disable|enable|ignore)\s*\[", re.IGNORECASE)

#: How much text counts as a reason for a `# shellcheck disable=`.
MIN_JUSTIFICATION = 10

#: The only `# shellcheck disable=` the tree may carry. A new one lists itself here, with its code
#: and its reason, in the same commit.
PINNED_SHELLCHECK_DISABLES = ("bin/ops/wake-gate-check:90", "bin/ops/wake-wait:105")

#: Files each scan must read, so a scan that silently read nothing fails.
PINNED_PYTHON_FILES = ("bin/ops/wake-shape-check", "bin/wake", "tests/test_wake.py")
PINNED_SHELL_FILES = (
    "bin/ops/wake-gate-check",
    "bin/ops/wake-wait",
    "install.sh",
    "scripts/scrub-check.sh",
    "tests/run.sh",
)


def text(path):
    with open(path, encoding="utf-8", errors="surrogateescape") as handle:
        return handle.read()


def first_line(root, rel):
    """The file's `#!` line, or empty when it has none or cannot be read."""
    try:
        with open(os.path.join(root, rel), "rb") as handle:
            return handle.readline().decode("utf-8", "surrogateescape")
    except OSError:
        return ""


def tracked_paths(root=ROOT):
    """Every tracked path, as git reports it."""
    try:
        listing = subprocess.run(["git", "-C", root, "ls-files", "-z"], check=True, capture_output=True).stdout
    except (OSError, subprocess.CalledProcessError) as error:
        raise unittest.SkipTest(f"not a git checkout, cannot list tracked files: {error}") from error
    return [raw.decode("utf-8", "surrogateescape") for raw in listing.split(b"\0") if raw]


def linters_files(root=ROOT, paths=None):
    """`(python, shell)`: the tracked files each linter reads, told apart by suffix and shebang.

    Python is every `.py` file plus the suffixless scripts `extend-include` adds; shell is every
    file whose shebang is a shell, a superset of the list the lint job hands to shellcheck - a
    suppression in a helper lint does not read yet is still this class.
    """
    python, shell = [], []
    for rel in tracked_paths(root) if paths is None else paths:
        if rel.endswith(".py"):
            python.append(rel)
            continue
        head = first_line(root, rel)
        if not head.startswith("#!"):
            continue
        if "python" in head:
            python.append(rel)
        elif re.match(r"^#!/(usr/)?bin/(env )?(ba)?sh", head):
            shell.append(rel)
    return sorted(python), sorted(shell)


def noqa_codes(rest):
    """The codes a `noqa` names, in order. Empty means it exempts its line and names nothing."""
    rest = rest.strip()
    if not rest.startswith(":"):
        return []
    codes = []
    for token in (part for part in re.split(r"[,\s]+", rest[1:]) if part):
        if not CODE.fullmatch(token):
            break
        codes.append(token)
    return codes


def python_comments(source):
    """`(line, comment)` for the real comments in a python source: not strings, not docstrings."""
    found = []
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type == tokenize.COMMENT:
                found.append((token.start[0], token.string))
    except (tokenize.TokenError, IndentationError):
        pass
    return found


def python_suppression_problems(root, paths):
    """Every suppression in a python file ruff lints that exempts more than it names."""
    problems = []
    for rel in paths:
        for line, comment in python_comments(text(os.path.join(root, rel))):
            where = f"{rel}:{line}: '{comment.strip()}'"
            if FILE_NOQA.search(comment):
                problems.append(
                    f"{where} is the file-level form: it silences the codes it names, or every rule, for the "
                    "whole file while `ruff check .` still exits 0 - put the codes on the line they cover, or "
                    "fix the finding (#191)"
                )
                continue
            marker = NOQA.search(comment)
            if marker and not noqa_codes(marker.group("rest")):
                problems.append(
                    f"{where} names no code: a bare `# noqa` silences every rule on that line at `ruff check .` "
                    "rc 0, and a malformed one is a keystroke away from it - write `# noqa: <CODE>` (#191)"
                )
            if FMT_DIRECTIVE.search(comment):
                problems.append(
                    f"{where} exempts code from `ruff format --check .`: `# fmt: off` or `# yapf: disable` at "
                    "line 1 took a whole misformatted file to rc 0. Format the code, and if a block is "
                    "deliberate, this pin grows a PINNED entry in the same commit (#191)"
                )
            if ISORT_DIRECTIVE.search(comment):
                problems.append(
                    f"{where} exempts import order from `ruff check .`: `# isort: skip_file` took a real I001 "
                    "in the file to rc 0, `# isort: off` does the same for everything below it, and `# isort: "
                    "skip` for one import line. Sort the imports, and if an order is deliberate, this pin "
                    "grows a PINNED entry in the same commit (#191)"
                )
            if RULE_SWITCH.search(comment):
                problems.append(
                    f"{where} switches a rule off below it: `# ruff: disable[F821]` / `# ruff: ignore[F821]` "
                    "took a real F821 further down the file to rc 0, and the hidden finding is not on the "
                    "comment's own line. Write `# noqa: <CODE>` on the line it covers, or fix the finding "
                    "(#191)"
                )
    return problems


def shell_suppression_problems(root, paths):
    """Every `# shellcheck disable=` that names more than one code, or carries no justification."""
    problems = []
    for rel in paths:
        for number, line in enumerate(text(os.path.join(root, rel)).splitlines(), start=1):
            stripped = line.strip()
            directive = SHELLCHECK_DISABLE.match(stripped)
            if not directive:
                continue
            where = f"{rel}:{number}: '{stripped}'"
            codes = [code for code in re.split(r"[,\s]+", directive.group("codes")) if code]
            if len(codes) != 1 or not SHELLCHECK_CODE.fullmatch(codes[0]):
                problems.append(
                    f"{where} must name one code: `disable=all` or a list silences a whole command, and the "
                    "shipped shellcheck step then exits 0 with the warning still in the file (#191)"
                )
            reason = re.match(r"^\s+#\s+(?P<reason>\S.*)$", directive.group("rest"))
            if not reason or len(reason.group("reason").strip()) < MIN_JUSTIFICATION:
                problems.append(
                    f"{where} carries no justification: a trailing `# why this warning is not a bug` is what "
                    "makes a disable reviewable, and every disable in the tree has one (#191)"
                )
    return problems


class InlineSuppressionTestCase(unittest.TestCase):
    """#191: a suppression inside a linted file is pinned, so one comment cannot disarm CI."""

    maxDiff = None

    def test_the_tree_names_every_suppression(self):
        """The shipped suppressions are code-scoped, and each shellcheck disable says why it is there."""
        python, shell = linters_files()
        self.assertEqual(
            python_suppression_problems(ROOT, python),
            [],
            "an inline suppression exempts more than it names: the file-level `# ruff: noqa` and a bare "
            "`# noqa` both hide a real error from `ruff check .` while every other check stays green (#191)",
        )
        self.assertEqual(
            shell_suppression_problems(ROOT, shell),
            [],
            "a `# shellcheck disable=` is unpinned: one directive above a warning takes the shipped "
            "shellcheck step from rc 1 to rc 0 (#191)",
        )

    def test_the_scans_read_the_files_the_linters_read(self):
        """A scan that read nothing would pass every pin above silently."""
        python, shell = linters_files()
        self.assertGreaterEqual(len(python), 20, f"the python scan read only {len(python)} files")
        self.assertGreaterEqual(len(shell), 5, f"the shell scan read only {len(shell)} files")
        for rel in PINNED_PYTHON_FILES:
            self.assertIn(rel, python, f"ruff lints {rel}, and the python scan does not read it")
        for rel in PINNED_SHELL_FILES:
            self.assertIn(rel, shell, f"shellcheck lints {rel}, and the shell scan does not read it")
        found = [
            f"{rel}:{number}"
            for rel in shell
            for number, line in enumerate(text(os.path.join(ROOT, rel)).splitlines(), start=1)
            if SHELLCHECK_DISABLE.match(line.strip())
        ]
        self.assertEqual(
            found,
            list(PINNED_SHELLCHECK_DISABLES),
            "the shipped shellcheck directives moved: a new one carries one code and a justification and is "
            "listed in PINNED_SHELLCHECK_DISABLES; a dropped one leaves the list (#191)",
        )
        comments = sum(len(python_comments(text(os.path.join(ROOT, rel)))) for rel in python)
        self.assertGreater(comments, 100, f"the python comment scan found only {comments} comments")

    def test_a_file_level_noqa_is_refused(self):
        """Measured: a standalone `# ruff: noqa` anywhere in the file, with a real F821 further down."""
        for comment in ("# ruff: noqa", "# ruff:noqa", "# RUFF: NOQA", "# flake8: noqa", "# ruff: noqa: F821"):
            with self.subTest(comment=comment):
                problems = self.problems(python=f"SEED = 1\n{comment}\nX = undefined_name\n")
                self.assertEqual(len(problems), 1, problems)
                self.assertIn("file-level form", problems[0])

    def test_the_file_level_form_is_caught_after_other_text(self):
        """Measured: `# do not add # ruff: noqa` silences the file too, so the match is not anchored."""
        problems = self.problems(python="SEED = 1\n# do not add # ruff: noqa\nX = undefined_name\n")
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("file-level form", problems[0])

    def test_a_bare_noqa_is_refused(self):
        """A bare `# noqa` trailing the offending line is the same hole on one line instead of a file."""
        for comment in ("# noqa", "#noqa", "# NOQA", "# see # noqa", "# noqa:", "# noqa: ,"):
            with self.subTest(comment=comment):
                problems = self.problems(python=f"X = undefined_name  {comment}\n")
                self.assertEqual(len(problems), 1, problems)
                self.assertIn("names no code", problems[0])

    def test_a_code_scoped_noqa_passes(self):
        """The form the tree uses: the codes, optionally with a reason after a dash."""
        for comment in (
            "# noqa: E402",
            "# noqa: E402, F401",
            "# noqa:F821",
            "# noqa: BLE001 - a gate that will not import is the caller's input",
        ):
            with self.subTest(comment=comment):
                self.assertEqual(self.problems(python=f"X = undefined_name  {comment}\n"), [])

    def test_a_directive_inside_a_string_is_not_a_directive(self):
        """`tokenize` sees the comments ruff honours, not the same words inside a docstring."""
        source = '"""A file-level `# ruff: noqa` and a bare `# noqa` are refused here."""\nX = 1  # noqa: E402\n'
        self.assertEqual(self.problems(python=source), [])
        self.assertEqual(len(python_comments(source)), 1)

    def test_a_formatter_directive_is_refused(self):
        """Measured: `# fmt: off` and `# yapf: disable` at line 1 each took `ruff format --check .` to rc 0."""
        for comment in ("# fmt: off", "# fmt:skip", "# fmt: on", "# FMT: OFF"):
            with self.subTest(comment=comment):
                problems = self.problems(python=f"X = 1  {comment}\n")
                self.assertEqual(len(problems), 1, problems)
                self.assertIn("ruff format --check", problems[0])
        for comment in ("# yapf: disable", "# yapf: enable", "# YAPF: DISABLE", "# yapf:disable"):
            with self.subTest(comment=comment):
                problems = self.problems(python="x = {'a':1}\n" + comment + "\n")
                self.assertEqual(len(problems), 1, problems)
                self.assertIn("ruff format --check", problems[0])

    def test_an_isort_directive_is_refused(self):
        """Measured: `# isort: skip_file`, `# ruff: isort: skip_file` and `# isort: off` each hid a real I001."""
        for comment in (
            "# isort: skip_file",
            "# isort:skip_file",
            "# ruff: isort: skip_file",
            "# RUFF: ISORT: SKIP_FILE",
            "# isort: off",
            "# isort: skip",
        ):
            with self.subTest(comment=comment):
                problems = self.problems(python=f"import sys\nimport os\n{comment}\n")
                self.assertEqual(len(problems), 1, problems)
                self.assertIn("import order", problems[0])

    def test_a_rule_switch_is_refused(self):
        """Measured: `# ruff: disable[F821]` and `# ruff: ignore[F821]` each took a real F821 to rc 0."""
        for comment in (
            "# ruff: disable[F821]",
            "# ruff: ignore[F821]",
            "# ruff: disable[F821, E501]",
            "# RUFF: DISABLE[F821]",
        ):
            with self.subTest(comment=comment):
                problems = self.problems(python=f"SEED = 1\n{comment}\nX = undefined_name\n")
                self.assertEqual(len(problems), 1, problems)
                self.assertIn("switches a rule off", problems[0])

    def test_the_spellings_ruff_honours_are_not_confused_with_prose(self):
        """A docstring naming the forms is not a directive, same as the `noqa` arm."""
        source = (
            '"""`# yapf: disable`, `# isort: skip_file` and `# ruff: disable[F821]` are refused here."""\nSEED = 1\n'
        )
        self.assertEqual(self.problems(python=source), [])

    def test_a_shellcheck_disable_needs_one_code_and_a_reason(self):
        """`disable=all` and a list are blanket silences; both shipped directives name one code and say why."""
        cases = {
            "# shellcheck disable=all\n": "must name one code",
            "# shellcheck disable=SC2086,SC2154\n": "must name one code",
            "# shellcheck disable=\n": "must name one code",
            "# shellcheck disable=SC2154\n": "carries no justification",
            "# shellcheck disable=SC2154  # nope\n": "carries no justification",
        }
        for line, expected in cases.items():
            with self.subTest(line=line):
                problems = self.problems(shell=line + ': "${undefined_zz}"\n')
                self.assertTrue(problems, f"{line!r} was accepted")
                self.assertIn(expected, " ".join(problems))
        self.assertEqual(
            self.problems(shell="# shellcheck disable=SC2154  # an awk program: $0 and $1 are awk's\n: 1\n"),
            [],
            "the shipped form was refused",
        )

    def test_the_shell_scan_bites_on_a_copy_of_a_real_helper(self):
        """The scan runs on the tree's own bash helper, not only on a three-line fixture."""
        root = tempfile.mkdtemp(prefix="inline-")
        try:
            source = text(os.path.join(ROOT, "bin/ops/wake-wait"))
            self.assertIn("# shellcheck disable=SC2016", source, "the shipped directive moved")
            planted = source.replace("# shellcheck disable=SC2016", "# shellcheck disable=all", 1)
            self.assertNotEqual(planted, source, "the replace did not land")
            with open(os.path.join(root, "wake-wait"), "w", encoding="utf-8") as handle:
                handle.write(planted)
            problems = shell_suppression_problems(root, ["wake-wait"])
            self.assertEqual(len(problems), 1, problems)
            self.assertIn("must name one code", problems[0])
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_both_scans_bite_on_a_fixture(self):
        """The fixtures are files on disk, so a broken path or shebang read cannot pass silently."""
        for kind, body, expected in (
            ("py", "X = 1  # noqa\n", "names no code"),
            ("py", "# ruff: noqa\nX = 1\n", "file-level form"),
            ("py", "X = 1  # fmt: off\n", "ruff format --check"),
            ("sh", "#!/bin/bash\n# shellcheck disable=all\n: 1\n", "must name one code"),
        ):
            with self.subTest(kind=kind, expected=expected):
                root = tempfile.mkdtemp(prefix="inline-")
                try:
                    name = os.path.join(root, "planted." + kind)
                    with open(name, "w", encoding="utf-8") as handle:
                        handle.write(body)
                    python_paths, shell_paths = linters_files(root, ["planted." + kind])
                    problems = python_suppression_problems(root, python_paths) + shell_suppression_problems(
                        root, shell_paths
                    )
                    self.assertTrue(problems, f"{body!r} was accepted")
                    self.assertIn(expected, " ".join(problems))
                finally:
                    shutil.rmtree(root, ignore_errors=True)

    def problems(self, python="", shell=""):
        """The scan's verdict on fixture files, without touching the tree."""
        root = tempfile.mkdtemp(prefix="inline-")
        try:
            paths = []
            if python:
                with open(os.path.join(root, "planted.py"), "w", encoding="utf-8") as handle:
                    handle.write(python)
                paths.append("planted.py")
            if shell:
                with open(os.path.join(root, "planted.sh"), "w", encoding="utf-8") as handle:
                    handle.write("#!/bin/bash\n" + shell)
                paths.append("planted.sh")
            python_paths, shell_paths = linters_files(root, paths)
            return python_suppression_problems(root, python_paths) + shell_suppression_problems(root, shell_paths)
        finally:
            shutil.rmtree(root, ignore_errors=True)
