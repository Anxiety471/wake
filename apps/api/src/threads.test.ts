import { Client } from 'pg';
import { afterAll, beforeAll, beforeEach, expect, test } from 'bun:test';
import { buildApp } from './app.js';
import { ConfigError } from './options.js';
import { assertTestDatabaseUrl } from './test-helpers/test-database.js';
import { parseThreadTtlHours } from './threads.js';
import type {
  CreatedThread,
  InboxPage,
  RegisteredAgent,
  ThreadDetail,
  ThreadPage,
  ThreadReply,
} from '@wake/shared';

const databaseUrl = assertTestDatabaseUrl(process.env.TEST_DATABASE_URL);

const app = buildApp(databaseUrl);
const shortTtlApp = buildApp(databaseUrl, { threadTtlHours: 1 });
const noTtlApp = buildApp(databaseUrl, { threadTtlHours: null });
const admin = new Client({ connectionString: databaseUrl });

const UNKNOWN_ID = '00000000-0000-4000-8000-000000000000';

beforeAll(async () => {
  await admin.connect();
  await app.ready();
  await app.migrationsReady;
  await shortTtlApp.ready();
  await shortTtlApp.migrationsReady;
  await noTtlApp.ready();
  await noTtlApp.migrationsReady;
});

beforeEach(async () => {
  await admin.query('TRUNCATE agents CASCADE');
});

afterAll(async () => {
  await noTtlApp.close();
  await shortTtlApp.close();
  await app.close();
  await admin.end();
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

async function ask(
  token: string,
  payload: { to: string; body: string; kind?: 'ask' | 'thread' },
): Promise<CreatedThread> {
  const response = await app.inject({
    method: 'POST',
    url: '/threads',
    headers: bearer(token),
    payload,
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

async function detail(token: string, threadId: string): Promise<ThreadDetail> {
  const response = await app.inject({
    method: 'GET',
    url: `/threads/${threadId}`,
    headers: bearer(token),
  });
  expect(response.statusCode).toBe(200);
  return response.json();
}

async function threads(
  token: string,
  page?: { limit?: number; cursor?: string },
): Promise<ThreadPage> {
  const params = new URLSearchParams();
  if (page?.limit !== undefined) {
    params.set('limit', String(page.limit));
  }
  if (page?.cursor !== undefined) {
    params.set('cursor', page.cursor);
  }
  const query = params.toString();
  const response = await app.inject({
    method: 'GET',
    url: query === '' ? '/threads' : `/threads?${query}`,
    headers: bearer(token),
  });
  expect(response.statusCode).toBe(200);
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

test('an ask stores one question, and both sides see the thread', async () => {
  const alpha = await register('alpha');
  const beta = await register('beta');

  const created = await ask(alpha.token, { to: 'beta', body: 'ping?' });
  expect(created.seq).toBe(1);
  expect(created.threadId).toMatch(/^[0-9a-f-]{36}$/);

  const seenByAlpha = await detail(alpha.token, created.threadId);
  expect(seenByAlpha.kind).toBe('ask');
  expect(seenByAlpha.status).toBe('open');
  expect(seenByAlpha.createdBy).toBe('alpha');
  expect(seenByAlpha.lastSeq).toBe(1);
  expect(seenByAlpha.myReadSeq).toBe(1);
  expect(seenByAlpha.expired).toBe(false);
  expect(seenByAlpha.participants).toEqual([
    { name: 'alpha' },
    { name: 'beta' },
  ]);
  expect(seenByAlpha.messages).toEqual([
    {
      id: created.messageId,
      seq: 1,
      from: 'alpha',
      body: 'ping?',
      createdAt: created.createdAt,
    },
  ]);
  expect(seenByAlpha.expiresAt).not.toBeNull();
  expect(new Date(seenByAlpha.expiresAt ?? '').getTime()).toBeGreaterThan(
    new Date(seenByAlpha.updatedAt).getTime(),
  );

  const seenByBeta = await detail(beta.token, created.threadId);
  expect(seenByBeta.myReadSeq).toBe(0);

  const alphaThreads = await threads(alpha.token);
  expect(alphaThreads.threads).toHaveLength(1);
  expect(alphaThreads.threads[0]?.unread).toBe(0);
  expect(alphaThreads.threads[0]?.participants).toEqual([
    { name: 'alpha' },
    { name: 'beta' },
  ]);

  const betaThreads = await threads(beta.token);
  expect(betaThreads.threads).toHaveLength(1);
  expect(betaThreads.threads[0]?.unread).toBe(1);

  const betaInbox = await inbox(beta.token);
  expect(betaInbox.messages).toHaveLength(1);
  expect(betaInbox.messages[0]?.threadId).toBe(created.threadId);
  expect(betaInbox.messages[0]?.from).toBe('alpha');
  expect(betaInbox.messages[0]?.body).toBe('ping?');
});

test('the recipient answers once and the round closes', async () => {
  const alpha = await register('alpha');
  const beta = await register('beta');
  const created = await ask(alpha.token, { to: 'beta', body: 'status?' });

  const answered = await reply(beta.token, created.threadId, 'green');
  expect(answered.seq).toBe(2);
  expect(answered.status).toBe('answered');

  const alphaInbox = await inbox(alpha.token);
  expect(alphaInbox.messages).toHaveLength(1);
  expect(alphaInbox.messages[0]?.threadId).toBe(created.threadId);
  expect(alphaInbox.messages[0]?.body).toBe('green');
  expect(alphaInbox.messages[0]?.from).toBe('beta');

  const after = await detail(alpha.token, created.threadId);
  expect(after.status).toBe('answered');
  expect(after.lastSeq).toBe(2);
  expect(after.messages.map((message) => message.seq)).toEqual([1, 2]);

  const againByBeta = await app.inject({
    method: 'POST',
    url: `/threads/${created.threadId}/reply`,
    headers: bearer(beta.token),
    payload: { body: 'one more' },
  });
  expect(againByBeta.statusCode).toBe(409);
  expect(againByBeta.json()).toEqual({ error: 'thread already answered' });

  const againByAlpha = await app.inject({
    method: 'POST',
    url: `/threads/${created.threadId}/reply`,
    headers: bearer(alpha.token),
    payload: { body: 'thanks' },
  });
  expect(againByAlpha.statusCode).toBe(409);
  expect(againByAlpha.json()).toEqual({
    error: 'the asker cannot answer its own ask',
  });
});

test('the asker cannot answer its own open ask', async () => {
  const alpha = await register('alpha');
  await register('beta');
  const created = await ask(alpha.token, { to: 'beta', body: 'ping?' });

  const response = await app.inject({
    method: 'POST',
    url: `/threads/${created.threadId}/reply`,
    headers: bearer(alpha.token),
    payload: { body: 'hello?' },
  });
  expect(response.statusCode).toBe(409);
  expect(response.json()).toEqual({
    error: 'the asker cannot answer its own ask',
  });
});

test('a thread older than the TTL refuses replies and reads as expired', async () => {
  const alpha = await register('alpha');
  const beta = await register('beta');
  const created = await ask(alpha.token, { to: 'beta', body: 'ping?' });
  await admin.query(
    `UPDATE threads
     SET updated_at = clock_timestamp() - interval '25 hours'
     WHERE id = $1`,
    [created.threadId],
  );

  const response = await app.inject({
    method: 'POST',
    url: `/threads/${created.threadId}/reply`,
    headers: bearer(beta.token),
    payload: { body: 'too late' },
  });
  expect(response.statusCode).toBe(410);
  expect(response.json()).toEqual({ error: 'thread expired' });

  const seen = await detail(beta.token, created.threadId);
  expect(seen.expired).toBe(true);
  expect(seen.messages).toHaveLength(1);
});

test('threadTtlHours changes the expiry window', async () => {
  const alpha = await register('alpha');
  const beta = await register('beta');
  const created = await ask(alpha.token, { to: 'beta', body: 'ping?' });
  await admin.query(
    `UPDATE threads
     SET updated_at = clock_timestamp() - interval '2 hours'
     WHERE id = $1`,
    [created.threadId],
  );

  const response = await shortTtlApp.inject({
    method: 'POST',
    url: `/threads/${created.threadId}/reply`,
    headers: bearer(beta.token),
    payload: { body: 'too late' },
  });
  expect(response.statusCode).toBe(410);
  expect(response.json()).toEqual({ error: 'thread expired' });
});

test('threadTtlHours null keeps an old thread answerable', async () => {
  const alpha = await register('alpha');
  const beta = await register('beta');
  const created = await ask(alpha.token, { to: 'beta', body: 'ping?' });
  await admin.query(
    `UPDATE threads
     SET updated_at = clock_timestamp() - interval '30 days'
     WHERE id = $1`,
    [created.threadId],
  );

  const response = await noTtlApp.inject({
    method: 'POST',
    url: `/threads/${created.threadId}/reply`,
    headers: bearer(beta.token),
    payload: { body: 'late but allowed' },
  });
  expect(response.statusCode).toBe(201);
  expect(response.json()).toEqual({
    threadId: created.threadId,
    messageId: expect.any(String),
    seq: 2,
    createdAt: expect.any(String),
    status: 'answered',
    wake: { enabled: false, deliveries: [] },
  });

  const read = await noTtlApp.inject({
    method: 'GET',
    url: `/threads/${created.threadId}`,
    headers: bearer(alpha.token),
  });
  expect(read.json().expired).toBe(false);
  expect(read.json().expiresAt).toBeNull();
});

test('a closed thread refuses replies', async () => {
  const alpha = await register('alpha');
  const beta = await register('beta');
  const created = await ask(alpha.token, { to: 'beta', body: 'ping?' });
  await admin.query(`UPDATE threads SET status = 'closed' WHERE id = $1`, [
    created.threadId,
  ]);

  const response = await app.inject({
    method: 'POST',
    url: `/threads/${created.threadId}/reply`,
    headers: bearer(beta.token),
    payload: { body: 'hello' },
  });
  expect(response.statusCode).toBe(409);
  expect(response.json()).toEqual({ error: 'thread closed' });
});

test('bots outside a thread cannot read it, answer it, or list it', async () => {
  const alpha = await register('alpha');
  await register('beta');
  const gamma = await register('gamma');
  const created = await ask(alpha.token, { to: 'beta', body: 'secret' });

  const read = await app.inject({
    method: 'GET',
    url: `/threads/${created.threadId}`,
    headers: bearer(gamma.token),
  });
  expect(read.statusCode).toBe(404);
  expect(read.json()).toEqual({ error: 'not found' });

  const answer = await app.inject({
    method: 'POST',
    url: `/threads/${created.threadId}/reply`,
    headers: bearer(gamma.token),
    payload: { body: 'butting in' },
  });
  expect(answer.statusCode).toBe(404);

  const listed = await threads(gamma.token);
  expect(listed.threads).toEqual([]);

  const unread = await app.inject({
    method: 'POST',
    url: `/threads/${created.threadId}/read`,
    headers: bearer(gamma.token),
    payload: { seq: 1 },
  });
  expect(unread.statusCode).toBe(404);
});

test('a kind=thread conversation keeps accepting messages', async () => {
  const alpha = await register('alpha');
  const beta = await register('beta');
  const created = await ask(alpha.token, {
    to: 'beta',
    body: 'plan?',
    kind: 'thread',
  });

  const first = await reply(beta.token, created.threadId, 'step one');
  expect(first.status).toBe('open');
  const second = await reply(alpha.token, created.threadId, 'step two');
  expect(second.seq).toBe(3);
  expect(second.status).toBe('open');

  const seen = await detail(beta.token, created.threadId);
  expect(seen.messages.map((message) => message.body)).toEqual([
    'plan?',
    'step one',
    'step two',
  ]);
});

test('marking a thread read clears its unread count and clamps to lastSeq', async () => {
  const alpha = await register('alpha');
  const beta = await register('beta');
  const created = await ask(alpha.token, { to: 'beta', body: 'ping?' });

  const before = await threads(beta.token);
  expect(before.threads[0]?.unread).toBe(1);

  const read = await app.inject({
    method: 'POST',
    url: `/threads/${created.threadId}/read`,
    headers: bearer(beta.token),
    payload: { seq: 99 },
  });
  expect(read.statusCode).toBe(200);
  expect(read.json()).toEqual({ threadId: created.threadId, readSeq: 1 });

  const after = await threads(beta.token);
  expect(after.threads[0]?.unread).toBe(0);
  expect(after.threads[0]?.myReadSeq).toBe(1);
});

test('threads page by an opaque cursor without overlap', async () => {
  const alpha = await register('alpha');
  const beta = await register('beta');
  for (const body of ['one', 'two', 'three']) {
    await ask(alpha.token, { to: 'beta', body });
  }

  const first = await threads(beta.token, { limit: 2 });
  expect(first.threads).toHaveLength(2);
  expect(first.next).not.toBeNull();

  const cursor = first.next ?? '';
  const second = await threads(beta.token, { limit: 2, cursor });
  expect(second.threads).toHaveLength(1);
  expect(second.next).toBeNull();

  const seen = new Set(first.threads.map((thread) => thread.id));
  expect(seen.has(second.threads[0]?.id ?? '')).toBe(false);
});

test('POST /threads refuses bad recipients and bodies', async () => {
  const alpha = await register('alpha');
  await register('beta');

  const cases: { payload: Record<string, unknown>; status: number }[] = [
    { payload: { to: '*', body: 'all' }, status: 400 },
    { payload: { to: 'alpha', body: 'me' }, status: 400 },
    { payload: { to: 'nobody', body: 'ghost' }, status: 404 },
    { payload: { to: 'beta', body: '' }, status: 400 },
    { payload: { to: 'beta', body: 'x'.repeat(8193) }, status: 400 },
    { payload: { to: 'beta', body: 'ok', kind: 'broadcast' }, status: 400 },
  ];
  for (const item of cases) {
    const response = await app.inject({
      method: 'POST',
      url: '/threads',
      headers: bearer(alpha.token),
      payload: item.payload,
    });
    expect(response.statusCode).toBe(item.status);
  }

  const anonymous = await app.inject({
    method: 'POST',
    url: '/threads',
    payload: { to: 'beta', body: 'hello' },
  });
  expect(anonymous.statusCode).toBe(401);
});

test('unknown thread ids are not found', async () => {
  const alpha = await register('alpha');
  const read = await app.inject({
    method: 'GET',
    url: `/threads/${UNKNOWN_ID}`,
    headers: bearer(alpha.token),
  });
  expect(read.statusCode).toBe(404);

  const answer = await app.inject({
    method: 'POST',
    url: `/threads/${UNKNOWN_ID}/reply`,
    headers: bearer(alpha.token),
    payload: { body: 'hello' },
  });
  expect(answer.statusCode).toBe(404);

  const malformed = await app.inject({
    method: 'GET',
    url: '/threads/not-a-uuid',
    headers: bearer(alpha.token),
  });
  expect(malformed.statusCode).toBe(400);
});

test('two simultaneous answers yield one 201 and one 409, never a 500', async () => {
  const alpha = await register('alpha');
  const beta = await register('beta');
  const created = await ask(alpha.token, { to: 'beta', body: 'race' });

  const gate = new Client({ connectionString: databaseUrl });
  await gate.connect();
  try {
    await gate.query('BEGIN');
    await gate.query('SELECT 1 FROM threads WHERE id = $1 FOR UPDATE', [
      created.threadId,
    ]);

    const first = app.inject({
      method: 'POST',
      url: `/threads/${created.threadId}/reply`,
      headers: bearer(beta.token),
      payload: { body: 'one' },
    });
    const second = app.inject({
      method: 'POST',
      url: `/threads/${created.threadId}/reply`,
      headers: bearer(beta.token),
      payload: { body: 'two' },
    });
    await new Promise((resolve) => setTimeout(resolve, 300));
    await gate.query('COMMIT');

    const codes = (await Promise.all([first, second]))
      .map((response) => response.statusCode)
      .sort();
    expect(codes).toEqual([201, 409]);
  } finally {
    await gate.query('ROLLBACK').catch(() => undefined);
    await gate.end();
  }

  const after = await detail(alpha.token, created.threadId);
  expect(after.lastSeq).toBe(2);
  expect(after.status).toBe('answered');
});

test('paging by creation never skips a thread that is answered mid-page', async () => {
  const alpha = await register('alpha');
  const beta = await register('beta');
  const oldest = await ask(alpha.token, { to: 'beta', body: 'one' });
  for (const body of ['two', 'three', 'four', 'five', 'six']) {
    await ask(alpha.token, { to: 'beta', body });
  }

  const first = await threads(beta.token, { limit: 2 });
  expect(first.threads).toHaveLength(2);
  expect(first.next).not.toBeNull();

  await reply(beta.token, oldest.threadId, 'answered after page one');

  const seen = new Set(first.threads.map((thread) => thread.id));
  let cursor = first.next;
  while (cursor !== null) {
    const page = await threads(beta.token, { limit: 2, cursor });
    for (const thread of page.threads) {
      seen.add(thread.id);
    }
    cursor = page.next;
  }

  expect(seen.size).toBe(6);
  expect(seen.has(oldest.threadId)).toBe(true);
});

test('reading an ask in the inbox also moves the thread read position', async () => {
  const alpha = await register('alpha');
  const beta = await register('beta');
  const created = await ask(alpha.token, { to: 'beta', body: 'ping?' });

  const pending = await inbox(beta.token);
  expect(pending.messages).toHaveLength(1);
  expect(pending.messages[0]?.threadId).toBe(created.threadId);

  const read = await app.inject({
    method: 'POST',
    url: '/inbox/read',
    headers: bearer(beta.token),
    payload: { ids: [pending.messages[0]?.id] },
  });
  expect(read.statusCode).toBe(200);

  const after = await detail(beta.token, created.threadId);
  expect(after.myReadSeq).toBe(1);
  const list = await threads(beta.token);
  expect(list.threads[0]?.myReadSeq).toBe(1);
  expect(list.threads[0]?.unread).toBe(0);
});

test('one read receipt covering two messages of the same thread advances to the newest', async () => {
  const alpha = await register('alpha');
  const beta = await register('beta');

  const created = await ask(alpha.token, {
    to: 'beta',
    body: 'discussing',
    kind: 'thread',
  });
  const first = await reply(beta.token, created.threadId, 'first');
  expect(first.seq).toBe(2);
  const second = await reply(alpha.token, created.threadId, 'second');
  expect(second.seq).toBe(3);

  const before = await detail(beta.token, created.threadId);
  expect(before.myReadSeq).toBe(2);
  expect(before.lastSeq).toBe(3);

  const pending = await inbox(beta.token);
  const ids = pending.messages
    .filter((message) => message.threadId === created.threadId)
    .map((message) => message.id);
  expect(ids).toContain(created.messageId);
  expect(ids).toContain(second.messageId);

  const read = await app.inject({
    method: 'POST',
    url: '/inbox/read',
    headers: bearer(beta.token),
    payload: { ids },
  });
  expect(read.statusCode).toBe(200);

  const after = await detail(beta.token, created.threadId);
  expect(after.myReadSeq).toBe(3);
  const list = await threads(beta.token);
  expect(list.threads[0]?.myReadSeq).toBe(3);
  expect(list.threads[0]?.unread).toBe(0);
});

test('WAKE_TTL_HOURS accepts 0 and 1 to 720, rejecting anything else', () => {
  expect(parseThreadTtlHours(undefined)).toBe(24);
  expect(parseThreadTtlHours('')).toBe(24);
  expect(parseThreadTtlHours('0')).toBeNull();
  expect(parseThreadTtlHours('1')).toBe(1);
  expect(parseThreadTtlHours('720')).toBe(720);
  for (const value of ['721', '-1', '1.5', 'soon', ' 24', '00']) {
    expect(() => parseThreadTtlHours(value)).toThrow(ConfigError);
  }
});
