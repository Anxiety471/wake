# Contributing

Thanks for helping. wake is small on purpose: Python 3.9+ standard library only, no daemon, no dependencies.

## Before you start

For anything bigger than a small fix, open an issue first so we can agree on the approach.

## Checks

Run these before opening a pull request. CI runs the same ones.

```sh
tests/run.sh                               # offline test suite; run as a non-root user
PYTHON=python3.9 tests/run.sh              # oldest supported Python, if you have it
uv run --with ruff ruff check .            # lint (or: ruff check .)
uv run --with ruff ruff format --check .   # formatting; `ruff format .` fixes it
shellcheck install.sh tests/run.sh scripts/scrub-check.sh scripts/changelog-guard.sh
scripts/scrub-check.sh --history           # no IPs, emails or tokens in files or commits
```

CI tests Python 3.9 to 3.13 on Ubuntu and pins ruff in `.github/workflows/ci.yml`. Ruff settings are in `pyproject.toml`. The inline-suppression pin also runs in `api (bun)`, the one job with `node_modules` installed, through `python3 tests/run_module_strict.py test_inline_suppressions`, which refuses any skip: a `@unittest.skipIf` on the eslint or prettier arm fails the step instead of restoring the silent green ([#216](https://github.com/Anxiety471/wake/issues/216)).

## Rules

- Keep the standard-library-only rule and Python 3.9 compatibility.
- Every value that goes into a shell command (on the host or through `WAKE_HOSTOPS`) must be validated or passed through `shlex.quote`.
- Tests are offline. Use the fake webhook server and temporary directories in `tests/test_wake.py`; never touch the real `~/.config/wake`, `/tmp` rate log or a real host.
- Add or update a test for every behaviour change.
- A change to a command wake sends is a gate change: update `PINNED` in
  `tests/test_gate_pins.py` in the same commit, and stage the gate route before the client
  ships (gate first, client second, [#89](https://github.com/Anxiety471/wake/issues/89)).
- Never commit real keys, hostnames, IP addresses, email addresses or bot ids. Use the names in `bots.example.tsv`. If a fake value trips `scripts/scrub-check.sh`, add the exact string to `scripts/scrub-allow.txt`.
  A test address on an RFC 2606/6761 reserved name (`example.com`, `example.net`, `example.org` and
  their subdomains, `*.invalid`, `*.test`, `*.example`, `*.localhost`) needs no entry: the email
  stage ignores it, because no mailbox can exist there, and any other address still fails.
- Update `README.md`, `docs/` and `CHANGELOG.md` (under an "Unreleased" heading) for user-visible changes.
- A pull request may add `CHANGELOG.md` lines freely, and reorder them. If it deletes a line its
  base branch still has, list that exact line in `scripts/changelog-allow.txt` with a short reason
  above it, or the `lint` job fails; reflowing an unreleased bullet is that case. The guard runs in
  `lint` on a pull request, against its base, and on a push to `main`, against the tip that ref had
  before the push, so the merge commit that puts a deletion on `main` is judged even when the pull
  request's own checks ran on an older base. A push to any other branch is not judged by itself: a
  branch is judged by its pull request, so it may reword a line it added itself. A line that starts
  with `#` is a heading, so write it with a leading backslash (`\### Fixed`): an unescaped `#` line
  is a comment. An entry is compared with its own indentation, so copy the line as it stands - a
  wrapped bullet's continuation lines start with two spaces - and an entry that matches no removed
  line is printed back. Remove the
  entry once the change has merged: an entry is a standing permission, not a note about one pull
  request. An entry the base branch already carries that matches no line the base loses is a
  permission whose deletion has not happened yet: the guard prints it as a NOTE and passes. It fails
  only when it is provably spent - the base no longer carries the line it permits to delete, so its
  change has merged (#321) - or when its reason carries `# expires: YYYY-MM-DD` and that date has
  passed (#350). Write the marker on its own comment line directly above the entry, in that exact
  form: a marker the guard cannot read (`# Expires:`, `# expires` without the colon) is refused
  rather than ignored, because an unread marker would make the entry a permission that never ends.
  A deletion that has to wait for something outside the repository gets a date, not a `# stale-when:`
  issue: staleness here is decided offline, so that marker is refused as unverifiable. The marker
  the base branch carries governs, so bumping the date in a pull request does not postpone an expiry
  that has already passed - the entry has to be removed in its own commit instead. An entry this
  pull request adds is only reported. The guard is not scoped to Unreleased - it accepts an entry for a released line too and
  prints the released heading it came from, because dropping a released note is the owner's decision
  in the pull request, not a reflow.
- One logical change per commit, with a message that says what and why.
- Keep it civil: [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) applies to issues, pull requests
  and reviews.

## Releases

Bump `VERSION` in `bin/wake_config.py`, move the "Unreleased" changelog entries under the new version, and tag `v<version>`.
