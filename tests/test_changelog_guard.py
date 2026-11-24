"""Tests for scripts/changelog-guard.sh: a released note may not vanish with every check green.

The hazard is structural (#199): a branch cut before a note merged carries a copy of `CHANGELOG.md`
without it, so a three-way merge resolved by taking the branch deletes the note, and nothing read
the file. Each test builds a throwaway repository holding the real script, commits a base revision,
then changes the file the way such a pull request would.

Run with the rest: python3 -m unittest discover -s tests
"""

import os
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GUARD = os.path.join(ROOT, "scripts", "changelog-guard.sh")
ALLOW = os.path.join(ROOT, "scripts", "changelog-allow.txt")
CHANGELOG = os.path.join(ROOT, "CHANGELOG.md")
CI = os.path.join(ROOT, ".github", "workflows", "ci.yml")

UNRELEASED = "- An unreleased note that a change is rewording (#900)."
FIXED_NOTE = "- An unreleased fix under its own heading (#901)."
RELEASED = "- A released note that must not vanish (#185)."

BASE = f"""# Changelog

## [Unreleased]

### Added

{UNRELEASED}

### Fixed

{FIXED_NOTE}

## [0.2.0] - 2026-11-15

### Fixed

{RELEASED}
"""

#: A commit needs an identity, and the fixture must not borrow the runner's.
GIT_ID = {
    "GIT_AUTHOR_NAME": "wake test",
    "GIT_AUTHOR_EMAIL": "bot@example.com",
    "GIT_COMMITTER_NAME": "wake test",
    "GIT_COMMITTER_EMAIL": "bot@example.com",
}


def clean_env(**extra):
    """The environment for a child, as tests/run.sh builds it: proxies dropped, identity fixed."""
    env = {key: value for key, value in os.environ.items() if "proxy" not in key.lower()}
    env.update(GIT_ID)
    env.update(extra)
    return env


def text(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def git_out(*args):
    """Run git in this checkout and return the finished process, never raising on a non-zero exit."""
    return subprocess.run(
        ["git"] + list(args),
        cwd=ROOT,
        env=clean_env(),
        text=True,
        capture_output=True,
    )


def live_base():
    """The base a pull request's guard run compares: `git merge-base origin/main HEAD`, or None.

    None means this checkout cannot answer - a shallow clone without origin/main, or a repository with
    no such branch - and the caller skips rather than comparing something else.
    """
    main = git_out("rev-parse", "--verify", "--quiet", "origin/main^{commit}")
    if main.returncode != 0:
        return None
    head = git_out("rev-parse", "--verify", "--quiet", "HEAD^{commit}")
    if head.returncode != 0:
        return None
    merge = git_out("merge-base", main.stdout.strip(), head.stdout.strip())
    if merge.returncode != 0:
        return None
    return merge.stdout.strip()


def unreleased_bullet(document):
    """The first bullet under `## [Unreleased]`, the note a stale branch is most likely to drop."""
    inside = False
    for line in document.splitlines():
        if line.startswith("## "):
            inside = line.startswith("## [Unreleased]")
            continue
        if inside and line.startswith("- "):
            return line
    raise AssertionError("the changelog has no bullet under ## [Unreleased]")


class ChangelogGuardTestCase(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix="changelog-guard-")
        self.addCleanup(shutil.rmtree, self.repo, True)
        os.makedirs(os.path.join(self.repo, "scripts"))
        self.guard = os.path.join(self.repo, "scripts", "changelog-guard.sh")
        shutil.copy(GUARD, self.guard)
        os.chmod(self.guard, 0o755)
        self.git("init", "-q")
        self.write_allow("# nothing listed yet\n")
        self.write("CHANGELOG.md", BASE)
        self.base = self.commit("base revision")

    def write(self, name, body):
        path = os.path.join(self.repo, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(body)
        return path

    def write_allow(self, body):
        return self.write(os.path.join("scripts", "changelog-allow.txt"), body)

    def git(self, *args, check=True):
        return subprocess.run(
            ["git"] + list(args),
            cwd=self.repo,
            env=clean_env(),
            text=True,
            capture_output=True,
            check=check,
        )

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD").stdout.strip()

    def run_guard(self, *args, allow=None):
        env = clean_env()
        if allow is not None:
            env["WAKE_CHANGELOG_ALLOW"] = allow
        return subprocess.run(
            [self.guard] + list(args),
            cwd=self.repo,
            env=env,
            text=True,
            capture_output=True,
        )

    # -- what passes -------------------------------------------------------------

    def test_an_addition_only_change_is_clean(self):
        self.write("CHANGELOG.md", BASE.replace("### Added\n\n", "### Added\n\n- A new note (#901).\n"))
        result = self.run_guard(self.base)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("changelog-guard: clean", result.stdout)

    def test_reordering_and_blank_line_churn_pass(self):
        reordered = BASE.replace("\n\n### Fixed", "\n### Fixed").replace(
            f"{UNRELEASED}\n\n{RELEASED}", f"{RELEASED}\n\n{UNRELEASED}"
        )
        self.assertEqual(reordered.count(UNRELEASED), 1)
        self.assertEqual(reordered.count(RELEASED), 1)
        self.write("CHANGELOG.md", reordered)
        result = self.run_guard(self.base)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_trailing_whitespace_on_the_base_side_is_ignored(self):
        """The comparison strips trailing whitespace from both sides; the base side had no test."""
        self.write("CHANGELOG.md", BASE.replace(UNRELEASED, UNRELEASED + "   "))
        base = self.commit("base revision with trailing whitespace")
        self.write("CHANGELOG.md", BASE)
        result = self.run_guard(base)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_crlf_base_against_a_lf_head_is_clean(self):
        """A Windows checkout of the base must not read as a whole-file deletion."""
        self.write("CHANGELOG.md", BASE.replace("\n", "\r\n"))
        base = self.commit("base revision with CRLF")
        self.write("CHANGELOG.md", BASE)
        result = self.run_guard(base)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    # -- what fails --------------------------------------------------------------

    def test_a_deleted_released_note_fails(self):
        self.write("CHANGELOG.md", BASE.replace(RELEASED + "\n", ""))
        result = self.run_guard(self.base)
        self.assertEqual(result.returncode, 1)
        self.assertIn(RELEASED, result.stdout)
        self.assertIn("changelog-guard: FAILED", result.stderr)

    def test_an_unreleased_note_is_not_exempt(self):
        """The measured case: bullets merged to main sit under Unreleased, and a stale branch drops them."""
        self.write("CHANGELOG.md", BASE.replace(UNRELEASED + "\n", ""))
        result = self.run_guard(self.base)
        self.assertEqual(result.returncode, 1)
        self.assertIn(UNRELEASED, result.stdout)

    def test_a_reworded_unreleased_note_needs_the_old_wording_listed(self):
        self.write("CHANGELOG.md", BASE.replace(UNRELEASED, "- A reworded unreleased note (#900)."))
        result = self.run_guard(self.base)
        self.assertEqual(result.returncode, 1)
        self.assertIn(UNRELEASED, result.stdout)

    def test_one_of_two_identical_lines_is_still_a_deletion(self):
        doubled = BASE.replace(RELEASED + "\n", f"{RELEASED}\n{RELEASED}\n")
        self.write("CHANGELOG.md", doubled)
        base = self.commit("two copies")
        self.write("CHANGELOG.md", BASE)
        result = self.run_guard(base)
        self.assertEqual(result.returncode, 1)
        self.assertIn(RELEASED, result.stdout)

    # -- the allowlist -----------------------------------------------------------

    def test_a_listed_deletion_passes_and_is_reported(self):
        self.write("CHANGELOG.md", BASE.replace(UNRELEASED, "- A reworded unreleased note (#900)."))
        self.write_allow(f"# rewording an unreleased note before it is released\n{UNRELEASED}\n")
        result = self.run_guard(self.base)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("listed in scripts/changelog-allow.txt", result.stdout)
        self.assertIn(UNRELEASED, result.stdout)

    def test_an_entry_must_match_the_deleted_line_verbatim(self):
        self.write("CHANGELOG.md", BASE.replace(UNRELEASED + "\n", ""))
        self.write_allow(f"# a near miss must not act as a wildcard\n{UNRELEASED[:-2]}\n")
        result = self.run_guard(self.base)
        self.assertEqual(result.returncode, 1)
        self.assertIn(UNRELEASED, result.stdout)

    def test_an_entry_without_a_reason_is_refused(self):
        self.write_allow(UNRELEASED + "\n")
        result = self.run_guard(self.base)
        self.assertEqual(result.returncode, 2)
        self.assertIn("no reason comment above it", result.stderr)
        self.assertIn(UNRELEASED, result.stderr)

    def test_a_heading_line_must_be_listed_with_a_leading_backslash(self):
        """A heading is what a reflow of the file itself deletes; a bare `#` line can never be an entry."""
        merged = BASE.replace(
            "### Added\n\n" + UNRELEASED + "\n\n### Fixed\n\n" + FIXED_NOTE + "\n",
            "### Added\n\n" + UNRELEASED + "\n" + FIXED_NOTE + "\n",
        )
        self.assertNotEqual(merged, BASE)
        self.write("CHANGELOG.md", merged)
        refused = self.run_guard(self.base)
        self.assertEqual(refused.returncode, 1)
        self.assertIn("### Fixed", refused.stdout)
        # The failure says how to list it: without that, a heading is unlistable in practice.
        self.assertIn("listed with a leading backslash", refused.stdout)

        self.write_allow("# collapsing two Fixed subsections before the release\n\\### Fixed\n")
        listed = self.run_guard(self.base)
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn("1 of 1 removed line(s) listed", listed.stdout)

    def test_a_heading_without_the_backslash_is_a_comment_and_allows_nothing(self):
        merged = BASE.replace(
            "### Added\n\n" + UNRELEASED + "\n\n### Fixed\n\n" + FIXED_NOTE + "\n",
            "### Added\n\n" + UNRELEASED + "\n" + FIXED_NOTE + "\n",
        )
        self.write("CHANGELOG.md", merged)
        self.write_allow("# a bare heading is a comment, not an entry\n### Fixed\n")
        result = self.run_guard(self.base)
        self.assertEqual(result.returncode, 1)
        self.assertIn("### Fixed", result.stdout)

    def test_an_escaped_entry_still_needs_a_reason(self):
        self.write_allow("# a reason for something else\n\n\\### Fixed\n")
        result = self.run_guard(self.base)
        self.assertEqual(result.returncode, 2)
        self.assertIn("no reason comment above it", result.stderr)

    def test_an_entry_that_matches_no_removed_line_is_reported(self):
        """An entry carries its whitespace, so a wrong indent matches nothing: say so, do not ignore it."""
        self.write("CHANGELOG.md", BASE.replace(UNRELEASED + "\n", ""))
        self.write_allow(f"# looks like an entry, is not one\n  {UNRELEASED}\n")
        result = self.run_guard(self.base)
        self.assertEqual(result.returncode, 1)
        self.assertIn("match no line this base loses", result.stdout)
        self.assertIn(UNRELEASED, result.stdout)
        # The deleted line is still named as unlisted, so the failure is not hidden by the note.
        self.assertIn("CHANGELOG.md drops 1 line(s)", result.stdout)

    def test_a_wrapped_bullets_continuation_line_is_listed_with_its_indent(self):
        """Reflowing a bullet deletes its continuation lines, which start with two spaces."""
        wrapped = BASE.replace(
            UNRELEASED + "\n",
            UNRELEASED + "\n  The rest of that note, on its own line (#900).\n",
        )
        self.write("CHANGELOG.md", wrapped)
        base = self.commit("a wrapped bullet")
        self.write("CHANGELOG.md", wrapped.replace("\n  The rest of that note", " The rest of that note"))
        self.write_allow(
            f"# joining a wrapped bullet\n{UNRELEASED}\n  The rest of that note, on its own line (#900).\n"
        )
        result = self.run_guard(base)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("2 of 2 removed line(s) listed", result.stdout)
        self.assertNotIn("match no line this base loses", result.stdout)

    def test_a_listed_line_from_a_released_section_is_reported_as_released(self):
        """The allowlist is not scoped to Unreleased, so the output has to say when it covered a release."""
        self.write("CHANGELOG.md", BASE.replace(RELEASED + "\n", ""))
        self.write_allow(f"# dropping a released note on purpose\n{RELEASED}\n")
        result = self.run_guard(self.base)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("come from a released section", result.stdout)
        self.assertIn("## [0.2.0]", result.stdout)

    def test_a_listed_unreleased_line_is_not_reported_as_released(self):
        self.write("CHANGELOG.md", BASE.replace(UNRELEASED + "\n", ""))
        self.write_allow(f"# reflowing an unreleased note\n{UNRELEASED}\n")
        result = self.run_guard(self.base)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("come from a released section", result.stdout)

    def test_text_that_also_sits_under_unreleased_is_not_called_released(self):
        """A heading appears once per section; the line compared by content cannot say which copy went."""
        merged = BASE.replace(
            "### Added\n\n" + UNRELEASED + "\n\n### Fixed\n\n" + FIXED_NOTE + "\n",
            "### Added\n\n" + UNRELEASED + "\n" + FIXED_NOTE + "\n",
        )
        self.write("CHANGELOG.md", merged)
        self.write_allow("# collapsing two Fixed subsections before the release\n\\### Fixed\n")
        result = self.run_guard(self.base)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("come from a released section", result.stdout)

    def test_a_missing_allowlist_is_refused(self):
        result = self.run_guard(self.base, allow=os.path.join(self.repo, "scripts", "absent.txt"))
        self.assertEqual(result.returncode, 2)
        self.assertIn("no allowlist", result.stderr)

    # -- the arguments and the revision compared ---------------------------------

    def test_a_second_revision_is_compared_instead_of_the_working_tree(self):
        self.write("CHANGELOG.md", BASE.replace(RELEASED + "\n", ""))
        head = self.commit("drop the note")
        self.write("CHANGELOG.md", BASE)  # the working tree no longer has the deletion
        same_tree = self.run_guard(self.base)
        self.assertEqual(same_tree.returncode, 0, same_tree.stderr)
        committed = self.run_guard(self.base, head)
        self.assertEqual(committed.returncode, 1)
        self.assertIn(RELEASED, committed.stdout)

    def test_an_unknown_base_is_a_usage_error(self):
        result = self.run_guard("no-such-revision")
        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown base revision", result.stderr)

    def test_the_base_is_required(self):
        result = self.run_guard()
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage:", result.stderr)

    def test_a_base_without_a_changelog_has_nothing_to_lose(self):
        self.git("rm", "-q", "CHANGELOG.md")
        base = self.commit("a base with no changelog")
        self.write("CHANGELOG.md", BASE)
        result = self.run_guard(base)
        self.assertEqual(result.returncode, 0, result.stderr)

    # -- an allowlist entry that outlives its change (#321) ---------------------

    def commit_base_with_allow(self, allow_body, message):
        self.write_allow(allow_body)
        return self.commit(message)

    def test_an_entry_the_base_carries_whose_line_it_already_lost_fails(self):
        """The #212/#321 case: the base lists the entry and no longer carries the line it allowed to go.

        The fixture has to delete the line in the base, because that is what a merged permission looks
        like. An entry whose line the base still carries is a different thing - see the next test (#350).
        """
        self.write("CHANGELOG.md", BASE.replace(UNRELEASED + "\n\n", ""))
        base = self.commit_base_with_allow(
            f"# inherited permission\n{UNRELEASED}\n", "base carries an entry no line needs"
        )
        result = self.run_guard(base)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("inherited from", result.stdout)
        self.assertIn(UNRELEASED, result.stdout)
        self.assertIn("FAILED", result.stderr)

    def test_an_inherited_permission_whose_line_the_base_still_carries_is_only_reported(self):
        """#350: the base carries the entry and still carries the line, and the change touches nothing.

        This is the shape that was red: the entry is a permission for a deletion that has not happened
        yet, so it cannot be told apart from a live one and must not fail an unrelated change.
        """
        base = self.commit_base_with_allow(
            f"# pending permission\n{UNRELEASED}\n", "base carries a pending entry"
        )
        result = self.run_guard(base)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("inherited from", result.stdout)
        self.assertIn("has not made yet", result.stdout)
        self.assertNotIn("FAILED", result.stderr)

    def test_a_standing_permission_does_not_red_the_next_unrelated_change(self):
        """The measured #350 shape: commit the entry, then one unrelated commit on top of it."""
        base = self.commit_base_with_allow(
            f"# standing permission\n{UNRELEASED}\n", "commit the permission"
        )
        self.write("README.md", "an unrelated change\n")
        head = self.commit("an unrelated change on top")
        result = self.run_guard(base, head)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("FAILED", result.stderr)

    def test_an_entry_this_change_adds_and_no_line_needs_is_only_reported(self):
        """The entry is new in the pull request, so the base does not carry it yet: a NOTE, not a failure."""
        self.write_allow(f"# new permission\n{UNRELEASED}\n")
        result = self.run_guard(self.base)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("match no line this base loses", result.stdout)
        self.assertNotIn("inherited from", result.stdout)

    def test_an_inherited_entry_that_still_matches_a_deletion_passes(self):
        """A base entry that does its job in this change is not dead: the deletion is listed and clean."""
        base = self.commit_base_with_allow(f"# reflow permission\n{UNRELEASED}\n", "base carries a live entry")
        self.write("CHANGELOG.md", BASE.replace(UNRELEASED, "- A reworded note (#900)."))
        result = self.run_guard(base)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("1 of 1 removed line(s) listed", result.stdout)
        self.assertNotIn("inherited from", result.stdout)

    def test_an_entry_committed_on_top_of_its_base_is_only_reported(self):
        """#350: the committed-permission shape. The base has no entry, the head commits one: a NOTE, not a failure."""
        self.write_allow(f"# new permission\n{UNRELEASED}\n")
        head = self.commit("commit an entry on top of the base")
        result = self.run_guard(self.base, head)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("match no line this base loses", result.stdout)
        self.assertNotIn("inherited from", result.stdout)

    def test_a_base_that_predates_the_allowlist_has_no_inherited_entries(self):
        """Graceful skip: a base without scripts/changelog-allow.txt carries nothing to be dead."""
        self.git("rm", "-q", os.path.join("scripts", "changelog-allow.txt"))
        base = self.commit("base predates the allowlist")
        self.write_allow(f"# new permission\n{UNRELEASED}\n")
        result = self.run_guard(base)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("inherited from", result.stdout)


class LiveRepositoryTestCase(unittest.TestCase):
    """The guard against this repository's own files, so the rule is not only a fixture's."""

    def test_this_changelog_is_clean_against_the_base_a_pull_request_uses(self):
        """#350: the base the CI step compares, not this head and not its parent.

        CI runs the guard with `${{ github.event.pull_request.base.sha }}`, and a local run has to mirror
        that or it measures a different question. `HEAD~1` was wrong in both directions: in the
        pull_request shape HEAD is the merge commit of the base tip and the branch, so HEAD~1 is the base
        tip only for a one-commit branch, and a base that carries a standing permission reds a change
        that touches nothing.
        """
        base = live_base()
        if base is None:
            self.skipTest("no origin/main to compare against")
        main = git_out("rev-parse", "origin/main").stdout.strip()
        head = git_out("rev-parse", "HEAD").stdout.strip()
        # The base choice is asserted here, where it is made: a revert to HEAD leaves the module green
        # while main's allowlist is empty, so nothing else would notice.
        self.assertEqual(base, git_out("merge-base", main, head).stdout.strip())
        if head != main:
            self.assertNotEqual(base, head, "the live guard compares this head with itself again (#350)")
        result = subprocess.run([GUARD, base, "HEAD"], cwd=ROOT, env=clean_env(), text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("changelog-guard: clean", result.stdout)

    def test_this_changelogs_unreleased_notes_are_protected(self):
        live = text(CHANGELOG)
        note = unreleased_bullet(live)
        repo = tempfile.mkdtemp(prefix="changelog-live-")
        self.addCleanup(shutil.rmtree, repo, True)
        os.makedirs(os.path.join(repo, "scripts"))
        shutil.copy(GUARD, os.path.join(repo, "scripts", "changelog-guard.sh"))
        os.chmod(os.path.join(repo, "scripts", "changelog-guard.sh"), 0o755)
        with open(os.path.join(repo, "scripts", "changelog-allow.txt"), "w", encoding="utf-8") as handle:
            handle.write("# nothing listed\n")
        changelog = os.path.join(repo, "CHANGELOG.md")
        with open(changelog, "w", encoding="utf-8") as handle:
            handle.write(live)
        subprocess.run(["git", "init", "-q"], cwd=repo, env=clean_env(), check=True)
        subprocess.run(["git", "add", "-A"], cwd=repo, env=clean_env(), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, env=clean_env(), check=True)
        with open(changelog, "w", encoding="utf-8") as handle:
            handle.write(live.replace(note + "\n", ""))
        result = subprocess.run(
            [os.path.join(repo, "scripts", "changelog-guard.sh"), "HEAD"],
            cwd=repo,
            env=clean_env(),
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(note, result.stdout)

    def test_this_allowlist_parses(self):
        """Every entry carries a reason, which the guard enforces before it compares anything."""
        result = subprocess.run([GUARD, "HEAD"], cwd=ROOT, env=clean_env(), text=True, capture_output=True)
        self.assertNotIn("no reason comment above it", result.stderr)
        self.assertTrue(os.path.isfile(ALLOW))


class CiWiringTestCase(unittest.TestCase):
    """The guard is only a gate if the lint job runs it against the pull request's base."""

    def lint_steps(self):
        lines = text(CI).splitlines()
        self.assertIn("  lint:", lines, "the lint job is gone")
        block = []
        for line in lines[lines.index("  lint:") + 1 :]:
            if line.startswith("  ") and not line.startswith("   ") and line.strip():
                break
            block.append(line)
        steps = []
        current = None
        for line in block:
            if line.startswith("      - "):
                current = [line[len("      - ") :].strip()]
                steps.append(current)
            elif current is not None and line.startswith("        ") and line.strip():
                current.append(line.strip())
        return steps

    def test_the_lint_job_runs_the_guard_on_the_pull_requests_base(self):
        steps = [step for step in self.lint_steps() if step and step[0] == "name: CHANGELOG.md deletions"]
        self.assertEqual(len(steps), 1, f"the changelog step is {steps}")
        step = steps[0]
        self.assertIn("BASE_SHA: ${{ github.event.pull_request.base.sha }}", step)
        self.assertIn('git fetch --no-tags --depth=1 origin "$BASE_SHA"', step)
        self.assertIn('scripts/changelog-guard.sh "$BASE_SHA"', step)
        # A push has no pull request base: the step says so and passes, so the guard never runs
        # against an empty revision and reports a green build it did not earn.
        self.assertIn('if [ -z "$BASE_SHA" ]; then', step)
        self.assertIn('echo "changelog-guard: not a pull request; nothing to compare"', step)

    def test_shellcheck_covers_the_new_script(self):
        self.assertRegex(
            text(CI),
            re.compile(r"shellcheck .*scripts/changelog-guard\.sh"),
            "the guard is shell and shellcheck's explicit list does not reach it",
        )


if __name__ == "__main__":
    unittest.main()
