# wake protocol and interfaces

## Webhook request

```
POST $WAKE_API_BASE/<bot-id>/webhook
Authorization: Bearer <contents of $WAKE_KEY_DIR/<bot-id>.key>
Content-Type: application/json
Idempotency-Key: <32 hex chars: sha256(bot-id|source|event|detail|unix_time//600)>
User-Agent: wake-cli/2.1

{
  "depth":     0,                      // WAKE_DEPTH of the sender
  "source":    "bot:coordinator",      // sender label kind:name
  "fromBotId": "bot-...",              // only when source is a bot in the registry; a label, not proof
  "fromAgent": "Oh My Pi",             // only for skill:/harness: sources; also a label, not proof
  "message":   "<the text before the host context is appended>",  // omitted when the body would pass 60,000 bytes
  "event":     "message",              // message | request (ask) | reply | command_end | hook event name
  "level":     "info",                 // ok | info | error
  "silent":    true,                   // the receiver posts no chat output; false from human: or --chatty
  "detail":    "<text> | host=<hostname> user=<user> cwd=<cwd>",
  "ref":       "thread:<id>",          // or "api-thread:<id>" / "" / --ref
  "time":      "2026-01-31T12:00:00+0000"
}
```

`silent` is the rule as data, so a receiver does not have to infer it from prose: a wake is a message
between bots and the human should see that one arrived, not the answer to it. The CLI also appends the
same rule as one bracketed sentence at the end of `detail`, because a receiver that renders `detail`
still needs to be told. The default follows the sender: a `human:` source sends `false` and appends
nothing, a bot or skill session sends `true`; `--chatty` forces `false` for one send, and `WAKE_SILENT=1`
or `0` pins one tone for every send. A thread opened
silent keeps it in its first record as `silent_payload`, and every answer inherits it, so the notice that
wakes the asker carries it too. A record carrying `silent: true` instead was written by `--silent`: no run
was started for it, so the field is a statement of fact rather than an instruction to a receiver.

Nobody on the receiving platform enforces either signal yet. A receiver stays quiet because its own routine
text says so; the prose sentence sits inside the fenced untrusted block, so a receiver that follows that
framing ignores it, and the key is data it may read, not a switch the platform throws. An implementer that
can read `silent` should prefer it to the prose.

`message` is the text before the sender appends `host=`, `user=` and `cwd=`. It is not always the bare
sentence: `--ask` appends `[thread <id>] reply: wake --reply <id> "YOUR ANSWER"` to the text it sends, and
`--reply` prefixes `reply on thread <id>: `, so those events carry the routing hint in `message` as well as
in `detail`. The receiving side renders `message` as the chat text and `detail` as the full record.

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

The store is provisioned once on the host, never by `wake`: `inbox/` and `inbox/active/` are
`root:wake 2770` (setgid, so new files inherit the `wake` group) and `threads/` is `root:wake 3770`
(setgid + sticky, so only a file's owner can unlink it); thread files are group-writable (`660`). An append
only appends: `wake` never re-applies a mode, so tightening the store sticks. Writing to a store the
user cannot write exits 1 and names the expected mode.

## Session inbox: `$WAKE_STATE_DIR/inbox/`

- `<hash>.jsonl` — the inbox; each delivered reply is one line `{"from", "kind": "reply", "thread", "text", "ts"}`.
- `active/<hash>.json` — liveness marker `{"inbox": "<path>", "pid": <session pid>, ...}`. A reply is delivered to the inbox recorded on the thread if its pid is alive, otherwise to the single live inbox; with two or more live inboxes and no match, nothing is guessed. Markers older than a day are pruned on `register`.

`wake-inbox-io register <hash> [json]` creates both and prints the inbox path; `beat <hash> [json]` rewrites the marker; `read <inbox>` prints the inbox. `<hash>` must be 6 to 32 lowercase hex characters (`[0-9a-f]{6,32}`), and `<inbox>` must be exactly `$WAKE_STATE_DIR/inbox/<hash>.jsonl`; anything else exits 1 before a command runs. A command runs locally when that inbox is writable and a read when it is readable; when it is neither but `/etc/group` lists this account for the store's group, the command runs through `sg <group>`, because a session started before the group existed keeps its old supplementary list and `sg` re-reads the group database. When an existing store has no route at all and `WAKE_HOSTOPS` is empty, nothing runs: the bridge prints one line saying why and exits 1, once per uid per outage rather than once per poll. If the command that does run fails — the host gate refuses any shape off its allow-list — `wake-inbox-io` prints that command's stderr and exits with its status, and a refused `register` prints no inbox path, so the session never exports a `WAKE_INBOX` that nothing created. The JSON goes through stdin, never through the shell, and every path is quoted with `shlex.quote`. The `register` command shape is matched by the host gate as an exact literal, including its trailing `; chmod 660 <inbox> <marker> 2>/dev/null`: the gate never runs that text and creates both files at `660` itself, but a command that drops the tail is refused, so the tail stays. The gate that accepted only the older `666` tail refuses `660`, so install a gate that matches both tails before deploying a client that sends this one, and drop the `666` route once every copy has been refreshed. `tests/test_gate_shapes.py` fails if the tail changes or the staged gate does not accept it.

## `WAKE_HOSTOPS` (guest mode)

An executable called as `$WAKE_HOSTOPS '<shell command>'` that runs the command on the host, forwards stdin, and returns its stdout, stderr and exit status. An `ssh host bash -c` wrapper is enough. When set, all thread/inbox I/O and key minting go through it and the default sender is `bot:container`.

## `WAKE_KEY_HELPER` (key minting, on the host)

| call | prints | meaning |
|---|---|---|
| `$WAKE_KEY_HELPER has-key <id>` | `yes` or `no` | a key already exists server-side |
| `$WAKE_KEY_HELPER <id> [--rotate]` | the key | create (or replace) and return the key |
| `$WAKE_KEY_HELPER enable-bot <id>` | free text | enable that bot's webhook routine |

Not shipped: it depends on the bot platform's admin API.
