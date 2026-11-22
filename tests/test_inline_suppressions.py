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
file read no `.ts` file at all (#196). `javascript_comments` reads one character at a time and skips
strings, template literals and regular expressions, because a directive quoted in any of the three
is not a directive and a `//` in a URL is not a comment, and it reports the text after the `//` or
`/*` the way eslint matches it: anchored at the comment's start, case-sensitively. Refused there:
the block `/* eslint-disable */`, which exempts everything below it - measured mid-file as well as
at line 1 - a `-line` / `-next-line` form that names no rule (and `//eslint-disable-next-line` with
no space after the `//` is honoured, so the marker cannot need one), an inline
`/* eslint <rule>: <severity> */` whose severity is not `error` (eslint exits 0 on a warning, so `0`,
`1`, `"off"`, `"warn"` and their list forms each disarmed the check), and `prettier-ignore`, which
exempts the next node from the `prettier --check` half of the same job with a misformatted file left
in the tree. Kept: a rule-scoped `// eslint-disable-next-line <rule>[, <rule>] [-- why]` and its block
spelling, the same shape as `# noqa: E402`. Measured as no hole: eslint reads its directives in
lower case only, so `/* ESLINT-DISABLE */` does nothing; `// eslint-disable` with no `-line` suffix
does nothing; a directive after other text (`// see eslint-disable-next-line`) does nothing; and
`@ts-nocheck` needs no pin, because `tseslint.configs.strict` reports it itself (rc 1). JSX text is
the one place this reader can over-report - the tree has no `.tsx` file today, and over-reporting a
directive is the direction a pin should fail in.

Run with the rest: `python3 -m unittest discover -s tests`
"""

import io
import json
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

#: The suffixes eslint reads a comment out of. `prettier --check` reads the same files.
JAVASCRIPT_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts")

#: The block form. Everything below it, to the end of the file, is exempt, so it names no line and
#: is refused whatever rules it lists. Read against the comment's text without its markers, as
#: eslint matches it, and case-sensitively: `/* ESLINT-DISABLE */` was measured to do nothing.
ESLINT_BLOCK_DISABLE = re.compile(r"^\s*eslint-disable(?![\w-])(?P<rest>.*)$", re.DOTALL)

#: The two line-scoped forms. Legal only when they name a rule.
ESLINT_LINE_DISABLE = re.compile(r"^\s*eslint-disable-(?:next-)?line(?![\w-])(?P<rest>.*)$", re.DOTALL)

#: An inline config comment, `/* eslint <rule>: <value> */`, which reconfigures a rule to the end of
#: the file. It never counts as a suppression: the value is what eslint applies, severity and
#: options both, so the pin reads the whole of it.
ESLINT_INLINE_CONFIG = re.compile(r"^\s*eslint\s+(?P<rule>[^:]+):(?P<severity>.*)$", re.DOTALL)

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

#: Files the javascript scan must read: two the api job lints, the config file eslint loads, and the
#: one `.ts` the lint script's paths leave out - a directive in a file lint does not read yet is
#: still this class.
PINNED_JAVASCRIPT_FILES = (
    "apps/api/src/app.ts",
    "eslint.config.js",
    "packages/shared/src/index.ts",
    "scripts/e2e-threads.ts",
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


def javascript_comments(source):
    """`(line, text, block)` for every real comment in a JS or TS source.

    A `//` inside a string, a template literal or a regular expression is not a comment, and the
    same text inside one cannot pass for a directive.
    """
    found = []
    scan_javascript(source, 0, False, found)
    return [(source.count("\n", 0, offset) + 1, text, block) for offset, text, block in found]


def eslint_rules(rest):
    """The rule names a line-scoped directive covers, the ones before any `-- why`."""
    body = rest.split("--", 1)[0].strip()
    rules = []
    for token in (part for part in re.split(r"[,\s]+", body) if part):
        if not RULE_NAME.match(token):
            break
        rules.append(token)
    return rules


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


def javascript_suppression_problems(root, paths):
    """Every eslint or prettier directive in a JS/TS file that exempts more than it names."""
    problems = []
    for rel in paths:
        for line, comment, block in javascript_comments(text(os.path.join(root, rel))):
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
                rule = config.group("rule").strip()
                fault = inline_config_fault(config.group("severity"))
                if fault == "options":
                    problems.append(
                        f"{where} reconfigures `{rule}` rather than raising it: the value carries "
                        "options after the severity, and eslint honours them with the severity still "
                        'at `error` - measured, `["error", {"varsIgnorePattern": ".*"}]`, '
                        '`["error", {"args": "none"}]` and `["error", "smart"]` each left a real '
                        "error at rc 0. The pin cannot read an options object as safe, so put the "
                        "options in `eslint.config.js`, where this scan reads them, and keep the "
                        "inline comment to `<rule>: error` (#196)"
                    )
                elif fault == "below":
                    problems.append(
                        f"{where} turns `{rule}` below error: eslint exits 0 on a warning, so "
                        '`/* eslint <rule>: 0` / `1` / `"off"` / `"warn"` disarms the check as surely '
                        "as removing the rule - an inline config may only raise a rule to error (#196)"
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
        js_comments = sum(len(javascript_comments(text(os.path.join(ROOT, rel)))) for rel in javascript)
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


class LinterBitesTest(unittest.TestCase):
    """#196: the real eslint and prettier, both directions, and the pin's verdict on the same file.

    Skipped where `bun install --frozen-lockfile` has not run - the `test` job has no `node_modules`,
    so this is the arm the suite cannot guarantee on its own. It is the control the pure scan rests
    on: the reader is only worth its fixtures while the linter still honours the forms it refuses.
    """

    DEFECT = "const neverUsed = 2;\nexport const value = 1;\n"

    def setUp(self):
        if shutil.which("node") is None:
            raise unittest.SkipTest("no node on PATH")
        if not os.path.isdir(os.path.join(ROOT, "node_modules", "eslint")):
            raise unittest.SkipTest("eslint is not installed here: run bun install --frozen-lockfile")
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

    def test_one_prettier_ignore_takes_the_api_step_from_rc_1_to_rc_0(self):
        """The formatter half of the same job, the same way."""
        self.write("export const  misformatted   =    1;\n")
        self.assertEqual(self.prettier(), 1, "the planted formatting no longer fails prettier")

        self.write("// prettier-ignore\nexport const  misformatted   =    1;\n")
        self.assertEqual(self.prettier(), 0, "one comment no longer disarms prettier: the arm moved")
        problems = javascript_suppression_problems(self.root, ["probe.ts"])
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("probe.ts:1", problems[0])
