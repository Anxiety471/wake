# The MCP endpoint

`POST /mcp` speaks MCP **2025-06-18** over **Streamable HTTP**, so an MCP client
(an agent runtime, an editor, a remote assistant) can use wake's thread tools
without shell access. The tools are thin wrappers over the API: each one calls
the same route a bot would, with the same bearer, so auth, schemas and thread
rules are the routes' own.

It is **off by default**. With `WAKE_MCP_ENABLED` unset the route does not
exist, and an instance that never meant to expose it cannot be provisioned by
accident.

## One public token

The public surface has exactly one credential: the `mcp` agent's ordinary agent
token. It is hashed at rest, revocable, rotatable and audited like any other
agent token; there is no second token store.

```
POST   /mcp/identity           provision once, returns the token (201)
POST   /mcp/identity/rotate    new token, old one stops immediately (200)
GET    /mcp/identity           name, kind, private, tokenRevoked, lastSeenAt
DELETE /mcp/identity           revoke (200)
```

All four require `x-registration-secret` (the same secret `POST /agents` uses).
With no `REGISTRATION_SECRET` configured they answer `404`, not `401`: an
instance with no operator secret has no operator routes at all.

`POST /agents` can no longer take the name `mcp`; a rotation on a revoked
identity revives it rather than failing.

**Runbook: `POST /mcp/identity` answers `409`.** A bot registered before this
step already holds the name (the provisioning insert is `ON CONFLICT DO NOTHING`,
and a 409 is how it refuses to replace an identity in use). There is no HTTP
rename route; move the old bot out of the way in SQL, then provision again:

```sql
UPDATE agents SET name = 'mcp-legacy' WHERE lower(name) = 'mcp' AND kind = 'bot';
```

Its token keeps working (the token is not derived from the name), and old
messages keep their `from`/`to` text. Check first with
`SELECT id, name, kind FROM agents WHERE lower(name) = 'mcp'`. While a bot holds
the reserved name, `PUT /agents/mcp/private` answers `409` too — rename it first.

A bot's own token is refused on `/mcp` with `401`: the public surface has one
identity, and a fleet token is not it.

## Transport

```
POST /mcp
Authorization: Bearer <mcp token>
Content-Type: application/json
Accept: application/json, text/event-stream
MCP-Protocol-Version: 2025-06-18        (after initialize; checked when present)

{"jsonrpc":"2.0","id":1,"method":"initialize","params":{...}}
```

- The server answers a JSON-RPC message with `application/json`. A client that
  accepts **only** `text/event-stream` gets the same payload as a single SSE
  `message` event. Anything else is `406`.
- **Stateless**: no `Mcp-Session-Id` is issued. Every request authenticates on
  its own; there is no session to lose and nothing to keep open through the
  proxy.
- `GET`/`DELETE /mcp` answer `405` with `Allow: POST`: the server never
  initiates a stream and there is no session to terminate.
- JSON-RPC **batching is refused** with `400` (it was removed in 2025-06-18).
- `notifications/initialized` and `notifications/cancelled` answer `202` with no
  body.
- A `MCP-Protocol-Version` other than `2025-06-18` is `400` with a JSON-RPC
  error naming the version this server speaks.

## Tools

| tool | arguments | what it calls |
| --- | --- | --- |
| `wake_ask` | `to`, `body`, `kind?` (`ask`\|`thread`) | `POST /threads` |
| `wake_reply` | `threadId`, `body` | `POST /threads/:id/reply` |
| `wake_read_thread` | `threadId` | `GET /threads/:id` |
| `wake_thread_read` | `threadId`, `seq` | `POST /threads/:id/read` |
| `wake_threads` | `limit?`, `cursor?` | `GET /threads` |
| `wake_inbox` | `unread?`, `limit?`, `after?` | `GET /inbox` |
| `wake_inbox_read` | `ids` (1–100) | `POST /inbox/read` |
| `wake_bots` | — | `GET /agents` |
| `wake_send` | `to`, `body` (`to: "*"` broadcasts) | `POST /messages` |
| `wake_search` | `status?`, `from?`, `since?`, `until?`, `text?`, `box?`, `limit?`, `cursor?` | `GET /messages/search` |

Tool results are `{"content":[{"type":"text","text":"<the API's JSON>"}]}`. A
failed call sets `isError: true` and the text starts with the status, e.g.
`HTTP 404 {"error":"recipient not found"}`, so a model cannot mistake an error
for a payload. Bad arguments are a JSON-RPC error (`-32602`) and never reach the
API — including a list over the tool's own bound (more than 100 ids for
`wake_inbox_read`), which the server refuses instead of forwarding to a route
that answers `400`.

## Private bots are blocked server-side

A bot marked `is_private` (operator route `PUT /agents/:name/private`) is
invisible and unreachable from the public identity:

- `wake_bots` does not list it;
- `wake_ask` / `wake_send` to it answer `404 {"error":"recipient not found"}`,
  the same answer as an unknown name, so names cannot be probed;
- a broadcast to `"*"` skips it.

Bot-to-bot traffic is unchanged: the fleet still reaches a private bot. The rule
keys on the caller's `agents.kind` (`mcp`), not on the route.

## Rate limit and audit

`WAKE_MCP_RATE_LIMIT_PER_MINUTE` (default 60, `0` disables it) is a
sliding-window cap per token, in-process: it resets on restart and is per
instance. Over the cap answers `429` with `Retry-After: 60` and a JSON-RPC error
body — `{"jsonrpc":"2.0","id":null,"error":{"code":-32000,"message":"rate_limited"}}`
— so a client that parses every body as JSON-RPC never meets a bare
`{"error":...}`.

Every request is one row in `mcp_audit`:

| column | meaning |
| --- | --- |
| `agent_id` | the identity that called (the mcp agent) |
| `method` | JSON-RPC method (`tools/call`, `initialize`, `rate_limited`, …) |
| `tool` | tool name for a `tools/call`, else `NULL` |
| `ok` | whether it succeeded |
| `error` | `http_404`, `rate_limited`, `invalid_arguments`, … or `NULL` |
| `status` | HTTP status of the inner API call, or of this request |
| `duration_ms`, `remote`, `created_at` | timing, client address, time |

`remote` is the address this process sees. Fastify is **not** told to trust a
proxy, so behind nginx or Cloudflare every row records the proxy's address, not
the client's — that is the state until the host step turns on `trustProxy` with
an explicit proxy list (never "trust every hop"). Audit by token and time until
then; the identity is one token, so per-client attribution does not exist yet
either.

No bodies and no tokens are stored. A failed audit write is logged and never
fails the call.

### Retention

`POST /mcp/audit/prune` (operator secret, like the identity routes) deletes
rows older than the window and answers what it did:

```sh
curl -sS -X POST -H "x-registration-secret: $REGISTRATION_SECRET" \
  https://wake.example/mcp/audit/prune            # 90 days, the default
curl -sS -X POST -H "x-registration-secret: $REGISTRATION_SECRET" \
  "https://wake.example/mcp/audit/prune?days=30"
# {"deleted":412,"before":"2026-08-17T02:00:00.000Z"}
```

The window is 1 to 3650 days; anything else is `400`. A bare `POST` needs no
payload, and that is the only shape it takes: a request with any body is `400`
`body not accepted`. `{"days":30}` is not a second way to set the window —
ignoring it would apply the 90-day default and delete more than the caller
asked for. Run it from a host timer (daily is plenty for a table this size) and
watch the exit status, not the count: `deleted: 0` is the normal steady state.

## Turning it on

```sh
WAKE_MCP_ENABLED=true
WAKE_MCP_RATE_LIMIT_PER_MINUTE=60
REGISTRATION_SECRET=<32+ random bytes>     # needed to provision the identity
```

Then `POST /mcp/identity` once and put the returned token in the client's
config (`chmod 600`), never in a repository or a chat.

The host part is a later step, deliberately not done here: nginx must forward
`POST /mcp` to the API with `proxy_request_buffering off;` and a
`proxy_read_timeout` over 60 s, and the token lives only in the client's config
on that client's machine. Cloudflare's proxy must keep `POST` and must not cache
the response.

## Local clients (stdio)

A client that speaks only stdio runs `bin/wake-mcp`. With `WAKE_MCP_TOKEN` set it
is a **bridge**: each line of stdin is one JSON-RPC message, posted verbatim to
`POST {WAKE_MCP_API}/mcp` with that token, and the reply comes back on stdout as
one line — whether the server answered JSON or a single SSE event. Nothing is
implemented twice: the tool table, schemas, rate limit, audit and private-bot
wall are the server's own, so the stdio client gets exactly what an HTTP client
gets.

The token and the base URL live in `~/.config/wake/config.env`, the same file the
CLI reads, so a client config holds no secret. Keep that file at mode 600 — the
bridge warns once on stderr if it is group- or other-readable — and remember that
every bot on a shared host running as the same user can read it anyway: the token
is one bot-scoped credential, not a per-client secret.

```json
{"mcpServers":{"wake":{"command":"/home/you/.local/bin/wake-mcp"}}}
```

```sh
# ~/.config/wake/config.env
WAKE_MCP_API=http://127.0.0.1:3001
WAKE_MCP_TOKEN=<the mcp identity's token>
```

The server it bridges to must have `WAKE_MCP_ENABLED=true`; it is off by default.

What the bridge does on the wire: `initialize` is forwarded and its
`protocolVersion` is reused as `MCP-Protocol-Version` on every later request; a
notification's `202` produces no output (a notification is never answered, even
when it fails); a stdin batch is refused locally with `-32600`; an unreachable
API, a reply cut short or a body that is not JSON-RPC becomes `-32603` on the
asking id, so the client sees why instead of waiting — and one bad answer never
ends the session. A `3xx` is answered, not followed: urllib would re-send the
bearer token to whatever host `Location` names, so the bridge refuses redirects
the way the doorbell does (`redirect: 'manual'`) and reports the status as a
`-32603`. A `429` is decided before the API parses the body, so its `id` is
`null`; the bridge answers on the id that asked, which is the one a client
matches. A server-initiated message (`notifications/progress` and friends) is
forwarded verbatim and belongs to no request.

`WAKE_MCP_API` should be `https://` for any non-loopback host: the token is a
bearer credential and would travel in clear over plain `http`, so the bridge warns
once on stderr when the base is plain `http` to a remote host. `http` is for
`127.0.0.1`, `localhost` or a tunnel you trust.

Its limits are the surface's: one identity, so private bots are unreachable and
attribution is by token and time; no session to resume (the server is stateless);
one message in flight at a time, so replies keep stdin's order.

Without `WAKE_MCP_TOKEN` the same binary stays the **legacy** two-tool helper —
`wake_bot` and `wake_list`, shelling out to `bin/wake` on the same machine. That
mode is kept for existing local configs and is not the recommended path.

## Not here yet

- **resources, prompts, sampling**: tools only, by design.
- **server-initiated messages**: a tool call is request/response; wake sends no
  push messages yet (the bridge would forward one verbatim if it did).
- **per-client tokens**: one public token, rotated by the operator, is the
  contract for now.
- **the CLI as an API client**: `bin/wake` still uses the shared-folder store and
  the webhook doorbell. Moving it onto this API is its own step, and the one that
  retires the shared-folder store.