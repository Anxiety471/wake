"""Guard for `docs/PUBLISH-CHECKLIST.md`: the release line must name the tag's own commit.

The checklist names the commit a release tag points at. That sentence is the one an operator
reads before publishing, and nothing checked it: after the public history rewrite on
2026-11-15 the `v0.2.0` tag moved to 75efb6d and the file still said 2375c80, a commit that is
no longer on `main`. A stale id there is a published fact that is wrong.

The live comparison needs the tag, which a shallow clone does not have; when it cannot run the
test skips with the reason printed, and the pure-function tests below still bite: the stale id
above is kept as the fixture that proves it does.
"""

import os
import re
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHECKLIST = os.path.join(ROOT, "docs", "PUBLISH-CHECKLIST.md")
CONFIG = os.path.join(ROOT, "bin", "wake_config.py")

#: "`v0.2.0` is on `main` at `75efb6d`" - a backticked release tag followed by the commit id
#: it claims, on one line. The sentence can be reworded; the shape (tag, then id) cannot.
CLAIM = re.compile(r"`v(\d+\.\d+\.\d+)`[^\n]*?\bat\b[^\n]*?`([0-9a-f]{7,40})`")

#: The tag the checklist must pin: the version this tree ships.
VERSION = re.compile(r'^VERSION\s*=\s*"(\d+\.\d+\.\d+)"', re.M)

#: The id the checklist named before the history rewrite. Kept as a fixture: it is not on
#: `main` any more, and the guard below must reject it.
STALE_ID = "2375c80"


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def claims(text):
    """Every (tag, id) pair the checklist pins, tag with its `v` prefix."""
    return [(f"v{tag}", sha) for tag, sha in CLAIM.findall(text)]


def stale_claims(text, resolve):
    """Problems with the ids the checklist claims; `resolve(tag)` gives the tag's commit.

    A tag that does not resolve is a problem too: the line names a release that this clone
    does not have, which is either a typo or a tag that was deleted.
    """
    problems = []
    for tag, named in claims(text):
        full = resolve(tag)
        if full is None:
            problems.append(f"{tag} is named but is not a tag in this clone")
        elif not full.startswith(named):
            problems.append(f"the checklist says {tag} is at {named}, but the tag is at {full[:12]}")
    return problems


def version():
    match = VERSION.search(read(CONFIG))
    assert match is not None, "bin/wake_config.py no longer carries a VERSION line"
    return match.group(1)


def git(*args):
    return subprocess.run(["git", "-C", ROOT, *args], capture_output=True, text=True)


def resolve_tag(tag):
    result = git("rev-parse", "--verify", "--quiet", f"refs/tags/{tag}^{{commit}}")
    return result.stdout.strip() if result.returncode == 0 else None


class ClaimParseTest(unittest.TestCase):
    def test_the_checklist_pins_the_version_this_tree_ships(self):
        # Deleting the id, or renaming the line so the pattern no longer sees it, fails here.
        expected = f"v{version()}"
        self.assertIn(expected, [tag for tag, _ in claims(read(CHECKLIST))], "the release line is gone")

    def test_the_parser_reads_a_claim_and_ignores_a_bare_tag(self):
        text = "`v9.9.9` is on `main` at `abcdef0`, and `v8.8.8` is mentioned alone"
        self.assertEqual(claims(text), [("v9.9.9", "abcdef0")])


class StaleClaimTest(unittest.TestCase):
    def test_the_pre_rewrite_id_is_rejected(self):
        # The exact bug: the tag moved and the checklist did not. The resolver is the tag's
        # real commit here, so this fails for the reason the file was wrong and not for luck.
        real = resolve_tag(f"v{version()}")
        full = real if real is not None else "75efb6d" + "0" * 33
        text = CLAIM.sub(lambda m: m.group(0).replace(m.group(2), STALE_ID), read(CHECKLIST), count=1)
        problems = stale_claims(text, lambda tag: full)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn(STALE_ID, problems[0])
        self.assertIn(full[:12], problems[0])

    def test_a_matching_id_is_accepted(self):
        full = "75efb6d" + "0" * 33
        self.assertEqual(stale_claims("`v0.2.0` is on `main` at `75efb6d`.", lambda tag: full), [])

    def test_a_tag_this_clone_does_not_have_is_a_problem(self):
        problems = stale_claims("`v9.9.9` is on `main` at `abcdef0`.", lambda tag: None)
        self.assertEqual(problems, ["v9.9.9 is named but is not a tag in this clone"])


class LiveTagTest(unittest.TestCase):
    def test_every_claimed_id_is_the_tags_own_commit(self):
        if not os.path.isdir(os.path.join(ROOT, ".git")):
            self.skipTest("no .git here, so the tags cannot be read")
        shallow = git("rev-parse", "--is-shallow-repository")
        if shallow.returncode != 0 or shallow.stdout.strip() == "true":
            self.skipTest("shallow clone: tags are not fetched, so the claim cannot be checked here")
        if not git("tag").stdout.strip():
            self.skipTest("this clone has no tags")
        self.assertEqual(stale_claims(read(CHECKLIST), resolve_tag), [])


if __name__ == "__main__":
    unittest.main()
