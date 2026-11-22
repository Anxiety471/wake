"""Run one test module and refuse a skip, so a silent `OK (skipped=1)` cannot pass CI.

`api (bun)` runs `tests.test_inline_suppressions` because it is the one job with `node_modules`
installed: `LinterBitesTest` proves that one `eslint-disable` line takes `eslint apps` from rc 1 to
rc 0, and a `prettier-ignore` the formatter half of the same job. `WAKE_REQUIRE_LINTERS=1` turns
the one skip the module knows about - no `node`, no `node_modules/eslint` - into a failure, but a
skip from anywhere else still exits 0: a `@unittest.skipIf` on an arm, a version guard,
`self.skipTest` inside a test. All three leave `OK (skipped=1)` and a green
`api (bun)`, which is the silent green [#216] was filed about, one level up from the skip the
module can see.

So the step runs this instead of `python3 -m unittest`, and `tests/test_ops_scripts.py` pins the
`run` body that names it. Exit codes: 0 every test ran and passed, 1 a failure, an error, a skip,
an expected failure, or a module that loaded no test at all, 2 usage. The last one matters: a
mistyped name loads an empty suite, which `unittest` reports as success.

Two shapes it cannot catch, so the guarantee above is the one it keeps: a `load_tests()` that
drops the arm which would fail, and a test that calls `os._exit(0)` before the verdict is
printed. Neither is reachable by counting what came back; both are a `tests/` edit, not a runner
one.

[#216]: https://github.com/Anxiety471/wake/issues/216
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def main(argv):
    if len(argv) != 2:
        print("usage: run_module_strict.py <module>  (from the repo root)", file=sys.stderr)
        return 2
    # The test modules import each other by plain name, as `tests/run.sh` (discover -s tests)
    # does; the caller's directory is searched too, so a fixture module beside a test works.
    for entry in (".", ROOT, HERE):
        if entry not in sys.path:
            sys.path.insert(0, entry)
    suite = unittest.defaultTestLoader.loadTestsFromName(argv[1])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.skipped:
        print(
            f"refused: {len(result.skipped)} test(s) skipped, and a skip is not a pass where this step runs (#216)",
            file=sys.stderr,
        )
        for test, reason in result.skipped:
            print(f"  {test}: {reason}", file=sys.stderr)
        return 1
    if result.expectedFailures:
        print(
            f"refused: {len(result.expectedFailures)} test(s) marked an expected failure, and a "
            "test that did not pass is not a pass where this step runs (#234)",
            file=sys.stderr,
        )
        for test, _ in result.expectedFailures:
            print(f"  {test}", file=sys.stderr)
        return 1
    if not result.testsRun:
        print(f"refused: {argv[1]!r} loaded no test, so there was nothing to pass", file=sys.stderr)
        return 1
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
