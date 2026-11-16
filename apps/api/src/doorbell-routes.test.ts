import { Client } from 'pg';
import { afterAll, beforeAll, beforeEach, expect, test } from 'bun:test';
import { buildApp } from './app.js';
import { parseDoorbell } from './doorbell.js';
import { assertTestDatabaseUrl } from './test-helpers/test-database.js';
import type {
  CreatedThread,
  InboxPage,
  RegisteredAgent,
  SentMessage,
  ThreadReply,
  WakeReport,
} from '@wake/shared';

const databaseUrl = assertTestDatabaseUrl(process.env.TEST_DATABASE_URL);

const SECRET = 'wake-doorbell-secret-0123456789';

type StubRequest = {
  path: string;
  headers: Record<string, string>;
  body: string;
};

type StubReply = {
  status: number;
  body: string;
};

const requests: StubRequest[] = [];
let stubReply: StubReply = {
  status: 200,
  body: JSON.stringify({ ok: true, messageId: 'm', runId: 'r', seq: 1 }),
};

const stub = Bun.serve({
  hostname: '127.0.0.1',
  port: 0,
  async fetch(request) {
    const body = await request.text();
    const headers: Record<string, string> = {};
    request.headers.forEach((value, key) => {
      headers[key] = value;
    });
    requests.push({
      path: new URL(request.url).pathname,
      headers,
      body,
    });
    return new Response(stubReply.body, {
      status: stubReply.status,
      headers: { 'content-type': 'application/json' },
    });
  },
});

function doorbell(agents: { name: string; botId: string }[]) {
  const bots: Record<string, { botId: string; secret: string }> = {};
  for (const agent of agents) {
    bots[agent.name] = { botId: agent.botId, secret: SECRET };
  }
  return parseDoorbell({
    WAKE_DOORBELL_BOTS: JSON.stringify(bots),
    WAKE_DOORBELL_URL: `http://127.0.0.1:${stub.port}/api/v1/bots`,
    WAKE_DOORBELL_TIMEOUT_MS: '500',
    // The cap is exercised on its own in doorbell.test.ts; these tests send
    // more than 30 messages to bob across the file.
    WAKE_DOORBELL_MAX_PER_HOUR: '0',
  });
}

const config = doorbell([
  { name: 'alice', botId: 'cmwalice' },
  { name: 'bob', botId: 'cmwbob' },
  { name: 'carol', botId: 'cmwcarol' },
]);

const app = buildApp(databaseUrl, { doorbell: config });
const admin = new Client({ connectionString: databaseUrl });

beforeAll(async () => {
  await admin.connect();
  await app.ready();
  await app.migrationsReady;
});

beforeEach(async () => {
  await admin.query('TRUNCATE agents CASCADE');
  requests.length = 0;
  stubReply = {
    status: 200,
    body: JSON.stringify({ ok: true, messageId: 'm', runId: 'r', seq: 1 }),
  };
});

afterAll(async () => {
  await admin.end();
  await app.close();
  stub.stop(true);
});

function bearer(token: string): { authorization: string } {
  return { authorization: `Bearer ${token}` };
}

async function register(name: string): Promise<RegisteredAgent> {
  const response = await app.inject({
    method: 'POST',
    url: '/agents',
    payload: { name },
  });
  expect(response.statusCode).toBe(201);
  return response.json();
}

async function send(
  token: string,
  payload: { to: string; body: string },
): Promise<SentMessage> {
  const response = await app.inject({
    method: 'POST',
    url: '/messages',
    headers: bearer(token),
    payload,
  });
  expect(response.statusCode).toBe(201);
  return response.json();
}

async function ask(
  token: string,
  to: string,
  body: string,
  kind?: 'ask' | 'thread',
): Promise<CreatedThread> {
  const response = await app.inject({
    method: 'POST',
    url: '/threads',
    headers: bearer(token),
    payload: kind === undefined ? { to, body } : { to, body, kind },
  });
  expect(response.statusCode).toBe(201);
  return response.json();
}

async function reply(
  token: string,
  threadId: string,
  body: string,
): Promise<ThreadReply> {
  const response = await app.inject({
    method: 'POST',
    url: `/threads/${threadId}/reply`,
    headers: bearer(token),
    payload: { body },
  });
  expect(response.statusCode).toBe(201);
  return response.json();
}

async function inbox(token: string): Promise<InboxPage> {
  const response = await app.inject({
    method: 'GET',
    url: '/inbox',
    headers: bearer(token),
  });
  expect(response.statusCode).toBe(200);
  return response.json();
}

function delivered(agent: string): WakeReport['deliveries'][number] {
  return { agent, delivered: true, reason: null, attempts: 1 };
}

async function deliveries(
  token: string,
  messageId: string,
): Promise<{ status: number; body: unknown }> {
  const response = await app.inject({
    method: 'GET',
    url: `/messages/${messageId}/deliveries`,
    headers: bearer(token),
  });
  return { status: response.statusCode, body: response.json() };
}

test('a sent message wakes the recipient through the doorbell', async () => {
  const alice = await register('alice');
  const bob = await register('bob');
  const sent = await send(alice.token, { to: 'bob', body: 'hello bob' });
  expect(sent.wake).toEqual({
    enabled: true,
    deliveries: [delivered('bob')],
  });
  expect(requests).toHaveLength(1);
  const request = requests[0];
  expect(request?.path).toBe('/api/v1/bots/cmwbob/webhook');
  expect(request?.headers['authorization']).toBe(`Bearer ${SECRET}`);
  // The message row id is the idempotency key, so a retry cannot run the bot twice.
  expect(request?.headers['idempotency-key']).toBe(sent.id);
  expect(JSON.parse(request?.body ?? '')).toEqual({
    depth: 0,
    source: 'bot:alice',
    event: 'message',
    level: 'info',
    detail: 'hello bob',
    ref: '',
    time: expect.any(String),
    messageId: sent.id,
    threadId: null,
    from: 'alice',
    to: 'bob',
  });
  const bobInbox = await inbox(bob.token);
  expect(bobInbox.messages.map((message) => message.id)).toEqual([sent.id]);
});

test('ignored:true is undelivered, and the message still lands', async () => {
  const alice = await register('alice');
  await register('bob');
  stubReply = {
    status: 200,
    body: JSON.stringify({ ok: true, ignored: true }),
  };
  const sent = await send(alice.token, { to: 'bob', body: 'anyone home' });
  expect(sent.wake).toEqual({
    enabled: true,
    deliveries: [
      { agent: 'bob', delivered: false, reason: 'no_routine', attempts: 1 },
    ],
  });
  expect(requests).toHaveLength(1);
});

test('a 5xx never fails the send, and is reported after the last attempt', async () => {
  const alice = await register('alice');
  const bob = await register('bob');
  stubReply = {
    status: 503,
    body: JSON.stringify({ error: 'Service unavailable' }),
  };
  const sent = await send(alice.token, { to: 'bob', body: 'still queued' });
  expect(sent.wake.deliveries[0]).toEqual({
    agent: 'bob',
    delivered: false,
    reason: 'server_error',
    attempts: 2,
  });
  expect(requests).toHaveLength(2);
  const bobInbox = await inbox(bob.token);
  expect(bobInbox.messages.map((message) => message.id)).toEqual([sent.id]);
});

test('a broadcast wakes every configured recipient once', async () => {
  const alice = await register('alice');
  await register('bob');
  await register('carol');
  const sent = await send(alice.token, { to: '*', body: 'all hands' });
  expect(sent.wake).toEqual({
    enabled: true,
    deliveries: [delivered('bob'), delivered('carol')],
  });
  expect(requests.map((request) => request.path).sort()).toEqual([
    '/api/v1/bots/cmwbob/webhook',
    '/api/v1/bots/cmwcarol/webhook',
  ]);
  expect(
    new Set(requests.map((request) => request.headers['idempotency-key'])),
  ).toEqual(new Set([sent.id]));
});

test('an ask wakes the recipient with an api-thread ref and the API reply route', async () => {
  const alice = await register('alice');
  const bob = await register('bob');
  const thread = await ask(alice.token, 'bob', 'why is CI red?');
  expect(thread.wake).toEqual({
    enabled: true,
    deliveries: [delivered('bob')],
  });
  expect(requests).toHaveLength(1);
  const payload = JSON.parse(requests[0]?.body ?? '');
  expect(payload).toMatchObject({
    event: 'request',
    // Not the CLI's `thread:` prefix: a routine that routes on it would run
    // `wake --reply`, which cannot read this API's store (issue #96).
    ref: `api-thread:${thread.threadId}`,
    threadId: thread.threadId,
    messageId: thread.messageId,
    from: 'alice',
    to: 'bob',
  });
  expect(payload.detail).toContain(`POST /threads/${thread.threadId}/reply`);
  expect(payload.detail).not.toContain('wake --reply');
  expect(payload.depth).toBe(0);
  expect(requests[0]?.headers['idempotency-key']).toBe(thread.messageId);

  // The hint is only honest if the route it names answers the recipient: the
  // bot's own agent token, not the CLI and not the asker's identity.
  const answer = await reply(bob.token, thread.threadId, 'because billing');
  expect(answer.seq).toBe(2);
});

test('a reply wakes the asker', async () => {
  const alice = await register('alice');
  const bob = await register('bob');
  const thread = await ask(alice.token, 'bob', 'question');
  requests.length = 0;
  const answer = await reply(bob.token, thread.threadId, 'because billing');
  expect(answer.wake).toEqual({
    enabled: true,
    deliveries: [delivered('alice')],
  });
  expect(requests).toHaveLength(1);
  expect(requests[0]?.path).toBe('/api/v1/bots/cmwalice/webhook');
  const payload = JSON.parse(requests[0]?.body ?? '');
  expect(payload).toMatchObject({
    depth: 0,
    event: 'reply',
    // Informational: no thread ref, so a ref-routing receiver cannot try to
    // answer an answer.
    ref: '',
    from: 'bob',
    to: 'alice',
    // The asker cannot answer its own ask (409), so a reply carries the form
    // bin/wake sends the asker: `reply on thread <id>: <text>`.
    detail: `reply on thread ${thread.threadId}: because billing`,
  });
  expect(payload.detail).not.toContain('wake --reply');
  expect(requests[0]?.headers['idempotency-key']).toBe(answer.messageId);
});

test('a recipient with no doorbell target is not rung and not reported', async () => {
  const alice = await register('alice');
  await register('dave');
  const sent = await send(alice.token, { to: 'dave', body: 'hello dave' });
  expect(sent.wake).toEqual({ enabled: true, deliveries: [] });
  expect(requests).toHaveLength(0);
});

test('with no doorbell configured a send still answers and rings nothing', async () => {
  const plain = buildApp(databaseUrl);
  try {
    await plain.ready();
    await plain.migrationsReady;
    const registration = await plain.inject({
      method: 'POST',
      url: '/agents',
      payload: { name: 'alice' },
    });
    const alice: RegisteredAgent = registration.json();
    await plain.inject({
      method: 'POST',
      url: '/agents',
      payload: { name: 'bob' },
    });
    const response = await plain.inject({
      method: 'POST',
      url: '/messages',
      headers: bearer(alice.token),
      payload: { to: 'bob', body: 'no doorbell' },
    });
    expect(response.statusCode).toBe(201);
    const sent: SentMessage = response.json();
    expect(sent.wake).toEqual({ enabled: false, deliveries: [] });
    expect(requests).toHaveLength(0);
    const none = await plain.inject({
      method: 'GET',
      url: `/messages/${sent.id}/deliveries`,
      headers: bearer(alice.token),
    });
    expect(none.statusCode).toBe(200);
    expect(none.json()).toEqual({ messageId: sent.id, deliveries: [] });
  } finally {
    await plain.close();
  }
});

// Regression: POST /threads used to hold its pool client across the ring, so with
// every connection checked out the receipts query waited out the 2 s connect
// timeout, the ring was skipped, and the response reported `deliveries: []` as if
// bob had no target. One connection is the deterministic repro.
test('an ask rings the doorbell even when the pool has one connection', async () => {
  const single = buildApp(databaseUrl, { doorbell: config, max: 1 });
  try {
    await single.ready();
    await single.migrationsReady;
    const registration = await single.inject({
      method: 'POST',
      url: '/agents',
      payload: { name: 'alice' },
    });
    const alice: RegisteredAgent = registration.json();
    await single.inject({
      method: 'POST',
      url: '/agents',
      payload: { name: 'bob' },
    });
    requests.length = 0;
    const response = await single.inject({
      method: 'POST',
      url: '/threads',
      headers: bearer(alice.token),
      payload: { to: 'bob', body: 'are you there?' },
    });
    expect(response.statusCode).toBe(201);
    const thread: CreatedThread = response.json();
    expect(thread.wake).toEqual({
      enabled: true,
      deliveries: [delivered('bob')],
    });
    expect(requests).toHaveLength(1);
    expect(requests[0]?.path).toBe('/api/v1/bots/cmwbob/webhook');
    expect(requests[0]?.headers['idempotency-key']).toBe(thread.messageId);
  } finally {
    await single.close();
  }
});

test('the sender can read what the doorbell did, with status and attempts', async () => {
  const alice = await register('alice');
  await register('bob');
  const sent = await send(alice.token, { to: 'bob', body: 'did it land?' });
  const got = await deliveries(alice.token, sent.id);
  expect(got.status).toBe(200);
  expect(got.body).toEqual({
    messageId: sent.id,
    deliveries: [
      {
        agent: 'bob',
        delivered: true,
        reason: null,
        attempts: 1,
        status: 200,
        at: expect.any(String),
      },
    ],
  });
});

test('an undelivered wake is persisted with its reason and last status', async () => {
  stubReply = { status: 503, body: JSON.stringify({ error: 'unavailable' }) };
  const alice = await register('alice');
  await register('bob');
  const sent = await send(alice.token, { to: 'bob', body: 'still there?' });
  expect(sent.wake.deliveries[0]).toMatchObject({
    delivered: false,
    reason: 'server_error',
    attempts: 2,
  });
  const got = await deliveries(alice.token, sent.id);
  expect(got.body).toEqual({
    messageId: sent.id,
    deliveries: [
      {
        agent: 'bob',
        delivered: false,
        reason: 'server_error',
        attempts: 2,
        status: 503,
        at: expect.any(String),
      },
    ],
  });
});

test('delivery outcomes are private to the sender', async () => {
  const alice = await register('alice');
  const bob = await register('bob');
  const sent = await send(alice.token, { to: 'bob', body: 'mine' });
  // A recipient asking about someone else's message gets the same answer as a
  // message that does not exist, so ids cannot be probed.
  const stranger = await deliveries(bob.token, sent.id);
  expect(stranger.status).toBe(404);
  expect(stranger.body).toEqual({ error: 'not found' });
  const missing = await deliveries(
    alice.token,
    '00000000-0000-4000-8000-000000000000',
  );
  expect(missing.status).toBe(404);
});

test('one sender exhausting the cap does not silence another sender', async () => {
  // The cap is 1 per sender and recipient: alice's wake spends alice's budget,
  // and carol waking the same bot must still ring. Keying on the recipient
  // alone made this a silent drop, not an error.
  const capped = buildApp(databaseUrl, {
    doorbell: parseDoorbell({
      WAKE_DOORBELL_BOTS: JSON.stringify({
        bob: { botId: 'cmwbob', secret: SECRET },
      }),
      WAKE_DOORBELL_URL: `http://127.0.0.1:${stub.port}/api/v1/bots`,
      WAKE_DOORBELL_TIMEOUT_MS: '500',
      WAKE_DOORBELL_MAX_PER_HOUR: '1',
    }),
  });
  try {
    await capped.ready();
    await capped.migrationsReady;
    await admin.query('TRUNCATE agents CASCADE');
    requests.length = 0;
    const people: Record<string, RegisteredAgent> = {};
    for (const name of ['alice', 'carol', 'bob']) {
      const response = await capped.inject({
        method: 'POST',
        url: '/agents',
        payload: { name },
      });
      expect(response.statusCode).toBe(201);
      people[name] = response.json<RegisteredAgent>();
    }
    const tokenOf = (name: string): string => {
      const agent = people[name];
      if (agent === undefined) {
        throw new Error(`missing agent ${name}`);
      }
      return agent.token;
    };
    const first = await capped.inject({
      method: 'POST',
      url: '/messages',
      headers: bearer(tokenOf('alice')),
      payload: { to: 'bob', body: 'from alice' },
    });
    expect(first.json<SentMessage>().wake.deliveries).toEqual([
      { agent: 'bob', delivered: true, reason: null, attempts: 1 },
    ]);
    const second = await capped.inject({
      method: 'POST',
      url: '/messages',
      headers: bearer(tokenOf('carol')),
      payload: { to: 'bob', body: 'from carol' },
    });
    expect(second.json<SentMessage>().wake.deliveries).toEqual([
      { agent: 'bob', delivered: true, reason: null, attempts: 1 },
    ]);
    expect(requests).toHaveLength(2);
    // Alice, spent, is refused without a request.
    const third = await capped.inject({
      method: 'POST',
      url: '/messages',
      headers: bearer(tokenOf('alice')),
      payload: { to: 'bob', body: 'from alice again' },
    });
    expect(third.json<SentMessage>().wake.deliveries).toEqual([
      { agent: 'bob', delivered: false, reason: 'rate_limited', attempts: 0 },
    ]);
    expect(requests).toHaveLength(2);
  } finally {
    await capped.close();
  }
});
