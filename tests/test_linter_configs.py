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

Five things decide what CI's linters see, and all five are read here:

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
4. `eslint.config.js`, which the `api` job's `eslint apps packages` loads instead of a file list:
   its imports, the entries handed to `tseslint.config(...)` and its `ignores` are pinned (#208).
   Measured at `a8180bd` (eslint 10.15.0): one added entry,
   `{ rules: { '@typescript-eslint/no-unused-vars': ['error', { args: 'none' }] } }`, left a planted
   unused parameter unlinted - rc 0, no messages - with every pin in this repository green.
5. the `lint` script in `package.json`, which is the command that loads that config: the `api` job
   runs `bun run lint`, so a second `-c` on that line points eslint at another file and a trailing
   `|| true` eats its exit status, while every pin above stays green (#224). Measured at `39c7d81`
   with eslint 10.15.0: on a planted unused parameter the shipped command exits 1 with one
   message, `-c eslint.local.js` exits 0 with none, and the shipped command plus `|| true` exits 0.

Run with the rest: `python3 -m unittest discover -s tests`
"""

import fnmatch
import json
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

#: The eslint config the `api` job's `bun run lint` loads. `eslint apps packages` runs from the
#: root, and flat config does not cascade, so this one file supplies every rule the job applies.
ROOT_ESLINT_CONFIG = "eslint.config.js"

#: The only imports the pinned config may carry, as `(local name, specifier)`. A relative specifier
#: is refused by the reader: it is a second config file wearing the root config's name.
ESLINT_IMPORTS = (
    ("eslint", "@eslint/js"),
    ("eslintConfigPrettier", "eslint-config-prettier"),
    ("tseslint", "typescript-eslint"),
)

#: The entries handed to `tseslint.config(...)`, in order, with the one object written as `ignores`.
#: These presets are where every rule value comes from, so `strict` -> `recommended` is the same
#: edit as writing the rule out by hand.
ESLINT_ENTRIES = (
    "ignores",
    "eslint.configs.recommended",
    "tseslint.configs.strict",
    "eslintConfigPrettier",
)

#: The `ignores` patterns, exactly. `eslint apps packages` reads the tree rather than a file list,
#: so an entry added here drops files from the job without touching a pinned value.
ESLINT_IGNORES = ("**/dist/**", "**/.next/**", "**/node_modules/**", "**/coverage/**")

#: The `api` job's `Lint` step is `bun run lint` (`ci.yml:111`), and this script is the only reason
#: eslint loads the file above: a second `-c` here names another config - on the command line, so
#: nothing imports it and the relative-import refusal never sees it - and a trailing `|| true` swallows
#: the exit status. `prettier` is in the same string because the step is green only if both pass.
ROOT_PACKAGE_JSON = "package.json"
LINT_SCRIPT = "node ./node_modules/eslint/bin/eslint.js apps packages && prettier --check apps packages"


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


class EslintConfigError(Exception):
    """The reader met a shape it does not pin, so the pin cannot vouch for the file (#208)."""


def without_js_comments(source):
    """The source with `//` and `/* */` comments removed and strings left alone.

    A comment is only a comment outside a string: an import specifier or an ignore pattern may carry
    a slash, and a stripped pattern would make the pin read something eslint does not.
    """
    kept, index, quoted = [], 0, None
    while index < len(source):
        char = source[index]
        if quoted:
            kept.append(char)
            if char == "\\" and index + 1 < len(source):
                kept.append(source[index + 1])
                index += 2
                continue
            if char == quoted:
                quoted = None
            index += 1
            continue
        if char in "'\"`":
            quoted = char
            kept.append(char)
            index += 1
            continue
        if source.startswith("//", index):
            newline = source.find("\n", index)
            index = len(source) if newline < 0 else newline
            continue
        if source.startswith("/*", index):
            end = source.find("*/", index + 2)
            if end < 0:
                raise EslintConfigError("a `/*` comment is never closed: the reader cannot tell what runs")
            kept.append("\n")
            index = end + 2
            continue
        kept.append(char)
        index += 1
    if quoted:
        raise EslintConfigError("a quoted string is never closed: the reader cannot tell an entry from text")
    return "".join(kept)


def split_arguments(body):
    """The top-level comma-separated arguments of a call, brackets and strings respected."""
    parts, depth, quoted, current, index = [], 0, None, [], 0
    while index < len(body):
        char = body[index]
        if quoted:
            current.append(char)
            if char == "\\" and index + 1 < len(body):
                current.append(body[index + 1])
                index += 2
                continue
            if char == quoted:
                quoted = None
            index += 1
            continue
        if char in "'\"`":
            quoted = char
            current.append(char)
            index += 1
            continue
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
            if depth < 0:
                raise EslintConfigError("a closing bracket has no opening one: the reader cannot split the entries")
        if char == "," and depth == 0:
            parts.append("".join(current))
            current = []
            index += 1
            continue
        current.append(char)
        index += 1
    if depth:
        raise EslintConfigError("a bracket is left open: the reader cannot tell where an entry ends")
    if quoted:
        raise EslintConfigError("a string is left open: the reader cannot tell an entry from text")
    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return [part.strip() for part in parts if part.strip()]


def read_eslint_config(source):
    """`{"imports": [...], "entries": [...], "ignores": [...]}` read from `eslint.config.js`.

    The reader understands exactly the shapes the pinned file uses and refuses everything else, so
    an entry it cannot read fails the pin instead of passing unread (#208).
    """
    body = without_js_comments(source)
    imports, rest = [], []
    for number, line in enumerate(body.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("import "):
            found = re.fullmatch(r"""import\s+([A-Za-z_$][\w$]*)\s+from\s+(['"])([^'"]+)\2\s*;?""", stripped)
            if found is None:
                raise EslintConfigError(
                    f"line {number} is an import this reader does not read ({stripped[:60]}): "
                    "pin the new shape here in the same commit (#208)"
                )
            if found.group(3).startswith("."):
                raise EslintConfigError(
                    f"line {number} imports {found.group(3)!r}: a relative import is a second config file "
                    "under another name, and nothing here pins what it holds (#208)"
                )
            imports.append((found.group(1), found.group(3)))
            continue
        rest.append(stripped)
    head = "".join(rest)
    prefix = "export default tseslint.config("
    if not head.startswith(prefix) or not head.endswith(");"):
        raise EslintConfigError(
            "the file no longer ends in `export default tseslint.config(...)`: the pin reads the entries "
            "handed to that call, and anything else decides the rules without being read (#208)"
        )
    entries, ignores = [], []
    for position, entry in enumerate(split_arguments(head[len(prefix) : -2]), start=1):
        if entry.startswith("{"):
            found = re.fullmatch(r"""\{\s*ignores\s*:\s*\[([^\]]*)\]\s*,?\s*\}""", entry)
            if found is None:
                key = re.match(r"""\{\s*['"]?([A-Za-z_$][\w$]*)""", entry)
                named = f" with `{key.group(1)}`" if key else ""
                raise EslintConfigError(
                    f"entry {position} is an inline object{named}: every rule value comes from the pinned "
                    "presets, and an object here carries a rule entry (`rules`), a file selection (`files`) or "
                    "an option, any of which change the verdict for the whole tree (#208)"
                )
            for quoted, bare in re.findall("'([^']*)'|\"([^\"]*)\"", found.group(1)):
                ignores.append(quoted or bare)
            entries.append("ignores")
            continue
        if not re.fullmatch(r"[A-Za-z_$][\w$]*(\.[A-Za-z_$][\w$]*)*", entry):
            raise EslintConfigError(
                f"entry {position} is `{entry[:60]}`, not a config object or a preset name: a spread, a call "
                "or a computed value can supply rules this pin never reads (#208)"
            )
        entries.append(entry)
    return {"imports": imports, "entries": entries, "ignores": ignores}


def ignores_path(pattern, path):
    """True when eslint's `ignores` pattern drops `path`, for the shapes this config uses.

    `fnmatch`'s `*` crosses `/`, so `**/dist/**` also has to be tried without its leading `**/`, and
    a directory pattern drops the directory itself as well as what is under it.
    """
    candidates = [pattern]
    if pattern.startswith("**/"):
        candidates.append(pattern[3:])
    for candidate in candidates:
        if fnmatch.fnmatchcase(path, candidate):
            return True
        if candidate.endswith("/**") and path.startswith(candidate[: -len("/**")].rstrip("/") + "/"):
            return True
    return False


def brace_ignore_problems(patterns):
    """Every `ignores` pattern this matcher cannot judge, because it contains a brace.

    `ignores_path` is `fnmatch`, which does not expand `{a,b}`; eslint does. Measured with eslint
    10.15.0: `ignores: ['{dist,build}/**']` drops both directories from `eslint .`, while
    `fnmatch.fnmatchcase(path, '{dist,build}/**')` is False for every path under either one.

    The judgement is made on the pattern rather than on the pinned list, so a brace pattern added
    together with a matching edit to `ESLINT_IGNORES` still fails here (#224).
    """
    return [
        f"{ROOT_ESLINT_CONFIG} ignores {pattern!r}, which contains a brace: eslint expands `{{a,b}}` and "
        "the matcher behind the tracked-file check does not, so this pattern can drop directories from "
        "the `api` job with both ignore layers green (#224)"
        for pattern in patterns
        if "{" in pattern
    ]


def lint_script_problems(scripts):
    """Every way `package.json`'s `lint` stops being the invocation the `api` job was proved on.

    Empty is good. The message names what the string decides, because the edit that moves it looks
    like a harmless one-liner and leaves every pin on the config file green (#224).
    """
    script = scripts.get("lint")
    if script == LINT_SCRIPT:
        return []
    return [
        f"{ROOT_PACKAGE_JSON} `lint` is {script!r}, pinned to {LINT_SCRIPT!r}: the `api` job runs "
        "`bun run lint`, and eslint loads `eslint.config.js` only because this command says so - a "
        "second `-c` names another config on the command line, where no import check can see it, and a "
        "trailing `|| true` eats the exit status the job reads (#224)"
    ]


def ignored_tracked_paths(patterns, paths=None):
    """Tracked paths an `ignores` pattern drops, which is what eslint does to them."""
    paths = tracked_paths() if paths is None else paths
    return sorted(path for path in paths if any(ignores_path(pattern, path) for pattern in patterns))


def eslint_config_problems(source, tracked=None):
    """Every way `eslint.config.js` differs from what the `api` job was proved on. Empty is good.

    Each message names the edit and why the `api` job stops meaning what it says (#208).
    """
    if source is None:
        return [
            f"{ROOT_ESLINT_CONFIG} is missing: the `api` job runs `eslint apps packages` from the root, and "
            "flat config loads this file or no rules at all (#208)"
        ]
    try:
        config = read_eslint_config(source)
    except EslintConfigError as error:
        return [f"{ROOT_ESLINT_CONFIG}: {error}"]
    problems = []
    if config["imports"] != list(ESLINT_IMPORTS):
        problems.append(
            f"{ROOT_ESLINT_CONFIG} imports {config['imports']}, pinned to {list(ESLINT_IMPORTS)}: the presets "
            "these name are where every rule value comes from (#208)"
        )
    if config["entries"] != list(ESLINT_ENTRIES):
        problems.append(
            f"{ROOT_ESLINT_CONFIG} hands {config['entries']} to tseslint.config, pinned to "
            f"{list(ESLINT_ENTRIES)}: a preset swapped or an entry dropped changes the rules the api job "
            "reports while ci.yml still says the same thing (#208)"
        )
    if config["ignores"] != list(ESLINT_IGNORES):
        problems.append(
            f"{ROOT_ESLINT_CONFIG} ignores {config['ignores']}, pinned to {list(ESLINT_IGNORES)}: `eslint apps "
            "packages` reads the tree, so an entry here drops files from the job with every pin green (#208)"
        )
    problems.extend(brace_ignore_problems(config["ignores"]))
    hidden = ignored_tracked_paths(config["ignores"], tracked)
    if hidden:
        problems.append(
            f"{ROOT_ESLINT_CONFIG} ignores patterns that match {len(hidden)} tracked file(s), first "
            f"{hidden[0]}: eslint drops what it ignores, so the api job never reads them (#208)"
        )
    return problems


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


class EslintConfigTestCase(unittest.TestCase):
    """#208: `eslint.config.js` is pinned by value, so one entry there fails here.

    Measured at `a8180bd` (eslint 10.15.0, node_modules from `bun install --frozen-lockfile`): a
    planted `export function f(unusedParam: number): void {}` in `apps/api/src/` made `eslint apps
    packages` exit 1 on `@typescript-eslint/no-unused-vars`; one added config entry, `{ rules: {
    '@typescript-eslint/no-unused-vars': ['error', { args: 'none' }] } }`, left it at rc 0 with no
    messages while this module and `tests.test_inline_suppressions` stayed green.
    """

    def setUp(self):
        path = os.path.join(ROOT, ROOT_ESLINT_CONFIG)
        self.source = text(path) if os.path.exists(path) else None
        self.tracked = tracked_paths()

    def problems(self, source=None):
        return eslint_config_problems(self.source if source is None else source, self.tracked)

    def mutated(self, old, new):
        changed = self.source.replace(old, new, 1)
        self.assertNotEqual(changed, self.source, f"the fixture's anchor moved: {old!r}")
        return changed

    def test_eslint_reads_what_the_api_job_was_proved_on(self):
        """The shipped config, read as eslint reads it, carries exactly the pinned rules."""
        self.assertEqual(self.problems(), [], "the shipped eslint configuration is not the pinned one")

    def test_the_shipped_config_is_actually_parsed(self):
        """A reader that returned nothing would make every pin above pass silently."""
        config = read_eslint_config(self.source)
        self.assertEqual(config["imports"], list(ESLINT_IMPORTS))
        self.assertEqual(config["entries"], list(ESLINT_ENTRIES))
        self.assertEqual(config["ignores"], list(ESLINT_IGNORES))

    def test_a_comment_does_not_trip_the_pin(self):
        """The file may be documented; only what eslint reads is pinned."""
        self.assertEqual(self.problems(self.source + "\n// a note about the presets\n/* and another */\n"), [])

    def test_the_pin_refuses_an_options_carrying_rule_entry(self):
        """The measured bypass: `args: 'none'` on the config left an unused parameter unlinted."""
        changed = self.mutated(
            "  eslintConfigPrettier,\n",
            "  eslintConfigPrettier,\n"
            "  { rules: { '@typescript-eslint/no-unused-vars': ['error', { args: 'none' }] } },\n",
        )
        self.assertIn("inline object with `rules`", " ".join(self.problems(changed)))

    def test_the_pin_refuses_an_entry_it_cannot_read(self):
        """A spread supplies rules the reader never sees, so it is refused rather than skipped."""
        changed = self.mutated("  tseslint.configs.strict,\n", "  ...tseslint.configs.strict,\n")
        self.assertIn("not a config object or a preset name", " ".join(self.problems(changed)))

    def test_the_pin_refuses_a_weaker_preset(self):
        """`strict` -> `recommended` is the same disarm as writing the rule out by hand."""
        changed = self.mutated("tseslint.configs.strict,", "tseslint.configs.recommended,")
        self.assertIn("pinned to", " ".join(self.problems(changed)))

    def test_the_pin_refuses_a_relative_import(self):
        """A relative import is a second config file, and nothing pins what it holds."""
        changed = self.mutated(
            "import tseslint from 'typescript-eslint';",
            "import tseslint from 'typescript-eslint';\nimport local from './eslint.local.js';",
        )
        self.assertIn("a relative import is a second config file", " ".join(self.problems(changed)))

    def test_the_pin_refuses_an_ignore_added_to_the_pinned_list(self):
        """`eslint apps packages` reads the tree, so an ignore entry is whole directories gone."""
        changed = self.mutated("      '**/coverage/**',\n", "      '**/coverage/**',\n      'apps/**',\n")
        joined = " ".join(self.problems(changed))
        self.assertIn("pinned to", joined)
        self.assertIn("match", joined)

    def test_an_ignore_pattern_that_matches_a_tracked_file_fails_on_its_own(self):
        """The tree check bites even when the pin itself was edited to carry the pattern."""
        problems = eslint_config_problems(
            self.source.replace("      '**/coverage/**',\n", "      '**/coverage/**',\n      'tests/*.py',\n"),
            ["tests/test_wake.py"],
        )
        self.assertIn("match 1 tracked file(s), first tests/test_wake.py", " ".join(problems))

    def test_the_ignore_patterns_drop_what_eslint_drops(self):
        """The matcher behind the tree check, on the shapes this config uses."""
        self.assertTrue(ignores_path("**/dist/**", "apps/api/dist/index.js"))
        self.assertTrue(ignores_path("**/dist/**", "dist/index.js"))
        self.assertTrue(ignores_path("apps/**", "apps/api/src/main.ts"))
        self.assertFalse(ignores_path("**/dist/**", "apps/api/src/distribution.ts"))
        self.assertFalse(ignores_path("**/.next/**", "apps/api/src/next.ts"))

    def test_the_pin_reads_the_script_that_loads_the_config(self):
        """#224: the config is one input; the `bun run lint` script that names it is the other."""
        scripts = json.loads(text(os.path.join(ROOT, ROOT_PACKAGE_JSON)))["scripts"]
        self.assertEqual(
            lint_script_problems(scripts),
            [],
            "the shipped `lint` script is not the invocation the api job was proved on",
        )

    def test_the_lint_script_pin_refuses_a_second_config_or_a_swallowed_status(self):
        """Both disarms keep `bun run lint` green: another `-c`, or `|| true` eating the status."""
        shipped = text(os.path.join(ROOT, ROOT_PACKAGE_JSON))
        for replacement in (
            LINT_SCRIPT.replace("eslint.js apps", "eslint.js -c eslint.local.js apps"),
            LINT_SCRIPT + " || true",
        ):
            self.assertNotEqual(replacement, LINT_SCRIPT, "the fixture's anchor moved")
            changed = shipped.replace(LINT_SCRIPT, replacement, 1)
            self.assertNotEqual(changed, shipped, "the fixture's anchor moved: the `lint` script")
            problems = lint_script_problems(json.loads(changed)["scripts"])
            self.assertEqual(len(problems), 1, problems)
            self.assertIn("package.json `lint` is", problems[0])

    def test_the_pin_refuses_a_brace_pattern_the_matcher_cannot_read(self):
        """`ignores_path` is `fnmatch`, which has no `{a,b}`; eslint expands it, so the drop is blind."""
        self.assertFalse(ignores_path("{apps,packages}/**", "apps/api/src/main.ts"))
        changed = self.mutated("      '**/coverage/**',\n", "      '**/coverage/**',\n      '{dist,build}/**',\n")
        self.assertIn("contains a brace", " ".join(self.problems(changed)))

    def test_the_brace_refusal_judges_the_pattern_not_the_pinned_list(self):
        """A pattern added with a matching pin edit must still fail, so the check reads patterns."""
        found = brace_ignore_problems(["**/dist/**", "{dist,build}/**"])
        self.assertEqual(len(found), 1)
        self.assertIn("'{dist,build}/**'", found[0])

    def test_the_shipped_config_still_reports_an_unused_parameter(self):
        """The pin says the presets are intact; this proves it where eslint is installed.

        The `test` job has no node_modules, so this skips there. The `api (bun)` job has the
        lockfile installed and runs this module through tests/run_module_strict.py with
        WAKE_REQUIRE_LINTERS=1, where a skip is a failure. A developer checkout without
        node_modules still skips.
        """
        eslint = os.path.join(ROOT, "node_modules", "eslint", "bin", "eslint.js")
        node = shutil.which("node")
        missing = []
        if not node:
            missing.append("node")
        if not os.path.exists(eslint):
            missing.append("node_modules/eslint")
        if missing:
            # Mirrors `linter_control_disposition` in test_inline_suppressions.py: only `1` arms it.
            if os.environ.get("WAKE_REQUIRE_LINTERS") == "1":
                self.fail(
                    f"WAKE_REQUIRE_LINTERS=1 and missing: {', '.join(missing)}: the api job installs "
                    "the lockfile with `bun install --frozen-lockfile`, so a skip here would hide that "
                    "job without its linters (#260)"
                )
            self.skipTest("eslint is not installed here (run `bun install --frozen-lockfile`)")
        root = tempfile.mkdtemp(prefix="eslint-probe-")
        try:
            with open(os.path.join(root, "zz-probe-args.ts"), "w", encoding="utf-8") as handle:
                handle.write("export function f(unusedParam: number): void {}\n")
            run = subprocess.run(
                [node, eslint, "--config", os.path.join(ROOT, ROOT_ESLINT_CONFIG), "-f", "json", "zz-probe-args.ts"],
                cwd=root,
                capture_output=True,
                text=True,
            )
            self.assertEqual(run.returncode, 1, f"eslint exited {run.returncode}: {run.stderr[-800:]}")
            report = json.loads(run.stdout)
            rules = [message.get("ruleId") for entry in report for message in entry.get("messages", [])]
            self.assertEqual(rules, ["@typescript-eslint/no-unused-vars"], "the pinned presets no longer report it")
        finally:
            shutil.rmtree(root, ignore_errors=True)
