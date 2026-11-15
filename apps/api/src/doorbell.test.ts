import { describe, expect, test } from 'bun:test';
import type { Pool } from 'pg';
import {
  DOORBELL_MAX_BODY_BYTES,
  doorbellPayload,
  encodeDoorbellPayload,
  parseDoorbell,
  parseDoorbellTargets,
  parseDoorbellTimeout,
  parseDoorbellMaxPerHour,
  notifyMessage,
  parseDoorbellUrl,
  replyHint,
  ringDoorbell,
  type DoorbellLogger,
  type DoorbellSettings,
} from './doorbell.js';
import { ConfigError } from './options.js';

const SECRET = 'wake-webhook-secret-0123456789';
const TARGET = { botId: 'cmwbot123', secret: SECRET };

type StubRequest = {
  method: string;
  path: string;
  headers: Record<string, string>;
  body: string;
};

type StubReply = {
  status: number;
  body: string;
  delayMs?: number;
};

function startStub(
  handler: (request: StubRequest) => StubReply | Promise<StubReply>,
) {
  const requests: StubRequest[] = [];
  const server = Bun.serve({
    hostname: '127.0.0.1',
    port: 0,
    async fetch(request) {
      const body = await request.text();
      const headers: Record<string, string> = {};
      request.headers.forEach((value, key) => {
        headers[key] = value;
      });
      const record: StubRequest = {
        method: request.method,
        path: new URL(request.url).pathname,
        headers,
        body,
      };
      requests.push(record);
      const reply = await handler(record);
      if (reply.delayMs !== undefined) {
        await new Promise((resolve) => setTimeout(resolve, reply.delayMs));
      }
      return new Response(reply.body, {
        status: reply.status,
        headers: { 'content-type': 'application/json' },
      });
    },
  });
  return {
    url: `http://127.0.0.1:${server.port}/api/v1/bots`,
    requests,
    close: () => {
      server.stop(true);
    },
  };
}

function settings(
  url: string,
  overrides?: Partial<DoorbellSettings>,
): DoorbellSettings {
  return {
    url,
    timeoutMs: 500,
    attempts: 2,
    backoffMs: 1,
    maxPerHour: 0,
    ...overrides,
  };
}

function wakeBody(): Uint8Array {
  return encodeDoorbellPayload(
    doorbellPayload({
      messageId: '11111111-1111-1111-1111-111111111111',
      from: 'alice',
      to: 'bob',
      body: 'hello',
      event: 'message',
      threadId: null,
      time: new Date('2026-11-14T16:00:00.000Z'),
    }),
  );
}

function freePortUrl(): Promise<string> {
  return new Promise((resolve) => {
    const server = Bun.serve({
      hostname: '127.0.0.1',
      port: 0,
      fetch: () => new Response(''),
    });
    const { port } = server;
    server.stop(true);
    resolve(`http://127.0.0.1:${port}/api/v1/bots`);
  });
}

describe('parseDoorbell', () => {
  test('is off when WAKE_DOORBELL_BOTS is unset or empty', () => {
    expect(parseDoorbell({})).toBeNull();
    expect(parseDoorbell({ WAKE_DOORBELL_BOTS: '   ' })).toBeNull();
    expect(parseDoorbell({ WAKE_DOORBELL_BOTS: '{}' })).toBeNull();
  });

  test('a disabled doorbell ignores a stale URL and timeout', () => {
    // The doorbell is off, so nothing reads these; a leftover bad value must
    // not break the boot of a server that will never ring.
    expect(
      parseDoorbell({
        WAKE_DOORBELL_BOTS: '{}',
        WAKE_DOORBELL_URL: 'not a url',
        WAKE_DOORBELL_TIMEOUT_MS: '0',
      }),
    ).toBeNull();
  });

  test('reads a target and lowercases the agent name', () => {
    const config = parseDoorbell({
      WAKE_DOORBELL_BOTS: JSON.stringify({
        Engineer: { botId: 'cmwbot123', secret: SECRET },
      }),
      WAKE_DOORBELL_URL: 'http://127.0.0.1:9999/api/v1/bots/',
    });
    expect(config).not.toBeNull();
    if (config === null) {
      return;
    }
    expect([...config.targets.keys()]).toEqual(['engineer']);
    expect(config.targets.get('engineer')).toEqual({
      botId: 'cmwbot123',
      secret: SECRET,
    });
    expect(config.url).toBe('http://127.0.0.1:9999/api/v1/bots');
  });

  test('rejects JSON that is not an object of entries', () => {
    for (const raw of ['not json', '[]', '"x"', 'null']) {
      expect(() => parseDoorbellTargets(raw)).toThrow(ConfigError);
    }
  });

  test('rejects an entry with a bad bot id', () => {
    expect(() =>
      parseDoorbellTargets(
        JSON.stringify({ bob: { botId: 'a/b', secret: SECRET } }),
      ),
    ).toThrow(/botId/);
  });

  test('rejects a short or whitespace secret without echoing it', () => {
    const short = 'too-short';
    expect(() =>
      parseDoorbellTargets(
        JSON.stringify({ bob: { botId: 'cmwbot123', secret: short } }),
      ),
    ).toThrow(/at least 16 characters/);
    expect(() =>
      parseDoorbellTargets(
        JSON.stringify({ bob: { botId: 'cmwbot123', secret: `${SECRET} x` } }),
      ),
    ).toThrow(/at least 16 characters/);
    try {
      parseDoorbellTargets(
        JSON.stringify({ bob: { botId: 'cmwbot123', secret: short } }),
      );
    } catch (error) {
      expect(String(error)).not.toContain(short);
    }
  });

  test('rejects an unknown field instead of ignoring it', () => {
    expect(() =>
      parseDoorbellTargets(
        JSON.stringify({
          bob: { botId: 'cmwbot123', secret: SECRET, url: 'http://x' },
        }),
      ),
    ).toThrow(/unknown field/);
  });

  test('rejects an invalid URL', () => {
    expect(() => parseDoorbellUrl('ftp://x')).toThrow(ConfigError);
    expect(() => parseDoorbellUrl('http://x/api?token=1')).toThrow(ConfigError);
    expect(() => parseDoorbellUrl('not a url')).toThrow(ConfigError);
  });

  test('bounds the timeout under the CLI send timeout', () => {
    expect(parseDoorbellTimeout(undefined)).toBe(1500);
    expect(parseDoorbellTimeout('250')).toBe(250);
    // 2 x 9 s + 200 ms stays under bin/wake's 20 s urllib timeout.
    expect(parseDoorbellTimeout('9000')).toBe(9000);
    for (const raw of ['0', '99', '9001', '10000', '1.5', '-1', 'ten']) {
      expect(() => parseDoorbellTimeout(raw)).toThrow(ConfigError);
    }
  });

  test('bounds the hourly cap', () => {
    expect(parseDoorbellMaxPerHour(undefined)).toBe(30);
    expect(parseDoorbellMaxPerHour('0')).toBe(0);
    expect(parseDoorbellMaxPerHour('7')).toBe(7);
    expect(parseDoorbellMaxPerHour('10000')).toBe(10000);
    for (const raw of ['-1', '10001', '1.5', 'ten']) {
      expect(() => parseDoorbellMaxPerHour(raw)).toThrow(ConfigError);
    }
  });
});

describe('doorbellPayload', () => {
  test('mirrors the CLI payload for a plain message', () => {
    const payload = doorbellPayload({
      messageId: 'm1',
      from: 'alice',
      to: 'bob',
      body: 'hello',
      event: 'message',
      threadId: null,
      time: new Date('2026-11-14T16:00:00.000Z'),
    });
    expect(payload).toEqual({
      depth: 0,
      source: 'bot:alice',
      event: 'message',
      level: 'info',
      detail: 'hello',
      ref: '',
      time: '2026-11-14T16:00:00.000Z',
      messageId: 'm1',
      threadId: null,
      from: 'alice',
      to: 'bob',
    });
  });

  test('carries the thread ref and the reply hint for a request', () => {
    const payload = doorbellPayload({
      messageId: 'm2',
      from: 'alice',
      to: 'bob',
      body: 'why is CI red?',
      event: 'request',
      threadId: 'thread-1',
      time: new Date('2026-11-14T16:00:00.000Z'),
    });
    expect(payload.ref).toBe('thread:thread-1');
    expect(payload.detail).toBe(`why is CI red?${replyHint('thread-1')}`);
    expect(payload.detail).toContain(
      'reply: wake --reply thread-1 "YOUR ANSWER"',
    );
    expect(payload.threadId).toBe('thread-1');
  });

  test('tells the asker to reply on the thread, not to answer its own ask', () => {
    const payload = doorbellPayload({
      messageId: 'm3',
      from: 'bob',
      to: 'alice',
      body: 'because billing',
      event: 'reply',
      threadId: 'thread-1',
      time: new Date('2026-11-14T16:00:00.000Z'),
    });
    // A reply cannot be answered, so it carries no thread ref: a receiver that
    // routes on `ref` must not fire wake --reply into a 409.
    expect(payload.ref).toBe('');
    expect(payload.threadId).toBe('thread-1');
    expect(payload.detail).toBe('reply on thread thread-1: because billing');
    // wake --reply from the asker is a 409: the asker cannot answer its own ask.
    expect(payload.detail).not.toContain('wake --reply');
  });
});

describe('ringDoorbell', () => {
  test('delivers a wake with the auth, idempotency and payload bytes', async () => {
    const stub = startStub(() => ({
      status: 200,
      body: JSON.stringify({ ok: true, messageId: 'm', runId: 'r', seq: 1 }),
    }));
    try {
      const body = wakeBody();
      const result = await ringDoorbell(settings(stub.url), TARGET, {
        idempotencyKey: 'message-1',
        body,
      });
      expect(result).toEqual({
        delivered: true,
        reason: null,
        attempts: 1,
        status: 200,
      });
      expect(stub.requests).toHaveLength(1);
      const request = stub.requests[0];
      expect(request?.method).toBe('POST');
      expect(request?.path).toBe('/api/v1/bots/cmwbot123/webhook');
      expect(request?.headers['authorization']).toBe(`Bearer ${SECRET}`);
      expect(request?.headers['idempotency-key']).toBe('message-1');
      expect(request?.headers['content-type']).toBe('application/json');
      expect(request?.headers['user-agent']).toBe('wake-api');
      expect(request?.body).toBe(new TextDecoder().decode(body));
      expect(JSON.parse(request?.body ?? '')).toMatchObject({
        event: 'message',
        messageId: '11111111-1111-1111-1111-111111111111',
        ref: '',
      });
    } finally {
      stub.close();
    }
  });

  test('treats ignored:true as undelivered and does not retry', async () => {
    const stub = startStub(() => ({
      status: 200,
      body: JSON.stringify({ ok: true, ignored: true }),
    }));
    try {
      const result = await ringDoorbell(settings(stub.url), TARGET, {
        idempotencyKey: 'message-2',
        body: wakeBody(),
      });
      expect(result).toEqual({
        delivered: false,
        reason: 'no_routine',
        attempts: 1,
        status: 200,
      });
      expect(stub.requests).toHaveLength(1);
    } finally {
      stub.close();
    }
  });

  test('does not retry a 401', async () => {
    const stub = startStub(() => ({
      status: 401,
      body: JSON.stringify({ error: 'Unauthorized' }),
    }));
    try {
      const result = await ringDoorbell(settings(stub.url), TARGET, {
        idempotencyKey: 'message-3',
        body: wakeBody(),
      });
      expect(result.delivered).toBe(false);
      expect(result.reason).toBe('unauthorized');
      expect(stub.requests).toHaveLength(1);
    } finally {
      stub.close();
    }
  });

  test('does not retry a 413', async () => {
    const stub = startStub(() => ({
      status: 413,
      body: JSON.stringify({ error: 'Payload too large' }),
    }));
    try {
      const result = await ringDoorbell(settings(stub.url), TARGET, {
        idempotencyKey: 'message-4',
        body: wakeBody(),
      });
      expect(result.reason).toBe('payload_too_large');
      expect(stub.requests).toHaveLength(1);
    } finally {
      stub.close();
    }
  });

  test('retries a 5xx with identical bytes and the same idempotency key', async () => {
    let calls = 0;
    const stub = startStub(() => {
      calls += 1;
      return calls === 1
        ? {
            status: 503,
            body: JSON.stringify({ error: 'Service unavailable' }),
          }
        : { status: 200, body: JSON.stringify({ ok: true, seq: 2 }) };
    });
    try {
      const body = wakeBody();
      const result = await ringDoorbell(settings(stub.url), TARGET, {
        idempotencyKey: 'message-5',
        body,
      });
      expect(result).toEqual({
        delivered: true,
        reason: null,
        attempts: 2,
        status: 200,
      });
      expect(stub.requests).toHaveLength(2);
      expect(stub.requests[0]?.body).toBe(stub.requests[1]?.body ?? '');
      expect(stub.requests[0]?.headers['idempotency-key']).toBe(
        stub.requests[1]?.headers['idempotency-key'] ?? '',
      );
    } finally {
      stub.close();
    }
  });

  test('gives up after the last attempt on 5xx', async () => {
    const stub = startStub(() => ({
      status: 500,
      body: JSON.stringify({ error: 'boom' }),
    }));
    try {
      const result = await ringDoorbell(settings(stub.url), TARGET, {
        idempotencyKey: 'message-6',
        body: wakeBody(),
      });
      expect(result).toEqual({
        delivered: false,
        reason: 'server_error',
        attempts: 2,
        status: 500,
      });
      expect(stub.requests).toHaveLength(2);
    } finally {
      stub.close();
    }
  });

  test('does not follow a redirect: a 3xx is a rejected configuration', async () => {
    const stub = startStub(() => ({
      status: 302,
      body: '',
    }));
    try {
      const attempt = await ringDoorbell(settings(stub.url), TARGET, {
        idempotencyKey: 'k',
        body: wakeBody(),
      });
      expect(attempt).toEqual({
        delivered: false,
        reason: 'rejected',
        attempts: 1,
        status: 302,
      });
      expect(stub.requests).toHaveLength(1);
    } finally {
      stub.close();
    }
  });

  test('refuses an oversized response body instead of reading it all', async () => {
    const stub = startStub(() => ({
      status: 200,
      body: JSON.stringify({ ok: true, padding: 'x'.repeat(64 * 1024) }),
    }));
    try {
      const attempt = await ringDoorbell(settings(stub.url), TARGET, {
        idempotencyKey: 'k',
        body: wakeBody(),
      });
      expect(attempt).toEqual({
        delivered: false,
        reason: 'bad_response',
        attempts: 1,
        status: 200,
      });
      expect(stub.requests).toHaveLength(1);
    } finally {
      stub.close();
    }
  });

  test('treats a 200 that is not a delivery as undelivered', async () => {
    for (const body of ['not json', JSON.stringify({ ok: false })]) {
      const stub = startStub(() => ({ status: 200, body }));
      try {
        const result = await ringDoorbell(settings(stub.url), TARGET, {
          idempotencyKey: 'message-7',
          body: wakeBody(),
        });
        expect(result.delivered).toBe(false);
        expect(result.reason).toBe('bad_response');
        expect(stub.requests).toHaveLength(1);
      } finally {
        stub.close();
      }
    }
  });

  test('retries a timeout, then reports it', async () => {
    const stub = startStub(() => ({
      status: 200,
      body: JSON.stringify({ ok: true }),
      delayMs: 300,
    }));
    try {
      const result = await ringDoorbell(
        settings(stub.url, { timeoutMs: 80 }),
        TARGET,
        { idempotencyKey: 'message-8', body: wakeBody() },
      );
      expect(result.delivered).toBe(false);
      expect(result.reason).toBe('timeout');
      expect(result.attempts).toBe(2);
      expect(stub.requests).toHaveLength(2);
    } finally {
      stub.close();
    }
  });

  test('reports a network error when nothing listens', async () => {
    const url = await freePortUrl();
    const result = await ringDoorbell(settings(url), TARGET, {
      idempotencyKey: 'message-9',
      body: wakeBody(),
    });
    expect(result.delivered).toBe(false);
    expect(result.reason).toBe('network');
    expect(result.attempts).toBe(2);
  });

  test('refuses a payload over the doorbell cap without a request', async () => {
    const stub = startStub(() => ({ status: 200, body: '{"ok":true}' }));
    try {
      const big = new Uint8Array(DOORBELL_MAX_BODY_BYTES + 1);
      const result = await ringDoorbell(settings(stub.url), TARGET, {
        idempotencyKey: 'message-10',
        body: big,
      });
      expect(result).toEqual({
        delivered: false,
        reason: 'payload_too_large',
        attempts: 0,
        status: null,
      });
      expect(stub.requests).toHaveLength(0);
    } finally {
      stub.close();
    }
  });
});

describe('notifyMessage', () => {
  function capture(): {
    log: DoorbellLogger;
    lines: Array<Record<string, unknown>>;
  } {
    const lines: Array<Record<string, unknown>> = [];
    return {
      lines,
      log: {
        info: (fields) => {
          lines.push({ level: 'info', ...fields });
        },
        warn: (fields) => {
          lines.push({ level: 'warn', ...fields });
        },
      },
    };
  }

  function recordingPool(
    recipients: Array<{ id: string; name: string }>,
    options?: { failInsert?: boolean },
  ): { pool: Pick<Pool, 'query'>; writes: Array<{ values: unknown[] }> } {
    const writes: Array<{ values: unknown[] }> = [];
    const pool = {
      query: (text: string, values?: unknown[]) => {
        if (text.includes('FROM message_receipts')) {
          return Promise.resolve({ rows: recipients });
        }
        if (text.includes('INSERT INTO wake_deliveries')) {
          if (options?.failInsert === true) {
            return Promise.reject(new Error('insert failed'));
          }
          writes.push({ values: values ?? [] });
          return Promise.resolve({ rows: [] });
        }
        return Promise.reject(new Error(`unexpected query: ${text}`));
      },
    } as unknown as Pick<Pool, 'query'>;
    return { pool, writes };
  }

  const message = {
    messageId: '11111111-1111-1111-1111-111111111111',
    from: 'alice',
    body: 'hello',
    event: 'message' as const,
    threadId: null,
  };

  test('is a no-op with no doorbell configured', async () => {
    const { log, lines } = capture();
    const pool = {
      query: () => {
        throw new Error('must not query');
      },
    } as unknown as Pick<Pool, 'query'>;
    expect(await notifyMessage(pool, null, message, log)).toEqual({
      enabled: false,
      deliveries: [],
    });
    expect(lines).toHaveLength(0);
  });

  test('never throws when the recipients cannot be read', async () => {
    const { log, lines } = capture();
    const pool = {
      query: () => Promise.reject(new Error('connection lost')),
    } as unknown as Pick<Pool, 'query'>;
    const config = parseDoorbell({
      WAKE_DOORBELL_BOTS: JSON.stringify({
        bob: { botId: 'cmwbot123', secret: SECRET },
      }),
    });
    expect(await notifyMessage(pool, config, message, log)).toEqual({
      enabled: true,
      deliveries: [],
      // Not "nobody had a target": the recipients could not be read at all.
      error: 'recipients_unreadable',
    });
    expect(lines).toHaveLength(1);
    expect(lines[0]).toMatchObject({
      level: 'warn',
      messageId: message.messageId,
      error: 'connection lost',
    });
  });

  test('rings only recipients that have a target and records the outcome', async () => {
    const stub = startStub(() => ({
      status: 200,
      body: JSON.stringify({ ok: true, seq: 1 }),
    }));
    try {
      const { log, lines } = capture();
      const config = parseDoorbell({
        WAKE_DOORBELL_BOTS: JSON.stringify({
          bob: { botId: 'cmwbot123', secret: SECRET },
        }),
        WAKE_DOORBELL_URL: stub.url,
      });
      if (config === null) {
        throw new Error('expected a doorbell config');
      }
      const { pool, writes } = recordingPool([
        { id: '22222222-2222-2222-2222-222222222222', name: 'bob' },
        { id: '33333333-3333-3333-3333-333333333333', name: 'carol' },
      ]);
      const report = await notifyMessage(pool, config, message, log);
      expect(report).toEqual({
        enabled: true,
        deliveries: [
          { agent: 'bob', delivered: true, reason: null, attempts: 1 },
        ],
      });
      expect(stub.requests).toHaveLength(1);
      expect(stub.requests[0]?.path).toBe('/api/v1/bots/cmwbot123/webhook');
      // Carol has no target, so only bob's outcome is written.
      expect(writes).toHaveLength(1);
      expect(writes[0]?.values).toEqual([
        message.messageId,
        '22222222-2222-2222-2222-222222222222',
        true,
        null,
        1,
        200,
      ]);
      expect(lines).toHaveLength(1);
      expect(lines[0]).toMatchObject({
        level: 'info',
        agent: 'bob',
        delivered: true,
      });
      expect(JSON.stringify(lines)).not.toContain(SECRET);
    } finally {
      stub.close();
    }
  });

  test('records an undelivered outcome with its reason and status', async () => {
    const stub = startStub(() => ({
      status: 401,
      body: JSON.stringify({ error: 'unauthorized' }),
    }));
    try {
      const { log } = capture();
      const config = parseDoorbell({
        WAKE_DOORBELL_BOTS: JSON.stringify({
          bob: { botId: 'cmwbot123', secret: SECRET },
        }),
        WAKE_DOORBELL_URL: stub.url,
      });
      if (config === null) {
        throw new Error('expected a doorbell config');
      }
      const { pool, writes } = recordingPool([
        { id: '22222222-2222-2222-2222-222222222222', name: 'bob' },
      ]);
      const report = await notifyMessage(pool, config, message, log);
      expect(report.deliveries[0]).toMatchObject({
        agent: 'bob',
        delivered: false,
        reason: 'unauthorized',
        attempts: 1,
      });
      expect(writes[0]?.values).toEqual([
        message.messageId,
        '22222222-2222-2222-2222-222222222222',
        false,
        'unauthorized',
        1,
        401,
      ]);
    } finally {
      stub.close();
    }
  });

  test('caps wakes per sender and recipient per hour without making a request', async () => {
    const stub = startStub(() => ({
      status: 200,
      body: JSON.stringify({ ok: true, seq: 1 }),
    }));
    try {
      const { log, lines } = capture();
      const config = parseDoorbell({
        WAKE_DOORBELL_BOTS: JSON.stringify({
          bob: { botId: 'cmwbot123', secret: SECRET },
        }),
        WAKE_DOORBELL_URL: stub.url,
        WAKE_DOORBELL_MAX_PER_HOUR: '1',
      });
      if (config === null) {
        throw new Error('expected a doorbell config');
      }
      const { pool, writes } = recordingPool([
        { id: '22222222-2222-2222-2222-222222222222', name: 'bob' },
      ]);
      const at = (iso: string) => new Date(iso);
      const first = await notifyMessage(
        pool,
        config,
        message,
        log,
        at('2026-11-15T00:00:00.000Z'),
      );
      expect(first.deliveries).toEqual([
        { agent: 'bob', delivered: true, reason: null, attempts: 1 },
      ]);
      const second = await notifyMessage(
        pool,
        config,
        message,
        log,
        at('2026-11-15T00:30:00.000Z'),
      );
      expect(second.deliveries).toEqual([
        { agent: 'bob', delivered: false, reason: 'rate_limited', attempts: 0 },
      ]);
      // One request, then nothing: the cap makes no network call at all.
      expect(stub.requests).toHaveLength(1);
      expect(writes[1]?.values).toEqual([
        message.messageId,
        '22222222-2222-2222-2222-222222222222',
        false,
        'rate_limited',
        0,
        null,
      ]);
      expect(
        lines.some(
          (line) =>
            line['level'] === 'warn' &&
            line['reason'] === 'rate_limited' &&
            line['attempts'] === 0,
        ),
      ).toBe(true);
      // One hour and a millisecond later the window has moved and bob rings again.
      const third = await notifyMessage(
        pool,
        config,
        message,
        log,
        at('2026-11-15T01:00:00.001Z'),
      );
      expect(third.deliveries[0]?.delivered).toBe(true);
      expect(stub.requests).toHaveLength(2);
    } finally {
      stub.close();
    }
  });

  test('one sender cannot spend the hourly budget of another', async () => {
    const stub = startStub(() => ({
      status: 200,
      body: JSON.stringify({ ok: true, seq: 1 }),
    }));
    try {
      const { log, lines } = capture();
      const config = parseDoorbell({
        WAKE_DOORBELL_BOTS: JSON.stringify({
          bob: { botId: 'cmwbot123', secret: SECRET },
        }),
        WAKE_DOORBELL_URL: stub.url,
        WAKE_DOORBELL_MAX_PER_HOUR: '1',
      });
      if (config === null) {
        throw new Error('expected a doorbell config');
      }
      const { pool } = recordingPool([
        { id: '22222222-2222-2222-2222-222222222222', name: 'bob' },
      ]);
      const at = new Date('2026-11-15T00:00:00.000Z');
      const first = await notifyMessage(pool, config, message, log, at);
      expect(first.deliveries).toEqual([
        { agent: 'bob', delivered: true, reason: null, attempts: 1 },
      ]);
      // Same window, same recipient, a different sender: a different budget,
      // so it must ring. Keying on the recipient alone refused it.
      const other = await notifyMessage(
        pool,
        config,
        { ...message, from: 'carol' },
        log,
        at,
      );
      expect(other.deliveries).toEqual([
        { agent: 'bob', delivered: true, reason: null, attempts: 1 },
      ]);
      // The first sender is still spent.
      const again = await notifyMessage(pool, config, message, log, at);
      expect(again.deliveries).toEqual([
        { agent: 'bob', delivered: false, reason: 'rate_limited', attempts: 0 },
      ]);
      expect(stub.requests).toHaveLength(2);
      expect(
        lines.filter((line) => line['reason'] === 'rate_limited'),
      ).toHaveLength(1);
    } finally {
      stub.close();
    }
  });

  test('a failed outcome write is logged and never fails the send', async () => {
    const stub = startStub(() => ({
      status: 200,
      body: JSON.stringify({ ok: true, seq: 1 }),
    }));
    try {
      const { log, lines } = capture();
      const config = parseDoorbell({
        WAKE_DOORBELL_BOTS: JSON.stringify({
          bob: { botId: 'cmwbot123', secret: SECRET },
        }),
        WAKE_DOORBELL_URL: stub.url,
      });
      if (config === null) {
        throw new Error('expected a doorbell config');
      }
      const { pool } = recordingPool(
        [{ id: '22222222-2222-2222-2222-222222222222', name: 'bob' }],
        { failInsert: true },
      );
      const report = await notifyMessage(pool, config, message, log);
      expect(report.deliveries).toEqual([
        { agent: 'bob', delivered: true, reason: null, attempts: 1 },
      ]);
      expect(
        lines.some(
          (line) =>
            line['message'] === undefined && line['error'] === 'insert failed',
        ),
      ).toBe(true);
    } finally {
      stub.close();
    }
  });
});
