# Changelog

All notable changes to this project are listed here. Versions follow [Semantic Versioning](https://semver.org/).
The version lives in `bin/wake_config.py` (`VERSION`) and is sent as `User-Agent: wake-cli/<version>`.

## [Unreleased]

### Added

- **API (`apps/api`, Switchboard merge step 1).** The Fastify + Postgres service, moved in from
  Switchboard with its tests and run on Bun. Threads: `POST /threads`, `GET /threads`,
  `GET /threads/:id`, `POST /threads/:id/reply`, `POST /threads/:id/read`, with migration
  `009_threads.sql` (`threads`, `thread_participants`, `messages.thread_id` / `messages.thread_seq`).
  One answer per ask: the asker never posts to its own ask and the recipient answers once (both
  `409`), `WAKE_TTL_HOURS` (`0` or whole hours `1..720`, default 24) refuses late answers with `410`,
  only participants can read or answer (`404` otherwise), and every thread message also lands in the
  recipient's inbox carrying its `threadId`. Inbox and search payloads gain that `threadId`. This is
  stricter than the CLI in two places, which README states: the CLI still allows a clarification from
  the asker on its own open ask and a second reply from the recipient, and `--force` has no route.
  `GET /threads` pages by creation, not by the mutable `updated_at`, so a thread answered between
  pages cannot jump above the cursor; `POST /inbox/read` also moves the thread read position, so the
  inbox and the thread agree on `unread`. 86 tests against a real Postgres; new CI job `api (bun)`
  with a `postgres:17` service.
- `wake --update` reports every bot's key state in one table (and mints missing keys when a key helper is configured), `--update --ping` sends a quiet ping to each open bot that can be woken, and `--all "<message>"` sends one message to every open bot, skipping private bots and the sender itself.
- `own_bot()` falls back to `<config>/self` (an alias or bot id), so a harness that runs from a neutral working directory is identified as its bot instead of `bot:container`.
- A thread read that fails because the host bridge is unreachable now says so, instead of reporting the thread as never opened.
- `tests/test_gate_shapes.py` checks every command wake builds against the allow-list of a staged `rakazo-wake-gate`, including `wake-inbox-io`'s `register`, `beat` and `read`, so the register route's literal-matched `chmod` tail cannot be dropped silently; a companion test keeps that shape even on a machine with no gate staged.
- `WAKE_TTL_HOURS` (default `24`): a thread older than this refuses answers, and says the age, because the asker has moved on and the answer would land unread. `0` disables it.
- One round per thread: once a thread has an answer, the asker cannot answer back on the same thread, so A to B to A cannot ping-pong. Ask again with `--ask` if there is more to say. `--force` overrides it.
- An audit line per ask, reply and refusal in `$WAKE_RATE_DIR/wake-audit-<uid>.log`, so a refused or failed answer is visible instead of looking like a delivered one.

### Fixed

- A reply that lost the race raised a `23502` (`last_read_seq` was set from an empty `inserted`
  CTE), so `POST /threads/:id/reply` answered `500` where it documents `409`. The read position is
  now kept when nothing was inserted, and a row-locked test proves one `201` and one `409` — never a
  `500`.
- A read receipt naming two messages of the same thread left the thread read position on whichever
  row Postgres happened to match, so `GET /threads` still reported `unread: 1`. The position now
  moves to the newest message the receipt covers, and only the threads the receipt names are
  updated. A test that asks, replies, replies, then reads both messages in one receipt fails on every
  run before the fix and passes after.
- `010_drop_threads_updated_at_idx.sql` drops the index `009` created before paging moved to
  `created_at`, for databases that ran that earlier `009`. Fresh databases never create it.
- The history secret scan masks `sha512` integrity hashes instead of dropping the whole line, so a
  registry token sitting on the same line as an integrity hash in a lockfile is still found. Proven:
  a repo whose history holds an `sk-…` token on such a `bun.lock` line is clean under the dropping
  filter and fails under this one.

### Changed

- The TypeScript workspace uses `bun install` / `bun test` (Switchboard's pnpm + vitest setup is
  gone) and TypeScript 6 with `@types/bun`. `bun run lint` still runs eslint 10 on node, because
  eslint 10 uses `util.styleText`, which Bun does not implement.
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