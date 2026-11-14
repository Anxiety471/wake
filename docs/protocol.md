# wake protocol and interfaces

## Webhook request

```
POST $WAKE_API_BASE/<bot-id>/webhook
Authorization: Bearer <contents of $WAKE_KEY_DIR/<bot-id>.key>
Content-Type: application/json
Idempotency-Key: <32 hex chars: sha256(bot-id|source|event|detail|unix_time//600)>
User-Agent: wake-cli/2.1

{
  "depth":  0,                         // WAKE_DEPTH of the sender
  "source": "bot:coordinator",         // sender label kind:name
  "event":  "message",                 // message | request (ask) | reply | command_end | hook event name
  "level":  "info",                    // ok | info | error
  "detail": "<text> | host=<hostname> user=<user> cwd=<cwd>",
  "ref":    "thread:<id>",             // or "" / --ref
  "time":   "2026-01-31T12:00:00+0000"
}
```

Responses: any 2xx is success and the first 200 bytes of the body are printed. `401` → key rejected (rotate). `404` → the bot has no enabled webhook routine.

## Registry: `bots.tsv`

Tab-separated, 4 or 5 columns, other lines ignored:

```
alias  id  name  kind  role
```

`kind` is `open` or `private`. A shell whose working directory contains `/bots/<id>` is treated as that bot
(`bot:<alias>`). A harness that always runs from a neutral directory (cwd `/tmp`) can write its alias or id to
`<config>/self` instead; `wake --me` shows which of the two named it.

## Thread file: `$WAKE_STATE_DIR/threads/<id>.jsonl`

Id format `MMDDHHMM-xxxx`. First line is the ask, later lines are replies:

```json
{"ts": "...", "from": "bot:coordinator", "owner": "bot:coordinator", "reply_to": "bot:engineer",
 "kind": "ask", "text": "why does CI fail", "to": "engineer", "inbox": "<asker WAKE_INBOX or empty>"}
{"ts": "...", "from": "bot:engineer", "kind": "reply", "text": "flaky test"}
```

The store is provisioned once on the host, never by `wake`: `threads/` and `inbox/` are `root:wake 2770`
(setgid, so new files inherit the `wake` group) and thread files are group-writable (`660`). An append
only appends: `wake` never re-applies a mode, so tightening the store sticks. Writing to a store the
user cannot write exits 1 and names the expected mode.

## Session inbox: `$WAKE_STATE_DIR/inbox/`

- `<hash>.jsonl` — the inbox; each delivered reply is one line `{"from", "kind": "reply", "thread", "text", "ts"}`.
- `active/<hash>.json` — liveness marker `{"inbox": "<path>", "pid": <session pid>, ...}`. A reply is delivered to the inbox recorded on the thread if its pid is alive, otherwise to the single live inbox; with two or more live inboxes and no match, nothing is guessed. Markers older than a day are pruned on `register`.

`wake-inbox-io register <hash> [json]` creates both and prints the inbox path; `beat <hash> [json]` rewrites the marker; `read <inbox>` prints the inbox. `<hash>` must be 6 to 32 lowercase hex characters (`[0-9a-f]{6,32}`), and `<inbox>` must be exactly `$WAKE_STATE_DIR/inbox/<hash>.jsonl`; anything else exits 1 before a command runs. The JSON goes through stdin, never through the shell, and every path is quoted with `shlex.quote`.

## `WAKE_HOSTOPS` (guest mode)

An executable called as `$WAKE_HOSTOPS '<shell command>'` that runs the command on the host, forwards stdin, and returns its stdout, stderr and exit status. An `ssh host bash -c` wrapper is enough. When set, all thread/inbox I/O and key minting go through it and the default sender is `bot:container`.

## `WAKE_KEY_HELPER` (key minting, on the host)

| call | prints | meaning |
|---|---|---|
| `$WAKE_KEY_HELPER has-key <id>` | `yes` or `no` | a key already exists server-side |
| `$WAKE_KEY_HELPER <id> [--rotate]` | the key | create (or replace) and return the key |
| `$WAKE_KEY_HELPER enable-bot <id>` | free text | enable that bot's webhook routine |

Not shipped: it depends on the bot platform's admin API.
