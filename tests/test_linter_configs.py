"""The linters' own config files: the ones ruff and shellcheck read without being asked.

`lint`'s workflow is pinned by value - `runs-on`, the job keys, every step key, its `run` payload
and the interpreter - but the files the tools load on their own were pinned by nothing (#185).
Measured at `33578c5`, one line in `[tool.ruff]` still disarmed the job:

    extend-exclude = ["tests"]     ->  `ruff check .` "All checks passed!", rc 0
                                       with a planted F821 in tests/test_wake.py
                                       26 files read became 12, both lint pins green

    a root .shellcheckrc
    reading disable=all            ->  the shipped shellcheck line, rc 0, warning invisible

Same shape as #181 one level out: the workflow can no longer be disarmed, its tool configuration
could. These pins are static on purpose: the `test` job runs on Python 3.9 to 3.13 with no ruff
and no shellcheck installed, so they read the config files rather than run the tools.

Three things decide what the `lint` job sees, and all three are read here:

1. `pyproject.toml` `[tool.ruff*]` - `select`, `ignore`, `extend-exclude`, `exclude`,
   `respect-gitignore`, `per-file-ignores` and a `[tool.ruff.format]` table all change the verdict.
2. any *other* config file by a name ruff or shellcheck looks for, at any depth: ruff takes the
   nearest `.ruff.toml`/`ruff.toml`/`pyproject.toml` as the whole configuration for the files
   under it, and shellcheck reads a `.shellcheckrc` (or `shellcheckrc`, with no dot) from the
   working directory and from each script's own directory.
3. `.gitignore` (`file_resolver.respect_gitignore = true` in `ruff check --show-settings`) and
   `.ignore`, which it honours the same way: with either file naming `tests/`, `ruff check .` reads
   12 files instead of 27. Git never mentions `.ignore`, so it is refused by name below rather than
   left to `git check-ignore`.

Run with the rest: `python3 -m unittest discover -s tests`
"""

import os
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: The only `[tool.ruff*]` tables allowed in `pyproject.toml`, and the keys each may carry.
RUFF_TABLES = {
    "tool.ruff": {"target-version", "line-length", "extend-include"},
    "tool.ruff.lint": {"select", "ignore"},
    "tool.ruff.lint.isort": {"known-first-party"},
}

#: The values `lint` was proved on. `extend-include` is checked against the tree instead.
RUFF_VALUES = {
    ("tool.ruff", "target-version"): "py39",
    ("tool.ruff", "line-length"): 120,
    ("tool.ruff.lint", "select"): ["E", "F", "W", "I", "B", "UP"],
    ("tool.ruff.lint", "ignore"): ["E501"],
    ("tool.ruff.lint.isort", "known-first-party"): ["wake_config"],
}

#: A file by one of these names gives a linter a configuration of its own. `pyproject.toml` is
#: listed because a second one decides the files under it; the root one is the pinned config.
#:
#: Only a name git does not track belongs here. The tuple is matched against `git ls-files`, so a
#: tracked name - the root `.gitignore` - fails `test_no_other_linter_config_file_is_tracked` on
#: this repo the moment it is added, which reads as the guard rejecting the file it protects.
#: The git-known ignore files are that test's sibling instead:
#: `test_no_tracked_file_is_hidden_from_ruff_by_gitignore` asks `git check-ignore --no-index`
#: whether a `.gitignore` or `.git/info/exclude` pattern matches a tracked path, which is the
#: shape ruff acts on (#201).
COMPETING_LINTER_FILES = (
    "pyproject.toml",
    ".ruff.toml",
    "ruff.toml",
    ".shellcheckrc",
    "shellcheckrc",
    # ruff's ignore-file support reads `.ignore` too, and `git check-ignore --no-index` never
    # reports it, so nothing else in the tree looks at it (#188).
    ".ignore",
)

#: Whether a new name belongs above is measured, not guessed. ruff's ignore-file support is a walk
#: rule, and `ruff check --show-settings` prints only `file_resolver.respect_gitignore`, so an
#: unhonoured name and an untested one look identical there. Recipe, run at `dbb5814` with ruff
#: 0.17.5 (#202):
#:
#:     printf '_x = undefined_name\n' > tests/_probe.py   # F821 is on by default
#:     ruff check .                                       # rc 1, names tests/_probe.py
#:     printf 'tests/\n' > <candidate>                    # at the repo root
#:     ruff check .                                       # rc 0 -> honoured: add the name above
#:                                                        # rc 1 -> not honoured: leave it out
#:     rm -f <candidate> tests/_probe.py                  # untracked scratch, never committed
#:
#: `ruff check . -v` prints the read set as `Checked 29 files` with the probe planted and
#: `Checked 12 files` once a `tests/` pattern is honoured. Honoured today: `.ignore`, `.gitignore`,
#: `.git/info/exclude`. Not honoured: `.ruffignore`, `.ruff-ignore`, `.lintignore`, `.eslintignore`,
#: `.dockerignore`.

ROOT_PYPROJECT = "pyproject.toml"


def text(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def tracked_paths(root=ROOT):
    """Every tracked path, as git reports it."""
    try:
        listing = subprocess.run(["git", "-C", root, "ls-files", "-z"], check=True, capture_output=True).stdout
    except (OSError, subprocess.CalledProcessError) as error:
        raise unittest.SkipTest(f"not a git checkout, cannot list tracked files: {error}") from error
    return [raw.decode("utf-8", "surrogateescape") for raw in listing.split(b"\0") if raw]


def without_comment(line):
    """The line up to the first `#` that is not inside a double-quoted string."""
    kept, quoted = [], False
    for char in line:
        if char == '"':
            quoted = not quoted
        if char == "#" and not quoted:
            break
        kept.append(char)
    return "".join(kept)


def toml_value(raw):
    """The value forms this file uses: a quoted string, an integer, a boolean, an array of them."""
    raw = raw.strip()
    if raw.startswith("["):
        inner = raw[1:].rsplit("]", 1)[0]
        return [toml_value(part) for part in re.findall(r'"[^"]*"|[^,]+', inner) if part.strip()]
    if len(raw) >= 2 and raw.startswith('"') and raw.endswith('"'):
        return raw[1:-1]
    if raw in ("true", "false"):
        return raw == "true"
    return int(raw) if re.fullmatch(r"-?\d+", raw) else raw


def toml_sections(source):
    """`{table: {key: value}}` for the shapes these pins read: a `[a.b]` header, `key = value`,
    and a value that may run over several lines. Anything else is left to ruff's own parser.

    Written by hand because `tomllib` is Python 3.11+ and the `test` job starts at 3.9.
    """
    sections, current = {}, None
    lines = source.splitlines()
    index = 0
    while index < len(lines):
        line = without_comment(lines[index]).strip()
        index += 1
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            current = sections.setdefault(line.strip("[]").strip(), {})
            continue
        if current is None or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        while value.startswith("[") and "]" not in value and index < len(lines):
            value += " " + without_comment(lines[index]).strip()
            index += 1
        current[key.strip()] = toml_value(value)
    return sections


def ruff_config_problems(sections, extend_include):
    """Every way `[tool.ruff*]` differs from what `lint` was proved on. An empty list is good.

    Each message names the edit and why it matters, so a deliberate change is made here too.
    """
    problems = []
    for table, values in sorted(sections.items()):
        if table != "tool.ruff" and not table.startswith("tool.ruff."):
            continue
        allowed = RUFF_TABLES.get(table)
        if allowed is None:
            problems.append(
                f"[{table}] is not one of the pinned ruff tables ({', '.join(sorted(RUFF_TABLES))}): a new "
                "table decides what ruff reads or reports without touching a pinned value (#185)"
            )
            continue
        for key in sorted(values):
            if key not in allowed:
                problems.append(
                    f"[{table}] {key} is not one of the pinned keys ({', '.join(sorted(allowed))}): a new key "
                    "here - extend-exclude, exclude, respect-gitignore, a per-file ignore - changes what ruff "
                    "reads while `ruff check .` still exits 0 (#185)"
                )
    for (table, key), expected in sorted(RUFF_VALUES.items()):
        found = sections.get(table, {}).get(key, "<missing>")
        if found != expected:
            problems.append(
                f"[{table}] {key} is {found!r}, pinned to {expected!r}: it is what `ruff check .` reads and "
                "reports, so a change is deliberate and edits this pin in the same commit (#185)"
            )
    found = sorted(sections.get("tool.ruff", {}).get("extend-include", []))
    if found != sorted(extend_include):
        problems.append(
            f"[tool.ruff] extend-include is {found}, the python scripts without a .py suffix in the tree are "
            f"{sorted(extend_include)}: an entry dropped here leaves that script unlinted while `ruff check .` "
            "still exits 0 (#185)"
        )
    return problems


def competing_linter_configs(paths, root=ROOT_PYPROJECT):
    """Tracked paths that would give a linter a second config file, other than the root one."""
    return sorted(path for path in paths if path.rsplit("/", 1)[-1] in COMPETING_LINTER_FILES and path != root)


def python_without_suffix(root=ROOT, paths=None):
    """The tracked scripts with no `.py` suffix that start with a python shebang.

    Exactly what `extend-include` has to name: ruff skips a file it does not recognise as python,
    and the eight scripts in `bin/` are why that key exists at all.
    """
    found = []
    for rel in tracked_paths(root) if paths is None else paths:
        if rel.endswith(".py"):
            continue
        try:
            with open(os.path.join(root, rel), "rb") as handle:
                first = handle.readline()
        except OSError:
            continue
        if first.startswith(b"#!") and b"python" in first:
            found.append(rel)
    return sorted(found)


def gitignored_tracked_paths(root=ROOT, paths=None):
    """Tracked paths a `.gitignore` pattern matches, which is what ruff drops from its read set.

    `--no-index` is required: without it git never reports a *tracked* path as ignored.
    """
    paths = tracked_paths(root) if paths is None else paths
    try:
        run = subprocess.run(
            ["git", "-C", root, "check-ignore", "--no-index", "--stdin"],
            input="\n".join(paths),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as error:  # pragma: no cover - git is there in CI and in any checkout
        raise unittest.SkipTest(f"git is not available: {error}") from error
    return [line for line in run.stdout.splitlines() if line.strip()]


class LinterConfigTestCase(unittest.TestCase):
    """#185: what the linters read for themselves is pinned, so a one-line edit there fails here."""

    def setUp(self):
        self.pyproject = text(os.path.join(ROOT, ROOT_PYPROJECT))
        self.extend_include = python_without_suffix()

    def problems(self, source=None):
        return ruff_config_problems(toml_sections(source or self.pyproject), self.extend_include)

    def test_ruff_reads_what_lint_was_proved_on(self):
        """The shipped `[tool.ruff*]` tables, read as ruff reads them, are exactly the pinned ones."""
        self.assertEqual(self.problems(), [], "the shipped ruff configuration is not the pinned one")

    def test_the_shipped_tables_are_actually_parsed(self):
        """A parser that returned nothing would make every value pin above pass silently."""
        sections = toml_sections(self.pyproject)
        self.assertEqual(sorted(sections), ["tool.ruff", "tool.ruff.lint", "tool.ruff.lint.isort"])
        self.assertEqual(sections["tool.ruff.lint"]["select"], RUFF_VALUES[("tool.ruff.lint", "select")])
        self.assertEqual(sorted(sections["tool.ruff"]["extend-include"]), self.extend_include)

    def test_the_pin_refuses_a_planted_f821_ignore(self):
        """#185: `ignore = [..., "F821"]` left a planted F821 at "All checks passed!", rc 0."""
        mutated = self.pyproject.replace('    "E501",  # long strings', '    "E501",\n    "F821",  #', 1)
        self.assertNotEqual(mutated, self.pyproject, "the E501 ignore line moved")
        self.assertIn("tool.ruff.lint] ignore", " ".join(self.problems(mutated)))

    def test_the_pin_refuses_a_narrowed_select(self):
        """#185: `select = ["E", "W"]` hid a planted F821 in `bin/wake_config.py`."""
        mutated = self.pyproject.replace('select = ["E", "F", "W", "I", "B", "UP"]', 'select = ["E", "W"]', 1)
        self.assertNotEqual(mutated, self.pyproject, "the select line moved")
        self.assertIn("tool.ruff.lint] select", " ".join(self.problems(mutated)))

    def test_the_pin_refuses_an_exclude(self):
        """#185: `extend-exclude = ["tests"]` was the one-line disarm; `exclude` is the same hole."""
        for key in ("extend-exclude", "exclude"):
            with self.subTest(key=key):
                mutated = self.pyproject.replace("[tool.ruff]\n", f'[tool.ruff]\n{key} = ["tests"]\n', 1)
                self.assertNotEqual(mutated, self.pyproject, "the [tool.ruff] header moved")
                self.assertIn(key, " ".join(self.problems(mutated)))

    def test_the_pin_refuses_a_table_added_for_the_formatter(self):
        """A `[tool.ruff.format]` table decides what `ruff format --check .` looks at."""
        mutated = self.pyproject.replace(
            "[tool.ruff.lint]", '[tool.ruff.format]\nexclude = ["tests"]\n\n[tool.ruff.lint]', 1
        )
        self.assertNotEqual(mutated, self.pyproject, "the [tool.ruff.lint] header moved")
        self.assertIn("[tool.ruff.format] is not one of the pinned ruff tables", " ".join(self.problems(mutated)))

    def test_the_pin_refuses_a_dropped_extend_include_entry(self):
        """An entry removed from `extend-include` leaves that script unlinted, at rc 0."""
        mutated = self.pyproject.replace('    "bin/wake-mcp",\n', "", 1)
        self.assertNotEqual(mutated, self.pyproject, "the bin/wake-mcp entry moved")
        self.assertIn("extend-include", " ".join(self.problems(mutated)))

    def test_a_python_script_without_a_suffix_is_found_by_its_shebang(self):
        """The expected `extend-include` is computed, so a new `bin/` script fails the pin."""
        root = tempfile.mkdtemp(prefix="linter-")
        try:
            os.makedirs(os.path.join(root, "bin"))
            for name, first in (
                ("bin/py", "#!/usr/bin/env python3\n"),
                ("bin/sh", "#!/bin/bash\n"),
                ("bin/none", "no shebang\n"),
            ):
                with open(os.path.join(root, name), "w", encoding="utf-8") as handle:
                    handle.write(first)
            self.assertEqual(python_without_suffix(root, ["bin/py", "bin/sh", "bin/none", "x.py"]), ["bin/py"])
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_no_other_linter_config_file_is_tracked(self):
        """#185: a second config file at any depth decides what a tool reads.

        Measured at `e4ca592`: a `tests/.ruff.toml` reading `select = []` hid a planted F821
        ("All checks passed!", rc 0, 27 files read), a root `.ruff.toml` replaced `[tool.ruff]` and
        dropped the eight suffixless `bin/` scripts from the read set (26 files became 19), and a
        root `shellcheckrc` - no dot - silenced the shipped command exactly as `.shellcheckrc` does.
        """
        found = competing_linter_configs(tracked_paths())
        self.assertEqual(
            found,
            [],
            "a linter reads a file of its own: "
            + ", ".join(found)
            + " - ruff takes the nearest config as the whole configuration for the files under it, shellcheck "
            "reads one from the working directory and from each script's directory, and ruff's ignore-file "
            "support drops whatever a `.ignore` pattern matches, so a second file decides what the job reads "
            "without touching pyproject.toml or ci.yml (#185, #188)",
        )
        self.assertEqual(
            competing_linter_configs(
                [
                    "pyproject.toml",
                    ".ruff.toml",
                    "tests/.ruff.toml",
                    "tests/pyproject.toml",
                    "shellcheckrc",
                    "scripts/.shellcheckrc",
                    ".ignore",
                    "tests/.ignore",
                ]
            ),
            [
                ".ignore",
                ".ruff.toml",
                "scripts/.shellcheckrc",
                "shellcheckrc",
                "tests/.ignore",
                "tests/.ruff.toml",
                "tests/pyproject.toml",
            ],
            "the refusal does not name every second config file",
        )

    def test_no_tracked_file_is_hidden_from_ruff_by_gitignore(self):
        """`.gitignore` is a ruff config too: its resolved settings say `respect_gitignore = true`.

        Measured at `e4ca592`: `.gitignore` += `tests/` left `ruff check .` at "All checks passed!"
        with a planted F821 in `tests/test_wake.py` (26 files read became 12), both lint pins green,
        and `git check-ignore` without `--no-index` reports nothing because the path is tracked.
        """
        self.assertEqual(
            gitignored_tracked_paths(),
            [],
            "a .gitignore pattern matches a tracked file, and ruff drops what it matches",
        )

    def test_the_gitignore_check_bites_on_a_fixture(self):
        """The refusal above is only as good as the command behind it, so prove it on a fixture."""
        root = tempfile.mkdtemp(prefix="gitignore-")
        try:
            os.makedirs(os.path.join(root, "tests"))
            with open(os.path.join(root, ".gitignore"), "w", encoding="utf-8") as handle:
                handle.write("tests/\n")
            with open(os.path.join(root, "tests", "test_x.py"), "w", encoding="utf-8") as handle:
                handle.write("x = 1\n")
            with open(os.path.join(root, "keep.txt"), "w", encoding="utf-8") as handle:
                handle.write("kept\n")
            init = subprocess.run(["git", "-C", root, "init", "-q"], capture_output=True, text=True)
            if init.returncode != 0:  # pragma: no cover - git is there in CI and in any checkout
                self.skipTest(f"git init failed: {init.stderr.strip()}")
            add = subprocess.run(["git", "-C", root, "add", "-A", "-f"], capture_output=True, text=True)
            if add.returncode != 0:  # pragma: no cover - git is there in CI and in any checkout
                self.skipTest(f"git add failed: {add.stderr.strip()}")
            self.assertEqual(gitignored_tracked_paths(root), ["tests/test_x.py"])
        finally:
            shutil.rmtree(root, ignore_errors=True)
