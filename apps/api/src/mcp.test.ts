import { Client } from 'pg';
import { afterAll, beforeAll, beforeEach, expect, test } from 'bun:test';
import type { LightMyRequestResponse } from 'fastify';
import { buildApp } from './app.js';
import { parseMcp } from './mcp.js';
import { assertTestDatabaseUrl } from './test-helpers/test-database.js';
import type { RegisteredAgent, ThreadDetail, ThreadPage } from '@wake/shared';

const databaseUrl = assertTestDatabaseUrl(process.env.TEST_DATABASE_URL);
const OPERATOR = 'operator-secret-0123456789';

const app = buildApp(databaseUrl, {
  registrationSecret: OPERATOR,
  mcp: parseMcp({
    WAKE_MCP_ENABLED: 'true',
    // This file exercises the protocol, not the limiter; the limiter has its
    // own test with its own app.
    WAKE_MCP_RATE_LIMIT_PER_MINUTE: '0',
  }),
});
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

function operator(): { 'x-registration-secret': string } {
  return { 'x-registration-secret': OPERATOR };
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

async function identity(): Promise<RegisteredAgent> {
  const response = await app.inject({
    method: 'POST',
    url: '/mcp/identity',
    headers: operator(),
  });
  expect(response.statusCode).toBe(201);
  return response.json();
}

async function rpc(
  token: string,
  message: Record<string, unknown>,
  headers: Record<string, string> = {},
): Promise<LightMyRequestResponse> {
  return app.inject({
    method: 'POST',
    url: '/mcp',
    headers: {
      ...bearer(token),
      accept: 'application/json, text/event-stream',
      ...headers,
    },
    payload: message,
  });
}

type RpcReply = {
  jsonrpc: string;
  id: number | string | null;
  result?: Record<string, unknown>;
  error?: { code: number; message: string };
};

async function json(token: string, message: Record<string, unknown>) {
  const response = await rpc(token, message);
  return { status: response.statusCode, body: response.json() as RpcReply };
}

async function callTool(
  token: string,
  name: string,
  args: Record<string, unknown> = {},
): Promise<{
  status: number;
  isError: boolean;
  text: string;
  raw: RpcReply;
}> {
  const response = await json(token, {
    jsonrpc: '2.0',
    id: 7,
    method: 'tools/call',
    params: { name, arguments: args },
  });
  const result = response.body.result as
    | { content: Array<{ type: string; text: string }>; isError?: boolean }
    | undefined;
  return {
    status: response.status,
    isError: result?.isError === true,
    text: result?.content[0]?.text ?? '',
    raw: response.body,
  };
}

test('the endpoint does not exist when MCP is off', async () => {
  const off = buildApp(databaseUrl, { registrationSecret: OPERATOR });
  try {
    await off.ready();
    const token = (await identity()).token;
    const missing = await off.inject({
      method: 'POST',
      url: '/mcp',
      headers: bearer(token),
      payload: { jsonrpc: '2.0', id: 1, method: 'ping' },
    });
    expect(missing.statusCode).toBe(404);
  } finally {
    await off.close();
  }
});

test('only the mcp identity may use the endpoint', async () => {
  const bob = await register('bob');
  const mcp = await identity();
  const anonymous = await app.inject({
    method: 'POST',
    url: '/mcp',
    payload: { jsonrpc: '2.0', id: 1, method: 'ping' },
  });
  expect(anonymous.statusCode).toBe(401);
  const asBot = await rpc(bob.token, {
    jsonrpc: '2.0',
    id: 1,
    method: 'ping',
  });
  expect(asBot.statusCode).toBe(401);
  expect(asBot.json()).toEqual({ error: 'unauthorized' });
  const asMcp = await rpc(mcp.token, { jsonrpc: '2.0', id: 1, method: 'ping' });
  expect(asMcp.statusCode).toBe(200);
  expect(asMcp.json()).toEqual({ jsonrpc: '2.0', id: 1, result: {} });
});

test('initialize names the protocol, tools and server', async () => {
  const mcp = await identity();
  const { status, body } = await json(mcp.token, {
    jsonrpc: '2.0',
    id: 1,
    method: 'initialize',
    params: {
      protocolVersion: '2025-06-18',
      capabilities: {},
      clientInfo: { name: 'test', version: '1' },
    },
  });
  expect(status).toBe(200);
  expect(body.result).toMatchObject({
    protocolVersion: '2025-06-18',
    capabilities: { tools: { listChanged: false } },
    serverInfo: { name: 'wake', version: '0.1.0' },
  });
  const initialized = await rpc(mcp.token, {
    jsonrpc: '2.0',
    method: 'notifications/initialized',
  });
  expect(initialized.statusCode).toBe(202);
  expect(initialized.body).toBe('');
  const wrongVersion = await rpc(
    mcp.token,
    { jsonrpc: '2.0', id: 2, method: 'ping' },
    { 'mcp-protocol-version': '2024-11-05' },
  );
  expect(wrongVersion.statusCode).toBe(400);
});

test('tools/list describes the thread tools', async () => {
  const mcp = await identity();
  const { status, body } = await json(mcp.token, {
    jsonrpc: '2.0',
    id: 1,
    method: 'tools/list',
  });
  expect(status).toBe(200);
  const tools = (body.result as { tools: Array<{ name: string }> }).tools;
  expect(tools.map((tool) => tool.name)).toEqual([
    'wake_ask',
    'wake_reply',
    'wake_read_thread',
    'wake_thread_read',
    'wake_threads',
    'wake_inbox',
    'wake_inbox_read',
    'wake_bots',
    'wake_send',
    'wake_search',
  ]);
});

test('wake_ask opens a thread and the recipient can answer it', async () => {
  const bob = await register('bob');
  const mcp = await identity();
  const asked = await callTool(mcp.token, 'wake_ask', {
    to: 'bob',
    body: 'why is the build red?',
  });
  expect(asked.isError).toBe(false);
  const thread = JSON.parse(asked.text) as {
    threadId: string;
    messageId: string;
    seq: number;
  };
  expect(thread.threadId).toBeString();
  expect(thread.seq).toBe(1);

  // The recipient sees it through the ordinary API.
  const bobThreads = await app.inject({
    method: 'GET',
    url: '/threads',
    headers: bearer(bob.token),
  });
  expect((bobThreads.json() as ThreadPage).threads[0]?.id).toBe(
    thread.threadId,
  );

  const answered = await app.inject({
    method: 'POST',
    url: `/threads/${thread.threadId}/reply`,
    headers: bearer(bob.token),
    payload: { body: 'because billing' },
  });
  expect(answered.statusCode).toBe(201);

  const read = await callTool(mcp.token, 'wake_read_thread', {
    threadId: thread.threadId,
  });
  expect(read.isError).toBe(false);
  const detail = JSON.parse(read.text) as ThreadDetail;
  expect(detail.messages.map((message) => message.body)).toEqual([
    'why is the build red?',
    'because billing',
  ]);

  const marked = await callTool(mcp.token, 'wake_thread_read', {
    threadId: thread.threadId,
    seq: 2,
  });
  expect(marked.isError).toBe(false);
  expect(JSON.parse(marked.text)).toMatchObject({
    threadId: thread.threadId,
    readSeq: 2,
  });

  const listed = await callTool(mcp.token, 'wake_threads', { limit: 5 });
  expect(listed.isError).toBe(false);
});

test('wake_send and wake_inbox move a message both ways', async () => {
  const bob = await register('bob');
  const mcp = await identity();
  const sent = await callTool(mcp.token, 'wake_send', {
    to: 'bob',
    body: 'doorbell says hi',
  });
  expect(sent.isError).toBe(false);
  const bobInbox = await app.inject({
    method: 'GET',
    url: '/inbox',
    headers: bearer(bob.token),
  });
  const messages = (
    bobInbox.json() as { messages: Array<{ id: string; body: string }> }
  ).messages;
  expect(messages.map((message) => message.body)).toEqual(['doorbell says hi']);

  const nudge = await app.inject({
    method: 'POST',
    url: '/messages',
    headers: bearer(bob.token),
    payload: { to: 'mcp', body: 'heard you' },
  });
  expect(nudge.statusCode).toBe(201);
  const mcpInbox = await callTool(mcp.token, 'wake_inbox', { unread: true });
  const page = JSON.parse(mcpInbox.text) as {
    messages: Array<{ id: string; body: string }>;
  };
  expect(page.messages.map((message) => message.body)).toEqual(['heard you']);
  const marked = await callTool(mcp.token, 'wake_inbox_read', {
    ids: [page.messages[0]?.id],
  });
  expect(marked.isError).toBe(false);
});

test('wake_bots hides private bots and wake_ask cannot reach them', async () => {
  await register('bob');
  const mcp = await identity();
  const before = await callTool(mcp.token, 'wake_bots');
  expect(before.isError).toBe(false);
  expect(before.text).toContain('"bob"');

  await app.inject({
    method: 'PUT',
    url: '/agents/bob/private',
    headers: operator(),
    payload: { private: true },
  });

  const bots = await callTool(mcp.token, 'wake_bots');
  expect(bots.isError).toBe(false);
  expect(bots.text).not.toContain('"bob"');
  expect(bots.text).toContain('"mcp"');
  const refused = await callTool(mcp.token, 'wake_ask', {
    to: 'bob',
    body: 'hello?',
  });
  expect(refused.isError).toBe(true);
  expect(refused.text).toContain('404');
});

test('a bad tool call is a JSON-RPC error, a failed call is a tool error', async () => {
  const mcp = await identity();
  const unknown = await json(mcp.token, {
    jsonrpc: '2.0',
    id: 1,
    method: 'tools/call',
    params: { name: 'wake_nope', arguments: {} },
  });
  expect(unknown.body.error).toMatchObject({ code: -32602 });
  const missing = await json(mcp.token, {
    jsonrpc: '2.0',
    id: 2,
    method: 'tools/call',
    params: { name: 'wake_ask', arguments: { to: 'bob' } },
  });
  expect(missing.body.error).toMatchObject({ code: -32602 });
  expect(missing.body.error?.message).toContain('body');
  const method = await json(mcp.token, {
    jsonrpc: '2.0',
    id: 3,
    method: 'resources/list',
  });
  expect(method.body.error).toMatchObject({ code: -32601 });
});

test('batching and a wrong Accept are refused at the transport', async () => {
  const mcp = await identity();
  const batch = await rpc(mcp.token, [
    { jsonrpc: '2.0', id: 1, method: 'ping' },
  ] as unknown as Record<string, unknown>);
  expect(batch.statusCode).toBe(400);
  const unacceptable = await rpc(
    mcp.token,
    { jsonrpc: '2.0', id: 1, method: 'ping' },
    { accept: 'text/plain' },
  );
  expect(unacceptable.statusCode).toBe(406);
  const get = await app.inject({
    method: 'GET',
    url: '/mcp',
    headers: bearer(mcp.token),
  });
  expect(get.statusCode).toBe(405);
  expect(get.headers['allow']).toBe('POST');
});

test('a client that only accepts event-stream gets one SSE event', async () => {
  const mcp = await identity();
  const response = await rpc(
    mcp.token,
    { jsonrpc: '2.0', id: 1, method: 'ping' },
    { accept: 'text/event-stream' },
  );
  expect(response.statusCode).toBe(200);
  expect(String(response.headers['content-type'])).toContain(
    'text/event-stream',
  );
  expect(response.body).toStartWith('event: message\ndata: ');
  expect(response.body.trimEnd()).toEndWith('}');
});

test('every call is audited, including a failed one', async () => {
  const mcp = await identity();
  await callTool(mcp.token, 'wake_ask', { to: 'nobody', body: 'hello' });
  await callTool(mcp.token, 'wake_bots');
  const rows = await admin.query<{
    method: string;
    tool: string | null;
    ok: boolean;
    error: string | null;
    status: number;
    remote: string | null;
  }>(
    `SELECT method, tool, ok, error, status, remote
     FROM mcp_audit
     WHERE agent_id = $1::uuid
     ORDER BY id ASC`,
    [mcp.id],
  );
  expect(rows.rows).toHaveLength(2);
  expect(rows.rows[0]).toMatchObject({
    method: 'tools/call',
    tool: 'wake_ask',
    ok: false,
    error: 'http_404',
    status: 404,
  });
  expect(rows.rows[1]).toMatchObject({
    method: 'tools/call',
    tool: 'wake_bots',
    ok: true,
    error: null,
    status: 200,
  });
});

test('the rate limit answers 429 with retry-after and is audited', async () => {
  const limited = buildApp(databaseUrl, {
    registrationSecret: OPERATOR,
    mcp: parseMcp({
      WAKE_MCP_ENABLED: 'true',
      WAKE_MCP_RATE_LIMIT_PER_MINUTE: '2',
    }),
  });
  try {
    await limited.ready();
    await limited.migrationsReady;
    await admin.query('TRUNCATE agents CASCADE');
    const created = await limited.inject({
      method: 'POST',
      url: '/mcp/identity',
      headers: operator(),
    });
    expect(created.statusCode).toBe(201);
    const token = created.json<RegisteredAgent>().token;
    const call = () =>
      limited.inject({
        method: 'POST',
        url: '/mcp',
        headers: bearer(token),
        payload: { jsonrpc: '2.0', id: 1, method: 'ping' },
      });
    expect((await call()).statusCode).toBe(200);
    expect((await call()).statusCode).toBe(200);
    const blocked = await call();
    expect(blocked.statusCode).toBe(429);
    expect(blocked.headers['retry-after']).toBe('60');
    const audited = await admin.query<{ error: string; status: number }>(
      `SELECT error, status FROM mcp_audit WHERE error = 'rate_limited'`,
    );
    expect(audited.rows).toHaveLength(1);
    expect(audited.rows[0]).toEqual({ error: 'rate_limited', status: 429 });
  } finally {
    await limited.close();
  }
});
