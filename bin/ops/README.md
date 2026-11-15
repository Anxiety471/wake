# bin/ops — operator helpers

Small commands for the person who runs a wake install. They are not part of the
five core scripts (`bin/wake`, `bin/wake-hook`, `bin/wake-run`, `bin/wake-mcp`,
`bin/wake-inbox-io`): the core scripts are what every user installs, these are
the tools around them.

`OPS=1 ./install.sh` installs them next to the core scripts (`$PREFIX/bin`).
They also run straight from a checkout, which is what `wake-api-e2e` expects.

| Command | What it does | Needs |
|---|---|---|
| `wake-pg` | A throwaway local Postgres for the API tests: `start`, `stop`, `status`, `reset`, `createdb NAME`, `dropdb NAME`, `psql ...`. | Postgres binaries on `PATH`, or `WAKE_PG_BASE` for a prefix that carries them (a prefix with only the server binaries works: the client tools fall back to `PATH`). `WAKE_PG_DATA`, `WAKE_PG_PORT` (5433), `WAKE_PG_SOCK` and `WAKE_PG_LOG_DIR` move the data, port, socket and logs. |
| `wake-api-e2e` | Runs the real API on a scratch database and proves the public surface over HTTP: `/mcp` initialize, 10 tools, `wake_ask` and `wake_read_thread`, the inbox, deliveries, private-bot refusal, `mcp_audit` rows, audit prune, `GET /mcp` 405, and the stdio bridge in `bin/wake-mcp`. Prints `PASS` per step and `ALL PASS`, or stops at the step that failed. | `bun`, `jq`, `curl`, `openssl`, `python3` and `wake-pg` on `PATH` (or `WAKE_PG`); a checkout (or `WAKE_REPO`). `WAKE_E2E_DB`, `WAKE_E2E_PORT`, `WAKE_E2E_LOG`, `WAKE_E2E_DATABASE_URL` and `WAKE_E2E_SECRET` move the scratch database, port, log, connection string and secret; the secret is generated per run, so none is written in the file. Every other file the run writes (request bodies, the bot and MCP tokens) lands in one `mktemp -d` directory at mode 0700 under `umask 077` and is removed on exit, failed or not. |
| `wake-keys` | Copies the webhook keys that were minted on the host into this machine's key dir (`<bot-id>.key`, mode 600) and prints `ok`, `skip`, `bad`, `none` or `err` per bot. Never prints a key value. `--force` re-copies. Exits non-zero if a key file is present but unreadable, or if a fetch fails: a dead host or a refused account prints `err` with the host's first line, never `none`, because only "no such file" means the key really is absent. | A registry (`WAKE_REGISTRY`, default `$WAKE_CONFIG_DIR/bots.tsv`) and either `WAKE_HOSTOPS` or a readable key dir (`WAKE_HOST_KEY_DIR`, default `WAKE_STATE_DIR`). |
| `wake-pingall` | `wake --ask` every open bot in the registry at once, one log per bot; pass aliases as arguments to pick bots instead. | A registry (`WAKE_REGISTRY`, default `$WAKE_CONFIG_DIR/bots.tsv`), `wake` on `PATH`, plus `WAKE_PING_LOG_DIR`, `WAKE_PING_TIMEOUT` (240 s) and `WAKE_PING_MESSAGE`. |

`WAKE_CONFIG_DIR` (default `~/.config/wake`) holds the registry, the same default `bin/wake` uses
(`bin/wake_config.py`). `WAKE_KEY_DIR` defaults to it and only moves the saved keys, so setting it
never hides the registry from these helpers.

Every host-specific value comes from an environment variable, never from a
literal, so one file works on the host, in a container or on a laptop.
`tests/test_ops_scripts.py` fails if a script grows a personal absolute path, a
real IP address, a real hostname or a `WAKE_*` variable that is not documented here.

## Not published

Four helpers we use locally stay out of this repo:

- `wake-gate-check` and `wake-shape-check` check a *staged* host gate, and their
  gate artifact and checker scripts live outside this repo. The public half of
  the same idea is `tests/test_gate_shapes.py`, which honours `WAKE_GATE`.
- `wake-deploy` and `wake-host-deploy` push this machine's copies to a host as a
  named account. They carry a home directory, a host account name and a real SSH
  destination, and CONTRIBUTING forbids committing those.

Issue #24 records what each one would need to become publishable.