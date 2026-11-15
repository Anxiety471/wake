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

The recipient sees the CLI's field names and wording, so an existing routine
keeps working when the CLI becomes a client of the API:

```json
{
  "depth": 0,
  "source": "bot:alice",
  "event": "request",
  "level": "info",
  "detail": "why is CI red? [thread 4f1c...] reply: wake --reply 4f1c... \"YOUR ANSWER\"",
  "ref": "thread:4f1c...",
  "time": "2026-11-14T16:00:00.000Z",
  "messageId": "9e2b...",
  "threadId": "4f1c...",
  "from": "alice",
  "to": "bob"
}
```

- `event` is `message` for a plain message, `request` for a thread opened with
  `POST /threads`, and `reply` for `POST /threads/:id/reply`.
- `ref` starts with `thread:` for thread messages, which is the routing signal a
  routine uses today; it is empty for a plain message.
- `depth` is `0`, the depth the CLI sends for a normal bot, so a receiver that
  exports it as `WAKE_DEPTH` can still send its own wakes (`wake-run` raises it
  to 1, and `wake` refuses at 2).
- `detail` for a `request` is the body followed by the CLI's reply hint,
  verbatim. For a `reply` it is `reply on thread <id>: <body>`: the reply wakes
  the asker, who cannot answer its own ask (`409`), so the reply hint would
  invite a refused call.
- What the payload does **not** carry: the CLI appends ` | host=… user=… cwd=…`
  to `detail`, and the API has no sender working directory, so it sends none of
  that. `time` is ISO 8601 with `Z` (`2026-11-14T16:14:49.244Z`) where the CLI
  sends `+0800`; both parse.
- The payload is capped at Rakazo's 64 KiB limit and refused locally
  (`payload_too_large`, no request) rather than sent and rejected. A wake that
  needs 64 KiB is a bug: send a summary and the ids.

## Configuration

```
WAKE_DOORBELL_BOTS={"engineer":{"botId":"cmw...","secret":"..."},"docs":{"botId":"cmw...","secret":"..."}}
WAKE_DOORBELL_URL=http://127.0.0.1:3417/api/v1/bots
WAKE_DOORBELL_TIMEOUT_MS=1500
```

- `WAKE_DOORBELL_BOTS` is a JSON object keyed by agent name (case-insensitive,
  matching how the API looks agents up). Each entry needs `botId` and `secret`
  and nothing else; a typo in a field name fails the boot instead of silently
  disabling that bot. Empty or unset means the doorbell is off: sends still work
  and answer `wake.enabled = false`.
- Secrets are issued by the **human** Rakazo account (`bots.rotateWebhookSecret`
  then `bots.revealWebhookSecret`, which records a `credential.revealed` security
  event), so provisioning is an operator action. Wake never mints one. Treat the
  environment file as secret (`chmod 600`) and never commit it.
- `WAKE_DOORBELL_URL` defaults to loopback on the host that runs Rakazo.
- `WAKE_DOORBELL_TIMEOUT_MS` bounds one attempt (100–10000 ms). A send waits for
  the delivery, so the worst case is `attempts × timeout` plus backoff: two
  attempts at 1500 ms is about 3.2 s, per recipient, in parallel.

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
attempts made, so `0` means the payload was refused locally and nothing was sent.

## Known gaps

- Delivery outcomes live in the response and the log, not in a table, so a
  sender that lost the response cannot ask later what happened.
- `GET /threads/:id` has no `unread` field (pre-existing, not part of the
  doorbell).
- Wake does not know a bot's Rakazo id; the target map is operator-provided.
  Step 3 decides whether that moves into the database.