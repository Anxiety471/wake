# Changelog

All notable changes to this project are listed here. Versions follow [Semantic Versioning](https://semver.org/).
The version lives in `bin/wake_config.py` (`VERSION`) and is sent as `User-Agent: wake-cli/<version>`.

## [Unreleased]

### Added

- `WAKE_TTL_HOURS` (default `24`): a thread older than this refuses answers, and says the age, because the asker has moved on and the answer would land unread. `0` disables it.
- One round per thread: once a thread has an answer, the asker cannot answer back on the same thread, so A to B to A cannot ping-pong. Ask again with `--ask` if there is more to say. `--force` overrides it.
- An audit line per ask, reply and refusal in `$WAKE_RATE_DIR/wake-audit-<uid>.log`, so a refused or failed answer is visible instead of looking like a delivered one.

### Changed

- CI runs the test matrix on `ubuntu-24.04`, because `ubuntu-latest` (Ubuntu 26) no longer has a Python 3.9 build to install.
- `scripts/scrub-check.sh` keeps the strong secret patterns (keys, bearer tokens, private keys) on every file and commit, but the long-blob heuristic now skips URLs and the scan no longer fails on a dependency bot's own `Signed-off-by` trailer, so a documentation link or a dependabot merge cannot fail the build.

## [0.1.0] - 2026-11-14

First public release.

### Added

- `wake` CLI: one-way wakes, `--ask` / `--reply` threads, `--inbox`, `--thread`, `--setup` key creation, registry `--add` / `--sync`, `--list` / `--who` / `--info` / `--me`, `--version`.
- Helpers: `wake-run`, `wake-hook` (Claude Code and Codex hooks), `wake-mcp` (MCP stdio server), `wake-inbox-io` (session inbox plumbing).
- Configuration through environment variables or `config.env`; host mode and guest mode (`WAKE_HOSTOPS`).
- Loop guard (depth limit and 30 wakes per bot per hour) and `Idempotency-Key` on every request.
- Offline test suite with a fake webhook server; CI on Python 3.9 to 3.13 with ruff, shellcheck and a secret scan.

### Security

- `wake-inbox-io` accepts only hex inbox ids (`[0-9a-f]{6,32}`) and inbox paths directly inside `$WAKE_STATE_DIR/inbox`, and quotes every value it puts into a shell command.
- `WAKE_REGISTRY_GROUP` is empty by default, so `wake --sync` no longer changes the registry's group unless configured.