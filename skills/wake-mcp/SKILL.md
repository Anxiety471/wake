---
name: wake-mcp
description: "Run and check wake's MCP server: the stdio bridge (`bin/wake-mcp`) that local MCP clients start, the public `/mcp` endpoint and its single identity, token handling, and the operator routes that must stay loopback-only. Use when a client cannot see the tools, a `/mcp` call answers 401 or 404, an identity needs creating, rotating or revoking, or nginx is being changed around the endpoint."
---

# wake over MCP

Two modes, one file:

- **with a token** (`WAKE_MCP_TOKEN`) `bin/wake-mcp` is a stdio server that bridges
  each call to `POST {WAKE_MCP_API}/mcp` and exposes the server's ten thread tools;
- **without one** it is the legacy two-tool CLI helper, and only when nothing names
  a config (`WAKE_CONFIG`, or `WAKE_CONFIG_DIR`, which names `config.env` inside it):
  a named file that is missing, unreadable or tokenless exits 2 with the path
  instead of quietly answering with two tools.

```json
{ "mcpServers": { "wake": { "command": "wake-mcp",
  "env": { "WAKE_MCP_API": "http://127.0.0.1:3001", "WAKE_MCP_TOKEN": "..." } } } }
```
Plain http only for loopback: any other host needs https, or the token crosses in
clear. Keep the config file at mode 600.

## The public endpoint

One exact location, nothing else:

```nginx
location = /mcp {
    client_max_body_size 64k;   # the app caps a body at 32 KiB; this bounds what nginx reads first
    proxy_pass http://127.0.0.1:3001;
}
```
A prefix `location /mcp` also publishes `/mcp/identity`, `/mcp/identity/rotate`,
`DELETE /mcp/identity` and `/mcp/audit/prune` — the operator routes — to the
internet. `tests/test_deploy_docs.py` fails on that shape.

## The single identity

```bash
python3 deploy/ops/mcp-identity.py create     # 201 + token file 0600, once per database
python3 deploy/ops/mcp-identity.py rotate     # 200, the old token dies at once, revives a revoked one
python3 deploy/ops/mcp-identity.py revoke     # 200, repeatable; the row stays, so create would be 409
python3 deploy/ops/mcp-identity.py show       # tokenRevoked, never the token
python3 deploy/ops/mcp-verify.py --url https://…/mcp --token-file …   # initialize, tools/list, one call
```
`create` is single-use per database: after a `revoke` the row survives, so a second
`create` answers `409 name already registered` — `rotate` is the way back.

## Reading a failure

- **401** on every call, API healthy: the value is not the secret the running service
  loaded - a stale edit the unit has not reloaded (`systemctl restart wake-api`), a
  different file from the unit's `EnvironmentFile`, or a secret rotated in one place only.
  Quoting is not the cause: systemd and the operator scripts both drop one matched pair.
- **404** on `/mcp` from outside: either the endpoint is unpublished (correct when the
  service is stopped or the location was removed) or the location does not match the
  path, which is exactly what an unpublished endpoint looks like. Check `ss -ltnp`
  for the port before blaming nginx.
- **405** on `GET /mcp`: expected with a valid token - the endpoint takes POST.
  With no token the same request answers **401** first, because auth runs before
  the method check.
- **`-32603` from the bridge**: the server refused the token mid-session; check
  `show` for `tokenRevoked`.
