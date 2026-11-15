---
name: wake-ops
description: "Operate a wake install from the shell: a throwaway Postgres for the API tests (`wake-pg`), a real-HTTP end-to-end run (`wake-api-e2e`), host-minted webhook keys (`wake-keys`), one ask to every open bot (`wake-pingall`), a script install locally or over ssh (`wake-deploy`), the API as a systemd service (`wake-host-deploy`), and a gate review before a gate is installed (`wake-gate-check`, `wake-shape-check`). Use when proving the API still works, moving scripts to another machine, installing or restarting the service, or checking that a gate will not refuse the commands wake sends."
---

# Operating wake

`bin/ops` holds the helpers around the five core scripts. `OPS=1 ./install.sh` puts
them next to the core scripts; they also run straight from a checkout. Every
host-specific value is a `WAKE_*` variable, documented in `bin/ops/README.md` and
`config.example.env`.

## Prove the API before you ship

```bash
wake-pg start && wake-pg status          # scratch Postgres on 5433, its own data dir
wake-api-e2e                             # real API on that database, PASS per step, ALL PASS at the end
wake-pg stop
```
`wake-api-e2e` covers `/mcp` initialize, the ten tools, `wake_ask` and
`wake_read_thread`, the inbox, deliveries, private-bot refusal, `mcp_audit`, audit
prune, `GET /mcp` 405 and the stdio bridge. It generates its own
`REGISTRATION_SECRET` and keeps every file it writes in one private `mktemp -d`
directory. One run at a time.

## Move the scripts, or install the service

```bash
wake-deploy local --prefix ~/.local/bin           # this machine
wake-deploy ssh:deploy-host --prefix /usr/local/bin   # sudo needed: WAKE_SSH_SUDO=1
wake-host-deploy status                           # is the service enabled, active, answering
wake-host-deploy build install env unit           # first install, one attributable step at a time
```
`wake-deploy` sends everything as one tar stream and verifies the copy by running
`wake --help` there. `wake-host-deploy` needs `WAKE_HOSTOPS` or `WAKE_HOST_SSH`, and
takes `WAKE_DB_PASSWORD` / `WAKE_REGISTRATION_SECRET` from the environment: they go
through stdin, so they never appear on a command line. `--dry-run` first on a host
you care about.

## Check a gate before it goes live

```bash
wake-gate-check "$WAKE_GATE" --selftest check.py --harness attack.py
wake-shape-check bin/wake "$WAKE_GATE"
```
A gate refuses anything but the exact shapes wake sends, so a shape it refuses is a
call that fails silently. `wake-shape-check` lifts the five builders out of a wake
copy, runs them against stubs and prints every command with the verdict; exit 1 means
something would be refused. Neither helper touches a host.

## Publish a change to the fleet

```bash
wake-keys                    # host-minted <bot-id>.key into the local key dir, mode 600, value never printed
wake-pingall                 # wake --ask every open bot in the registry, one log each
```

## Rules

- Never print a key, a token or a secret: these helpers report `ok` / `skip` / `bad` /
  `none` / `err` and never the value.
- `wake-host-deploy` changes a host. Read the step list and run `--dry-run` before a
  step that installs, rewrites `/etc/wake/api.env` or restarts the unit.
- One e2e at a time, and stop the scratch Postgres when you are done.