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

`api (bun)` runs a third linter over the same tree, and its directives were unpinned: at `ab6e0e8`
with eslint 10.15.0 from the repo's own lockfile, a `.ts` file whose only defect is a real
`@typescript-eslint/no-unused-vars` takes `eslint apps` to rc 1, and one `/* eslint-disable */` line
on top takes it to rc 0 with `suppressedMessages: 1` - the same shape one linter over, while this
file read no `.ts` file at all (#196). The pin has two readers. `javascript_comments` asks
`@typescript-eslint/parser`, through the `typescript-eslint` devDependency the tree already
installs, for the comments of a file - one `node scripts/js-comments.mjs` process for the whole
tree, because a process per file is not affordable - and a comment is then where eslint sees one
whatever the source around it looks like: a `/` that opens a regular expression, a quote inside
that regex or a keyword used as a property name cannot be read as a comment (#205). That reader
needs `node` on PATH and `node_modules` installed. The Python test matrix has neither, and a file
the parser cannot parse is that file's own fallback: both are read by `scanner_comments`, a
character scan that skips strings, template literals and regular expressions, because a directive
quoted in any of the three is not a directive and a `//` in a URL is not a comment. Both report the
text after the `//` or `/*` the way eslint matches it: anchored at the comment's start,
case-sensitively. Refused there:
the block `/* eslint-disable */`, which exempts everything below it - measured mid-file as well as
at line 1 - a `-line` / `-next-line` form that names no rule (and `//eslint-disable-next-line` with
no space after the `//` is honoured, so the marker cannot need one), an inline
`/* eslint <rule>: <value> */` whose value is not that rule raised to `error` - a severity below it
(eslint exits 0 on a warning, so `0`, `1`, `"off"`, `"warn"` and their list forms each disarmed the
check), or `error` carrying options, which keeps the severity and reconfigures the rule (measured,
`["error", {"varsIgnorePattern": ".*"}]`, `["error", {"args": "none"}]` and `["error", "smart"]`
each left a real error at rc 0) - each rule read on its own, one pair per top-level comma, so the
legal two-rule raise `/* eslint no-console: error, eqeqeq: error */` passes while the pair that
carries options is named for what it is, and `prettier-ignore`, which
exempts the next node from the `prettier --check` half of the same job with a misformatted file left
in the tree. Kept: a rule-scoped `// eslint-disable-next-line <rule>[, <rule>] [-- why]` and its block
spelling, the same shape as `# noqa: E402`. Measured as no hole: eslint reads its directives in
lower case only, so `/* ESLINT-DISABLE */` does nothing; `// eslint-disable` with no `-line` suffix
does nothing; a directive after other text (`// see eslint-disable-next-line`) does nothing; and
`@ts-nocheck` needs no pin, because `tseslint.configs.strict` reports it itself (rc 1). The two
readers stop in different places, and neither limit is glossed. The parser needs `node_modules`:
without it the scanner reads, and the `test` matrix runs that way on purpose, so the fallback is
tested rather than kept for a claim. The scanner decides whether `/` opens a regular expression
from the previous character alone, which JavaScript does not decide there, so it under-reports
three shapes - a property named after a keyword then a division (`record.in / 2`), a regex holding
a quote after `)`, and a regex holding `/*` after `}` - each measured with a real `no-unused-vars`
behind the directive at rc 0 (#205), and it can over-report a `//` in JSX text, which is the
direction a pin should fail in (the tree has no `.tsx` file today). A file the parser cannot parse
is read by the scanner for that file alone, which is the pin's one silent narrowing;
`CommentReaderParityTest` refuses it, failing on a tracked file the parser could not parse as well
as on a comment the two readers disagree about.

Run with the rest: `python3 -m unittest discover -s tests`
"""

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tokenize
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def linters_unusable(which=None, isdir=None, root=None):
    """Why the real-linter control cannot run here, or None when it can.

    Two ways only, both measured: `node` is not on PATH, or `bun install --frozen-lockfile` has not
    been run. The lookups are arguments so the decision is testable on a machine with no node.
    """
    which = shutil.which if which is None else which
    isdir = os.path.isdir if isdir is None else isdir
    root = ROOT if root is None else root
    if which("node") is None:
        return "no node on PATH"
    if not isdir(os.path.join(root, "node_modules", "eslint")):
        return "eslint is not installed here: run bun install --frozen-lockfile"
    return None


def javascript_parser_unusable(which=None, isdir=None, root=None):
    """Why the parser-backed comment reader cannot run here, or None when it can.

    The same two ways `linters_unusable` has, with the package this reader needs: `node` is not on
    PATH, or `bun install --frozen-lockfile` has not been run. The lookups are arguments so the
    decision is testable on a machine with no node, and `root` is the repo in every call: the parser
    is resolved out of the repo's `node_modules` even when the files it reads sit in a fixture.
    """
    which = shutil.which if which is None else which
    isdir = os.path.isdir if isdir is None else isdir
    root = ROOT if root is None else root
    if which("node") is None:
        return "no node on PATH"
    if not isdir(os.path.join(root, "node_modules", "typescript-eslint")):
        return "typescript-eslint is not installed here: run bun install --frozen-lockfile"
    return None


def linter_control_disposition(reason, environ):
    """What `LinterBitesTest` does with that reason: `run`, `skip` or `fail`.

    `api (bun)` sets `WAKE_REQUIRE_LINTERS=1`, so the one job that claims to run the control fails
    instead of skipping it - skipping there and staying green is the failure #216 was filed about
    (measured at ce29cf01, both tests skipped in CI). Only `1` arms it: `0`, `false` and an empty
    value are how a shell says no, and a truthiness test would make `=0` mean the opposite.
    """
    if reason is None:
        return "run"
    return "fail" if environ.get("WAKE_REQUIRE_LINTERS") == "1" else "skip"


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

#: The suffixes eslint reads a comment out of. `prettier --check` reads the same files.
JAVASCRIPT_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts")

#: The parser-backed reader, as the repo's own script: it is run from ROOT whatever root the files
#: come from, so `typescript-eslint` resolves out of the repo's `node_modules` and not a fixture's.
JAVASCRIPT_COMMENTS_SCRIPT = os.path.join("scripts", "js-comments.mjs")

#: The block form. Everything below it, to the end of the file, is exempt, so it names no line and
#: is refused whatever rules it lists. Read against the comment's text without its markers, as
#: eslint matches it, and case-sensitively: `/* ESLINT-DISABLE */` was measured to do nothing.
ESLINT_BLOCK_DISABLE = re.compile(r"^\s*eslint-disable(?![\w-])(?P<rest>.*)$", re.DOTALL)

#: The two line-scoped forms. Legal only when they name a rule.
ESLINT_LINE_DISABLE = re.compile(r"^\s*eslint-disable-(?:next-)?line(?![\w-])(?P<rest>.*)$", re.DOTALL)

#: An inline config comment, `/* eslint <rule>: <value>[, <rule>: <value>] */`, which reconfigures
#: its rules to the end of the file. It never counts as a suppression: the value is what eslint
#: applies, severity and options both, so the pin reads the whole of it, one pair per rule.
ESLINT_INLINE_CONFIG = re.compile(r"^\s*eslint\s+(?P<body>.*)$", re.DOTALL)

#: The only inline config value the pin accepts: a raise to `error` on that rule and nothing else.
INLINE_CONFIG_RAISE = re.compile(r"^(?:\[\s*)?(?:2|error|\"error\"|'error')(?:\s*\])?$", re.IGNORECASE)

#: `prettier-ignore`, the formatter's own file- or node-wide exemption. Lower case only: the upper
#: case spelling was measured to leave a misformatted file at rc 1.
PRETTIER_IGNORE = re.compile(r"^\s*prettier-ignore\s*$")

#: A rule name as eslint spells it: `no-console`, `eqeqeq`, `@typescript-eslint/no-unused-vars`.
RULE_NAME = re.compile(r"^@?[A-Za-z0-9][A-Za-z0-9_.-]*(?:/[A-Za-z0-9_.-]+)?$")

#: Keywords after which `/` opens a regular expression rather than dividing.
REGEX_KEYWORDS = frozenset("await case delete do else in instanceof new of return throw typeof void yield".split())

#: The characters after which `/` opens a regular expression. `++` and `--` are handled before this
#: set is consulted: an increment yields a value, so the `/` after one divides.
REGEX_PRECEDERS = frozenset("(,=:[!&|?{};+-*%<>~^")

IDENTIFIER_START = re.compile(r"[A-Za-z_$]")
IDENTIFIER_PART = re.compile(r"[A-Za-z0-9_$]")

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

#: Files the javascript scan must read: two the api job lints, the config file eslint loads, the
#: one `.ts` the lint script's paths leave out, and the reader the scan itself runs - a directive in a
#: file lint does not read yet is still this class, and a directive in the reader would be one the
#: reader refuses while reading itself.
PINNED_JAVASCRIPT_FILES = (
    "apps/api/src/app.ts",
    "eslint.config.js",
    "packages/shared/src/index.ts",
    "scripts/e2e-threads.ts",
    "scripts/js-comments.mjs",
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


def eslint_files(root=ROOT, paths=None):
    """Every tracked file eslint can read a comment out of, by suffix."""
    listed = tracked_paths(root) if paths is None else paths
    return sorted(rel for rel in listed if rel.lower().endswith(JAVASCRIPT_SUFFIXES))


def skip_quoted(source, index):
    """The index after the quoted string opening at `index`, or the line end when it never closes."""
    quote = source[index]
    index += 1
    while index < len(source):
        char = source[index]
        if char == "\\":
            index += 2
            continue
        if char == quote:
            return index + 1
        if char in "\r\n":
            return index
        index += 1
    return index


def skip_regex(source, index):
    """The index after the regular expression opening at `index`, or the line end."""
    index += 1
    in_class = False
    while index < len(source):
        char = source[index]
        if char == "\\":
            index += 2
            continue
        if char in "\r\n":
            return index
        if in_class:
            if char == "]":
                in_class = False
        elif char == "[":
            in_class = True
        elif char == "/":
            index += 1
            while index < len(source) and source[index].isalpha():
                index += 1
            return index
        index += 1
    return index


def skip_template(source, index, found):
    """The index after the template literal opening at `index`.

    A comment inside its `${ ... }` is a real comment, so the interpolation is scanned as code.
    """
    index += 1
    while index < len(source):
        char = source[index]
        if char == "\\":
            index += 2
            continue
        if char == "`":
            return index + 1
        if source.startswith("${", index):
            index = scan_javascript(source, index + 2, True, found)
            continue
        index += 1
    return index


def scan_javascript(source, index, in_interpolation, found):
    """Append `(offset, text, block)` for every real comment; return the index the scan stopped at.

    With `in_interpolation`, the scan stops at the `}` that closes the `${ ... }` it started in. The
    text is the comment without its markers, which is what eslint matches a directive against.
    """
    previous = ""
    while index < len(source):
        char = source[index]
        if char in " \t\r\n\f\v":
            index += 1
        elif source.startswith("//", index):
            end = source.find("\n", index)
            if end < 0:
                end = len(source)
            found.append((index, source[index + 2 : end], False))
            index = end
        elif source.startswith("/*", index):
            end = source.find("*/", index + 2)
            if end < 0:
                found.append((index, source[index + 2 :], True))
                index = len(source)
            else:
                found.append((index, source[index + 2 : end], True))
                index = end + 2
            previous = "value"
        elif char in "\"'":
            index = skip_quoted(source, index)
            previous = "value"
        elif char == "`":
            index = skip_template(source, index, found)
            previous = "value"
        elif char in "+-" and source.startswith(char * 2, index):
            # `counter++ / 2` divides: reading the `/` as a regex ate the rest of the line, and a
            # `/* eslint-disable */` after it went unread (#196).
            index += 2
            previous = "value"
        elif char == "/":
            if previous in REGEX_KEYWORDS or previous in REGEX_PRECEDERS or previous == "":
                index = skip_regex(source, index)
                previous = "value"
            else:
                index += 1
                previous = "/"
        elif char == "}":
            if in_interpolation:
                return index + 1
            index += 1
            previous = "value"
        elif IDENTIFIER_START.match(char):
            end = index + 1
            while end < len(source) and IDENTIFIER_PART.match(source[end]):
                end += 1
            word = source[index:end]
            previous = word if word in REGEX_KEYWORDS else "value"
            index = end
        else:
            index += 1
            previous = char if char in REGEX_PRECEDERS else "value"
    return index


def parser_comments(paths, node=None, script=None):
    """One `{path, comments, error}` record per path, from one `node scripts/js-comments.mjs` run.

    `paths` are handed over absolute, so the parser knows each file's extension from its own path,
    and the script is always the repo's, so the import resolves out of the repo's `node_modules`
    even when the files being read sit in a fixture directory.

    A failure of the reader itself - a non-zero exit, output that is not one JSON object per path,
    or fewer records than paths - raises `RuntimeError` instead of letting the caller scan: the
    scanner misses a directive in three shapes (#205), so a reader that crashed would turn a real
    directive into a green check. A file the parser cannot parse is not that failure: it comes back
    with `error` set, and the caller decides what to do with that file.
    """
    node = shutil.which("node") if node is None else node
    script = os.path.join(ROOT, JAVASCRIPT_COMMENTS_SCRIPT) if script is None else script
    finished = subprocess.run([node, script, *paths], capture_output=True, text=True)
    if finished.returncode != 0:
        raise RuntimeError(
            f"the JS/TS comment reader failed: node {script} exited {finished.returncode} and the "
            f"scanner is not a safe fallback for it: {finished.stderr.strip()[-400:]}"
        )
    records = []
    for line in finished.stdout.splitlines():
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except ValueError as error:
            raise RuntimeError(f"the JS/TS comment reader wrote no JSON object: {line.strip()[:200]}") from error
    if len(records) != len(paths):
        raise RuntimeError(
            f"the JS/TS comment reader answered {len(records)} of {len(paths)} files, and a missing "
            "verdict would turn into a scanner read"
        )
    return records


def record_comments(record):
    """`(line, text, block)` from one reader record, in the order the reader reported them."""
    return [(comment["line"], comment["text"], bool(comment["block"])) for comment in record["comments"]]


def scanner_comments(source):
    """`(line, text, block)` for every comment a character scan finds in a JS or TS source.

    The fallback reader: it runs where `node_modules` is missing - the Python test matrix - and on
    a file the parser cannot parse. It reads one character at a time and decides whether `/` opens
    a regular expression from the previous character alone, which JavaScript does not decide there,
    so it under-reports the three shapes #205 measured, and it can over-report a `//` in JSX text.
    Kept, tested and pinned rather than removed: it is the only reader a checkout without
    `node_modules` has.
    """
    found = []
    scan_javascript(source, 0, False, found)
    return [(source.count("\n", 0, offset) + 1, text, block) for offset, text, block in found]


def javascript_comments(source, path=None):
    """`(line, text, block)` for every real comment in a JS or TS source.

    With `path` - the file `source` was read from, absolute or repo-relative - and the parser
    installed under ROOT, the comments are the parser's, so a `/` inside a regular expression or a
    quote inside one is not read as anything but the literal it is. Without a path, or without
    `node_modules`, `scanner_comments` reads them; the two limit lists are the module docstring's.

    A file the parser cannot parse is read by the scanner here, for that file alone. That is the
    one place this switch narrows the pin, so it is not left quiet: `CommentReaderParityTest` fails
    on a tracked file the parser cannot parse, as well as on a comment the two readers disagree
    about.
    """
    if path is None or javascript_parser_unusable() is not None:
        return scanner_comments(source)
    where = path if os.path.isabs(path) else os.path.join(ROOT, path)
    record = parser_comments([where])[0]
    if record["error"] is not None:
        return scanner_comments(source)
    return record_comments(record)


def eslint_rules(rest):
    """The rule names a line-scoped directive covers, the ones before any `-- why`."""
    body = rest.split("--", 1)[0].strip()
    rules = []
    for token in (part for part in re.split(r"[,\s]+", body) if part):
        if not RULE_NAME.match(token):
            break
        rules.append(token)
    return rules


def split_top_level(text, separator):
    """`text` cut at every `separator` that sits outside brackets and quoted strings.

    An inline config's value holds both, `["error", {"varsIgnorePattern": ".*"}]`, so a plain
    `str.split` reads one rule's options as the start of the next rule.
    """
    pieces = []
    depth = 0
    quote = ""
    escaped = False
    start = 0
    for index, char in enumerate(text):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in "\"'`":
            quote = char
        elif char in "[{(":
            depth += 1
        elif char in "]})":
            depth = max(depth - 1, 0)
        elif char == separator and depth == 0:
            pieces.append(text[start:index])
            start = index + 1
    pieces.append(text[start:])
    return pieces


def inline_config_pairs(body):
    """`(rule, value)` for each `rule: value` pair of an inline config's body, one per top-level comma.

    Measured at eslint 10.15.0: `/* eslint no-console: error, eqeqeq: error */` raises both rules
    and reports a violation of either at rc 1, so a two-rule raise is two pairs, not one rule
    carrying a comma-laden value. A pair with no value (`eqeqeq:`) is dropped by eslint while the
    rest of the comment still applies, so it comes back as an empty string and is judged nothing; a
    segment with no `:` at all is one eslint fails the file on (`Failed to parse JSON from
    '"no-console": error, eqeqeq'`, rc 1), so it comes back as `None` and is refused.
    """
    pairs = []
    for segment in split_top_level(body, ","):
        segment = segment.strip()
        if not segment:
            continue
        head, *tail = split_top_level(segment, ":")
        pairs.append((head.strip(), ":".join(tail).strip() if tail else None))
    return pairs


def inline_config_fault(value):
    """What an inline config's value does beyond raising one rule to `error`, or `None` when it does.

    Measured at eslint 10.15.0: a list that carries an options object after the severity keeps its
    `error` and still disarms the rule - `{"varsIgnorePattern": ".*"}` on a real unused variable,
    `{"args": "none"}` on an unused parameter, `{"caughtErrors": "none"}` on an unused catch
    binding - and a string option does the same, `["error", "smart"]` letting `x == null` through
    `eqeqeq`. No list of options can be read as safe here, so the value is refused whole unless it
    is the severity alone.
    """
    text = value.strip()
    if INLINE_CONFIG_RAISE.match(text):
        return None
    inner = text[1:-1].strip() if text.startswith("[") and text.endswith("]") else text
    if "," in inner:
        return "options"
    return "below"


def javascript_comment_map(root, paths):
    """`{rel: [(line, text, block)]}` for every path, parser first and the scanner per file.

    One node process reads every file when the parser is usable: the directive list does not depend
    on which file carries it, and 38 tracked JS/TS files make a process per file unaffordable. A
    file the parser could not parse, and every file when `node_modules` is missing, is scanned.
    """
    parsed = {}
    if paths and javascript_parser_unusable() is None:
        records = parser_comments([os.path.join(root, rel) for rel in paths])
        parsed = {rel: record_comments(record) for rel, record in zip(paths, records) if record["error"] is None}
    comments = {}
    for rel in paths:
        found = parsed.get(rel)
        comments[rel] = scanner_comments(text(os.path.join(root, rel))) if found is None else found
    return comments


def javascript_suppression_problems(root, paths):
    """Every eslint or prettier directive in a JS/TS file that exempts more than it names.

    The comments come from `javascript_comment_map`, so the verdict is the parser's where it is
    installed and the scanner's where it is not, or for a file the parser cannot parse.
    """
    problems = []
    for rel, comments in javascript_comment_map(root, list(paths)).items():
        for line, comment, block in comments:
            where = f"{rel}:{line}: '{comment.strip()}'"
            if PRETTIER_IGNORE.match(comment):
                problems.append(
                    f"{where} exempts the next node from `prettier --check`: one comment on top of a "
                    "misformatted file left the formatter half of the `api` job at rc 0 - format the "
                    "code (#196)"
                )
                continue
            line_directive = ESLINT_LINE_DISABLE.match(comment)
            if line_directive and not eslint_rules(line_directive.group("rest")):
                problems.append(
                    f"{where} names no rule: `// eslint-disable-next-line` with no rule exempts every "
                    "rule on the next line, and one comment took `eslint apps` from rc 1 to rc 0 - "
                    "write `// eslint-disable-next-line <rule>` (#196)"
                )
                continue
            if block and ESLINT_BLOCK_DISABLE.match(comment):
                problems.append(
                    f"{where} is the block form: it exempts the rules it names, and every rule when it "
                    "names none, from here to the end of the file, and one comment took "
                    "`eslint apps` from rc 1 to rc 0 - write `// eslint-disable-next-line <rule>` "
                    "on the line it covers (#196)"
                )
                continue
            config = ESLINT_INLINE_CONFIG.match(comment)
            if block and config:
                for rule, value in inline_config_pairs(config.group("body")):
                    if value is None:
                        problems.append(
                            f"{where} is not a `rule: value` list: `{rule}` carries no value, and "
                            "eslint fails the file on the comment rather than reading it - "
                            "measured, `Failed to parse JSON` at rc 1 - so write `<rule>: error` "
                            "(#204)"
                        )
                        continue
                    # eslint drops a pair with no value and applies the rest of the comment
                    # (measured), so there is nothing to refuse there.
                    if not value:
                        continue
                    fault = inline_config_fault(value)
                    if fault == "options":
                        problems.append(
                            f"{where} reconfigures `{rule}` rather than raising it: the value "
                            "carries options after the severity, and eslint honours them with the "
                            'severity still at `error` - measured, `["error", '
                            '{"varsIgnorePattern": ".*"}]`, `["error", {"args": "none"}]` and '
                            '`["error", "smart"]` each left a real error at rc 0. The pin cannot '
                            "read an options object as safe, so a rule entry carrying options "
                            "belongs outside the pin - `eslint.config.js` is read for directives "
                            "only (#208), so it is not the way out either. Keep the inline "
                            "comment to `<rule>: error` (#196)"
                        )
                    elif fault == "below":
                        problems.append(
                            f"{where} turns `{rule}` below error: eslint exits 0 on a warning, so "
                            '`/* eslint <rule>: 0` / `1` / `"off"` / `"warn"` disarms the check as '
                            "surely as removing the rule - an inline config may only raise a rule "
                            "to error (#196)"
                        )
    return problems


class InlineSuppressionTestCase(unittest.TestCase):
    """#191: a suppression inside a linted file is pinned, so one comment cannot silence a check."""

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
        self.assertEqual(
            javascript_suppression_problems(ROOT, eslint_files()),
            [],
            "an eslint directive is unpinned: one `/* eslint-disable */` in a `.ts` file takes the `api` "
            "job's eslint step from rc 1 to rc 0 while every other check stays green (#196)",
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
        javascript = eslint_files()
        self.assertGreaterEqual(len(javascript), 30, f"the javascript scan read only {len(javascript)} files")
        for rel in PINNED_JAVASCRIPT_FILES:
            self.assertIn(rel, javascript, f"eslint reads {rel}, and the javascript scan does not read it")
        js_comments = sum(len(found) for found in javascript_comment_map(ROOT, javascript).values())
        self.assertGreater(js_comments, 150, f"the javascript comment scan found only {js_comments} comments")

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

    def test_an_eslint_block_disable_is_refused(self):
        """Measured: `/* eslint-disable */` at line 1 and mid-file each took `eslint apps` to rc 0."""
        for comment in (
            "/* eslint-disable */",
            "/*eslint-disable*/",
            "/* eslint-disable @typescript-eslint/no-unused-vars */",
            "/* eslint-disable -- a reason does not help */",
        ):
            with self.subTest(comment=comment):
                problems = self.problems(javascript=f"{comment}\nconst neverUsed = 2;\n")
                self.assertEqual(len(problems), 1, problems)
                self.assertIn("block form", problems[0])
                self.assertIn("planted.ts:1", problems[0])
        problems = self.problems(javascript="export const value = 1;\n/* eslint-disable */\nconst neverUsed = 2;\n")
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("planted.ts:2", problems[0])

    def test_an_eslint_line_disable_must_name_a_rule(self):
        """A `-line` / `-next-line` form with no rule exempts every rule on its line."""
        cases = (
            "const neverUsed = 2; // eslint-disable-line\n",
            "const neverUsed = 2; /* eslint-disable-line */\n",
            "const neverUsed = 2; /*eslint-disable-line */\n",
            "// eslint-disable-next-line\nconst neverUsed = 2;\n",
            "//eslint-disable-next-line\nconst neverUsed = 2;\n",
            "/* eslint-disable-next-line */\nconst neverUsed = 2;\n",
            "// eslint-disable-next-line -- a reason and no rule\nconst neverUsed = 2;\n",
        )
        for body in cases:
            with self.subTest(body=body):
                problems = self.problems(javascript=body)
                self.assertEqual(len(problems), 1, problems)
                self.assertIn("names no rule", problems[0])

    def test_a_rule_scoped_eslint_disable_passes(self):
        """The form the tree may use: the rule named, optionally with a `-- why` tail."""
        for body in (
            "const neverUsed = 2; // eslint-disable-line @typescript-eslint/no-unused-vars\n",
            "const neverUsed = 2; // eslint-disable-line @typescript-eslint/no-unused-vars -- a reason\n",
            "const neverUsed = 2; /* eslint-disable-line @typescript-eslint/no-unused-vars */\n",
            "// eslint-disable-next-line @typescript-eslint/no-unused-vars\nconst neverUsed = 2;\n",
            "/* eslint-disable-next-line eqeqeq */\nconst neverUsed = 2;\n",
            "// eslint-disable-next-line eqeqeq, no-console\nconst neverUsed = 2;\n",
        ):
            with self.subTest(body=body):
                self.assertEqual(self.problems(javascript=body), [])

    def test_an_inline_eslint_config_may_only_raise_a_rule(self):
        """Measured: `0`, `1`, `"off"`, `"warn"` and their list forms all left eslint at rc 0."""
        rule = "@typescript-eslint/no-unused-vars"
        for severity in ("0", "1", '"off"', '"warn"', "[0]", "[1]", " 0 ", '["warn"]'):
            with self.subTest(severity=severity):
                problems = self.problems(javascript=f"/* eslint {rule}: {severity} */\nconst neverUsed = 2;\n")
                self.assertEqual(len(problems), 1, problems)
                self.assertIn("below error", problems[0])
        for severity in ("2", '"error"', "error", '["error"]', "[2]"):
            with self.subTest(severity=severity):
                self.assertEqual(
                    self.problems(javascript=f"/* eslint eqeqeq: {severity} */\nexport const f = (a, b) => a == b;\n"),
                    [],
                )

    def test_an_inline_eslint_config_that_carries_options_is_refused(self):
        """Measured: each of these left a real error at rc 0 with the severity still at `error`."""
        rule = "@typescript-eslint/no-unused-vars"
        for value in (
            '["error", {"varsIgnorePattern": ".*"}]',
            '[2, {"varsIgnorePattern": ".*"}]',
            '["error", {"args": "none"}]',
            '["error", {"argsIgnorePattern": ".*"}]',
            '["error", {"caughtErrors": "none"}]',
            '["error", "smart"]',
        ):
            with self.subTest(value=value):
                problems = self.problems(javascript=f"/* eslint {rule}: {value} */\nconst neverUsed = 2;\n")
                self.assertEqual(len(problems), 1, problems)
                self.assertIn("reconfigures", problems[0])
                self.assertIn("#196", problems[0])
                # The refusal used to send the developer to eslint.config.js as if the pin read
                # rule values there. It does not: the file is scanned for directives, so an entry
                # carrying options leaves the rule unguarded, and the message now says so (#208).
                self.assertIn("#208", problems[0])
                self.assertIn("directives only", problems[0])

    def test_a_multi_rule_inline_config_is_read_one_pair_per_rule(self):
        """Measured: eslint raises both rules and reports a violation of either (rc 1, 2 errors)."""
        for body in (
            "/* eslint no-console: error, eqeqeq: error */\nexport const v = 1;\n",
            "/* eslint eqeqeq: error, no-console: error */\nexport const v = 1;\n",
            "/* eslint no-console: 2, eqeqeq: 2 */\nexport const v = 1;\n",
            "/* eslint   no-console :  error ,  eqeqeq :error  */\nexport const v = 1;\n",
            "/* eslint no-console: error,\n   eqeqeq: error */\nexport const v = 1;\n",
            '/* eslint no-console: error, eqeqeq: ["error"] */\nexport const v = 1;\n',
            # A trailing comma, and a pair with no value: eslint drops the empty pair and applies
            # the rest of the comment (measured), so neither is a refusal.
            "/* eslint no-console: error, */\nexport const v = 1;\n",
            "/* eslint no-console: error, eqeqeq: */\nexport const v = 1;\n",
        ):
            with self.subTest(body=body):
                self.assertEqual(self.problems(javascript=body), [], "a legal two-rule raise was refused")

    def test_a_multi_rule_inline_config_names_the_pair_at_fault(self):
        """The message names the rule that carries the options, not the first rule in the comment."""
        body = (
            "/* eslint no-console: error, "
            '@typescript-eslint/no-unused-vars: ["error", {"varsIgnorePattern": ".*"}] */\n'
            "const neverUsed = 2;\n"
        )
        problems = self.problems(javascript=body)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("reconfigures `@typescript-eslint/no-unused-vars`", problems[0])
        self.assertNotIn("reconfigures `no-console`", problems[0])
        body = "/* eslint no-console: error, eqeqeq: warn */\nexport const v = 1;\n"
        problems = self.problems(javascript=body)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("turns `eqeqeq` below error", problems[0])
        # Two faults are two problems, one for each pair that carries one.
        body = '/* eslint no-console: 1, eqeqeq: "off" */\nexport const v = 1;\n'
        problems = self.problems(javascript=body)
        self.assertEqual(len(problems), 2, problems)

    def test_an_inline_config_segment_that_is_not_a_pair_is_refused(self):
        """Measured: eslint fails the file on it, `Failed to parse JSON`, rather than reading it."""
        for body in (
            "/* eslint no-console: error, eqeqeq */\nexport const v = 1;\n",
            "/* eslint eqeqeq */\nexport const v = 1;\n",
        ):
            with self.subTest(body=body):
                problems = self.problems(javascript=body)
                self.assertEqual(len(problems), 1, problems)
                self.assertIn("not a `rule: value` list", problems[0])
                self.assertIn("#204", problems[0])

    def test_a_division_after_an_increment_is_not_a_regex(self):
        """Measured: `counter++ / 2` was read as a regex, so the directive behind it went unread."""
        for body, expected in (
            ("let c = 1;\nexport function f() { return c++ / 2; } /* eslint-disable */\n", "block form"),
            ("let c = 1;\nexport function f() { return c-- / 2; } /* eslint-disable */\n", "block form"),
            ("let c = 1;\nexport const g = () => c++ / 2; // eslint-disable-next-line\n", "names no rule"),
        ):
            with self.subTest(body=body):
                problems = self.problems(javascript=body)
                self.assertEqual(len(problems), 1, problems)
                self.assertIn(expected, problems[0])
                self.assertIn("planted.ts:2", problems[0])
        # A `/` after a single `+` still opens a regex, so a directive inside it is not a comment.
        self.assertEqual(
            self.problems(javascript="export const n = 1 + /\\/\\* eslint-disable \\*\\//.source.length;\n"),
            [],
            "a regex after `+` was read as division, and the text in it as a comment",
        )

    def test_a_prettier_ignore_is_refused(self):
        """Measured: `// prettier-ignore` above a misformatted line left `prettier --check` at rc 0."""
        for body in (
            "// prettier-ignore\nexport const  misformatted   =    1;\n",
            "/* prettier-ignore */\nexport const  misformatted   =    1;\n",
        ):
            with self.subTest(body=body):
                problems = self.problems(javascript=body)
                self.assertEqual(len(problems), 1, problems)
                self.assertIn("prettier --check", problems[0])
                self.assertIn("planted.ts:1", problems[0])
        self.assertEqual(
            self.problems(javascript="// PRETTIER-IGNORE\nexport const  misformatted   =    1;\n"),
            [],
            "the upper case spelling was measured to leave prettier at rc 1, so it is not a directive",
        )

    def test_a_directive_inside_a_string_or_a_template_is_not_a_directive(self):
        """A `//` in a URL and a directive quoted in a template are the reader's own trap."""
        for body in (
            'export const url = "http://localhost";\n',
            'export const s = "/* eslint-disable */ http://localhost";\n',
            "export const s = 'it\\'s // not a comment';\n",
            "export const t = `/* eslint-disable */ a // b`;\n",
            "export const t = `\n// eslint-disable-next-line\n`;\n",
            "export const t = `a // b /* c */ ${d} // e`;\n",
            "export const re = /\\/\\* eslint-disable \\*\\//;\n",
        ):
            with self.subTest(body=body):
                self.assertEqual(self.problems(javascript=body), [], "a literal was read as a comment")
        # The same file, with a real directive after the literal: one problem, and it is the real one.
        body = 'export const url = "http://localhost";\n// eslint-disable-next-line\nconst neverUsed = 2;\n'
        problems = self.problems(javascript=body)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("planted.ts:2", problems[0])

    def test_the_regex_arm_leaves_the_rest_of_the_line_to_read(self):
        """A quoted character inside a regex is not a string: reading it as one eats the comment."""
        body = "export const re = /['\"]/; // eslint-disable-next-line\nconst neverUsed = 2;\n"
        problems = self.problems(javascript=body)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("planted.ts:1", problems[0])

    def test_the_eslint_spellings_that_do_not_bite_are_left_alone(self):
        """Measured inert: upper case, no `-line` suffix, after other text, and `@ts-nocheck`."""
        for body in (
            "/* ESLINT-DISABLE */\nconst neverUsed = 2;\n",
            "// eslint-disable\nconst neverUsed = 2;\n",
            "// see eslint-disable-next-line\nconst neverUsed = 2;\n",
            "// my-eslint-disable-next-line\nconst neverUsed = 2;\n",
            "// @ts-nocheck\nconst neverUsed = 2;\n",
            "/* eslint-enable */\nconst neverUsed = 2;\n",
        ):
            with self.subTest(body=body):
                self.assertEqual(self.problems(javascript=body), [], "a form eslint ignores was refused")

    def problems(self, python="", shell="", javascript=""):
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
            if javascript:
                with open(os.path.join(root, "planted.ts"), "w", encoding="utf-8") as handle:
                    handle.write(javascript)
                paths.append("planted.ts")
            python_paths, shell_paths = linters_files(root, paths)
            return (
                python_suppression_problems(root, python_paths)
                + shell_suppression_problems(root, shell_paths)
                + javascript_suppression_problems(
                    root, [rel for rel in eslint_files(root, paths) if rel == "planted.ts"]
                )
            )
        finally:
            shutil.rmtree(root, ignore_errors=True)


#: #205's shapes, one row per line the issue measured: the code line, whether the directive sits on
#: that line, and whether `scanner_comments` reads the directive today. Every body carries a real
#: `@typescript-eslint/no-unused-vars` behind the directive - the line `comment_shape_source` adds -
#: so `LinterBitesTest` measures both readers against the real eslint on the same fixture, and a
#: scanner that starts reading one of these moves the boundary the module docstring writes down.
COMMENT_SHAPE_CASES = (
    ("a property named after a keyword, then a division", "export const half = record.in / 2;", True, True),
    ("the same, with the directive on its own line", "export const half = record.in / 2;", False, False),
    ("a regex holding a quote, after `)`", 'if (s.length > 0) /"/.test(s);', True, True),
    ("the same, with the directive on its own line", 'if (s.length > 0) /"/.test(s);', False, False),
    ("a regex holding `/*`, after `}`", "for (const c of s) { void c; } /\\/*/.test(s);", True, True),
    (
        "the same, with the directive on its own line",
        "for (const c of s) { void c; } /\\/*/.test(s);",
        False,
        True,
    ),
)


def comment_shape_source(code, same_line=True):
    """The shape's code line, its block directive, and the unused variable the directive hides."""
    directive = "/* eslint-disable */"
    joiner = " " if same_line else "\n"
    return f"{code}{joiner}{directive}\nconst neverUsed = 2;\n"


def block_directives(comments):
    """The block-level `eslint-disable` each `(line, text, block)` carries, stripped of its markers."""
    return [comment.strip() for _line, comment, _block in comments if ESLINT_BLOCK_DISABLE.match(comment)]


class CommentReaderLimitTest(unittest.TestCase):
    """#205: the parser reads the directives the scanner cannot, and the scanner's gap stays pinned.

    The pin read one character at a time and decided a regex from the previous character alone, so
    three shapes hid a real `/* eslint-disable */` from it; `scripts/js-comments.mjs` reads the
    same files through `@typescript-eslint/parser` now, and the scanner is the fallback for a
    checkout without `node_modules`. Both are asserted here: the parser on every shape, and the
    scanner on the shapes it reads today as well as the ones it does not, so neither the limit nor
    its boundary can move without this file saying so.
    """

    def root(self, prefix="shape-"):
        """A fixture directory, removed with the test."""
        root = tempfile.mkdtemp(prefix=prefix)
        self.addCleanup(shutil.rmtree, root, True)
        return root

    def write(self, source, name="planted.ts"):
        """`(root, path)` of a fixture file holding `source`."""
        root = self.root()
        path = os.path.join(root, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(source)
        return root, path

    def test_the_parser_reads_every_shape(self):
        """One node process reads all of them, and the pin names the directive in each file."""
        reason = javascript_parser_unusable()
        if reason is not None:
            self.skipTest(reason)
        root = self.root("shapes-")
        paths = []
        for index, (_name, code, same_line, _hidden) in enumerate(COMMENT_SHAPE_CASES):
            path = os.path.join(root, f"planted-{index}.ts")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(comment_shape_source(code, same_line))
            paths.append(f"planted-{index}.ts")
        problems = javascript_suppression_problems(root, paths)
        self.assertEqual(len(problems), len(paths), problems)
        for index, (name, code, same_line, _hidden) in enumerate(COMMENT_SHAPE_CASES):
            with self.subTest(shape=name):
                rel = f"planted-{index}.ts"
                found = javascript_comments(comment_shape_source(code, same_line), os.path.join(root, rel))
                self.assertEqual(
                    block_directives(found),
                    ["eslint-disable"],
                    f"the parser read no directive out of the {name} shape (#205)",
                )
                line = 1 if same_line else 2
                where = f"{rel}:{line}: 'eslint-disable'"
                named = [problem for problem in problems if problem.startswith(where)]
                self.assertEqual(len(named), 1, problems)
                self.assertIn("block form", named[0])

    def test_the_scanner_reads_only_the_shapes_outside_its_limit(self):
        """The gap #205 measured, asserted rather than deleted: this is the fallback's own limit."""
        for name, code, same_line, hidden in COMMENT_SHAPE_CASES:
            with self.subTest(shape=name):
                found = block_directives(scanner_comments(comment_shape_source(code, same_line)))
                if hidden:
                    self.assertEqual(
                        found,
                        [],
                        f"the scanner now reads the {name} shape (#205): the limit the module "
                        "docstring and CHANGELOG.md record moved, so they move with it",
                    )
                else:
                    self.assertEqual(found, ["eslint-disable"], f"the scanner lost the {name} shape")

    def test_a_file_the_parser_cannot_parse_is_scanned(self):
        """Per-file fallback, and the reason the parity test refuses an unparsable tracked file."""
        reason = javascript_parser_unusable()
        if reason is not None:
            self.skipTest(reason)
        source = "export const broken = ;\n/* eslint-disable */\n"
        _root, path = self.write(source)
        self.assertEqual(
            block_directives(javascript_comments(source, path)),
            ["eslint-disable"],
            "a file the parser cannot parse was neither parsed nor scanned, so its directives are read by nobody",
        )

    def test_a_reader_that_crashes_is_loud_rather_than_scanned(self):
        """A crashed reader must not become a scanner read: that is how a directive goes silent."""
        with self.assertRaises(RuntimeError):
            parser_comments([os.path.join(ROOT, "apps", "api", "src", "app.ts")], node="/bin/false")


class CommentReaderParityTest(unittest.TestCase):
    """#205: on the tracked tree the two readers must agree, or the switch loses a directive.

    Skipped where the parser is not installed - the Python test matrix has no `node_modules` - and
    run by `api (bun)`, which installs it and refuses a skip, so the parity is measured in the job
    that reads the tree with the parser.
    """

    def test_the_two_readers_agree_on_every_tracked_javascript_file(self):
        """One node process reads the tree; the scanner must say the same thing about every file."""
        reason = javascript_parser_unusable()
        if reason is not None:
            self.skipTest(reason)
        paths = eslint_files()
        records = parser_comments([os.path.join(ROOT, rel) for rel in paths])
        self.assertEqual(len(records), len(paths), "the reader answered for a different set of files")
        for rel, record in zip(paths, records):
            with self.subTest(path=rel):
                self.assertIsNone(
                    record["error"],
                    f"the parser cannot parse {rel}, so the pin reads it with the scanner whose three "
                    "limits #205 lists - fix the file, not this test",
                )
                self.assertEqual(
                    record_comments(record),
                    scanner_comments(text(os.path.join(ROOT, rel))),
                    f"the two readers disagree about the comments in {rel}: the pin's verdict would "
                    "depend on whether node_modules is installed, and that disagreement is a finding "
                    "to trace back to the scanner limit in the module docstring, not to smooth over",
                )


class LinterRequirementTest(unittest.TestCase):
    """#216: the run/skip/fail decision, tested without a node or a lockfile installed."""

    def test_no_node_on_path_is_a_reason(self):
        self.assertEqual(linters_unusable(which=lambda name: None), "no node on PATH")

    def test_a_missing_eslint_in_node_modules_is_a_reason(self):
        seen = []

        def isdir(path):
            seen.append(path)
            return False

        reason = linters_unusable(which=lambda name: "/usr/bin/node", isdir=isdir)
        self.assertIn("bun install --frozen-lockfile", reason)
        self.assertTrue(seen[0].endswith(os.path.join("node_modules", "eslint")), seen)

    def test_node_with_eslint_installed_is_no_reason(self):
        self.assertIsNone(linters_unusable(which=lambda name: "/usr/bin/node", isdir=lambda path: True))

    def test_the_parser_reader_needs_its_own_package(self):
        """#205: the same two ways, with `typescript-eslint` as the package that has to be installed."""
        self.assertEqual(javascript_parser_unusable(which=lambda name: None), "no node on PATH")
        seen = []

        def isdir(path):
            seen.append(path)
            return False

        reason = javascript_parser_unusable(which=lambda name: "/usr/bin/node", isdir=isdir)
        self.assertIn("bun install --frozen-lockfile", reason)
        self.assertTrue(seen[0].endswith(os.path.join("node_modules", "typescript-eslint")), seen)
        self.assertIsNone(javascript_parser_unusable(which=lambda name: "/usr/bin/node", isdir=lambda path: True))

    def test_without_the_variable_an_unusable_linter_skips(self):
        self.assertEqual(linter_control_disposition("no node on PATH", {}), "skip")

    def test_the_variable_turns_the_skip_into_a_failure(self):
        self.assertEqual(linter_control_disposition("no node on PATH", {"WAKE_REQUIRE_LINTERS": "1"}), "fail")

    def test_zero_and_false_are_how_a_shell_says_no(self):
        for value in ("0", "false", "", "no"):
            with self.subTest(value=value):
                self.assertEqual(linter_control_disposition("no node on PATH", {"WAKE_REQUIRE_LINTERS": value}), "skip")

    def test_a_usable_linter_runs_whatever_the_variable_says(self):
        self.assertEqual(linter_control_disposition(None, {"WAKE_REQUIRE_LINTERS": "1"}), "run")


class LinterRequirementWiringTest(unittest.TestCase):
    """#216: the guard lives in `setUp`, so it is proved by running the control where node cannot
    be found - with the variable it must fail, without it it must skip. A `LinterRequirementTest`
    alone passes with the guard deleted, because the function it tests is still there."""

    def run_control(self, extra):
        bare = tempfile.mkdtemp(prefix="no-node-path-")
        self.addCleanup(shutil.rmtree, bare, True)
        env = {key: value for key, value in os.environ.items() if key != "WAKE_REQUIRE_LINTERS"}
        env["PATH"] = bare
        env.update(extra)
        return subprocess.run(
            [sys.executable, "-m", "unittest", "test_inline_suppressions.LinterBitesTest"],
            cwd=os.path.join(ROOT, "tests"),
            env=env,
            capture_output=True,
            text=True,
        )

    def test_the_variable_makes_the_control_fail_instead_of_skip(self):
        finished = self.run_control({"WAKE_REQUIRE_LINTERS": "1"})
        self.assertNotEqual(finished.returncode, 0, finished.stderr[-600:])
        self.assertIn("WAKE_REQUIRE_LINTERS is set and the linters are unusable", finished.stderr)
        self.assertNotIn("skipped", finished.stderr.strip().splitlines()[-1])

    def test_without_the_variable_the_control_skips_and_the_step_can_see_it(self):
        # Every test in the class skips in setUp when node is missing, so the count is the class's
        # own test count. It was hard-coded as 2, and #204 added a third LinterBitesTest, which
        # failed this test on main with no change to the guard.
        finished = self.run_control({})
        self.assertEqual(finished.returncode, 0, finished.stderr[-600:])
        expected = unittest.defaultTestLoader.loadTestsFromTestCase(LinterBitesTest).countTestCases()
        self.assertEqual(finished.stderr.strip().splitlines()[-1], f"OK (skipped={expected})")


class StrictRunnerTest(unittest.TestCase):
    """#216: the CI step runs `tests/run_module_strict.py`, so a skip there must fail the step."""

    def run_fixture(self, body):
        work = tempfile.mkdtemp(prefix="strict-runner-")
        self.addCleanup(shutil.rmtree, work, True)
        with open(os.path.join(work, "test_fixture_module.py"), "w", encoding="utf-8") as handle:
            handle.write(body)
        return subprocess.run(
            [sys.executable, os.path.join(ROOT, "tests/run_module_strict.py"), "test_fixture_module"],
            cwd=work,
            capture_output=True,
            text=True,
        )

    def test_a_module_that_passes_is_zero(self):
        finished = self.run_fixture(
            "import unittest\nclass T(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n"
        )
        self.assertEqual(finished.returncode, 0, finished.stdout[-400:] + finished.stderr[-400:])

    def test_a_skip_is_a_failure_naming_the_reason(self):
        finished = self.run_fixture(
            "import unittest\n"
            "class T(unittest.TestCase):\n"
            "    def test_dimmed(self):\n"
            "        self.skipTest('eslint is not installed here')\n"
        )
        self.assertEqual(finished.returncode, 1, finished.stdout[-400:])
        self.assertIn("refused: 1 test(s) skipped", finished.stderr)
        self.assertIn("eslint is not installed here", finished.stderr)

    def test_a_module_with_no_test_is_a_failure(self):
        finished = self.run_fixture("value = 1\n")
        self.assertEqual(finished.returncode, 1, finished.stdout[-400:])
        self.assertIn("loaded no test", finished.stderr)

    def test_a_failing_test_is_a_failure(self):
        """A control that FAILS must fail the step too, not only one that skips.

        Every fixture above is a module that passes, skips or loads nothing, so the runner's own
        `return 0 if result.wasSuccessful() else 1` was reached by no test: measured at `ebbb590`,
        `return 0` in its place, and `0 if True else 1`, both survived all 38 tests, and the step
        then reports a broken control as a pass - the silent green this runner exists to refuse,
        one level below the skip it already refuses.
        """
        finished = self.run_fixture(
            "import unittest\n"
            "class T(unittest.TestCase):\n"
            "    def test_broke(self):\n"
            "        self.assertTrue(False, 'the control no longer bites')\n"
        )
        self.assertEqual(finished.returncode, 1, finished.stdout[-400:])
        self.assertIn("FAILED (failures=1)", finished.stderr)
        self.assertIn("the control no longer bites", finished.stderr)

    def test_an_expected_failure_is_a_failure(self):
        """`@unittest.expectedFailure` is how a rotten control goes quiet without a red check.

        Measured at `ae59de6`: a fixture whose only test is an expected failure that then fails
        left the runner at rc 0 with `OK (expected failures=1)`, so the step reports a control
        that did not pass as a pass (#234). Same silent green as the skip, one decorator in, and
        the realistic route is a platform difference: "prettier differs on the runner, mark the arm
        an expected failure".
        """
        finished = self.run_fixture(
            "import unittest\n"
            "class T(unittest.TestCase):\n"
            "    @unittest.expectedFailure\n"
            "    def test_dimmed(self):\n"
            "        self.assertTrue(False, 'the control no longer bites')\n"
        )
        self.assertEqual(finished.returncode, 1, finished.stdout[-400:])
        self.assertIn("refused: 1 test(s) marked an expected failure", finished.stderr)
        self.assertIn("test_dimmed", finished.stderr)

    def test_usage_is_two(self):
        """A missing module name is a usage error, and the usage exit is 2."""
        finished = subprocess.run(
            [sys.executable, os.path.join(ROOT, "tests/run_module_strict.py")],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(finished.returncode, 2, finished.stdout[-400:])
        self.assertIn("usage: run_module_strict.py", finished.stderr)


class LinterBitesTest(unittest.TestCase):
    """#196: the real eslint and prettier, both directions, and the pin's verdict on the same file.

    Skipped where `bun install --frozen-lockfile` has not run - the `test` job has no `node_modules`,
    so this is the arm the suite cannot guarantee on its own. It is the control the pure scan rests
    on: the reader is only worth its fixtures while the linter still honours the forms it refuses.
    `api (bun)` runs this module with `WAKE_REQUIRE_LINTERS=1`, so a missing `node_modules` there
    fails the step instead of skipping the control and reporting green, and it runs it through
    `tests/run_module_strict.py`, which refuses a skip from any other route too
    ([#216](https://github.com/Anxiety471/wake/issues/216)).
    """

    DEFECT = "const neverUsed = 2;\nexport const value = 1;\n"

    def setUp(self):
        # The reason and what to do with it are functions, so a mutant that deletes the guard, or
        # that arms it on any non-empty value, fails `LinterRequirementTest` instead of passing
        # quietly wherever the linters happen to be installed (#216).
        reason = linters_unusable()
        disposition = linter_control_disposition(reason, os.environ)
        if disposition == "fail":
            self.fail(f"WAKE_REQUIRE_LINTERS is set and the linters are unusable: {reason}")
        if disposition == "skip":
            raise unittest.SkipTest(reason)
        self.root = tempfile.mkdtemp(prefix="eslint-arm-")
        self.addCleanup(shutil.rmtree, self.root, True)
        os.symlink(os.path.join(ROOT, "node_modules"), os.path.join(self.root, "node_modules"))
        # `eslint.config.js` is an ES module, so the scratch checkout needs the repo's `"type": "module"`.
        for name in ("eslint.config.js", "package.json"):
            shutil.copyfile(os.path.join(ROOT, name), os.path.join(self.root, name))

    def write(self, body, name="probe.ts"):
        with open(os.path.join(self.root, name), "w", encoding="utf-8") as handle:
            handle.write(body)

    def eslint(self, name="probe.ts"):
        """`(rc, errors)` for the real eslint over one file, told apart from a formatter crash."""
        finished = subprocess.run(
            [shutil.which("node"), os.path.join(ROOT, "node_modules/eslint/bin/eslint.js"), name, "--format", "json"],
            cwd=self.root,
            capture_output=True,
            text=True,
        )
        try:
            report = json.loads(finished.stdout or "[]")
        except ValueError:
            self.fail(f"eslint printed no JSON report: {finished.stdout[-400:]}{finished.stderr[-400:]}")
        return finished.returncode, sum(entry["errorCount"] for entry in report)

    def prettier(self, name="probe.ts"):
        """The rc of the real prettier over one file."""
        finished = subprocess.run(
            [shutil.which("node"), os.path.join(ROOT, "node_modules/prettier/bin/prettier.cjs"), "--check", name],
            cwd=self.root,
            capture_output=True,
            text=True,
        )
        self.assertIn(finished.returncode, (0, 1), finished.stderr[-400:])
        return finished.returncode

    def test_one_eslint_disable_takes_the_api_step_from_rc_1_to_rc_0(self):
        """The planted defect fails eslint, one comment hides it, and the pin names the file and line."""
        self.write(self.DEFECT)
        rc, errors = self.eslint()
        self.assertEqual(rc, 1, "the planted defect no longer fails eslint")
        self.assertGreaterEqual(errors, 1)
        self.assertEqual(javascript_suppression_problems(self.root, ["probe.ts"]), [], "a clean file was refused")

        self.write("/* eslint-disable */\n" + self.DEFECT)
        self.assertEqual(self.eslint(), (0, 0), "one comment no longer disarms eslint: the arms moved")
        problems = javascript_suppression_problems(self.root, ["probe.ts"])
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("probe.ts:1", problems[0])
        self.assertIn("block form", problems[0])

    def test_a_two_rule_inline_raise_keeps_both_rules_armed(self):
        """Measured: `/* eslint a: error, b: error */` is legal, and eslint reports both rules."""
        body = "console.log(1);\nexport const same = (a, b) => a == b;\n"
        self.write(body)
        self.assertEqual(self.eslint(), (0, 0), "the fixture no longer starts clean")
        self.write("/* eslint no-console: error, eqeqeq: error */\n" + body)
        self.assertEqual(self.eslint(), (1, 2), "eslint no longer reads both pairs of the raise")
        self.assertEqual(
            javascript_suppression_problems(self.root, ["probe.ts"]),
            [],
            "the pin refused a raise eslint itself honours",
        )

    def test_each_scanner_limit_shape_is_a_real_directive_to_eslint(self):
        """#205: control rc 1, the directive rc 0, and the pin reads it through the parser.

        The three same-line shapes are the ones the scanner missed; the own-line rows are the
        boundary it still reads, and the block regex on its own line is the shape it missed past
        the end of the code line too. All of them are directives to eslint either way.
        """
        for name, code, same_line, _hidden in COMMENT_SHAPE_CASES:
            with self.subTest(shape=name):
                self.write(f"{code}\nconst neverUsed = 2;\n")
                self.assertEqual(self.eslint(), (1, 1), "the control no longer fails eslint exactly once")
                self.write(comment_shape_source(code, same_line))
                self.assertEqual(self.eslint(), (0, 0), "one comment no longer disarms eslint: the arm moved")
                problems = javascript_suppression_problems(self.root, ["probe.ts"])
                self.assertEqual(len(problems), 1, problems)
                self.assertIn(f"probe.ts:{1 if same_line else 2}: 'eslint-disable'", problems[0])
                self.assertIn("block form", problems[0])

    def test_one_prettier_ignore_takes_the_api_step_from_rc_1_to_rc_0(self):
        """The formatter half of the same job, the same way."""
        self.write("export const  misformatted   =    1;\n")
        self.assertEqual(self.prettier(), 1, "the planted formatting no longer fails prettier")

        self.write("// prettier-ignore\nexport const  misformatted   =    1;\n")
        self.assertEqual(self.prettier(), 0, "one comment no longer disarms prettier: the arm moved")
        problems = javascript_suppression_problems(self.root, ["probe.ts"])
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("probe.ts:1", problems[0])
