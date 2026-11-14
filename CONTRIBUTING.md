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
shellcheck install.sh tests/run.sh scripts/scrub-check.sh
scripts/scrub-check.sh --history           # no IPs, emails or tokens in files or commits
```

CI tests Python 3.9 to 3.13 on Ubuntu and pins ruff in `.github/workflows/ci.yml`. Ruff settings are in `pyproject.toml`.

## Rules

- Keep the standard-library-only rule and Python 3.9 compatibility.
- Every value that goes into a shell command (on the host or through `WAKE_HOSTOPS`) must be validated or passed through `shlex.quote`.
- Tests are offline. Use the fake webhook server and temporary directories in `tests/test_wake.py`; never touch the real `~/.config/wake`, `/tmp` rate log or a real host.
- Add or update a test for every behaviour change.
- Never commit real keys, hostnames, IP addresses, email addresses or bot ids. Use the names in `bots.example.tsv`. If a fake value trips `scripts/scrub-check.sh`, add the exact string to `scripts/scrub-allow.txt`.
- Update `README.md`, `docs/` and `CHANGELOG.md` (under an "Unreleased" heading) for user-visible changes.
- One logical change per commit, with a message that says what and why.

## Releases

Bump `VERSION` in `bin/wake_config.py`, move the "Unreleased" changelog entries under the new version, and tag `v<version>`.
