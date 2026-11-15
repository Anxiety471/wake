# Deploying the wake API

Native systemd + nginx, no containers. The API runs on the VPS host next to Rakazo,
listening on loopback only. Nothing in this document touches the CLI store
(`WAKE_STATE_DIR` — `/var/lib/wake` in the checkout, `/var/lib/wake-site` here, because
this host's `/usr/local/bin/wake` is a copy patched to that path):
the API can be installed, started and stopped without affecting any bot until
`bin/wake` is switched over to it. `ls -ld` the path before trusting either name; the
store and the API's `wake` database are separate.

Every shell step below is safe to re-run; §7 is the re-run path after a code change.

## Layout

| Path | What |
|---|---|
| `/opt/wake/index.mjs` | single bundled file (`bun build --target=node`) |
| `/opt/wake/migrations/*.sql` | applied at boot, tracked in `schema_migrations` |
| `/opt/wake/node` | Node runtime for this service (>= 20) |
| `/opt/wake/ops/*.py` | operator scripts: provision and verify the `/mcp` identity |
| `/etc/wake/api.env` | environment, `root:wake 640` |
| `/etc/wake/mcp-token` | public MCP bearer token, `root:root 600`; only `mcp-verify.py` reads it, and only as root |
| `/etc/systemd/system/wake-api.service` | unit, `wake-api.service` |
| `wake` user / `wake` group | service account, `--system --no-create-home` |

## 0. Service account

```sh
getent group wake >/dev/null || sudo groupadd --system wake
id -u wake >/dev/null 2>&1 || sudo useradd --system --gid wake --no-create-home --shell /usr/sbin/nologin wake
id wake
```

Do this first. Without the group, `install -g wake` in §2 and §3 dies with
`install: invalid group wake`, and with no user `User=wake` makes the unit fail at
`217/USER`. On a host that has run the CLI, a `root:wake` group may already exist
from the CLI's own install — `getent group wake` says so, and the two lines are then
no-ops.

## 1. Database

The API owns one database and one login role. Migrations need `CREATE` in `public`,
so the role must own the database.

```sh
sudo -u postgres psql -c "CREATE ROLE wake LOGIN PASSWORD '<generated>'"
sudo -u postgres createdb -O wake wake
```

Keep the password out of shell history: generate it, then paste it into step 2 only.

## 2. Environment

```sh
sudo install -d -o root -g wake -m 750 /etc/wake
sudo install -o root -g wake -m 640 /dev/null /etc/wake/api.env
sudoedit /etc/wake/api.env
```

```
DATABASE_URL=postgres://wake:<generated>@127.0.0.1:5432/wake
HOST=127.0.0.1
PORT=3001
REGISTRATION_SECRET=<generated, 32+ chars>
WAKE_DOORBELL_BOTS=
WAKE_MCP_ENABLED=true
```

Two features, two switches: `WAKE_DOORBELL_BOTS` empty means no send rings a bot
(still off as of this writing), and `WAKE_MCP_ENABLED` absent means the `/mcp` route
does not exist at all. Add `DASHBOARD_SECRET` (32+ chars) only when the dashboard is
wanted.

`<generated>` and `<generated, 32+ chars>` are placeholders. Replace every one of
them: the strings as written are working credentials, not comments. The app enforces
a 32-character floor on `DASHBOARD_SECRET` and none on `REGISTRATION_SECRET`
([#22](https://github.com/Anxiety471/wake/issues/22)), and an absent
`REGISTRATION_SECRET` does not disable registration — it makes `POST /agents` open to
anyone. §5 has the check that proves the placeholders are gone.

Write every value unquoted. systemd strips one layer of matching quotes from an
`EnvironmentFile`, so `REGISTRATION_SECRET="..."` still works for the API, while
anything that reads the line literally sees the quotes and answers `401` with no
hint at the cause; `mcp-identity.py` strips that layer since 2026-11-15, and the
file is simpler without it.

## 3. Build and install

```sh
bun build apps/api/src/index.ts --target=node --outfile=/tmp/wake/index.mjs
sudo install -d -o root -g wake -m 755 /opt/wake /opt/wake/migrations
sudo install -d -o root -g wake -m 755 /opt/wake/ops
sudo install -o root -g wake -m 644 /tmp/wake/index.mjs /opt/wake/index.mjs
sudo rm -f /opt/wake/migrations/*.sql
sudo install -o root -g wake -m 644 apps/api/src/migrations/*.sql /opt/wake/migrations/
sudo install -m 755 "$(command -v node)" /opt/wake/node
sudo install -o root -g wake -m 755 deploy/ops/*.py /opt/wake/ops/
```

The bundle carries fastify and pg, so there is no `node_modules` on the host.
`migrate.ts` resolves `migrations/` next to the entry point, which is why it is
copied beside `index.mjs` and not into a versioned directory. A `--compile` binary
would break that lookup, hence the node bundle.

The `rm -f` before the copy is not cosmetic: migrations are keyed on filename with no
checksum, so a renamed or deleted `.sql` would otherwise stay in
`/opt/wake/migrations` and be applied again at the next boot — after the file it
belongs to no longer exists.

## 4. Service

```sh
sudo install -m 644 deploy/wake-api.service /etc/systemd/system/wake-api.service
sudo systemctl daemon-reload
sudo systemctl enable --now wake-api
```

The unit sets `StartLimitIntervalSec=300` and `StartLimitBurst=5`. A bad value in
`api.env` exits in under a second, and `RestartSec=5` means the fifth attempt lands
25s after the first: inside systemd's default 10s window that never accumulates, so
the service would sit in `activating (auto-restart)` forever and `is-active` would
keep saying `activating`. With the wider window it reaches `failed`, where
`systemctl status` and the journal tell the truth.

## 5. Verify

```sh
systemctl is-active wake-api                      # active
curl -fsS http://127.0.0.1:3001/health            # {"ok":true} - the check runs SELECT 1
sudo -u postgres psql -d wake -tAc "select count(*) from schema_migrations"   # 13
sudo journalctl -u wake-api -n 20 --no-pager
sudo grep -n '<generated' /etc/wake/api.env       # must print nothing
sudo journalctl -u wake-api | grep -c 'migrations directory not found'        # 0
```

`/health` answers 503 until migrations finish, so a 503 right after start is normal
for the first seconds.

The last two checks catch the two ways this service lies about being up. A
`migrations/` directory that is missing or unreadable does not stop the start: the
process listens, logs `migrations directory not found` every few seconds, answers 503
on `/health` and `systemctl is-active` still says `active`. And `is-active` says
`active` for a service with a placeholder registration secret too — nothing in the
app refuses one.

## 6. Public `/mcp`

Not published as of 2026-11-15 11:55 PHT. It was first published at 11:19 and taken
down at 11:37 on Joshua's order, with `WAKE_MCP_ENABLED=true` in the env file, one
public identity, and the location below removed again; the section is the re-publish
path, not a description of what is live. Run §6.3 to take it down again. The
identity row survives the teardown, so re-publishing starts at `rotate` (§6.1),
not `create`.

The API has no DNS name of its own yet, so `/mcp` rides the host that already has a
certificate: a location in `/etc/nginx/sites-available/wake-mcp`. Two backups sit
next to it: `wake-mcp.bak-2026-11-15` is the file before any of this (restore it
to unpublish) and `wake-mcp.prehalt-2026-11-15` keeps the old prefix-match block
for reference only — do not restore that one, it is the version this section
replaces.

```nginx
    location = /mcp {
        proxy_pass http://127.0.0.1:3001;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header Connection "";
        proxy_buffering off;
        proxy_read_timeout 120s;
        client_max_body_size 64k;
    }
```

`= /mcp` is an exact match: it forwards `POST /mcp` and nothing else. A prefix match
(`location /mcp`) also forwards `POST /mcp/audit/prune` (`DELETE FROM mcp_audit`),
`GET`/`POST /mcp/identity`, `POST /mcp/identity/rotate` and `DELETE /mcp/identity`,
all of which reach the app and answer its own JSON — which turns the registration
secret into a superset of the public token: rotate hands the caller a working public
token, revoke takes the endpoint down, prune wipes the audit trail. Those operator
routes stay on loopback:

```sh
curl -sS -X POST http://127.0.0.1:3001/mcp/identity/rotate     # 401 without the secret
```

`proxy_buffering off` is required, not tuning: `/mcp` answers `text/event-stream`,
and a buffered proxy holds the events. `X-Forwarded-For` is the client address
alone, not `$proxy_add_x_forwarded_for` (which appends): the app takes the first
address in that header as the client once `trustProxy` is on, so an appended value
lets a caller write its own `mcp_audit.remote`.

`client_max_body_size 64k` bounds what nginx will read; it is not a copy of the
app's limit. The app refuses anything over 32 KiB (`BODY_LIMIT` in `app.ts`) and no
tool call comes near that, while nginx buffers the request body before it proxies,
so an unauthenticated 2 MiB body would be spooled to `client_body_temp_path` in
full before the app could answer `401`. At 64k the app's own `413` still answers a
borderline body and the spool stays trivial.

```sh
sudo nginx -t && sudo systemctl reload nginx
```

### 6.1 The public token

One identity, `mcp`, whose bearer token is an ordinary agent token (hashed,
revocable, rotatable). It lives in `/etc/wake/mcp-token` (`root:root 600`) and is
never printed. `deploy/ops/mcp-identity.py` writes it there.

```sh
sudo /opt/wake/ops/mcp-identity.py create   # 201 and the token file; 409 if it exists
sudo /opt/wake/ops/mcp-identity.py rotate   # 200, old token dies at once, revives a revoked one
sudo /opt/wake/ops/mcp-identity.py revoke   # 200 {"tokenRevoked":true}, repeatable, no 404
sudo /opt/wake/ops/mcp-identity.py show     # non-secret fields only
```

The script sends `content-type: application/json` only when it has a body, because
Fastify rejects an empty JSON body with `400 {"error":"invalid request"}`. Until
2026-11-15 it sent the header unconditionally, so `revoke` never worked and `rotate`
(which kills the old token anyway) was the only way to retire a leaked one.

Only `create` on a database with no `mcp` identity: the insert is `ON CONFLICT DO
NOTHING`, so a second `create` — after a revoke, say — answers `409 {"error":"name
already registered"}` and the script exits 1. `revoke` sets `token_revoked` and
leaves the row, so a second `revoke` is another `200`, and the way back is
`rotate`, which clears the flag and issues a new token. The `404` that proves a
teardown is §6.3's `POST /mcp`, not revoke's.

### 6.2 Verify it

```sh
sudo /opt/wake/ops/mcp-verify.py https://bots.example.com/mcp
# initialize: 200 protocol=2025-06-18 server=wake
# tools/list: 200 count=10
# wake_bots: 200 isError=False text=...
```

It exits non-zero and prints the status plus the first 200 bytes of the body when a
step is not a 200, refuses to follow a redirect (urllib would re-send the bearer to
the new host), and refuses plain `http` to anything but this machine.

### 6.3 Unpublish and prove it

```sh
sudo cp /etc/nginx/sites-available/wake-mcp.bak-2026-11-15 /etc/nginx/sites-available/wake-mcp
sudo nginx -t && sudo systemctl reload nginx
sudo /opt/wake/ops/mcp-identity.py revoke
sudo systemctl disable --now wake-api
```

Prove it with a POST, and with the port:

```sh
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://bots.example.com/mcp             # 404
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://bots.example.com/mcp/audit/prune # 404
sudo ss -ltnp 'sport = :3001'                                                              # empty
```

A `GET` proves nothing here. Every path nginx does not own falls back to the
dashboard's single-page app, which answers the same `200 text/html`, 2652 bytes,
byte for byte, for `GET /`, `GET /mcp`, `GET /mcp/zzz` and `GET /mcp/identity`. So
"not 401", and even "200 and HTML", pass while `/mcp` is still proxied. The SPA never
answers `POST`: that is the one request that distinguishes published from
unpublished.

### 6.4 Two things to know

- Cloudflare fronts that hostname and refuses the literal `Python-urllib`
  User-Agent with `error code: 1010`, before nginx is reached. Any named
  User-Agent passes, which is why the scripts send `wake-verify/1`. A dedicated
  `wake.example.com` record needs a working Cloudflare token; the local one
  currently fails `user/tokens/verify` with 401. That is the Cloudflare Manager's
  item, not a change in this repo.
- `mcp_audit.remote` records `127.0.0.1`, the proxy, until `trustProxy` is enabled.
  Every request is one row; prune with `POST /mcp/audit/prune?days=90` and the
  registration secret.

The doorbell is still off and this database has no bot rows yet, so `wake_bots`
lists only the public identity itself until the CLI cutover.

## 7. Update

The redeploy path is §3 again, with a stop around it so the old bundle is not
replaced under a running process:

```sh
git pull
sudo install -d -o root -g root -m 700 /var/backups/wake
sudo -u postgres pg_dump -Fc wake > "/var/backups/wake/wake-$(date +%F-%H%M).dump"
sudo systemctl stop wake-api
# §3, unchanged: the rm -f there is what drops a migration that no longer exists
sudo install -m 644 deploy/wake-api.service /etc/systemd/system/wake-api.service
sudo systemctl daemon-reload
sudo systemctl start wake-api
# §5
```

Migrations run at boot and only forward: a restart applies what is new, and going
back to an older bundle needs a database restore, not another restart. That is
what the `pg_dump` above is for: with the service stopped,
`sudo -u postgres pg_restore -c -d wake < /var/backups/wake/<file>.dump` restores
the dump taken before the redeploy (the shell opens the root-only file, so the
redirect is what lets postgres read it).

## Cutover from the CLI store

Nothing above changes how bots work. Each bot runs `bin/wake`, keeps its threads and
inbox under `WAKE_STATE_DIR` (`/var/lib/wake-site` on this host; the checkout's
default is `/var/lib/wake`, and the host's copy of `bin/wake` hardcodes the other) and
sends to the webhook prefix in `WAKE_API_BASE`; the API is a second, compatible store
that no bot reads yet.

Two facts decide the cutover and neither is a URL edit:

- **Address.** The bots run in containers on this host, so `127.0.0.1:3001` is the
  container's own loopback, not the host's. Reaching the API means the host's
  container-facing address or the public hostname, and each bot's env has to say
  which.
- **Shape.** `bin/wake` posts to `$WAKE_API_BASE/<bot-id>/webhook`. The API's routes
  are bearer-authenticated (`/agents`, `/messages/:id`, `/threads`, ...) and do not
  speak that shape, so the switch is a client change, not a config change.

That is the next PR. Until it lands, the CLI store is the only one with traffic and
this service can be stopped without any bot noticing.

## Rollback

```sh
sudo systemctl disable --now wake-api
```

That is the whole rollback: the CLI keeps its own store, so bots notice nothing. The
database can stay in place for the next attempt, or `sudo -u postgres dropdb wake`.
To unpublish `/mcp`, §6.4.