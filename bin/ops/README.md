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
| `wake-gate-check` | Reviews a gate artifact before it replaces the live one: sha256 and size, then its own selftest, an adversarial harness and an e2e run through a copied checkout, each step only when its file is set. A gate decides which shell commands wake may send, so it is reviewed before it is installed. | `python3` and the gate file. `WAKE_GATE_SELFTEST`, `WAKE_GATE_HARNESS`, `WAKE_GATE_E2E` and `WAKE_REPO` stand in for `--selftest`, `--harness`, `--e2e` and `--repo`. |
| `wake-shape-check` | Lifts the five command builders out of a wake copy, runs them against stubs and prints every command with the gate's verdict. A shape the gate refuses is a call that fails silently, which is why a new build is checked before it is installed. | `python3`, a wake file and a gate (argument or `WAKE_GATE`). It runs no host command and touches no state directory. |
| `wake-deploy` | Installs a checkout's scripts — the five core files, `wake_config.py` and every helper in `bin/ops` — into a prefix on this machine or on an `ssh:DEST` host, as one tar stream, then verifies the copy by running `wake --help` there. Optionally copies the two agent extension files. | `python3`, and `ssh` for a remote target. `WAKE_PREFIX` moves the install dir, `WAKE_DEPLOY_TARGETS` sets the default targets, `WAKE_SSH_SUDO=1` prefixes the remote install with `sudo -n`, `WAKE_REPO` names the checkout and `WAKE_EXTENSION_SRC` / `WAKE_EXTENSION_DIRS` copy the extension files. Registering those extensions in your agent's `config.yml` stays a manual step. |
| `wake-host-deploy` | Puts the API on a host as a native systemd service, one attributable step at a time: `db`, `user`, `build`, `install`, `env`, `unit`, `status`, `log`. Every step is idempotent, so a failed run is re-run rather than repaired by hand. | `python3`, a checkout (`WAKE_REPO`), a builder (`WAKE_BUILDER`), and one transport: `WAKE_HOSTOPS` or `WAKE_HOST_SSH`. `WAKE_DB_PASSWORD` and `WAKE_REGISTRATION_SECRET` for the `env` step: they are read from the environment and written through stdin, never printed. It changes a host, so read the step list before running one. |

`WAKE_CONFIG_DIR` (default `~/.config/wake`) holds the registry, the same default `bin/wake` uses
(`bin/wake_config.py`). `WAKE_KEY_DIR` defaults to it and only moves the saved keys, so setting it
never hides the registry from these helpers.

Every host-specific value comes from an environment variable, never from a
literal, so one file works on the host, in a container or on a laptop.
`tests/test_ops_scripts.py` fails if a script grows a personal absolute path, a
real IP address, a real hostname or a `WAKE_*` variable that is not documented here.

## Deploy and review

The four helpers above that touch a host take every host-specific value from the
environment, so the same file works on the host, in a container or on a laptop:
`WAKE_HOSTOPS` (a runner that executes one shell command) or `WAKE_HOST_SSH` (an ssh
destination), `WAKE_SUDO`, `WAKE_PREFIX`, `WAKE_SERVICE`, `WAKE_INSTALL_DIR`,
`WAKE_ENV_FILE`, `WAKE_UNIT_DIR`, `WAKE_DB_NAME`, `WAKE_DB_ROLE`, `WAKE_DB_HOST`,
`WAKE_DB_PORT`, `WAKE_PORT`, `WAKE_BIND`, `WAKE_BUILDER`, `WAKE_NODE`, `WAKE_GATE`,
`WAKE_GATE_SELFTEST`, `WAKE_GATE_HARNESS`, `WAKE_GATE_E2E`, `WAKE_DEPLOY_TARGETS`,
`WAKE_SSH_SUDO`, `WAKE_EXTENSION_SRC` and `WAKE_EXTENSION_DIRS`. Secrets —
`WAKE_DB_PASSWORD`, `WAKE_REGISTRATION_SECRET` — come from the environment and are
never written into a command or into this repository.

`wake-deploy` and `wake-host-deploy` ship as code. Nothing in this repository runs
them against a host: the tests drive them with a stubbed `ssh`, a stubbed host
runner and a stubbed builder, which is what proves that a secret travels through
stdin and that a remote copy is verified without a remote.
