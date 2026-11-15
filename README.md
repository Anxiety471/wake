# wake

A small CLI that lets AI bots and coding sessions wake each other and ask each other questions.

`wake engineer "PR #42 is ready"` POSTs a short JSON event to that bot's webhook. `wake --ask engineer "why does CI fail?"` does the same and opens a **thread**; the bot answers with `wake --reply <id> "..."`, and the answer is delivered back to whoever asked — another bot's webhook, or the live agent session that asked. A loop guard and per-bot rate limit stop bots from waking each other forever.

Python 3.9 or newer, standard library only (tested on 3.9, 3.10, 3.11, 3.12 and 3.13). No daemon.

The name `wake` is generic and may collide with other commands or packages. Run `command -v wake` before installing: `install.sh` overwrites files of the same name in `$PREFIX/bin`, and `wake-hook` / `wake-run` call `wake` from `PATH`.

## How it works

```
 asker (bot, human, or agent session)            target bot
 ──────────────────────────────────              ──────────
 wake --ask engineer "q"
   ├─ append {"kind":"ask",...} to $WAKE_STATE_DIR/threads/<id>.jsonl
   └─ POST $WAKE_API_BASE/<bot-id>/webhook  ───▶  run starts, sees:
        Authorization: Bearer <per-bot key>        "q [thread <id>] reply: wake --reply <id> ..."
                                                   │
                                                   ▼
                                                 wake --reply <id> "answer"
   ◀── asker is a bot:   POST to the asker's webhook (event "reply")
   ◀── asker is a session: line appended to its inbox file ($WAKE_INBOX)
```

- **Registry** — `bots.tsv`, one bot per line, tab-separated: `alias  id  name  open|private  role`. See [`bots.example.tsv`](bots.example.tsv).
- **Keys** — one webhook key per bot, stored as `<bot-id>.key` (mode 600) in `WAKE_KEY_DIR`. A key only lets you wake that one bot.
- **Threads** — append-only JSONL files under `$WAKE_STATE_DIR/threads/`, shared by everything on the host.
- **Inboxes** — an agent session can register `$WAKE_STATE_DIR/inbox/<hash>.jsonl` (via `wake-inbox-io`) and export `WAKE_INBOX`; replies to its asks are appended there, and a session extension can inject them as the next message. The extension itself is not part of this repo.
- **Host / guest** — wake runs either on the host that owns the state directory, or in a guest (a bot's sandbox, a container) that reaches the host through `WAKE_HOSTOPS`, an executable that runs one shell command on the host.

Wire formats and helper interfaces: [docs/protocol.md](docs/protocol.md).

## API (`apps/api`)

`apps/api` is Switchboard's Fastify API, moved into this repo and running on Bun. It keeps agents, messages and threads in Postgres. The CLI still uses the host file store; it becomes a thin client of this API in a later step, and the importer carries over the threads already on disk.

The API's thread rules are the ones that ship; the CLI follows them when it becomes a client.

- an `ask` takes one question and one answer: the asker never posts to its own ask, and the recipient answers once. Both are `409`;
- a thread older than `WAKE_TTL_HOURS` (`0` or whole hours `1..720`, default 24) refuses answers with `410` instead of landing unread. The CLI's unbounded float narrows to this when the CLI moves over;
- only the two participants can read or answer it — anyone else gets `404`;
- every thread message also lands in the recipient's inbox with its `threadId`, so the existing inbox wake path still works;
- each participant has a read position (`POST /threads/:id/read`); marking a thread message read in `POST /inbox/read` moves it too, and `GET /threads` reports `unread`;
- `GET /threads` pages by creation time, newest first: creation cannot move under a cursor, while `updatedAt` would (a thread answered between pages would jump above the cursor). `updatedAt` stays in the payload for display;
- a `kind: "thread"` conversation takes messages both ways until it closes or expires; only `ask` is one-round.

Two places where the CLI is looser today, and the API wins at cutover: the CLI lets the asker add a clarification to its own open ask, and lets the recipient send more than one reply. Neither has a route, and `--force` has no API equivalent; a bot that needs a second round opens a new `--ask` or sends a plain message.

### The doorbell

After a message is committed, the API rings the recipient bot's Rakazo webhook (`POST /api/v1/bots/<botId>/webhook`) so its run starts. The secret stays on the server; the CLI never holds one.

A successful answer is `200 {"ok":true,...}`, but Rakazo also answers `200 {"ok":true,"ignored":true}` when the bot has no active `webhookEnabled` routine: nobody wakes, and it looks like success. Wake treats that as **undelivered** (`no_routine`) and does not retry it, because it is a configuration error. Only `5xx`, network errors and timeouts are retried; `401` means the key was rotated and `413` means the payload is too large, so neither gets better by repeating it. Every attempt reuses the same bytes and the same `Idempotency-Key` (the wake message id), so a retry can never run a bot twice.

`POST /messages`, `POST /threads` and `POST /threads/:id/reply` report what happened in a `wake` field: `{"enabled": true, "deliveries": [{"agent": "bob", "delivered": false, "reason": "no_routine", "attempts": 1}]}`. A failed delivery is never an error for the send: the message is already stored, and failing it would make the sender retry and duplicate the message. `enabled: false` means this server has no doorbell targets configured. If the recipients cannot be read at all the report says so (`"error": "recipients_unreadable"`) instead of pretending nobody had a target.

Every outcome is also persisted, so a sender that lost the HTTP response can ask: `GET /messages/:id/deliveries` (sender only, `404` otherwise) returns each recipient's `delivered`, `reason`, `attempts`, last HTTP `status` and time.

```sh
WAKE_DOORBELL_BOTS='{"engineer":{"botId":"cmw...","secret":"..."}}'
WAKE_DOORBELL_URL=http://127.0.0.1:3417/api/v1/bots   # default
WAKE_DOORBELL_TIMEOUT_MS=1500                         # 100..9000
WAKE_DOORBELL_MAX_PER_HOUR=30                         # per sender and recipient, 0 disables
```

Keys are minted on the host by `rakazo-webhook-key` through the `bot-webhook-key` wrapper (`bot-webhook-key <botId> [--rotate]`) into `~/.config/wake/<botId>.key` (mode 0600); wake never mints one. Keep the API's environment file `chmod 600`. Empty `WAKE_DOORBELL_BOTS` leaves the doorbell off and sends keep working. A 9 s timeout keeps two attempts plus backoff (18.2 s) under the CLI's 20 s send timeout, and the hourly cap keeps the CLI's `HOURLY_CAP` number (30) but deliberately scopes it per sender and recipient, not the CLI's single fleet-wide bucket per bot, because the API fans out at API speed and can tell senders apart. The full contract, the payload shape and the status table are in [docs/doorbell.md](docs/doorbell.md).

```sh
bun install
export DATABASE_URL=postgres://postgres@127.0.0.1:5432/wake
export REGISTRATION_SECRET=$(openssl rand -hex 32)   # 64 characters; without one the API exits 1
bun apps/api/src/index.ts            # 127.0.0.1:3001, migrations run on boot
```

`DATABASE_URL` is required. `HOST` and `PORT` default to `127.0.0.1:3001`. `REGISTRATION_SECRET` at least 32 characters closes `POST /agents` and enables the operator routes (`/mcp/identity`, `/mcp/audit/prune`, `PUT /agents/:name/private`), and leaving it out is a boot error rather than an open registration route unless `WAKE_ALLOW_OPEN_REGISTRATION=1` says so on purpose; `DASHBOARD_SECRET` at least 32 characters enables the dashboard. `WAKE_TTL_HOURS` sets the thread TTL, `WAKE_DOORBELL_BOTS` / `WAKE_DOORBELL_URL` / `WAKE_DOORBELL_TIMEOUT_MS` / `WAKE_DOORBELL_MAX_PER_HOUR` configure the doorbell, and `WAKE_MCP_ENABLED` / `WAKE_MCP_RATE_LIMIT_PER_MINUTE` the MCP endpoint; a malformed value fails the boot with one line instead of starting half-configured. The API starts only for a database it can migrate; `/health` reports `503` until the migrations are applied.

### MCP

`POST /mcp` speaks MCP 2025-06-18 over Streamable HTTP, so an MCP client can use the thread tools (`wake_ask`, `wake_reply`, `wake_read_thread`, `wake_inbox`, …) without shell access. It is off unless `WAKE_MCP_ENABLED=true`, and the whole public surface has exactly one credential: the `mcp` agent's ordinary agent token, provisioned once with `POST /mcp/identity` and revocable/rotatable from there. Calls are rate limited per token and audited in `mcp_audit` (prune with `POST /mcp/audit/prune`); a bot marked private is invisible and unreachable from the public identity. See [docs/mcp.md](docs/mcp.md).

| Method | Path | What |
| --- | --- | --- |
| `POST` | `/agents` | register a bot, returns its token once |
| `GET` | `/agents` | list bots and who is online |
| `POST` | `/messages` | send one message (`to: "*"` broadcasts) |
| `GET` | `/inbox` | read my inbox, `unread=true` for only unread |
| `POST` | `/inbox/read` | mark inbox messages read |
| `GET` | `/messages/search` | search received or sent history |
| `POST` | `/threads` | ask a bot a question, or open a conversation (`kind: "thread"`) |
| `GET` | `/threads` | my threads, newest creation first, paged |
| `GET` | `/threads/:id` | the whole thread transcript |
| `POST` | `/threads/:id/reply` | answer a thread |
| `POST` | `/threads/:id/read` | move my read position |
| `GET` | `/messages/:id/deliveries` | what the doorbell did for one of my messages |
| `POST` | `/mcp` | MCP 2025-06-18 over Streamable HTTP (off by default) |
| `GET` | `/health` | readiness and database check |

All routes except `/health` and `POST /agents` need `Authorization: Bearer <token>`.


## Install

```sh
git clone <this repo> wake && cd wake
./install.sh                                 # copies bin/* to ~/.local/bin (PREFIX=... to change)
mkdir -p ~/.config/wake
cp bots.example.tsv   ~/.config/wake/bots.tsv     # then list your real bots
cp config.example.env ~/.config/wake/config.env   # then set WAKE_API_BASE etc.
wake --list
```

Put each bot's webhook key in `~/.config/wake/<bot-id>.key` (`chmod 600`), or let `wake --setup <bot>` create it through your key helper (below).

### Every bot needs its own copy

`wake` is only a command. A bot that receives a wake as a webhook run answers a question with `wake --reply <thread-id> "<answer>"`. That only works if `wake` exists **on that bot's own computer**.

Bots with separate computers do not share a home folder, so installing `wake` on one machine does not install it on another. A bot without it gets "command not found" on `--reply` and will usually answer in its own chat instead. The asker sees no reply on the thread, and the user gets an unexpected message.

Checklist for each bot (do this when you add a bot to `bots.tsv`):

1. Run `./install.sh` on that bot's computer, so `wake` is on its PATH.
2. Put the keys in `WAKE_KEY_DIR`. Keys are per target bot, so the replying bot needs the key for whichever bot it is answering, not only its own.
3. Add a rule to the bot's webhook routine: *if `ref` starts with `thread:`, answer only with `wake --reply <id> "<short answer>" --source bot:<alias>` and send no other message.* Without this rule a bot may treat the payload as untrusted text and report in its own chat.
4. Verify: run `command -v wake` on that computer, send it `wake --ask <alias> "ping" --wait 60`, and confirm a reply appears with `wake --thread <id>`.

Symptom of a missing install: the thread file shows the ask and the wake, but no `reply` record.

### Configuration

Every setting is an environment variable; the same names can be set in `~/.config/wake/config.env` (`KEY=VALUE`). The environment wins.

| variable | default | meaning |
|---|---|---|
| `WAKE_API_BASE` | *(none, required to send)* | webhook prefix; wake POSTs to `$WAKE_API_BASE/<bot-id>/webhook` |
| `WAKE_MCP_API` | `http://127.0.0.1:3001` | API base for the `wake-mcp` stdio bridge; it POSTs to `$WAKE_MCP_API/mcp`. `https` for any non-loopback host: the token is a bearer credential |
| `WAKE_MCP_TOKEN` | *(empty: legacy two-tool mode)* | the `mcp` identity's token; with one set, `wake-mcp` bridges to `/mcp` |
| `WAKE_CONFIG_DIR` | `~/.config/wake` | where `config.env`, `bots.tsv` and keys live by default |
| `WAKE_CONFIG` | `$WAKE_CONFIG_DIR/config.env` | config file path |
| `WAKE_REGISTRY` | `$WAKE_CONFIG_DIR/bots.tsv` | bot registry |
| `WAKE_KEY_DIR` | `$WAKE_CONFIG_DIR` | `<bot-id>.key` files |
| `WAKE_STATE_DIR` | `/var/lib/wake` | shared `threads/` and `inbox/`; a deployment may patch the host copy to a site directory of its own |
| `WAKE_HOSTOPS` | *(empty: this is the host)* | guest mode: executable that runs `$1` as a shell command on the host, stdin passed through |
| `WAKE_KEY_HELPER` | `wake-webhook-key` | host command that mints keys (see protocol doc) |
| `WAKE_GROUP` | `wake` | group allowed to read keys; used by the `sg` fallback |
| `WAKE_REGISTRY_GROUP` | *(empty: unchanged)* | if set, `--sync` gives the host registry copy this group |
| `WAKE_RATE_DIR` | `/tmp` | holds the per-user rate log `wake-rate-<uid>.json` |
| `WAKE_BOT` | `coordinator` | default bot for `wake-hook`, `wake-run` and the legacy form |
| `WAKE_SOURCE` | *(derived)* | sender label `kind:name`, e.g. `bot:engineer`, `skill:agent` |
| `WAKE_INBOX` | *(none)* | this session's inbox file; set by a session extension |
| `WAKE_DEPTH` | `0` | wake nesting depth; raised by `wake-run` for child commands |
| `WAKE_TTL_HOURS` | `24` | a thread older than this refuses answers (`0` = never expire) |
| `WAKE_HOOK` | *(off)* | `errors` or `all` turns `wake-hook` on |

## Usage

```sh
wake --list                         # every bot: alias, name, kind, role
wake --who dns nginx                # bots whose role matches
wake --info engineer                # id, kind, role and how to reach it
wake --me                           # which bot this shell is (cwd .../bots/<id>), and the sender label
wake --version                      # e.g. "wake 0.2.0"; the same version is sent as User-Agent wake-cli/<version>

wake engineer "PR #42 is ready"     # one-way notify
wake                                # interactive picker (on a TTY)
```

`--me` and the default sender label recognise a bot by its working directory: a path containing `/bots/<bot-id>` means "this shell is that bot". That layout comes from one bot platform. Anywhere else, set `WAKE_SOURCE=bot:<alias>` (or pass `--source`).

### `--ask`: ask and get an answer

```sh
wake --ask engineer "why does CI fail on main?"            # opens a thread, wakes the bot
wake --ask engineer "why does CI fail on main?" --wait 180 # headless: block until a reply (exit 3 on timeout)
wake --ask engineer "..." --no-send                        # record the thread only (dry run)
```

The bot receives `<question> [thread <id>] reply: wake --reply <id> "YOUR ANSWER"`.

### `--reply`: answer a thread

```sh
wake --reply 1114-a3f2 "flaky test, fixed in #43"
```

Only the thread's target (`reply_to`) or its owner may write to it (`--force` overrides). When the target answers:
- owner is a bot → that bot's webhook is woken with event `reply` (always for bot senders, or with `--notify`);
- owner is a human or session → the reply is appended to the owner's live inbox, if its process is still running.

### `--inbox` and `--thread`

```sh
wake --inbox            # threads opened by $WAKE_SOURCE, with their answers
wake --thread 1114-a3f2 # the full exchange
```

### `--setup`: create a webhook key

```sh
wake --setup engineer            # mint a key via $WAKE_KEY_HELPER, save it 600, enable the bot's webhook routine
wake --setup engineer --rotate   # replace it (the old key stops working)
```

If no key is saved and no key helper is reachable, wake exits with a clear message naming the missing file and the command an admin should run.

### Registry maintenance

```sh
wake --add ops2 bot-ops2-0009 "Ops Bot 2" open "second ops bot"   # append a row here (then sync on the host)
wake --sync                                                       # guest: print the rows, write nothing
```

A guest shell (bot computer, `WAKE_HOSTOPS` set) does not push the registry. It prints the rows it would
have written, says `registry sync is an operator action; run it on the host`, and exits 1. A guest ingests
untrusted content, and the bot list is the routing table every bot trusts: rewriting it needs privileges a
bot key must not carry. On the host the registry is already the live copy, so there is nothing to push.

### `--update`, `--ping`, `--all`: every bot at once

```sh
wake --update            # pull missing keys, print key state per bot (registry push is refused in a guest)
wake --update --ping     # also send a quiet ping to every open bot with a readable key
wake --all "read-only window until 18:00"   # one message to every open bot; private bots and yourself are skipped
```

`--update` never guesses: a bot whose key was made elsewhere is shown as `elsewhere`, one with an unreadable key as `locked`, and one with no key as `none`. `--all` prints a per-bot line and a `N sent, M failed` summary, and exits 1 if any send failed. `--update` also exits 1 in a guest, because the registry push it would do is an operator action.

### Helpers

- `wake-run [--bot BOT] [--only-errors] [--name label] -- cmd...` — run a command, wake a bot when it ends (`ok` on exit 0, `error` otherwise). Child commands see `WAKE_DEPTH+1`.
- `wake-hook claude-code Stop|StopFailure` (hook JSON on stdin) or `wake-hook codex '<json>'` — turn coding-agent hook events into wakes. Off unless `WAKE_HOOK=errors|all`.
- `wake-mcp` — MCP stdio server. With `WAKE_MCP_TOKEN` set it is a bridge to `POST /mcp`, so a stdio-only client gets the same ten thread tools, rate limit, audit and private-bot wall as an HTTP client, plus `wake_fleet` from this machine's `bots.tsv`; with no token, and with neither `WAKE_CONFIG` nor `WAKE_CONFIG_DIR` naming a config, it keeps the legacy `wake_bot` and `wake_list` tools that shell out to `wake`. A config named by either variable that resolves to a missing, unreadable or tokenless file now exits 2 instead of quietly serving those two tools.
- `wake-inbox-io register <hash> [json] | read <inbox> | beat <hash> [json]` — session inbox plumbing for a session extension. `<hash>` is 6 to 32 lowercase hex characters; `read` only accepts `$WAKE_STATE_DIR/inbox/<hash>.jsonl`.
- Every command above answers `-h`/`--help` with its usage, its exit codes and the environment it
  reads, before it does anything else. `tests/test_command_help.py` runs each one in an empty
  environment and fails if that stops being true.
- [`docs/agent-skill/SKILL.md`](docs/agent-skill/SKILL.md) — a skill file that teaches an agent when and how to use wake (`SKILL_DIR=... ./install.sh`).

## Security model

- **Per-bot keys.** Each target bot has its own webhook key. Holding a key lets you wake that one bot and nothing else. Keys are written with mode 600; a shared group (`WAKE_GROUP`) can be granted read access. A session started before its user joined that group retries the read through `sg $WAKE_GROUP`; if that fails too, wake stops with a clear error.
- **Sender labels are labels, not proof.** `source` (`bot:x`, `human:y`, `skill:z`, `harness:w`) comes from `--source`, `WAKE_SOURCE`, the working directory, or `$USER`. The receiving bot must treat a wake as a teammate's request, never as authorisation for merges, deploys or anything irreversible.
- **Private bots** (`kind=private` in the registry) are refused unless `--allow-private` is given, and are listed last in role matches.
- **Thread ownership.** Only the owner or the target may append to a thread; thread ids with `/` or `..` are rejected.
- **Payload context.** Every wake appends `host=<hostname> user=<user> cwd=<cwd>` to `detail` so the receiver can see where it came from. Do not use wake from a directory whose path is itself sensitive.
- **Shared state.** The store is group-owned and group-writable: `$WAKE_STATE_DIR` and its `threads/` and
  `inbox/` dirs are `root:wake 2770` (setgid, so new files inherit the `wake` group); `threads/` is
  `root:wake 3770` — the same, plus sticky, so only a file's owner can unlink it — and thread files
  are `660`. Every bot account that appends is in the `wake` group. `wake` never changes these modes, so
  an append cannot widen a store an admin tightened. Anyone in the group can read and forge thread
  lines; keep untrusted users out of it.
- **Host commands.** Thread and inbox I/O runs as shell commands on the host: through `WAKE_HOSTOPS` from a guest, or `bash -lc` (a login shell, so profiles are sourced) on the host itself. Every value placed in those commands is quoted with `shlex.quote`; thread ids and inbox hashes are validated first.
- **Nothing secret in messages.** Messages go to bot runs and logs. Do not send keys, tokens or personal data.

Report vulnerabilities privately; see [SECURITY.md](SECURITY.md).

## Loop guard

Bots that wake bots can loop. Two limits apply to every send:

1. **Depth.** `WAKE_DEPTH` is sent as `depth` in every payload, and `wake-run` raises it by one for the command it runs. A wake at depth 2 or more is refused (`refused: wake depth 2 >= 2 (loop guard)`). Receivers should export the incoming `depth` as `WAKE_DEPTH` for the run it starts.
2. **Rate.** At most 30 wakes per target bot per hour per Unix user, tracked in `$WAKE_RATE_DIR/wake-rate-<uid>.json`.

Depth alone does not stop an ask/reply ping-pong, because every reply is a fresh wake. Two thread rules close that:

- **One round.** A thread takes one answer. Once it has a reply, the asker cannot answer back on the same thread (`one round only, so A to B to A cannot ping-pong`); ask again with `wake --ask` if there is more to say.
- **TTL.** A thread older than `WAKE_TTL_HOURS` (default 24) refuses answers, because the asker has already moved on and the answer would land unread (`thread ... is 72h old`). `WAKE_TTL_HOURS=0` turns this off.

`--force` skips all of these. Agents are told not to use it: if the guard trips, stop and tell a human.

Every ask, reply and refusal is appended to `$WAKE_RATE_DIR/wake-audit-<uid>.log` as one JSON line (`cmd`, `source`, `bot`, `thread`, `rc`, `text`), so a refused or failed answer is visible instead of looking like a delivered one. The log never holds key material.

Each request also carries an `Idempotency-Key` derived from bot, source, event, detail and a 10-minute bucket, so a retried identical wake can be deduplicated by the server.

## Agent skills

`skills/` holds Claude Agent Skills — a `SKILL.md` whose frontmatter carries `name` and
`description`, then the recipe — that teach an assistant to operate wake instead of
rediscovering it. They are plain files: copy the directory you want into your agent's
skills directory.

| Skill | Covers |
|---|---|
| `skills/wake-ops` | the `bin/ops` helpers: a scratch database, the HTTP end-to-end run, host-minted keys, a fleet ping, a script install, the service install, and a gate review |
| `skills/wake-mcp` | the stdio bridge, the public `/mcp` endpoint, its single identity, and how to read a 401/404/405 |
| `skills/wake-deploy` | scripts, the systemd service, the one nginx location, the check from outside, and an exact rollback |

The bundled `docs/agent-skill/SKILL.md`, installed by `SKILL_DIR=… ./install.sh`, stays
the short "talk to the other bots" skill.

## Development

```sh
tests/run.sh                      # python3 from PATH
PYTHON=python3.12 tests/run.sh    # a specific interpreter; the bin/ scripts run under it too
uv run --with ruff ruff check . && uv run --with ruff ruff format --check .
shellcheck install.sh tests/run.sh scripts/scrub-check.sh $(grep -lE '^#!/(usr/)?bin/(env )?(ba)?sh' bin/ops/wake-* bin/wake*)
scripts/scrub-check.sh --history  # fails on IPs other than 127.0.0.1, emails and token-like strings

bun install                       # API and MCP workspace
bun run typecheck
bun run lint                      # eslint 10 needs node >= 20; bun runs everything else
TEST_DATABASE_URL=postgres://postgres@127.0.0.1:5432/wake_test bun run test
bun run build
DATABASE_URL=postgres://postgres@127.0.0.1:5432/wake_e2e_test bun scripts/e2e-threads.ts
```

The API tests need a real Postgres and truncate every table between tests, so the database name must end in `_test`; the suite refuses anything else. CI runs them against a `postgres:17` service. `scripts/e2e-threads.ts` boots the real API on a database of its own and drives it over HTTP; the database can be empty, because the API applies its migrations on first use.

Tests are offline: a fake webhook server on 127.0.0.1 records every request; state, keys, registry and rate log live in a temporary directory.

Run the suite as a non-root user. As root, the two `sg` fallback tests are skipped with a reason (root reads any file, so the fallback is never reached) and `run.sh` prints a note.

CI (`.github/workflows/ci.yml`) runs the tests on Python 3.9 to 3.13, ruff, shellcheck, `compileall`, the secret scan, the API suite and the threads e2e on every push and pull request. The test job fetches tags as well as history, because `tests/test_publish_checklist.py` compares the release line in `docs/PUBLISH-CHECKLIST.md` with the commit the tag points at. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Changelog

See [CHANGELOG.md](CHANGELOG.md). The version is `VERSION` in `bin/wake_config.py`.

## License

MIT, see [LICENSE](LICENSE).
