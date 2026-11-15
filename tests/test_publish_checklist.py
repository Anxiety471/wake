"""Guard for `docs/PUBLISH-CHECKLIST.md`: every release line must name its tag's own commit.

The checklist names the commit a release tag points at. That sentence is the one an operator
reads before publishing, and nothing checked it: after the public history rewrite on
2026-11-15 the `v0.2.0` tag moved to 75efb6d and the file still said 2375c80, a commit that is
no longer on `main`. A stale id there is a published fact that is wrong.

The release commit is the one case where the tag cannot be checked yet: the branch bumps
`VERSION` and names its own tag, and the tag is cut from the merge commit, which does not exist
while the file is being written. A claim naming exactly `v<VERSION>` is therefore allowed to
have no tag in this clone - and its id goes unchecked until the tag exists, which is how the id
is corrected after the cut. Every other claim keeps its teeth: a line for an older release that
no longer resolves is a typo or a deleted tag, and it fails.

The live comparison needs the tag, which a shallow clone does not have; when it cannot run the
test skips with the reason printed, and the pure-function tests below still bite: the stale id
above is kept as the fixture that proves it does.
"""

import os
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHECKLIST = os.path.join(ROOT, "docs", "PUBLISH-CHECKLIST.md")
CONFIG = os.path.join(ROOT, "bin", "wake_config.py")

#: "`v0.2.0` is on `main` at `75efb6d`" - a backticked release tag followed by the commit id
#: it claims, on one line. The sentence can be reworded; the shape (tag, then id) cannot.
CLAIM = re.compile(r"`v(\d+\.\d+\.\d+)`[^\n]*?\bat\b[^\n]*?`([0-9a-f]{7,40})`")

#: The version this tree ships, read from the marker the CLI, the MCP bridge and the tag use.
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


def claim_problems(text, version, resolve):
    """Problems with the ids the checklist claims; `resolve(tag)` gives the tag's commit.

    `version` is the tree's own version, the one `VERSION` names. A claim for exactly
    `v<version>` may name no tag: that is the release commit, whose tag is cut from the merge
    commit afterwards. Every other unresolved tag is a problem, and a tag that resolves must be
    the tag's own commit.
    """
    release = f"v{version}"
    problems = []
    for tag, named in claims(text):
        full = resolve(tag)
        if full is None:
            if tag != release:
                problems.append(f"{tag} is named but is not a tag in this clone")
        elif not full.startswith(named):
            problems.append(f"the checklist says {tag} is at {named}, but the tag is at {full[:12]}")
    return problems


def version():
    match = VERSION.search(read(CONFIG))
    assert match is not None, "bin/wake_config.py no longer carries a VERSION line"
    return match.group(1)


def git(*args, root=ROOT):
    return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True)


def resolve_tag(tag, root=ROOT):
    result = git("rev-parse", "--verify", "--quiet", f"refs/tags/{tag}^{{commit}}", root=root)
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
        problems = claim_problems(text, version(), lambda tag: full)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn(STALE_ID, problems[0])
        self.assertIn(full[:12], problems[0])

    def test_a_matching_id_is_accepted(self):
        full = "75efb6d" + "0" * 33
        self.assertEqual(claim_problems("`v0.2.0` is on `main` at `75efb6d`.", "0.2.0", lambda tag: full), [])

    def test_a_tag_this_clone_does_not_have_is_a_problem(self):
        problems = claim_problems("`v9.9.9` is on `main` at `abcdef0`.", "0.2.0", lambda tag: None)
        self.assertEqual(problems, ["v9.9.9 is named but is not a tag in this clone"])


class ReleaseCommitTest(unittest.TestCase):
    """The one claim that may name a tag this clone does not have: the release line itself."""

    def test_the_release_line_may_name_its_own_tag_before_the_tag_exists(self):
        # VERSION 0.3.0 and a `v0.3.0` line, no tag: the release commit, which could never be
        # green before this, so nobody could cut a release without a red suite.
        self.assertEqual(claim_problems("`v0.3.0` is on `main` at `1a2b3c4`.", "0.3.0", lambda tag: None), [])

    def test_a_line_for_another_version_that_does_not_resolve_is_a_problem(self):
        # A typo in the release line, not the release commit: v0.3.1 while this tree is 0.3.0.
        problems = claim_problems("`v0.3.1` is on `main` at `1a2b3c4`.", "0.3.0", lambda tag: None)
        self.assertEqual(problems, ["v0.3.1 is named but is not a tag in this clone"])

    def test_an_older_release_line_that_no_longer_resolves_is_still_a_problem(self):
        # The exemption is the release line, not "a claim that does not resolve".
        text = "`v0.3.0` is on `main` at `1a2b3c4`.\n`v0.1.0` is on `main` at `9f9f9f9`."
        problems = claim_problems(text, "0.3.0", lambda tag: None)
        self.assertEqual(problems, ["v0.1.0 is named but is not a tag in this clone"])

    def test_the_release_line_is_checked_once_its_tag_exists(self):
        # After the cut the id is checked like any other, which is when it gets corrected.
        full = "1a2b3c4" + "0" * 33
        problems = claim_problems("`v0.3.0` is on `main` at `fffffff`.", "0.3.0", lambda tag: full)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("fffffff", problems[0])
        self.assertIn(full[:12], problems[0])


class ReleaseSimulationTest(unittest.TestCase):
    """A real clone with real tags: prepare 0.3.0, cut the tag, then stale the line.

    The pure-function tests above take a resolver; this one runs `git rev-parse` in a
    repository built for the occasion, so the release path is proved against git and not
    against a lambda's idea of what a tag is.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="release-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.repo = os.path.join(self.dir, "clone")
        os.makedirs(self.repo)
        self.git("init", "--quiet")
        self.write("VERSION", 'VERSION = "0.2.0"\n')
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "0.2.0")
        self.git("tag", "v0.2.0")
        self.tagged = self.git("rev-parse", "HEAD").stdout.strip()
        # A second commit, so "the tag's commit" and "the newest commit" are different ids:
        # with one commit a wrong-id test would pass by coincidence.
        self.write("README.md", "a tree with history\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "documentation")
        self.head = self.git("rev-parse", "HEAD").stdout.strip()

    def git(self, *args):
        result = subprocess.run(
            [
                "git",
                "-C",
                self.repo,
                "-c",
                "user.name=Release Simulation",
                "-c",
                "user.email=simulation@example.invalid",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "tag.gpgsign=false",
                *args,
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            self.fail(f"git {args[0]} failed: {(result.stderr or result.stdout).strip()}")
        return result

    def write(self, name, body):
        with open(os.path.join(self.repo, name), "w", encoding="utf-8") as handle:
            handle.write(body)

    def claim(self, tag, sha):
        return f"`{tag}` is on `main` at `{sha}`.\n"

    def resolve(self, tag):
        return resolve_tag(tag, root=self.repo)

    def test_the_release_commit_is_green_before_its_tag_exists(self):
        self.write("VERSION", 'VERSION = "0.3.0"\n')
        self.assertEqual(self.resolve("v0.3.0"), None, "the tag must not exist on the release commit")
        self.assertEqual(claim_problems(self.claim("v0.3.0", self.head), "0.3.0", self.resolve), [])

    def test_the_release_line_is_green_once_the_tag_is_cut(self):
        self.write("VERSION", 'VERSION = "0.3.0"\n')
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "0.3.0")
        self.git("tag", "v0.3.0")
        sha = self.git("rev-parse", "HEAD").stdout.strip()
        self.assertEqual(claim_problems(self.claim("v0.3.0", sha), "0.3.0", self.resolve), [])

    def test_a_release_line_the_tag_disagrees_with_is_red(self):
        # The tag is cut on the previous commit: an id that was right before the rebase and is
        # wrong after it is exactly the failure this guard exists for.
        self.git("tag", "v0.3.0", self.tagged)
        problems = claim_problems(self.claim("v0.3.0", self.head), "0.3.0", self.resolve)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn(self.tagged[:12], problems[0])

    def test_a_stale_older_line_is_red_while_the_release_line_is_not(self):
        text = self.claim("v0.3.0", "1a2b3c4") + self.claim("v0.1.0", "9f9f9f9")
        problems = claim_problems(text, "0.3.0", self.resolve)
        self.assertEqual(problems, ["v0.1.0 is named but is not a tag in this clone"])


class LiveTagTest(unittest.TestCase):
    def test_every_claimed_id_is_the_tags_own_commit(self):
        if not os.path.isdir(os.path.join(ROOT, ".git")):
            self.skipTest("no .git here, so the tags cannot be read")
        shallow = git("rev-parse", "--is-shallow-repository")
        if shallow.returncode != 0 or shallow.stdout.strip() == "true":
            self.skipTest("shallow clone: tags are not fetched, so the claim cannot be checked here")
        if not git("tag").stdout.strip():
            self.skipTest("this clone has no tags")
        self.assertEqual(claim_problems(read(CHECKLIST), version(), resolve_tag), [])


if __name__ == "__main__":
    unittest.main()
