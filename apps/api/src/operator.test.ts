import { Client } from 'pg';
import { afterAll, beforeAll, beforeEach, expect, test } from 'bun:test';
import { buildApp } from './app.js';
import { assertTestDatabaseUrl } from './test-helpers/test-database.js';
import type {
  AgentSummary,
  InboxPage,
  RegisteredAgent,
  SentMessage,
} from '@wake/shared';

const databaseUrl = assertTestDatabaseUrl(process.env.TEST_DATABASE_URL);
const OPERATOR = 'operator-secret-0123456789';

const app = buildApp(databaseUrl, { registrationSecret: OPERATOR });
const admin = new Client({ connectionString: databaseUrl });

beforeAll(async () => {
  await admin.connect();
  await app.ready();
  await app.migrationsReady;
});

beforeEach(async () => {
  await admin.query('TRUNCATE agents CASCADE');
});

afterAll(async () => {
  await admin.end();
  await app.close();
});

function bearer(token: string): { authorization: string } {
  return { authorization: `Bearer ${token}` };
}

function operator(secret = OPERATOR): {
  'x-registration-secret': string;
} {
  return { 'x-registration-secret': secret };
}

async function register(name: string): Promise<RegisteredAgent> {
  const response = await app.inject({
    method: 'POST',
    url: '/agents',
    headers: operator(),
    payload: { name },
  });
  expect(response.statusCode).toBe(201);
  return response.json();
}

async function provision(secret = OPERATOR): Promise<RegisteredAgent> {
  const response = await app.inject({
    method: 'POST',
    url: '/mcp/identity',
    headers: operator(secret),
  });
  expect(response.statusCode).toBe(201);
  return response.json();
}

test('operator routes do not exist without a registration secret', async () => {
  const bare = buildApp(databaseUrl, { migrate: false });
  try {
    await bare.ready();
    for (const request of [
      { method: 'GET' as const, url: '/mcp/identity' },
      { method: 'POST' as const, url: '/mcp/identity' },
      { method: 'DELETE' as const, url: '/mcp/identity' },
      { method: 'POST' as const, url: '/mcp/audit/prune' },
      {
        method: 'PUT' as const,
        url: '/agents/bob/private',
        payload: { private: true },
      },
    ]) {
      const response = await bare.inject(request);
      expect(response.statusCode).toBe(404);
    }
  } finally {
    await bare.close();
  }
});

test('operator routes reject the wrong secret', async () => {
  for (const request of [
    { method: 'GET' as const, url: '/mcp/identity' },
    { method: 'POST' as const, url: '/mcp/identity' },
    { method: 'DELETE' as const, url: '/mcp/identity' },
    { method: 'POST' as const, url: '/mcp/audit/prune' },
    {
      method: 'PUT' as const,
      url: '/agents/bob/private',
      payload: { private: true },
    },
  ]) {
    const response = await app.inject({
      ...request,
      headers: operator('wrong-secret'),
    });
    expect(response.statusCode).toBe(401);
    expect(response.json()).toEqual({ error: 'unauthorized' });
  }
});

test('the mcp identity is provisioned once, rotated and revoked', async () => {
  const created = await provision();
  expect(created.name).toBe('mcp');
  expect(created.token).toHaveLength(43);
  expect(JSON.stringify(created)).not.toContain(OPERATOR);

  const again = await app.inject({
    method: 'POST',
    url: '/mcp/identity',
    headers: operator(),
  });
  expect(again.statusCode).toBe(409);

  const listed = await app.inject({
    method: 'GET',
    url: '/mcp/identity',
    headers: operator(),
  });
  expect(listed.statusCode).toBe(200);
  expect(listed.json()).toMatchObject({
    id: created.id,
    name: 'mcp',
    kind: 'mcp',
    private: false,
    tokenRevoked: false,
    lastSeenAt: null,
  });

  // The token works as a normal agent token until it is rotated.
  expect(
    (
      await app.inject({
        method: 'GET',
        url: '/agents',
        headers: bearer(created.token),
      })
    ).statusCode,
  ).toBe(200);

  const rotated = await app.inject({
    method: 'POST',
    url: '/mcp/identity/rotate',
    headers: operator(),
  });
  expect(rotated.statusCode).toBe(200);
  const fresh: RegisteredAgent = rotated.json();
  expect(fresh.id).toBe(created.id);
  expect(fresh.token).not.toBe(created.token);
  expect(
    (
      await app.inject({
        method: 'GET',
        url: '/agents',
        headers: bearer(created.token),
      })
    ).statusCode,
  ).toBe(401);
  expect(
    (
      await app.inject({
        method: 'GET',
        url: '/agents',
        headers: bearer(fresh.token),
      })
    ).statusCode,
  ).toBe(200);

  const revoked = await app.inject({
    method: 'DELETE',
    url: '/mcp/identity',
    headers: operator(),
  });
  expect(revoked.statusCode).toBe(200);
  expect(revoked.json()).toEqual({ name: 'mcp', tokenRevoked: true });
  expect(
    (
      await app.inject({
        method: 'GET',
        url: '/agents',
        headers: bearer(fresh.token),
      })
    ).statusCode,
  ).toBe(401);

  // Rotating a revoked identity revives it rather than failing.
  const revived = await app.inject({
    method: 'POST',
    url: '/mcp/identity/rotate',
    headers: operator(),
  });
  expect(revived.statusCode).toBe(200);
  expect(
    (
      await app.inject({
        method: 'GET',
        url: '/agents',
        headers: bearer(revived.json<RegisteredAgent>().token),
      })
    ).statusCode,
  ).toBe(200);
});

test('the open registration route cannot take the mcp name', async () => {
  const response = await app.inject({
    method: 'POST',
    url: '/agents',
    headers: operator(),
    payload: { name: 'MCP' },
  });
  expect(response.statusCode).toBe(409);
  expect(response.json()).toEqual({ error: 'name already registered' });
});

test('a private bot is reachable from the fleet but not from the mcp identity', async () => {
  const alice = await register('alice');
  const bob = await register('bob');
  const marked = await app.inject({
    method: 'PUT',
    url: '/agents/bob/private',
    headers: operator(),
    payload: { private: true },
  });
  expect(marked.statusCode).toBe(200);
  expect(marked.json()).toEqual({ name: 'bob', private: true });

  // Bot-to-bot traffic is unchanged.
  const direct = await app.inject({
    method: 'POST',
    url: '/messages',
    headers: bearer(alice.token),
    payload: { to: 'bob', body: 'still reachable' },
  });
  expect(direct.statusCode).toBe(201);

  const listedForBot = await app.inject({
    method: 'GET',
    url: '/agents',
    headers: bearer(alice.token),
  });
  const forBot: { agents: AgentSummary[] } = listedForBot.json();
  expect(forBot.agents.map((agent) => agent.name)).toEqual(['alice', 'bob']);
  expect(forBot.agents.find((agent) => agent.name === 'bob')?.private).toBe(
    true,
  );

  const identity = await provision();
  const listedForMcp = await app.inject({
    method: 'GET',
    url: '/agents',
    headers: bearer(identity.token),
  });
  const forMcp: { agents: AgentSummary[] } = listedForMcp.json();
  expect(forMcp.agents.map((agent) => agent.name)).not.toContain('bob');

  for (const [url, payload] of [
    ['/messages', { to: 'bob', body: 'let me in' }],
    ['/threads', { to: 'bob', body: 'let me in' }],
  ] as const) {
    const refused = await app.inject({
      method: 'POST',
      url,
      headers: bearer(identity.token),
      payload,
    });
    expect(refused.statusCode).toBe(404);
    expect(refused.json()).toEqual({ error: 'recipient not found' });
  }

  // A broadcast from the mcp identity reaches the open bots and skips bob.
  const broadcast = await app.inject({
    method: 'POST',
    url: '/messages',
    headers: bearer(identity.token),
    payload: { to: '*', body: 'all hands' },
  });
  expect(broadcast.statusCode).toBe(201);
  const sent: SentMessage = broadcast.json();
  expect(sent.id).toBeDefined();
  const bobInbox = await app.inject({
    method: 'GET',
    url: '/inbox',
    headers: bearer(bob.token),
  });
  expect(
    (bobInbox.json() as InboxPage).messages.map((message) => message.body),
  ).toEqual(['still reachable']);
  const aliceInbox = await app.inject({
    method: 'GET',
    url: '/inbox',
    headers: bearer(alice.token),
  });
  expect(
    (aliceInbox.json() as InboxPage).messages.map((message) => message.body),
  ).toEqual(['all hands']);
});

test('marking a private bot is idempotent and a missing bot is a 404', async () => {
  await register('bob');
  for (const value of [true, true, false]) {
    const response = await app.inject({
      method: 'PUT',
      url: '/agents/bob/private',
      headers: operator(),
      payload: { private: value },
    });
    expect(response.statusCode).toBe(200);
    expect(response.json()).toEqual({ name: 'bob', private: value });
  }
  const missing = await app.inject({
    method: 'PUT',
    url: '/agents/nobody/private',
    headers: operator(),
    payload: { private: true },
  });
  expect(missing.statusCode).toBe(404);
});

test('the public identity cannot be made private', async () => {
  await provision();
  for (const name of ['mcp', 'MCP']) {
    const refused = await app.inject({
      method: 'PUT',
      url: `/agents/${name}/private`,
      headers: operator(),
      payload: { private: true },
    });
    expect(refused.statusCode).toBe(409);
    expect(refused.json()).toEqual({
      error: 'the public identity cannot be private',
    });
  }
  // Refused means unchanged, not half-applied.
  const row = await admin.query<{ is_private: boolean }>(
    `SELECT is_private FROM agents WHERE lower(name) = 'mcp'`,
  );
  expect(row.rows[0]?.is_private).toBe(false);
  // An ordinary bot is still markable.
  await register('bob');
  const marked = await app.inject({
    method: 'PUT',
    url: '/agents/bob/private',
    headers: operator(),
    payload: { private: true },
  });
  expect(marked.statusCode).toBe(200);
});

test('pruning the audit deletes only rows past the window', async () => {
  await provision();
  const insert = (at: string) =>
    admin.query(
      `INSERT INTO mcp_audit
         (agent_id, method, tool, ok, error, status, duration_ms, remote, created_at)
       SELECT id, 'initialize', NULL, true, NULL, 200, 1, '127.0.0.1', $1::timestamptz
       FROM agents WHERE kind = 'mcp'`,
      [at],
    );
  // 2026-01-01 is more than 90 days before this suite runs; now() is not.
  await insert('2026-01-01T00:00:00.000Z');
  await insert('2026-06-01T00:00:00.000Z');
  await insert(new Date().toISOString());

  const pruned = await app.inject({
    method: 'POST',
    url: '/mcp/audit/prune',
    headers: operator(),
  });
  expect(pruned.statusCode).toBe(200);
  const body = pruned.json<{ deleted: number; before: string }>();
  expect(body.deleted).toBe(2);
  // The default window is 90 days, and it reports the cutoff it used.
  const cutoff = Date.now() - 90 * 24 * 60 * 60 * 1000;
  expect(Math.abs(Date.parse(body.before) - cutoff)).toBeLessThan(60_000);

  const left = await admin.query<{ count: string }>(
    'SELECT count(*)::text AS count FROM mcp_audit',
  );
  expect(left.rows[0]?.count).toBe('1');
  // A second run finds nothing and is still a 200.
  const again = await app.inject({
    method: 'POST',
    url: '/mcp/audit/prune',
    headers: operator(),
  });
  expect(again.statusCode).toBe(200);
  expect(again.json<{ deleted: number }>().deleted).toBe(0);
});

async function insertAuditAged(age: string): Promise<void> {
  await admin.query(
    `INSERT INTO mcp_audit
       (agent_id, method, tool, ok, error, status, duration_ms, remote, created_at)
     SELECT id, 'initialize', NULL, true, NULL, 200, 1, '127.0.0.1', now() - $1::interval
     FROM agents WHERE kind = 'mcp'`,
    [age],
  );
}

async function auditAges(): Promise<number[]> {
  const rows = await admin.query<{ age_days: number }>(
    `SELECT floor(extract(epoch FROM now() - created_at) / 86400)::int AS age_days
       FROM mcp_audit
      ORDER BY created_at`,
  );
  return rows.rows.map((row) => row.age_days);
}

test('the prune window decides what is deleted, not just what is reported', async () => {
  await provision();
  // Real ages, not a mocked clock: 200 days, 40 days, and now. Oldest first.
  await insertAuditAged('200 days');
  await insertAuditAged('40 days');
  await insertAuditAged('0 days');
  expect(await auditAges()).toEqual([200, 40, 0]);

  // Ten years keeps everything.
  const widest = await app.inject({
    method: 'POST',
    url: '/mcp/audit/prune?days=3650',
    headers: operator(),
  });
  expect(widest.statusCode).toBe(200);
  expect(widest.json<{ deleted: number }>().deleted).toBe(0);
  expect(await auditAges()).toEqual([200, 40, 0]);

  // The default 90-day window deletes the 200-day row and spares the 40-day one.
  const byDefault = await app.inject({
    method: 'POST',
    url: '/mcp/audit/prune',
    headers: operator(),
  });
  expect(byDefault.statusCode).toBe(200);
  expect(byDefault.json<{ deleted: number }>().deleted).toBe(1);
  expect(await auditAges()).toEqual([40, 0]);

  // A 30-day window deletes the row the default spared.
  const narrow = await app.inject({
    method: 'POST',
    url: '/mcp/audit/prune?days=30',
    headers: operator(),
  });
  expect(narrow.statusCode).toBe(200);
  expect(narrow.json<{ deleted: number }>().deleted).toBe(1);
  expect(await auditAges()).toEqual([0]);
});

test('prune refuses a body instead of pruning at the default window', async () => {
  await provision();
  await insertAuditAged('200 days');

  // A body is not a second way to say `days`. Both a body that names a window
  // and an empty one are refused, and nothing is deleted -- ignoring
  // {"days":30} would have pruned at the 90-day default.
  for (const payload of [{ days: 30 }, {}]) {
    const refused = await app.inject({
      method: 'POST',
      url: '/mcp/audit/prune',
      headers: operator(),
      payload,
    });
    expect(refused.statusCode).toBe(400);
    expect(refused.json<{ error: string }>().error).toBe('body not accepted');
  }
  // A literal null is a body as well; only no body at all may prune.
  const nullBody = await app.inject({
    method: 'POST',
    url: '/mcp/audit/prune',
    headers: { ...operator(), 'content-type': 'application/json' },
    payload: 'null',
  });
  expect(nullBody.statusCode).toBe(400);
  expect(await auditAges()).toEqual([200]);

  // The bare POST a host timer runs is untouched.
  const bare = await app.inject({
    method: 'POST',
    url: '/mcp/audit/prune',
    headers: operator(),
  });
  expect(bare.statusCode).toBe(200);
  expect(bare.json<{ deleted: number }>().deleted).toBe(1);
});

test('the prune window is bounded and needs the operator secret', async () => {
  for (const days of ['0', '-1', '3651', '1.5', 'ten']) {
    const refused = await app.inject({
      method: 'POST',
      url: `/mcp/audit/prune?days=${days}`,
      headers: operator(),
    });
    expect(refused.statusCode).toBe(400);
  }
  const refused = await app.inject({ method: 'POST', url: '/mcp/audit/prune' });
  expect(refused.statusCode).toBe(401);
  const wrong = await app.inject({
    method: 'POST',
    url: '/mcp/audit/prune',
    headers: operator('not-the-secret-0123456789'),
  });
  expect(wrong.statusCode).toBe(401);
  const custom = await app.inject({
    method: 'POST',
    url: '/mcp/audit/prune?days=30',
    headers: operator(),
  });
  expect(custom.statusCode).toBe(200);
  expect(
    Math.abs(
      Date.parse(custom.json<{ before: string }>().before) -
        (Date.now() - 30 * 86_400_000),
    ),
  ).toBeLessThan(60_000);
});
