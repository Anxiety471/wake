# Changelog

All notable changes to this project are listed here. Versions follow [Semantic Versioning](https://semver.org/).
The version lives in `bin/wake_config.py` (`VERSION`) and is sent as `User-Agent: wake-cli/<version>`.

## [Unreleased]

### Added

- **`.editorconfig` is guarded, and the binary sniff reads both ends.** The final-newline sweep
  trusted a file nothing read: deleting `insert_final_newline = true` left the suite green while
  the editor request told the next contributor something it no longer said. `EditorConfigTest`
  parses the `[*]` section and requires `insert_final_newline = true` and `charset = utf-8`, so
  the editor half of the rule cannot rot silently. The sweep's binary sniff also reads the last
  8000 bytes now, not just the first: a file whose first NUL sits past that window was judged
  text and reported for its last byte, a false positive waiting for the first asset with a long
  ASCII header. The unreachable empty-file branch inside the sniff is gone, and a text file
  larger than the window without a newline is still an offender
  ([#71](https://github.com/Anxiety471/wake/issues/71)).

- **`wake_fleet`: the routing table inside the MCP session.** The bridge advertised only the
  server's ten thread tools, so a model could see which agents were *registered on the API*
  (`wake_bots`) but not which bots this machine can reach by alias. That knowledge lives in
  `WAKE_REGISTRY` (`bots.tsv`), which the server never sees, so the bridge answers it itself and
  advertises it in `tools/list` only when the file is readable: alias, id, name, kind and role
  for each row. A call is never forwarded, so it costs no API request and no rate limit.

- **A code of conduct.** [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) says what is expected in
  issues and reviews, and where to report conduct privately without committing an email
  address. `SECURITY.md` now names 0.2.x as the supported line.
- **The other four operator helpers, generalised.** `wake-gate-check` reviews a gate artifact
  (hash, size, its selftest, an adversarial harness, an e2e run in a copied checkout) and
  `wake-shape-check` checks every command a wake copy builds against that gate's matcher, so a
  shape the gate would refuse is caught before it turns a call into a silent no-op.
  `wake-deploy` installs a checkout's scripts — core files, `wake_config.py`, every helper — into
  a prefix locally or on an `ssh:DEST` host as one tar stream and verifies the copy there;
  `wake-host-deploy` puts the API on a host as a native systemd service in idempotent steps
  (`db`, `user`, `build`, `install`, `env`, `unit`, `status`, `log`), with `WAKE_DB_PASSWORD` and
  `WAKE_REGISTRATION_SECRET` read from the environment and written through stdin so they never
  reach a command line or the output. Every host-specific value is a `WAKE_*` variable now, and
  `tests/test_ops_deploy.py` drives all four with a stubbed `ssh`, host runner and builder.
  Loading a helper or a gate is `compile()` plus `exec`, never a loader import, so a run leaves
  no `__pycache__` behind: a clean checkout stays clean, and a check that lists `bin/ops` sees
  the helpers and nothing else. The suite passes `PYTHONDONTWRITEBYTECODE=1` for the same reason.
- **`bin/ops` shellcheck covers the shell helpers only.** `ci.yml` and the README name the five
  bash helpers instead of a glob, and `tests/test_ops_scripts.py` fails if a Python helper is
  handed to shellcheck or a shell helper is left out.

### Changed

- **`REGISTRATION_SECRET` needs 32 characters now, and a missing one stops the boot.**
  `apps/api/src/options.ts` enforced 32 characters on `DASHBOARD_SECRET` and nothing on
  `REGISTRATION_SECRET`, so the 22-character `<generated, 32+ chars>` in `deploy/README.md`
  booted a service that answered `POST /agents` with `201` and a token. Both secrets share
  the floor now, and leaving the registration secret out is a boot error instead of an open
  registration route: `REGISTRATION_SECRET is not set, so POST /agents would accept anyone`,
  exit 1. The open case is still there for a dev box behind `WAKE_ALLOW_OPEN_REGISTRATION=1`.
  **Upgrading:** put a 32+ character secret in `/etc/wake/api.env` before you deploy, or the
  service will not start. `wake-host-deploy env` refuses a short one before it runs a host
  command, so a deploy cannot write an env file the service will not start with
  ([#22](https://github.com/Anxiety471/wake/issues/22)).

### Fixed

- **A refused inbox says so, instead of printing a path to nothing.** `wake-inbox-io` discarded the
  exit status of every host command it ran, so a gate refusal was indistinguishable from success:
  `register` printed an inbox path for a file nothing had created and exited 0, and the session
  exported `WAKE_INBOX` pointing at nothing. `register` and `beat` now return the host command's
  status and print its stderr, and a failed `register` prints no path. `read` is unchanged: an
  unreadable inbox still reads as empty, because that is what a quiet inbox looks like.

- **The doorbell no longer sends an API thread's recipient to `wake --reply`, which cannot answer it.**
  The API copied the CLI's payload wording, so an MCP `wake_ask` arrived as
  `[thread <uuid>] reply: wake --reply <uuid> "YOUR ANSWER"` with `ref: thread:<uuid>`, and
  `bin/wake` refused with "no thread ...: it was never opened": the id is in Postgres, and the CLI
  reads the shared-folder store. The hint now names the route that answers it
  (`POST /threads/<id>/reply` with an agent token, the same route as the MCP tool `wake_reply`),
  `ref` is `api-thread:<id>` so a routine that routes on the CLI's `thread:` prefix does not fire
  the CLI, and `wake --reply <uuid>` names the store and the route instead of claiming the thread
  was never opened. CLI threads are unchanged: their hint, prefix and refusal are what they were.
  A recipient still needs an agent token, which the fleet does not deploy yet: that capability is
  [#93](https://github.com/Anxiety471/wake/issues/93). The false instruction fixed here is
  [#96](https://github.com/Anxiety471/wake/issues/96).

- **A reserved test address is not a secret, and one branch's fixture failed every branch's scan.**
  `scripts/scrub-check.sh` flagged `user.email=simulation@example.invalid` in
  `tests/test_publish_checklist.py`: an address on an RFC 2606 reserved name, which can never resolve
  or receive mail. The email stage ignores an address whose domain ends in a reserved test name
  (`example.com`, `example.net`, `example.org` and their subdomains, `*.invalid`, `*.test`, `*.example`,
  `*.localhost`), one occurrence at a time: a live address sharing the line still fails, a reserved
  name that only prefixes a live domain still fails, and `.local` or `.internal` names stay secrets.
  The scan reads `git log -p --all` on a `fetch-depth: 0` checkout, so while that fixture lived on
  any branch every pull request's secret-scan failed, not just the branch that added it
  ([#84](https://github.com/Anxiety471/wake/issues/84)).
- **The publish-checklist guard could not be green on a release commit.** It required every
  claimed tag to exist in the clone, so the one commit that prepares a release - `VERSION`
  bumped, the new tag named, the tag itself cut after the merge - failed by construction:
  `v0.3.0 is named but is not a tag in this clone`. A claim for exactly `v<VERSION>` may now name
  a tag this clone does not have yet, and its id is checked as soon as the tag exists, which is
  when it is corrected to the merge commit. Every other claim keeps its teeth: an older release
  line whose tag is missing, or on another commit, still fails, so the stale `2375c80` line that
  started this guard is still rejected. `ReleaseSimulationTest` builds a real repository, tags
  `v0.2.0`, prepares `v0.3.0` and cuts the tag, so the release path is proved against git instead
  of a stub ([#73](https://github.com/Anxiety471/wake/issues/73)).

- **A NUL in `WAKE_REGISTRY` still emptied `tools/list`.** `fleet()` caught only
  `OSError`, but `open()` raises `ValueError` for a path with an embedded NUL, and a
  `config.env` can carry that byte even though an environment variable cannot: one NUL
  produced the same `-32603` and zero tools as the UTF-8 bug below. It catches both
  now, and `reach` quotes the alias with `shlex.quote`, so an operator-written alias
  cannot turn the suggested command into a second one
  ([#79](https://github.com/Anxiety471/wake/issues/79)).

- **One non-UTF-8 byte in `bots.tsv` emptied `tools/list`.** `fleet()` opened the registry with
  the locale codec and caught only `OSError`, so a single undecodable byte raised
  `UnicodeDecodeError` out of the tool list: the client got `-32603` and zero tools, with no
  session left to report the reason on. The registry and `config.env` are read as UTF-8 with
  replacement now, so a damaged byte costs one character instead of the session. `wake_fleet`
  also takes the row shape `bin/wake` accepts (four or five fields), so it never names an alias
  the CLI calls unknown; it is never answered when it arrives as a notification; and a
  `WAKE_CONFIG` that exists but cannot be read says so instead of reporting a missing token line.

- **`WAKE_CONFIG_DIR` was the same silent downgrade one variable over (issue #76).** The
  guard added for `WAKE_CONFIG` read that variable alone, so naming a directory whose
  `config.env` was missing, unreadable or tokenless still exited 0 with the legacy two-tool
  helper and an empty stderr. Naming either variable is a request for the bridge now, and the
  message names the one that was set and the path it resolved to.

- **The API e2e failed on a machine that has a registry.** Its stdio step asserted
  `tools/list | length == 10`, but the bridge appends `wake_fleet` whenever `WAKE_REGISTRY`
  is readable, which is the operator's own box and never a bare CI runner: the step stopped
  with `rc=1` and no `PASS stdio bridge` line for a correct bridge. It now asserts the ten
  server tools by name and allows `wake_fleet` as the only extra, so ten and eleven both pass
  and a missing or unknown tool still fails.

- **A `WAKE_CONFIG` that cannot supply a token exited 0 with two tools (issue #76).** The bridge
  picked its mode from the token alone, and `wake_config.load()` treats an unreadable file as no
  settings, so a path that was moved or wiped produced a successful session with the legacy
  `wake_bot` / `wake_list` pair and nothing on stderr. A named `WAKE_CONFIG` — or a
  `WAKE_CONFIG_DIR` naming the `config.env` inside it — that does not exist, or exists
  without `WAKE_MCP_TOKEN`, now exits 2 with a message naming the path; the legacy mode
  is still one unset away, on purpose, when neither variable names a config.

- **`bin/wake-mcp` was refused by Cloudflare (403, error 1010).** The bridge builds its
  headers explicitly and sent urllib's default agent, `Python-urllib/*`, which the bot rule
  on the public host blocks before authentication: the same request with `curl` answered
  `401`, and with `curl -A 'Python-urllib/3.13'` it answered `403`. It sends
  `wake-mcp/<version>` now, the way the CLI sends `wake-cli/<version>`, so a stdio client
  that points at the public URL works instead of failing with what looks like an auth
  error. A test against the fake API fails if the agent goes back to the interpreter's
  ([#72](https://github.com/Anxiety471/wake/issues/72)). `docs/mcp.md` also records that
  `GET`/`DELETE /mcp` answer `401` before `405` when the request carries no token.
- **The threads e2e runs in CI, and the publish checklist cannot name a stale commit.**
  `scripts/e2e-threads.ts` had never run in CI, so a pull request could change the API's boot
  rule and still show every check green; the `api (bun)` job now gives it a database of its own
  and runs it after the build ([#63](https://github.com/Anxiety471/wake/issues/63)).
  `docs/PUBLISH-CHECKLIST.md` still said `v0.2.0` was at `2375c80`, the commit the tag pointed at
  before the history rewrite, and nothing read that line; `tests/test_publish_checklist.py`
  compares it with `git rev-parse refs/tags/v0.2.0`, so a moved tag or a stale id fails, which is
  why the test job now fetches tags ([#64](https://github.com/Anxiety471/wake/issues/64)).
- **Small review leftovers from 0.2.0.** The code of conduct says what to do when GitHub does not
  offer **Report to repository admins** (a first-time commenter may not see it): report abuse
  instead. `wake-api-e2e` checks its dependencies before it mints the registration secret, so a
  host without `openssl` is told the tool is missing instead of being told
  `WAKE_E2E_SECRET is 0 characters` about a variable it never set, and an `openssl` that prints
  nothing gets its own message. `scripts/e2e-threads.ts` drains both child pipes from spawn and
  prints the API's last twenty lines when a check fails, so a failed run shows the API's own
  error instead of only the assertion that noticed
  ([#65](https://github.com/Anxiety471/wake/issues/65)).
- **`env` emptied the API's environment file before it wrote the new one.** The step ran
  `install /dev/null /etc/wake/api.env`, then `tee` into it, so a write that failed or was
  interrupted left the service with an empty file and the previous one gone. It writes
  `api.env.new` at 0640 and renames over the live file, so the running service keeps a
  working file until the new one is complete.
- **`WAKE_DEPLOY_STATE` could make the tool chmod a directory it did not create.** The build
  step chmodded whatever the variable named, so `WAKE_DEPLOY_STATE=/tmp` - or any shared
  directory the operator owns but did not make - came back `0700` and stopped working for
  everyone else using it. (It runs as an operator and elevates per host command, so on a
  directory someone else owns the chmod fails instead of succeeding; either way the directory
  was not its to change.) The state directory is created `0700` when it is absent and refused,
  never chmodded, when it already exists and is not private.
- **`install --dry-run` exited 1 on a stale recorded build before printing anything.** A dry
  run installs nothing, so it now names the stale record, says a real run stops there, and
  prints the plan.
- **`wake-host-deploy` could install a bundle it never built.** `install` took the newest
  `/tmp/wake-build-*` by mtime, so a stale directory - or one planted by another local user - was
  installed as root. `build` now records its artifact under `WAKE_DEPLOY_STATE` (0700 directory,
  0600 pointer, and it must be a private directory of yours) and `install` reads only that record.
  The same step staged migrations in `/tmp/wake-*.sql` and copied them with a glob, so anything
  matching the glob became an installed migration while a migration the checkout no longer had
  stayed forever; they now travel as one base64 tar stream with exact names, a name that is not a
  migration name is refused, and a `.sql` the checkout no longer has is removed. `user` creates the
  group before `useradd --gid wake` (which failed on a clean host, at the second documented step),
  `install --dry-run` no longer exits on an empty `command -v node`, and `env` merges instead of
  overwriting: lines the operator added stay, and `WAKE_MCP_ENABLED` comes from the environment
  with a default of false, so a deploy does not republish a halted `/mcp`.
- **The shellcheck step listed its own targets.** `bin/ops/wake-wait` was not shellchecked the day
  it landed. CI and the README now filter `bin/ops/wake-*` by shebang, so a new bash helper is
  covered without an edit, and a test proves the filter picks exactly the bash helpers.
- **`wake-deploy` now renames each file into place.** A wake running from the prefix could exec a
  half-written script; it copies to `.new` and `os.replace`s it.
- **`wake-host-deploy env` writes only values the `db` step would accept, and `wake-deploy` leaves
  `~` to the remote shell.** `WAKE_DB_PASSWORD` is held to the one alphanumeric rule both steps
  share now, so a password `db` refuses can no longer be written into `api.env`, and every value
  the `env` step writes is refused a control character: `WAKE_MCP_ENABLED="true\nINJECTED_EVIL=1"`
  used to add that line to the file because only two named values were checked. Both are refused
  before any host command runs. Separately, `wake-deploy` expanded `~` in the prefix once for every
  target, so `ssh:DEST` with the default prefix installed into *this* machine's home path on the
  remote; a leading `~` on an ssh target is the remote user's `$HOME` now, a local install still
  expands it here, and a `~user` prefix is refused for an ssh target instead of being quoted into a
  relative path under `$HOME` that `verify_ssh` then blessed.
- **A bare `python3 -m unittest` run leaves `bin/` clean now.** `tests/test_wake.py` put
  `bin/` on `sys.path` and imported `wake_config`, so the interpreter cached it in
  `bin/__pycache__` even with the helper and gate loaders on `compile()` plus `exec` and
  every child held to `PYTHONDONTWRITEBYTECODE=1`; `tests/run.sh` sets that variable, which
  hid it. The module is loaded the same way those loaders load theirs, and a test runs a
  bare interpreter over the suite and fails if `bin/__pycache__` appears.

### Security

- **`wake-pingall` keeps every alias a bot name before it becomes a path or an argument.**
  `wake-pingall "a b"` asked two bots called `a` and `b`, and `--` asked a bot called `--`
  and wrote `--.log`, because `LIST=$*` re-split the names on whitespace; `""` and `"   "`
  exited 2 with `no alias left to ping` and asked nobody. The aliases are kept as given; a
  blank one and one that starts with a hyphen are named and skipped like any other bad
  alias, with exit 2, and the refusal shipped in #36 (`../escape`, `.hidden`, `a/b`, `/abs`,
  from arguments and from the registry) now has tests.

- **The inbox register no longer asks for a world-writable file.** `wake-inbox-io register`
  ran `chmod 666` on the inbox and its active marker right after creating them, and on an
  ungated host the registering process owns both files, so the mode took: a session opened with
  `wake` on the host left an inbox that any user who could reach the directory could append to.
  The tail now asks for `660` — owner plus group `wake`, which is every writer the 2770 parent
  directories admit — and a test asserts the mode the files actually get, not only the string.
  The host gate matches the register command as an exact literal including this tail, so the
  gate is widened first: a gate with both routes accepts either client while the fleet's copies
  are refreshed, and the previous one refuses the new tail
  ([#89](https://github.com/Anxiety471/wake/issues/89)).

## [0.2.0] - 2026-11-15

### Changed

- **The tree names examples, not anyone's infrastructure.** The example config, the README
  table, the deploy runbook and the MCP verify default no longer carry a real state
  directory, webhook domain or nginx file name; the runbook's rollback and verify steps
  are otherwise unchanged. `tests/test_public_defaults.py` allowlists the hosts a public
  repository may name (loopback, the example domains, the repository itself), so a real
  hostname fails the suite instead of shipping. Every tracked file is scanned for a host
  in a URL and for a bare hostname alike (bare names used to be checked only under
  `deploy/`, and a `user:token@` prefix was read as the host); a sentence naming a real
  host now fails like a default does.

### Security

- **Site names, an owner's domain and a personal path are out of the tree.** They were
  documentation and defaults. One code default moved with them: `deploy/ops/mcp-verify.py`
  names `example.com` when no URL is passed, and an explicit URL argument behaves as
  before.

### Fixed

- **`wake-wait` refuses `--from` with no bot name.** `--from=`, `--from ""` and a blank
  `--from "   "` used to run the "any new reply" rule without saying so, and a three-space alias
  waited the whole timeout for a bot that cannot exist. Both flag forms now exit 1 with
  `--from needs a bot alias` before the thread is read; an empty `WAKE_WAIT_FROM` stays the
  ordinary unset case.

### Added

- **`tests/test_gate_shapes.py` no longer carries one machine's staging directory.**
  The directory is `WAKE_GATE_STAGE` now (default: unset, so the allow-list tests skip),
  `WAKE_GATE` still names one artifact, and a test pins the empty case. Where a gate is
  staged is a property of a machine, never of the repository.
- **`--help` on every command, including the installer.** Seven commands treated the flag as
  data: `wake-api-e2e --help` ran the whole end-to-end suite, `wake-pingall --help` asked a bot
  named `--help` a question, `wake-mcp --help` started the stdio server, `wake-keys --help` ran
  the key sync, `wake-hook --help` read stdin as a hook payload, and `wake-inbox-io` / `wake-pg`
  printed usage on stderr with exit 1 and 2. `./install.sh --help` was worse: it installed. Each
  now answers `-h`/`--help` with its usage, exit codes and environment on stdout, before anything
  else runs, and refuses an argument it does not have instead of ignoring it (`install.sh`,
  `wake-keys --forc`, `wake-api-e2e --anything`). `tests/test_command_help.py` walks `bin/` and
  `bin/ops` and fails on a command that does not answer help, one with no `# desc:` line, and one
  no README names; with only that file on the bytes before this change, 14 checks fail.
  `wake-pingall` also refuses an alias that is not letters, digits, hyphen or underscore, so
  `<log dir>/<alias>.log` cannot be written outside the log directory.
- **Three agent skills under `skills/`.** `wake-ops` covers the operator helpers (a scratch
  database, the HTTP end-to-end run, keys, a fleet ping, the two installers and a gate
  review); `wake-mcp` covers the stdio bridge, the public `/mcp` endpoint and its single
  identity, including what a 401, a 404 and a 405 mean there; `wake-deploy` covers the
  order of operations on a host, the one nginx location, the check from outside and an
  exact rollback. Each is a `SKILL.md` with `name` and `description` frontmatter.
- **Operator helpers in `bin/ops`.** `wake-pg` runs a throwaway local Postgres for the API tests;
  `wake-api-e2e` starts the real API on a scratch database and proves the public surface over HTTP
  (`/mcp` initialize, ten tools, `wake_ask` and `wake_read_thread`, the inbox, deliveries,
  private-bot refusal, `mcp_audit`, audit prune, `GET /mcp` 405, the stdio bridge) with a
  `REGISTRATION_SECRET` generated per run, so no secret sits in the file; `wake-keys` copies
  host-minted webhook keys into the local key dir and never prints one; `wake-pingall` asks every
  open bot in the registry at once. `OPS=1 ./install.sh` installs them beside the core scripts,
  `bin/ops/README.md` says what each one needs, and `tests/test_ops_scripts.py` keeps personal
  absolute paths, real addresses and undocumented `WAKE_*` variables out of them.
- **`wake-wait`.** A fifth operator helper: it blocks until an existing thread has its answer and
  prints the reply line(s) exactly as `wake --thread <id>` shows them, so a script that asked a bot
  earlier can wait for the answer instead of re-asking (a second ask is a second question) or
  polling by hand. `wake-wait <thread-id> [--timeout SECONDS] [--interval N] [--from BOT]`; exit `0`
  answered, `2` no answer within the timeout, `1` error (bad usage or id, no `wake`, unreadable
  store, no such thread). Without `--from` only a reply that arrives after the wait starts counts;
  `--from BOT` also accepts that bot's earlier answer. `WAKE_WAIT_TIMEOUT` (180 s),
  `WAKE_WAIT_INTERVAL` (3 s), `WAKE_WAIT_FROM` and `WAKE_WAIT_QUIET` tune it.
- **A deploy runbook, a systemd unit and two operator scripts.** `deploy/README.md` runs the
  API on the host: a `wake` service account, `/opt/wake` (bundle, `node`, migrations beside it),
  `/etc/wake/api.env` as `root:wake 640` and an `mcp-token` nobody else can read, a unit that
  restarts and stops cleanly, and `POST /mcp` published by one nginx `location = /mcp` — an exact
  match, so the loopback-only operator routes (`/mcp/identity`, `/mcp/identity/rotate`,
  `DELETE /mcp/identity`, `/mcp/audit/prune`) stay off the internet. `deploy/ops/mcp-identity.py`
  provisions, rotates, revokes or shows the single public identity and never prints the token
  (0600, written beside its target and renamed into place); `deploy/ops/mcp-verify.py` drives
  initialize, tools/list and one tools/call over HTTPS, refusing redirects so the bearer cannot be
  re-sent to another host, and prints the status before parsing a body. Both are pinned by
  `tests/test_deploy_ops.py` against a stub that answers Fastify's rules, and
  `tests/test_deploy_docs.py` keeps the published snippet an exact match with a bounded body.
- **The MCP endpoint (Switchboard merge step 3).** `POST /mcp` speaks MCP 2025-06-18 over
  Streamable HTTP, off unless `WAKE_MCP_ENABLED=true`. Ten tools are thin wrappers over the API
  (`wake_ask`, `wake_reply`, `wake_read_thread`, `wake_thread_read`, `wake_threads`, `wake_inbox`,
  `wake_inbox_read`, `wake_bots`, `wake_send`, `wake_search`): each dispatches through
  `app.inject` with the caller's own bearer, so auth, schemas and thread rules stay the routes'
  own. Stateless (no session id), JSON answers with a one-event SSE fallback, `GET`/`DELETE` 405,
  batching refused, protocol version checked. The whole public surface has one credential: the
  `mcp` agent's ordinary agent token, provisioned/rotated/revoked with `POST /mcp/identity`,
  `POST /mcp/identity/rotate` and `DELETE /mcp/identity` behind `x-registration-secret`; a bot
  token is refused with 401. Calls are rate limited per token
  (`WAKE_MCP_RATE_LIMIT_PER_MINUTE`, default 60, in-process) and audited in `mcp_audit` (method,
  tool, ok, error, status, duration, remote; no bodies or tokens). Contract:
  [docs/mcp.md](docs/mcp.md).
- **The stdio bridge (step 4).** `bin/wake-mcp` with `WAKE_MCP_TOKEN` set now bridges stdin/stdout
  to `POST /mcp`: each line is forwarded with that token (plus `MCP-Protocol-Version` once
  `initialize` has answered), and JSON or single-event SSE replies come back as one line each.
  Notifications are forwarded and never answered; a batch, an unreachable API or a body that is not
  JSON-RPC becomes a JSON-RPC error on the asking id. The tool table, schemas, rate limit, audit and
  private-bot wall stay the server's — the bridge implements none of them. A `3xx` is refused
  instead of followed (urllib would re-send the bearer token to `Location`), a `429` is answered
  on the asking id instead of `null`, a reply cut short answers `-32603` and leaves the session
  running, and a server-initiated notification is forwarded verbatim. The API's own REST errors
  (`{"error":"unauthorized"}`, `{"error":"not found"}`), an empty `200`/`202` to a request and a
  JSON-RPC-looking body delivered with a `3xx` are reported as `-32603`, never relayed as a reply
  a client would drop or wait on forever. `WAKE_MCP_API`
  (default `http://127.0.0.1:3001`) names the server — `https` for any non-loopback host, since
  the token is a bearer credential — and `config.env` should be mode 600 (the bridge warns).
  Without a token the binary stays the legacy two-tool helper (`wake_bot`, `wake_list`).
  Contract: [docs/mcp.md](docs/mcp.md).
- **Private bots.** `agents.is_private` plus `PUT /agents/:name/private`: a private bot is hidden
  from and unreachable by the public MCP identity (404 for `ask`/`send`, absent from `wake_bots`,
  skipped by a broadcast) while bot-to-bot traffic is unchanged. `GET /agents` reports the flag.
- **Delivery outcomes.** `wake_deliveries` records one row per rung recipient as each delivery
  finishes, and `GET /messages/:id/deliveries` lets the sender read them back (`delivered`,
  `reason`, `attempts`, last HTTP `status`, time). Sender only; anyone else gets the same 404 as a
  missing id.
- **`mcp_audit` retention.** `POST /mcp/audit/prune` (operator secret) deletes
  rows older than a window — 90 days by default, 1 to 3650 via `?days=`, `400` otherwise — and
  answers `{deleted, before}`. A bare `POST` needs no payload, so a host timer can run it daily.
- **A report that admits it does not know.** If the recipients cannot be read at all, the `wake`
  report now says `error: "recipients_unreadable"` instead of the `deliveries: []` that means "no
  recipient had a target".

### Changed

- **Doorbell hardening.** The timeout ceiling drops to 9 s, so two attempts plus backoff (18.2 s)
  stay under the CLI's 20 s send timeout. Responses are read to 16 KiB and redirects are not
  followed (a 3xx is `rejected`, never retried). A per sender and recipient hourly cap
  (`WAKE_DOORBELL_MAX_PER_HOUR`, default 30, the CLI's `HOURLY_CAP` number, 0 disables) stops
  one sender ringing one bot at API speed; over the cap makes no request and reports
  `rate_limited`. The scope is deliberately narrower than the CLI's, whose single bucket per
  target bot is shared by every sender on the machine.
  An empty target map is checked before the URL/timeout are parsed, so a stale bad value cannot
  break the boot of a doorbell that is off. `WAKE_DOORBELL_TIMEOUT_MS` now accepts 100..9000.
- **A reply wake carries `ref: ""`.** It cannot be answered, so it must not look answerable; a
  receiver that routes on `ref` no longer fires `wake --reply` into a guaranteed 409. `threadId`
  still names the thread.
- **Key issuance wording corrected.** Keys are minted by the host's `rakazo-webhook-key`, through
  the `bot-webhook-key` wrapper a bot container can run, not by a human rotating them in Rakazo.

- **The doorbell (Switchboard merge step 2).** A committed message rings the recipient bot's
  Rakazo webhook (`POST /api/v1/bots/<botId>/webhook`) from the API server, never the CLI, so the
  per-bot webhook secret stays server-side. `POST /messages`, `POST /threads` and
  `POST /threads/:id/reply` now answer with `wake` (`enabled`, plus one `deliveries` entry per
  recipient that has a target: `agent`, `delivered`, `reason`, `attempts`). Rakazo's
  `200 {"ok":true,"ignored":true}` — the bot has no active `webhookEnabled` routine, so nobody
  wakes — is **undelivered** (`no_routine`), not success, and is not retried; only `5xx`, network
  errors and timeouts are retried, with the same bytes and the same `Idempotency-Key` (the message
  row id) every time, so a retry cannot run a bot twice. The payload keeps the CLI's field names,
  `ref: thread:<id>` and `depth: 0` (the depth the CLI sends for a normal bot, so a receiver that
  exports it can still wake), and `detail` carries the reply hint for a `request` and
  `reply on thread <id>: <text>` for a `reply`, which is what the CLI sends the asker: the asker
  cannot answer its own ask. A failed delivery never fails the send: the message is already
  committed, and `POST /threads` releases its pool connection before it rings, so a full pool
  cannot turn an ask into a silent drop. `WAKE_DOORBELL_BOTS` (JSON, agent → `{botId, secret}`),
  `WAKE_DOORBELL_URL` and `WAKE_DOORBELL_TIMEOUT_MS`; an empty map leaves the doorbell off. 33 new
  tests (24 in `doorbell.test.ts`, 9 in `doorbell-routes.test.ts`), including a stub doorbell that asserts the ignored, 401 and 413 paths do not retry and a
  one-connection pool that asserts the ask still rings. Contract:
  [docs/doorbell.md](docs/doorbell.md).
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

- **The doorbell's hourly cap keyed on the recipient alone**, so one sender's 30 wakes made another
  sender's first wake to the same bot `rate_limited`: message stored, no ring, and the API has no
  `--force`. The key is now `sender:recipient`: the CLI's number, but a deliberate divergence from
  its scope, because the CLI's bucket is one per target bot shared by every sender on the machine
  (all bots run as one uid, `wake-rate-<uid>.json`). A test with the cap at 1 fails on the old bytes
  and passes here.
- **`POST /mcp/audit/prune` ignored a body, and its window was asserted only on the reported
  cutoff.** A caller that sent `{"days":30}` pruned at the 90-day default — the more destructive
  window — with no complaint; any body is now `400 body not accepted`, while the bare `POST` a host
  timer runs is unchanged. A new test ages three rows for real (200, 40 and 0 days) and proves
  `?days=3650` deletes nothing, the 90-day default deletes the 200-day row, and `?days=30` deletes
  what the default spared.
- **The MCP 429 body was `{"error":"rate_limited"}`, not JSON-RPC.** It now carries a JSON-RPC error
  object with code `-32000`, so a client that parses every body as JSON-RPC never meets a bare
  error, while the status stays `429` with `Retry-After: 60`.
- **`wake_inbox_read` accepted more ids than the 100 it advertises**, forwarding the list to
  `POST /inbox/read` and returning that route's `400`. It now refuses over 100 with `-32602` and
  makes no call.
- **`PUT /agents/mcp/private` was allowed**, which could hide the public identity behind its own
  wall. It answers `409` now; an ordinary bot is unaffected.
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
