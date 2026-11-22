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

CI tests Python 3.9 to 3.13 on Ubuntu and pins ruff in `.github/workflows/ci.yml`. Ruff settings are in `pyproject.toml`.

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
  above it, or the `lint` job fails; reflowing an unreleased bullet is that case. A line that starts
  with `#` is a heading, so write it with a leading backslash (`\### Fixed`): an unescaped `#` line
  is a comment. An entry is compared with its own indentation, so copy the line as it stands - a
  wrapped bullet's continuation lines start with two spaces - and an entry that matches no removed
  line is printed back. Remove the
  entry once the change has merged: an entry is a standing permission, not a note about one pull
  request. The guard is not scoped to Unreleased - it accepts an entry for a released line too and
  prints the released heading it came from, because dropping a released note is the owner's decision
  in the pull request, not a reflow.
- One logical change per commit, with a message that says what and why.
- Keep it civil: [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) applies to issues, pull requests
  and reviews.

## Releases

Bump `VERSION` in `bin/wake_config.py`, move the "Unreleased" changelog entries under the new version, and tag `v<version>`.
