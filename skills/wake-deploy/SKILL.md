---
name: wake-deploy
description: "Put wake on a machine and prove it: install the scripts locally or over ssh, run the API as a native systemd service, publish exactly one nginx location for `/mcp`, verify from the outside, and roll back with an exact database restore. Use when installing or upgrading the host, when the service will not start, when nginx or the environment file is being changed, or before a redeploy that needs a database dump."
---

# Deploying wake

`deploy/README.md` is the runbook and it is the source of truth; this is the order of
operations and the traps. Do the steps in order — each one is verifiable alone.

## 1. Scripts

```bash
./install.sh                        # into ~/.local/bin
PREFIX=/usr/local sudo ./install.sh  # system-wide
OPS=1 ./install.sh                   # also the bin/ops helpers
wake-deploy ssh:deploy-host --prefix /usr/local/bin   # another machine, one tar stream, verified there
```

## 2. The API as a service

```bash
wake-host-deploy build install env unit status
```
- service account `wake` (nologin), no file it serves is writable by it;
- `/opt/wake` holds `index.mjs`, `node` and `migrations/` at `root:wake`;
- `/etc/wake/api.env` is `root:wake 640` and holds the `DATABASE_URL` and the
  `REGISTRATION_SECRET`; systemd drops one pair of quotes and so do the operator
  scripts, so either form works — unquoted is one less thing to explain;
- the unit restarts on failure and carries `ProtectSystem=strict`; the only filesystem
  calls under `apps/` and `packages/` are reads, so it needs no `ReadWritePaths`.

## 3. The public endpoint

One exact location, `location = /mcp`, with `client_max_body_size 64k` — the app caps a
body at 32 KiB and nginx spools the body to disk before the app can answer 401. A
prefix `location /mcp` publishes the operator routes (`/mcp/identity`,
`/mcp/identity/rotate`, `DELETE /mcp/identity`, `/mcp/audit/prune`) and
`tests/test_deploy_docs.py` fails on it.

## 4. Verify, from the outside

```bash
curl -fsS http://127.0.0.1:3001/health        # the app answers on loopback
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://…/mcp   # 401 without a token, 200 with it
ss -ltnp | grep 3001                          # exactly one listener, on 127.0.0.1
```
`GET /mcp` → **405** is expected. A **404** from outside means the endpoint is not
published — which is also what a stopped service looks like, so check the listener
before blaming nginx.

## 5. Before a redeploy: dump

```bash
sudo install -d -o postgres -g postgres -m 700 /var/backups/wake
sudo -u postgres pg_dump -Fc wake > /var/backups/wake/wake-$(date +%F-%H%M).dump
sudo -u postgres pg_restore -l /var/backups/wake/wake-….dump | head   # is the archive readable
```
`pg_restore -c` is **not** an exact rollback: it drops what the archive has and leaves
objects a later migration added. For exactness, `dropdb` + `createdb -O wake` +
`pg_restore` into an empty database.

## 6. Traps

- A saved nginx config from before the hardening keeps the **prefix** location and the
  old body ceiling. Restoring it re-publishes the operator routes; copy the block from
  the runbook instead of restoring the file.
- `git`-tracked copies and the host copy can drift: the host's `wake` may point at a
  different state directory. Check `/usr/local/bin/wake` and `WAKE_STATE_DIR` before
  concluding the CLI is broken.
- Never rotate a credential you cannot read back. If a key was made elsewhere, mint a
  new one and retire the old through its own tool.