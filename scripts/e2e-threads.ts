#!/usr/bin/env bun
// End-to-end check for the thread API over real HTTP against a real Postgres.
// Usage: DATABASE_URL=postgres://... bun scripts/e2e-threads.ts
// Starts the API on 127.0.0.1:3188 (E2E_PORT moves it), drives it with fetch, kills it at
// the end. It mints its own registration secret: the API refuses to boot without one, and
// refuses POST /agents without the header once it has one.
import { randomBytes } from 'node:crypto';
import { Client } from 'pg';

const databaseUrl = process.env.DATABASE_URL;
if (databaseUrl === undefined || databaseUrl === '') {
  console.error('DATABASE_URL is required');
  process.exit(1);
}

const port = Number(process.env.E2E_PORT ?? '3188');
const base = `http://127.0.0.1:${port}`;
const stamp = Date.now();
// 32 hex characters, per run, never a literal. The API reads REGISTRATION_SECRET at boot
// and wants x-registration-secret on every operator route once it has one.
const registrationSecret = randomBytes(16).toString('hex');
const paceMs = Number(process.env.E2E_PACE_MS ?? '0');
let failures = 0;

function pass(what: string, extra?: string): void {
  console.log(`ok   ${what}${extra === undefined ? '' : ` (${extra})`}`);
  if (paceMs > 0) {
    Bun.sleepSync(paceMs);
  }
}

function assert(what: string, condition: boolean, detail?: unknown): void {
  if (condition) {
    pass(what);
  } else {
    failures += 1;
    console.error(`FAIL ${what}:`, detail);
  }
}

type Answer = { status: number; json: any };

async function call(
  method: string,
  path: string,
  token?: string,
  body?: unknown,
  extraHeaders?: Record<string, string>,
): Promise<Answer> {
  const headers: Record<string, string> = { ...(extraHeaders ?? {}) };
  if (token !== undefined) {
    headers.authorization = `Bearer ${token}`;
  }
  if (body !== undefined) {
    headers['content-type'] = 'application/json';
  }
  const response = await fetch(`${base}${path}`, {
    method,
    headers,
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  const text = await response.text();
  const answer = {
    status: response.status,
    json: text === '' ? null : JSON.parse(text),
  };
  if (process.env.E2E_VERBOSE === '1') {
    const safe = text.replace(/"token":"[^"]+"/g, '"token":"[redacted]"');
    const shown = safe.length > 320 ? `${safe.slice(0, 320)}...` : safe;
    console.log(`     ${method} ${path} -> ${response.status} ${shown}`);
  }
  return answer;
}

async function retire(
  databaseUrlValue: string,
  threadId: string,
): Promise<void> {
  const admin = new Client({ connectionString: databaseUrlValue });
  await admin.connect();
  try {
    await admin.query(
      `UPDATE threads
       SET updated_at = clock_timestamp() - interval '25 hours'
       WHERE id = $1`,
      [threadId],
    );
  } finally {
    await admin.end();
  }
}

const child = Bun.spawn(['bun', 'apps/api/src/index.ts'], {
  cwd: `${import.meta.dir}/..`,
  env: {
    ...process.env,
    DATABASE_URL: databaseUrl,
    PORT: String(port),
    REGISTRATION_SECRET: registrationSecret,
  },
  stdout: 'pipe',
  stderr: 'pipe',
});

// Both pipes are drained from the moment the child starts. An unread pipe does not block the
// child on bun 1.4.4 (measured: 200 MB of child output left the parent holding 219 MB of it),
// so this is not about a hung API; stderr was read only on the exit-before-health path, so a
// run that failed later never showed the API's own error. Each stream is read once and awaited
// in the finally block, which also keeps a broken pipe from surfacing as an unhandled rejection.
const childOut = new Response(child.stdout).text();
const childErr = new Response(child.stderr).text();

async function drained(stream: Promise<string>): Promise<string> {
  try {
    return await stream;
  } catch {
    return '(the child\'s log could not be read)';
  }
}

function tail(text: string, lines = 20): string {
  return text.trimEnd().split('\n').slice(-lines).join('\n');
}

async function waitForHealth(): Promise<void> {
  const deadline = Date.now() + 20_000;
  while (Date.now() < deadline) {
    // A child that never bound the port exits instead of answering, and without this the
    // loop spent its twenty seconds asking whatever else held the port.
    if (child.exitCode !== null) {
      const why = (await drained(childErr)).trim();
      throw new Error(`API exited ${child.exitCode} before answering /health: ${why}`);
    }
    try {
      const response = await fetch(`${base}/health`);
      if (response.status === 200) {
        return;
      }
    } catch {
      // not listening yet
    }
    await Bun.sleep(200);
  }
  throw new Error('API did not become healthy');
}

try {
  await waitForHealth();
  pass('API answers /health on a migrated database');

  const register = async (
    role: string,
  ): Promise<{ token: string; name: string }> => {
    const name = `e2e-${role}-${stamp}`;
    const created = await call('POST', '/agents', undefined, { name }, {
      'x-registration-secret': registrationSecret,
    });
    assert(`registered ${name}`, created.status === 201, created);
    return { token: created.json.token as string, name };
  };

  const alpha = await register('alpha');
  const beta = await register('beta');
  const gamma = await register('gamma');

  const asked = await call('POST', '/threads', alpha.token, {
    to: beta.name,
    body: 'e2e: is the thread store alive?',
  });
  assert('POST /threads opens a thread', asked.status === 201, asked);
  const threadId = asked.json.threadId as string;
  assert('the first message has seq 1', asked.json.seq === 1, asked.json);

  const opened = await call('GET', `/threads/${threadId}`, alpha.token);
  assert('GET /threads/:id returns the transcript', opened.status === 200);
  assert(
    'the thread is open and holds one message',
    opened.json.status === 'open' && opened.json.messages.length === 1,
    opened.json,
  );

  const betaInbox = await call('GET', '/inbox?unread=true', beta.token);
  assert(
    'the question lands in the recipient inbox with its threadId',
    betaInbox.json.messages.length === 1 &&
      betaInbox.json.messages[0].threadId === threadId,
    betaInbox.json,
  );

  const answered = await call(
    'POST',
    `/threads/${threadId}/reply`,
    beta.token,
    { body: 'e2e: alive, and one round only' },
  );
  assert(
    'the recipient answers with seq 2',
    answered.status === 201 && answered.json.seq === 2,
    answered,
  );
  assert(
    'the answer closes the round',
    answered.json.status === 'answered',
    answered.json,
  );

  const secondAnswer = await call(
    'POST',
    `/threads/${threadId}/reply`,
    beta.token,
    { body: 'e2e: should be refused' },
  );
  assert(
    'a second answer is refused with 409',
    secondAnswer.status === 409,
    secondAnswer,
  );

  const outsider = await call('GET', `/threads/${threadId}`, gamma.token);
  assert('a non-participant gets 404, not the thread', outsider.status === 404);

  const read = await call('POST', `/threads/${threadId}/read`, alpha.token, {
    seq: 2,
  });
  assert(
    'the asker moves its read position to seq 2',
    read.status === 200 && read.json.readSeq === 2,
    read,
  );

  const lateAsk = await call('POST', '/threads', beta.token, {
    to: gamma.name,
    body: 'e2e: a fresh ask for the TTL check',
  });
  await retire(databaseUrl, lateAsk.json.threadId as string);
  const lateAnswer = await call(
    'POST',
    `/threads/${lateAsk.json.threadId}/reply`,
    gamma.token,
    { body: 'e2e: too late' },
  );
  assert(
    'an answer older than the TTL is refused with 410',
    lateAnswer.status === 410,
    lateAnswer,
  );

  const listed = await call('GET', '/threads', alpha.token);
  assert(
    'GET /threads lists my threads',
    listed.status === 200 && listed.json.threads.length >= 1,
    listed.json,
  );
} catch (error) {
  failures += 1;
  console.error('FAIL e2e run:', error);
} finally {
  child.kill();
  await child.exited;
  const [out, err] = await Promise.all([drained(childOut), drained(childErr)]);
  if (failures > 0) {
    // The child's own log, not just its exit status: a route that answers 500 explains itself
    // there first, and both streams are only worth printing when something failed.
    console.error(`--- API stdout (last 20 lines) ---\n${tail(out)}`);
    console.error(`--- API stderr (last 20 lines) ---\n${tail(err)}`);
  }
}

if (failures > 0) {
  console.error(`\n${failures} check(s) failed`);
  process.exit(1);
}
console.log('\nall e2e checks passed');