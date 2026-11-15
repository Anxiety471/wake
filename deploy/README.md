# Deploying the wake API

Native systemd + nginx, no containers. The API runs on the VPS host next to Rakazo,
listening on loopback only. Nothing in this document touches the CLI store
(`/var/lib/rakazo-wake`): the API can be installed, started and stopped without
affecting any bot until `bin/wake` is switched over to it.

## Layout

| Path | What |
|---|---|
| `/opt/wake/index.mjs` | single bundled file (`bun build --target=node`) |
| `/opt/wake/migrations/*.sql` | applied at boot, tracked in `schema_migrations` |
| `/opt/wake/node` | Node runtime for this service (>= 20) |
| `/opt/wake/ops/*.py` | operator scripts: provision the `/mcp` identity, verify it |
| `/etc/wake/api.env` | environment, `root:wake 640` |
| `/etc/wake/mcp-token` | public MCP bearer token, `root:wake 640` |
| `/etc/systemd/system/wake-api.service` | unit, `wake-api.service` |
| `wake` user / `wake` group | service account, `--system --no-create-home` |

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

## 3. Build and install

```sh
bun build apps/api/src/index.ts --target=node --outfile=/tmp/wake/index.mjs
sudo install -d -o root -g wake -m 755 /opt/wake /opt/wake/migrations
sudo install -o root -g wake -m 644 /tmp/wake/index.mjs /opt/wake/index.mjs
sudo install -o root -g wake -m 644 apps/api/src/migrations/*.sql /opt/wake/migrations/
sudo install -m 755 "$(command -v node)" /opt/wake/node
```

The bundle carries fastify and pg, so there is no `node_modules` on the host.
`migrate.ts` resolves `migrations/` next to the entry point, which is why it is
copied beside `index.mjs` and not into a versioned directory. A `--compile` binary
would break that lookup, hence the node bundle.

## 4. Service

```sh
sudo install -m 644 deploy/wake-api.service /etc/systemd/system/wake-api.service
sudo systemctl daemon-reload
sudo systemctl enable --now wake-api
```

## 5. Verify

```sh
systemctl is-active wake-api                      # active
curl -fsS http://127.0.0.1:3001/health            # {"ok":true} - the check runs SELECT 1
sudo -u postgres psql -d wake -tAc "select count(*) from schema_migrations"
journalctl -u wake-api -n 20 --no-pager
```

`/health` answers 503 until migrations finish, so a 503 right after start is normal
for the first seconds.

## 6. Public `/mcp`

Deployed 2026-11-15 with `WAKE_MCP_ENABLED=true` and one public identity.

The API has no DNS name of its own yet, so `/mcp` rides the host that already has a
certificate: a location in `/etc/nginx/sites-available/bots-bocchi` (backup kept as
`bots-bocchi.bak-2026-11-15`).

```
    location /mcp {
        proxy_pass http://127.0.0.1:3001;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header Connection "";
        proxy_buffering off;
        proxy_read_timeout 120s;
        client_max_body_size 2m;
    }
```

Public URL: `https://bots.bocchi.trade/mcp`. Nothing else of the API is exposed;
registration and the operator routes stay on loopback.

### The public token

One identity, `mcp`, whose bearer token is an ordinary agent token (hashed,
revocable, rotatable). It lives in `/etc/wake/mcp-token` (`root:wake 640`) and is
never printed. `deploy/ops/mcp-identity.py` writes it there.

```sh
sudo /opt/wake/ops/mcp-identity.py create   # first time: 201 and the token file
sudo /opt/wake/ops/mcp-identity.py rotate   # old token dies immediately
sudo /opt/wake/ops/mcp-identity.py revoke   # 404 from then on
sudo /opt/wake/ops/mcp-identity.py show     # non-secret fields only
```

### Verify it

```sh
sudo /opt/wake/ops/mcp-verify.py https://bots.bocchi.trade/mcp
# initialize: 200 protocol=2025-06-18 server=wake
# tools/list: 200 count=10
```

### Two things to know

- Cloudflare fronts that hostname and refuses the literal `Python-urllib`
  User-Agent with `error code: 1010`, before nginx is reached. Any named
  User-Agent passes, so a client must send one. A dedicated `wake.bocchi.trade`
  record needs a working Cloudflare token; the local one currently fails
  `user/tokens/verify` with 401.
- `mcp_audit.remote` records `127.0.0.1`, the proxy, until `trustProxy` is enabled.
  Every request is one row; prune with `POST /mcp/audit/prune?days=90` and the
  registration secret.

The doorbell is still off and this database has no bot rows yet, so `wake_bots`
lists only the public identity itself until the CLI cutover.

## Rollback

```sh
sudo systemctl disable --now wake-api
```

That is the whole rollback: the CLI keeps its own store, so bots notice nothing. The
database can stay in place for the next attempt, or `sudo -u postgres dropdb wake`.
To unpublish `/mcp`, restore `bots-bocchi.bak-2026-11-15` and reload nginx.