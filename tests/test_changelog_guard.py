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
RELEASED = "- A released note that must not vanish (#185)."

BASE = f"""# Changelog

## [Unreleased]

### Added

{UNRELEASED}

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


class LiveRepositoryTestCase(unittest.TestCase):
    """The guard against this repository's own files, so the rule is not only a fixture's."""

    def test_this_changelog_is_clean_against_its_own_head(self):
        result = subprocess.run([GUARD, "HEAD"], cwd=ROOT, env=clean_env(), text=True, capture_output=True)
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
