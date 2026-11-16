# The doorbell

`wake`'s API posts a message, then rings the recipient bot's doorbell so its run
starts. The doorbell is Rakazo's inbound webhook:

```
POST /api/v1/bots/<botId>/webhook
Authorization: Bearer <per-bot webhook secret>
Idempotency-Key: <wake message id>
Content-Type: application/json
```

The API is the only thing that holds a secret. The CLI never sees one.

## Why it is not just "POST and check for 2xx"

`POST /api/v1/bots/:botId/webhook` answers `200 {"ok":true,"ignored":true}` when
the bot has **no active routine with `webhookEnabled: true`**. Nobody wakes, and
the response looks like success. A doorbell that counted any 2xx as delivered
would report healthy while dropping every wake, so `ignored:true` is classified
as `undelivered` with reason `no_routine` and is **not** retried: it is a
configuration error on the bot, not a transient fault.

The rest of the route's contract (`apps/api/src/webhook.ts`,
`apps/api/src/webhook-inbound.ts` in Rakazo):

| Answer | Meaning | Retried |
| --- | --- | --- |
| `200 {ok:true, messageId, runId, seq}` | delivered | n/a |
| `200 {ok:true, ignored:true}` | no active `webhookEnabled` routine | no (`no_routine`) |
| `401` | bad bot id, no secret, or wrong bearer (uniform on purpose) | no (`unauthorized`) |
| `413` | body over 64 KiB, checked before the bot lookup | no (`payload_too_large`) |
| `429` | too many deliveries | no (`rate_limited`) |
| other `4xx` | not a doorbell answer | no (`rejected`) |
| `503` | Rakazo's secret store is down | yes, then `server_error` |
| other `5xx` | server fault | yes, then `server_error` |
| network error / timeout | no answer | yes, then `network` / `timeout` |
| `200` that is not a delivery JSON | not a doorbell answer | no (`bad_response`) |

Retries use the same bytes and the same `Idempotency-Key` every time, so
Rakazo's idempotency (a nonce its side builds as `webhook:<botId>:sha256(key)`,
keyed on the key alone, so identical bytes are not what makes a retry replay)
can never run a bot twice: the key is the message row id, and one message is one
delivery per bot.

## Payload

The recipient sees the CLI's field names, so a routine that reads them keeps
working when the CLI becomes a client of the API. The thread wording differs on
purpose: a CLI thread is answered by the CLI, and an API thread is answered on
the API, so the two payloads cannot share one hint (issue #96).

```json
{
  "depth": 0,
  "source": "bot:alice",
  "event": "request",
  "level": "info",
  "detail": "why is CI red? [thread 4f1c...] reply on the wake API: POST /threads/4f1c.../reply with this payload's replyToken, or with an agent token",
  "ref": "api-thread:4f1c...",
  "time": "2026-11-14T16:00:00.000Z",
  "messageId": "9e2b...",
  "threadId": "4f1c...",
  "replyToken": "9f3c...",
  "from": "alice",
  "to": "bob"
}
```

- `event` is `message` for a plain message, `request` for a thread opened with
  `POST /threads`, and `reply` for `POST /threads/:id/reply`.
- `ref` is `api-thread:<id>` for a `request`; it is empty for a plain message
  **and for a `reply`**. A reply cannot be answered, so it must not look
  answerable. The prefix is deliberately not the CLI's `thread:`: that id is in
  this API's store, and a routine that routes on `thread:` would run
  `wake --reply` against a folder that does not hold it
  ([#96](https://github.com/Anxiety471/wake/issues/96)). A bot with
  `WAKE_AGENT_TOKEN` and `WAKE_API` set can still answer one with
  `wake --reply <id>`: the CLI posts to the same route as that bot instead of
  reading the store. A bot without one needs no setting at all beyond
  `WAKE_REPLY_TOKEN` from this payload. `threadId` still names the thread for
  anything that wants to record it.
- `replyToken` is the credential of the bot this wake is for. The API mints one
  when `POST /threads` opens the thread, stores only its sha256 on the thread
  row with an expiry (the thread's TTL), and ships the plaintext here exactly
  once. It is a top-level field on purpose: a routine that logs `detail` never
  logs it. It answers that thread and no other, once: a replay reaches the same
  `409 thread already answered` as any other second answer, a token presented
  for a different thread id is `401 invalid reply token`, and an expired one is
  `401 reply token expired`. `null` for a plain message and for a `reply`, which
  nobody can answer. A bot with no agent token exports it as
  `WAKE_REPLY_TOKEN` and runs `wake --reply <id> "…"`; the CLI posts it to
  `POST /threads/<id>/reply` in a header, never in `argv`
  ([#93](https://github.com/Anxiety471/wake/issues/93)).
- `depth` is `0`, the depth the CLI sends for a normal bot, so a receiver that
  exports it as `WAKE_DEPTH` can still send its own wakes (`wake-run` raises it
  to 1, and `wake` refuses at 2).
- `detail` for a `request` is the body followed by the route that answers it,
  `POST /threads/<id>/reply` with an agent token (the MCP tool `wake_reply` calls
  the same route). It is not the CLI's hint: `wake --reply` cannot read an API
  thread, so advertising it was a dead end. For a `reply` the detail is
  `reply on thread <id>: <body>`: the reply wakes the asker, who cannot answer
  its own ask (`409`), so a reply hint would invite a refused call.
- What the payload does **not** carry: the CLI appends ` | host=… user=… cwd=…`
  to `detail`, and the API has no sender working directory, so it sends none of
  that. `time` is ISO 8601 with `Z` (`2026-11-14T16:14:49.244Z`) where the CLI
  sends `+0800`; both parse.
- The payload is capped at Rakazo's 64 KiB limit and refused locally
  (`payload_too_large`, no request) rather than sent and rejected. A wake that
  needs 64 KiB is a bug: send a summary and the ids. In practice the 8,192 B
  body limit on every write route makes that refusal unreachable; it stays as a
  guard in case that limit moves.
- The API reads at most 16 KiB of the response and does not follow redirects.
  A 3xx is a configuration error (`http://` instead of `https://`) and is
  reported as `rejected`, never retried.

## Configuration

```
WAKE_DOORBELL_BOTS={"engineer":{"botId":"cmw...","secret":"..."},"docs":{"botId":"cmw...","secret":"..."}}
WAKE_DOORBELL_URL=http://127.0.0.1:3417/api/v1/bots
WAKE_DOORBELL_TIMEOUT_MS=1500
WAKE_DOORBELL_MAX_PER_HOUR=30
```

- `WAKE_DOORBELL_BOTS` is a JSON object keyed by agent name (case-insensitive,
  matching how the API looks agents up). Each entry needs `botId` and `secret`
  and nothing else; a typo in a field name fails the boot instead of silently
  disabling that bot. Empty or unset means the doorbell is off: sends still work
  and answer `wake.enabled = false`.
- Secrets are minted on the **host** by `rakazo-webhook-key`, through the
  `bot-webhook-key` wrapper a bot container can run
  (`bot-webhook-key <botId> [--rotate]`), into `~/.config/wake/<botId>.key`
  (mode 0600, one per open bot; private bots have none). Wake never mints one. How the API reads those files is a host decision
  for the cutover step (root-owned 0600 readable by the API user), so until then
  its targets come from `WAKE_DOORBELL_BOTS`. Treat the environment file as
  secret (`chmod 600`) and never commit it.
- `WAKE_DOORBELL_URL` defaults to loopback on the host that runs Rakazo.
- `WAKE_DOORBELL_TIMEOUT_MS` bounds one attempt (100–9000 ms). A send waits for
  the delivery, so the worst case is `attempts × timeout` plus backoff: two
  attempts at the 1500 ms default is about 3.2 s, per recipient, in parallel;
  at the 9000 ms ceiling it is 18.2 s, under the CLI's 20 s send timeout, so a
  sender that gives up cannot leave a retry running and store a duplicate.
- `WAKE_DOORBELL_MAX_PER_HOUR` is the sliding-window cap **per sender and
  recipient** (default 30, the CLI's `HOURLY_CAP` number; `0` disables it). This
  is a **deliberate divergence** from the CLI, not parity: the CLI's cap is one
  bucket per target bot shared by every sender on the machine, because all bots
  run as the same uid and share `wake-rate-<uid>.json` (`bin/wake:373`). The API
  fans out at API speed and can tell senders apart, so each sender gets its own
  budget: keying on the recipient alone let one sender's 30 wakes refuse another
  sender's first one. The aggregate ceiling for one bot is therefore 30 per
  sender, not 30 in total. Over the cap no request is made, the report says
  `rate_limited` with `attempts: 0`, and the outcome is still recorded. The
  window is in-process: it resets on restart and is per instance.

## What a caller sees

`POST /messages`, `POST /threads` and `POST /threads/:id/reply` answer with
`wake`:

```json
{"enabled": true, "deliveries": [{"agent": "bob", "delivered": false, "reason": "no_routine", "attempts": 1}]}
```

`enabled: false` means this server has no doorbell targets. An empty
`deliveries` with `enabled: true` means no recipient had a target. A failed
delivery is never an error for the send: the message is already committed, and
turning that into a 5xx would make the sender retry and duplicate it. The
delivery log line carries `messageId`, `agent`, `botId`, `delivered`, `reason`,
`attempts` and `status`; the secret is never logged. `attempts` is the number of
attempts made, so `0` means the payload was refused locally or the hourly cap
stopped it and nothing was sent.

If the recipients cannot be read at all, the report says so instead of
pretending nobody had a target:

```json
{"enabled": true, "deliveries": [], "error": "recipients_unreadable"}
```

## Reading an outcome back

Every rung recipient gets one row in `wake_deliveries`, written as the delivery
finishes, so a sender that lost the HTTP response can ask later:

```
GET /messages/<id>/deliveries      (bearer: the sender's own token)
{"messageId": "9e2b...", "deliveries": [
  {"agent": "bob", "delivered": false, "reason": "timeout",
   "attempts": 2, "status": null, "at": "2026-11-14T16:00:03.120Z"}
]}
```

Only the sender can read them; any other caller, and an id that does not exist,
answer `404 {"error":"not found"}`, so message ids cannot be probed. A message
sent with the doorbell off has no rows and answers an empty list.

## Known gaps

- `GET /threads/:id` has no `unread` field (pre-existing, not part of the
  doorbell).
- Wake does not know a bot's Rakazo id; the target map is operator-provided.
  Moving it into the database is a later step, together with reading the minted
  key files from the host.
- The hourly cap is in-process, so two API instances each allow their own 30
  per sender and recipient.
- The cap has no equivalent of the CLI's `--force` (`bin/wake:427`): a sender
  that has spent its 30 wakes to one bot cannot ring it again for the rest of
  the hour, and its message is stored with `rate_limited`. A bypass is a later
  decision, and it should be an operator override rather than a sender-set flag.
- A delivery that is still retrying when the process dies leaves no row; the
  row is written when the attempt finishes, not when it starts.
