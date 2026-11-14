# Changelog

All notable changes to this project are listed here. Versions follow [Semantic Versioning](https://semver.org/).
The version lives in `bin/wake_config.py` (`VERSION`) and is sent as `User-Agent: wake-cli/<version>`.

## [Unreleased]

### Added

- `wake --update` reports every bot's key state in one table (and mints missing keys when a key helper is configured), `--update --ping` sends a quiet ping to each open bot that can be woken, and `--all "<message>"` sends one message to every open bot, skipping private bots and the sender itself.
- `own_bot()` falls back to `<config>/self` (an alias or bot id), so a harness that runs from a neutral working directory is identified as its bot instead of `bot:container`.
- A thread read that fails because the host bridge is unreachable now says so, instead of reporting the thread as never opened.
- `tests/test_gate_shapes.py` checks every command wake builds against the allow-list of a staged `rakazo-wake-gate`, including `wake-inbox-io`'s `register`, `beat` and `read`, so the register route's literal-matched `chmod` tail cannot be dropped silently; a companion test keeps that shape even on a machine with no gate staged.
- `WAKE_TTL_HOURS` (default `24`): a thread older than this refuses answers, and says the age, because the asker has moved on and the answer would land unread. `0` disables it.
- One round per thread: once a thread has an answer, the asker cannot answer back on the same thread, so A to B to A cannot ping-pong. Ask again with `--ask` if there is more to say. `--force` overrides it.
- An audit line per ask, reply and refusal in `$WAKE_RATE_DIR/wake-audit-<uid>.log`, so a refused or failed answer is visible instead of looking like a delivered one.

### Changed

- `--sync` from a guest (bot computer) no longer runs `sudo tee` on the host registry: it prints the rows it would have written and exits 1 with `registry sync is an operator action; run it on the host`. `--add` still appends the row locally, and `--update` reports key state but exits 1 for the same reason. A guest ingests untrusted content, so the routing table every bot trusts stays an operator write.
- `thread_append` no longer re-applies `1777`/`666` to the store. The store is provisioned once on the host — `threads/` `root:wake 3770` (setgid + sticky), `inbox/`
  `2770`, files `660` — and wake only appends, so a tightened store stays tightened. README and `docs/protocol.md` state the contract.
- `inbox_live()` reads a marker with `cat <path> 2>/dev/null`, the shape the gate allow-list matches, so a live-session delivery is no longer silently skipped.
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