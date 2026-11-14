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
| `WAKE_CONFIG_DIR` | `~/.config/wake` | where `config.env`, `bots.tsv` and keys live by default |
| `WAKE_CONFIG` | `$WAKE_CONFIG_DIR/config.env` | config file path |
| `WAKE_REGISTRY` | `$WAKE_CONFIG_DIR/bots.tsv` | bot registry |
| `WAKE_KEY_DIR` | `$WAKE_CONFIG_DIR` | `<bot-id>.key` files |
| `WAKE_STATE_DIR` | `/var/lib/wake` | shared `threads/` and `inbox/` (on the host) |
| `WAKE_HOSTOPS` | *(empty: this is the host)* | guest mode: executable that runs `$1` as a shell command on the host, stdin passed through |
| `WAKE_KEY_HELPER` | `wake-webhook-key` | host command that mints keys (see protocol doc) |
| `WAKE_GROUP` | `wake` | group allowed to read keys; used by the `sg` fallback |
| `WAKE_REGISTRY_GROUP` | *(empty: unchanged)* | if set, `--sync` gives the host registry copy this group |
| `WAKE_RATE_DIR` | `/tmp` | holds the per-user rate log `wake-rate-<uid>.json` |
| `WAKE_BOT` | `coordinator` | default bot for `wake-hook`, `wake-run` and the legacy form |
| `WAKE_SOURCE` | *(derived)* | sender label `kind:name`, e.g. `bot:engineer`, `skill:agent` |
| `WAKE_INBOX` | *(none)* | this session's inbox file; set by a session extension |
| `WAKE_DEPTH` | `0` | wake nesting depth; raised by `wake-run` for child commands |
| `WAKE_HOOK` | *(off)* | `errors` or `all` turns `wake-hook` on |

## Usage

```sh
wake --list                         # every bot: alias, name, kind, role
wake --who dns nginx                # bots whose role matches
wake --info engineer                # id, kind, role and how to reach it
wake --me                           # which bot this shell is (cwd .../bots/<id>), and the sender label
wake --version                      # e.g. "wake 0.1.0"; the same version is sent as User-Agent wake-cli/<version>

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
- `wake-mcp` — MCP stdio server with tools `wake_bot` and `wake_list`.
- `wake-inbox-io register <hash> [json] | read <inbox> | beat <hash> [json]` — session inbox plumbing for a session extension. `<hash>` is 6 to 32 lowercase hex characters; `read` only accepts `$WAKE_STATE_DIR/inbox/<hash>.jsonl`.
- [`docs/agent-skill/SKILL.md`](docs/agent-skill/SKILL.md) — a skill file that teaches an agent when and how to use wake (`SKILL_DIR=... ./install.sh`).

## Security model

- **Per-bot keys.** Each target bot has its own webhook key. Holding a key lets you wake that one bot and nothing else. Keys are written with mode 600; a shared group (`WAKE_GROUP`) can be granted read access. A session started before its user joined that group retries the read through `sg $WAKE_GROUP`; if that fails too, wake stops with a clear error.
- **Sender labels are labels, not proof.** `source` (`bot:x`, `human:y`, `skill:z`, `harness:w`) comes from `--source`, `WAKE_SOURCE`, the working directory, or `$USER`. The receiving bot must treat a wake as a teammate's request, never as authorisation for merges, deploys or anything irreversible.
- **Private bots** (`kind=private` in the registry) are refused unless `--allow-private` is given, and are listed last in role matches.
- **Thread ownership.** Only the owner or the target may append to a thread; thread ids with `/` or `..` are rejected.
- **Payload context.** Every wake appends `host=<hostname> user=<user> cwd=<cwd>` to `detail` so the receiver can see where it came from. Do not use wake from a directory whose path is itself sensitive.
- **Shared state.** The store is group-owned and group-writable: `$WAKE_STATE_DIR` and its `threads/` and
  `inbox/` dirs are `root:wake 2770` (setgid, so new files inherit the `wake` group) and thread files are
  `660`. Every bot account that appends is in the `wake` group. `wake` never changes these modes, so
  an append cannot widen a store an admin tightened. Anyone in the group can read and forge thread
  lines; keep untrusted users out of it.
- **Host commands.** Thread and inbox I/O runs as shell commands on the host: through `WAKE_HOSTOPS` from a guest, or `bash -lc` (a login shell, so profiles are sourced) on the host itself. Every value placed in those commands is quoted with `shlex.quote`; thread ids and inbox hashes are validated first.
- **Nothing secret in messages.** Messages go to bot runs and logs. Do not send keys, tokens or personal data.

Report vulnerabilities privately; see [SECURITY.md](SECURITY.md).

## Loop guard

Bots that wake bots can loop. Two limits apply to every send:

1. **Depth.** `WAKE_DEPTH` is sent as `depth` in every payload, and `wake-run` raises it by one for the command it runs. A wake at depth 2 or more is refused (`refused: wake depth 2 >= 2 (loop guard)`). Receivers should export the incoming `depth` as `WAKE_DEPTH` for the run it starts.
2. **Rate.** At most 30 wakes per target bot per hour per Unix user, tracked in `$WAKE_RATE_DIR/wake-rate-<uid>.json`.

`--force` skips both. Agents are told not to use it: if the guard trips, stop and tell a human.

Each request also carries an `Idempotency-Key` derived from bot, source, event, detail and a 10-minute bucket, so a retried identical wake can be deduplicated by the server.

## Development

```sh
tests/run.sh                      # python3 from PATH
PYTHON=python3.12 tests/run.sh    # a specific interpreter; the bin/ scripts run under it too
uv run --with ruff ruff check . && uv run --with ruff ruff format --check .
shellcheck install.sh tests/run.sh scripts/scrub-check.sh
scripts/scrub-check.sh --history  # fails on IPs other than 127.0.0.1, emails and token-like strings
```

Tests are offline: a fake webhook server on 127.0.0.1 records every request; state, keys, registry and rate log live in a temporary directory.

Run the suite as a non-root user. As root, the two `sg` fallback tests are skipped with a reason (root reads any file, so the fallback is never reached) and `run.sh` prints a note.

CI (`.github/workflows/ci.yml`) runs the tests on Python 3.9 to 3.13, ruff, shellcheck, `compileall` and the secret scan on every push and pull request. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Changelog

See [CHANGELOG.md](CHANGELOG.md). The version is `VERSION` in `bin/wake_config.py`.

## License

MIT, see [LICENSE](LICENSE).
