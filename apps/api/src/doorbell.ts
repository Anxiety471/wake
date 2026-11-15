import type { Pool } from 'pg';
import type {
  WakeDelivery,
  WakeDeliveryReason,
  WakeReport,
} from '@wake/shared';
import { ConfigError } from './options.js';
import { slidingWindow, type RateLimit } from './rate-limit.js';
import { NAME_PATTERN } from './recipients.js';

// Rakazo's doorbell: POST /api/v1/bots/<botId>/webhook. See docs/doorbell.md for
// the contract this client is written against.
export const DEFAULT_DOORBELL_URL = 'http://127.0.0.1:3417/api/v1/bots';
export const DOORBELL_MAX_BODY_BYTES = 64 * 1024;
export const DEFAULT_DOORBELL_TIMEOUT_MS = 1500;
export const MIN_DOORBELL_TIMEOUT_MS = 100;
// 2 attempts x 9 s + 200 ms backoff = 18.2 s, under the CLI's 20 s urllib
// timeout (bin/wake:send), so a sender that gives up cannot leave a retry
// running on the server and store a duplicate message.
export const MAX_DOORBELL_TIMEOUT_MS = 9_000;
export const DOORBELL_ATTEMPTS = 2;
export const DOORBELL_BACKOFF_MS = 200;
export const DOORBELL_USER_AGENT = 'wake-api';
// The CLI's HOURLY_CAP is 30 wakes per bot per hour, per sender. The API now
// fans out at API speed, so it holds the same ceiling per sender and recipient;
// 0 disables the cap.
export const DEFAULT_DOORBELL_MAX_PER_HOUR = 30;
export const MAX_DOORBELL_MAX_PER_HOUR = 10_000;
// Rakazo answers with a short JSON body. Reading an unbounded body would let a
// broken or hostile doorbell stream forever.
export const MAX_RESPONSE_BYTES = 16 * 1024;

const MIN_SECRET_LENGTH = 16;
const BOT_ID_PATTERN = /^[A-Za-z0-9_-]{1,64}$/;
const SECRET_PATTERN = /^\S+$/;

export type DoorbellTarget = {
  botId: string;
  secret: string;
};

export type DoorbellSettings = {
  url: string;
  timeoutMs: number;
  attempts: number;
  backoffMs: number;
  maxPerHour: number;
};

export type DoorbellConfig = DoorbellSettings & {
  // Keyed by lowercased agent name, because agent names are looked up
  // case-insensitively everywhere else in the API.
  targets: Map<string, DoorbellTarget>;
  // One window for the process: created here so every send shares it.
  limit: RateLimit;
};

function setting(value: string | undefined): string | undefined {
  const trimmed = value?.trim();
  return trimmed === undefined || trimmed === '' ? undefined : trimmed;
}

export function parseDoorbellUrl(value: string | undefined): string {
  const raw = setting(value) ?? DEFAULT_DOORBELL_URL;
  let parsed: URL;
  try {
    parsed = new URL(raw);
  } catch {
    throw new ConfigError(
      `WAKE_DOORBELL_URL is not a URL: ${JSON.stringify(raw)}`,
    );
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
    throw new ConfigError(
      'WAKE_DOORBELL_URL must start with http:// or https://',
    );
  }
  if (parsed.search !== '' || parsed.hash !== '') {
    throw new ConfigError(
      'WAKE_DOORBELL_URL must not carry a query or fragment',
    );
  }
  const path = parsed.pathname.replace(/\/+$/, '');
  return `${parsed.protocol}//${parsed.host}${path}`;
}

export function parseDoorbellTimeout(value: string | undefined): number {
  const raw = setting(value);
  if (raw === undefined) {
    return DEFAULT_DOORBELL_TIMEOUT_MS;
  }
  if (!/^[1-9]\d*$/.test(raw) || Number(raw) > MAX_DOORBELL_TIMEOUT_MS) {
    throw new ConfigError(
      `WAKE_DOORBELL_TIMEOUT_MS must be an integer from ${MIN_DOORBELL_TIMEOUT_MS} to ${MAX_DOORBELL_TIMEOUT_MS}`,
    );
  }
  const ms = Number(raw);
  if (ms < MIN_DOORBELL_TIMEOUT_MS) {
    throw new ConfigError(
      `WAKE_DOORBELL_TIMEOUT_MS must be an integer from ${MIN_DOORBELL_TIMEOUT_MS} to ${MAX_DOORBELL_TIMEOUT_MS}`,
    );
  }
  return ms;
}

export function parseDoorbellMaxPerHour(value: string | undefined): number {
  const raw = setting(value);
  if (raw === undefined) {
    return DEFAULT_DOORBELL_MAX_PER_HOUR;
  }
  if (!/^\d+$/.test(raw) || Number(raw) > MAX_DOORBELL_MAX_PER_HOUR) {
    throw new ConfigError(
      `WAKE_DOORBELL_MAX_PER_HOUR must be an integer from 0 to ${MAX_DOORBELL_MAX_PER_HOUR} (0 disables the cap)`,
    );
  }
  return Number(raw);
}

// {"engineer": {"botId": "cmw...", "secret": "..."}}: one entry per agent that
// should be woken. The secret never leaves the server; it is not logged and not
// part of any response. Empty means the doorbell is off, not misconfigured.
export function parseDoorbellTargets(
  value: string | undefined,
): Map<string, DoorbellTarget> {
  const targets = new Map<string, DoorbellTarget>();
  const raw = setting(value);
  if (raw === undefined) {
    return targets;
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    throw new ConfigError(
      'WAKE_DOORBELL_BOTS must be JSON: {"<agent>": {"botId": "...", "secret": "..."}}',
    );
  }
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    throw new ConfigError(
      'WAKE_DOORBELL_BOTS must be a JSON object: {"<agent>": {"botId": "...", "secret": "..."}}',
    );
  }
  for (const [name, entry] of Object.entries(
    parsed as Record<string, unknown>,
  )) {
    const agent = name.toLowerCase();
    if (!NAME_PATTERN.test(agent)) {
      throw new ConfigError(
        `WAKE_DOORBELL_BOTS has an invalid agent name: ${JSON.stringify(name)}`,
      );
    }
    if (targets.has(agent)) {
      throw new ConfigError(
        `WAKE_DOORBELL_BOTS lists ${JSON.stringify(agent)} more than once`,
      );
    }
    if (typeof entry !== 'object' || entry === null || Array.isArray(entry)) {
      throw new ConfigError(
        `WAKE_DOORBELL_BOTS entry for ${JSON.stringify(agent)} must be an object`,
      );
    }
    const record = entry as Record<string, unknown>;
    for (const key of Object.keys(record)) {
      if (key !== 'botId' && key !== 'secret') {
        throw new ConfigError(
          `WAKE_DOORBELL_BOTS entry for ${JSON.stringify(agent)} has an unknown field: ${JSON.stringify(key)}`,
        );
      }
    }
    const botId = record['botId'];
    const secret = record['secret'];
    if (typeof botId !== 'string' || !BOT_ID_PATTERN.test(botId)) {
      throw new ConfigError(
        `WAKE_DOORBELL_BOTS entry for ${JSON.stringify(agent)} needs a botId of letters, digits, "-" or "_"`,
      );
    }
    if (
      typeof secret !== 'string' ||
      secret.length < MIN_SECRET_LENGTH ||
      !SECRET_PATTERN.test(secret)
    ) {
      throw new ConfigError(
        `WAKE_DOORBELL_BOTS entry for ${JSON.stringify(agent)} needs a webhook secret of at least ${MIN_SECRET_LENGTH} characters`,
      );
    }
    targets.set(agent, { botId, secret });
  }
  return targets;
}

export function parseDoorbell(env: NodeJS.ProcessEnv): DoorbellConfig | null {
  // An empty map means the doorbell is off. Check that before parsing the URL
  // and timeout: a stale bad value must not break the boot of a doorbell that
  // is disabled anyway.
  const targets = parseDoorbellTargets(env.WAKE_DOORBELL_BOTS);
  if (targets.size === 0) {
    return null;
  }
  const url = parseDoorbellUrl(env.WAKE_DOORBELL_URL);
  const timeoutMs = parseDoorbellTimeout(env.WAKE_DOORBELL_TIMEOUT_MS);
  const maxPerHour = parseDoorbellMaxPerHour(env.WAKE_DOORBELL_MAX_PER_HOUR);
  return {
    url,
    targets,
    timeoutMs,
    maxPerHour,
    attempts: DOORBELL_ATTEMPTS,
    backoffMs: DOORBELL_BACKOFF_MS,
    limit: slidingWindow({ limit: maxPerHour, windowMs: 60 * 60 * 1000 }),
  };
}

export type DoorbellEvent = 'message' | 'request' | 'reply';

// Field names and wording follow bin/wake so a recipient's routine keeps working
// when the CLI becomes a client of this API: `ref` starts with `thread:` and
// `detail` carries the same reply hint.
export type DoorbellPayload = {
  depth: number;
  source: string;
  event: DoorbellEvent;
  level: 'info';
  detail: string;
  ref: string;
  time: string;
  messageId: string;
  threadId: string | null;
  from: string;
  to: string;
};

export function replyHint(threadId: string): string {
  return ` [thread ${threadId}] reply: wake --reply ${threadId} "YOUR ANSWER"`;
}

// A reply wakes the asker, who cannot answer its own ask (409), so it carries
// what bin/wake sends the asker instead of the reply hint: `reply on thread <id>: <text>`.
function replyNotice(threadId: string, body: string): string {
  return `reply on thread ${threadId}: ${body}`;
}

function detailFor(input: {
  body: string;
  event: DoorbellEvent;
  threadId: string | null;
}): string {
  if (input.threadId === null) {
    return input.body;
  }
  if (input.event === 'reply') {
    return replyNotice(input.threadId, input.body);
  }
  return `${input.body}${replyHint(input.threadId)}`;
}

export function doorbellPayload(input: {
  messageId: string;
  from: string;
  to: string;
  body: string;
  event: DoorbellEvent;
  threadId: string | null;
  time: Date;
}): DoorbellPayload {
  return {
    // The CLI sends WAKE_DEPTH, 0 for a normal bot (bin/wake:417, docs/protocol.md).
    // A receiver that exports it and runs through wake-run reaches depth 2, where
    // wake refuses, so 1 here would strangle its completion wakes.
    depth: 0,
    source: `bot:${input.from}`,
    event: input.event,
    level: 'info',
    detail: detailFor(input),
    // A reply cannot be answered (the asker would get a 409), so it carries no
    // thread ref: a receiver that routes on `ref` must not try wake --reply.
    // `threadId` still names the thread for anything that wants to record it.
    ref:
      input.threadId === null || input.event === 'reply'
        ? ''
        : `thread:${input.threadId}`,
    time: input.time.toISOString(),
    messageId: input.messageId,
    threadId: input.threadId,
    from: input.from,
    to: input.to,
  };
}

export function encodeDoorbellPayload(payload: DoorbellPayload): Uint8Array {
  return new TextEncoder().encode(JSON.stringify(payload));
}

export type DoorbellAttempt = {
  delivered: boolean;
  reason: WakeDeliveryReason | null;
  attempts: number;
  status: number | null;
};

function errorName(error: unknown): string | null {
  return typeof error === 'object' && error !== null
    ? (Reflect.get(error, 'name') as unknown as string | null)
    : null;
}

function retryable(reason: WakeDeliveryReason | null): boolean {
  return (
    reason === 'server_error' || reason === 'network' || reason === 'timeout'
  );
}

function reasonForStatus(status: number): WakeDeliveryReason {
  if (status === 401) {
    return 'unauthorized';
  }
  if (status === 413) {
    return 'payload_too_large';
  }
  if (status === 429) {
    return 'rate_limited';
  }
  if (status >= 500) {
    return 'server_error';
  }
  return 'rejected';
}

function parseDeliveryBody(text: string): Record<string, unknown> | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    return null;
  }
  return typeof parsed === 'object' && parsed !== null && !Array.isArray(parsed)
    ? (parsed as Record<string, unknown>)
    : null;
}

function classify(
  status: number,
  text: string,
  attempts: number,
): DoorbellAttempt {
  if (status !== 200) {
    return {
      delivered: false,
      reason: reasonForStatus(status),
      attempts,
      status,
    };
  }
  const body = parseDeliveryBody(text);
  if (body === null || body['ok'] !== true) {
    return { delivered: false, reason: 'bad_response', attempts, status };
  }
  // 200 with ignored:true means the bot has no active webhook routine: nobody
  // wakes. Counting that as delivered would report healthy while dropping wakes.
  if (body['ignored'] === true) {
    return { delivered: false, reason: 'no_routine', attempts, status };
  }
  return { delivered: true, reason: null, attempts, status };
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function readCapped(
  response: Response,
): Promise<{ text: string; tooLarge: boolean }> {
  if (response.body === null) {
    return { text: '', tooLarge: false };
  }
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) {
      break;
    }
    if (value === undefined) {
      continue;
    }
    size += value.byteLength;
    if (size > MAX_RESPONSE_BYTES) {
      await reader.cancel().catch(() => undefined);
      return { text: '', tooLarge: true };
    }
    chunks.push(value);
  }
  const joined = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    joined.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return { text: new TextDecoder().decode(joined), tooLarge: false };
}

// One delivery, retried only on 5xx and network/timeout: a 401 means the key was
// rotated, a 413 means the payload is too big and `no_routine` is a configuration
// error on the bot, so none of those get better by repeating them in place.
export async function ringDoorbell(
  config: DoorbellSettings,
  target: DoorbellTarget,
  request: { idempotencyKey: string; body: Uint8Array },
): Promise<DoorbellAttempt> {
  if (request.body.byteLength > DOORBELL_MAX_BODY_BYTES) {
    return {
      delivered: false,
      reason: 'payload_too_large',
      attempts: 0,
      status: null,
    };
  }
  const url = `${config.url}/${encodeURIComponent(target.botId)}/webhook`;
  let attempts = 0;
  let last: DoorbellAttempt = {
    delivered: false,
    reason: 'network',
    attempts: 0,
    status: null,
  };
  while (attempts < config.attempts) {
    attempts += 1;
    try {
      const response = await fetch(url, {
        method: 'POST',
        headers: {
          authorization: `Bearer ${target.secret}`,
          'content-type': 'application/json',
          'idempotency-key': request.idempotencyKey,
          'user-agent': DOORBELL_USER_AGENT,
        },
        body: request.body,
        // A 3xx is a configuration error (http:// instead of https://), not
        // something a repeat fixes: read it as a status and report `rejected`
        // instead of following it, which could also leave the host.
        redirect: 'manual',
        signal: AbortSignal.timeout(config.timeoutMs),
      });
      const body = await readCapped(response);
      last = body.tooLarge
        ? {
            delivered: false,
            reason: 'bad_response',
            attempts,
            status: response.status,
          }
        : classify(response.status, body.text, attempts);
    } catch (error) {
      last = {
        delivered: false,
        reason: errorName(error) === 'TimeoutError' ? 'timeout' : 'network',
        attempts,
        status: null,
      };
    }
    if (!retryable(last.reason)) {
      return last;
    }
    if (attempts < config.attempts) {
      await sleep(config.backoffMs * attempts);
    }
  }
  return last;
}

export type DoorbellLogger = {
  info: (fields: Record<string, unknown>, message: string) => void;
  warn: (fields: Record<string, unknown>, message: string) => void;
};

// The CLI's HOURLY_CAP is per target bot per local user, so two senders waking
// the same bot are two budgets. Keying the window on the recipient alone let one
// sender's 30 wakes refuse another sender's first one.
export function capKey(from: string, to: string): string {
  return `${from.toLowerCase()}:${to.toLowerCase()}`;
}

export type NotifyMessage = {
  messageId: string;
  from: string;
  body: string;
  event: DoorbellEvent;
  threadId: string | null;
};

type Recipient = {
  id: string;
  name: string;
};

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

// One row per recipient that was rung, so a sender that lost the HTTP response
// can read the outcome later. A retry of the same message upserts the same row.
export async function recordDelivery(
  pool: Pick<Pool, 'query'>,
  input: {
    messageId: string;
    agentId: string;
    delivered: boolean;
    reason: WakeDeliveryReason | null;
    attempts: number;
    status: number | null;
  },
): Promise<void> {
  await pool.query(
    `INSERT INTO wake_deliveries
       (message_id, agent_id, delivered, reason, attempts, status)
     VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6)
     ON CONFLICT (message_id, agent_id) DO UPDATE
     SET delivered = EXCLUDED.delivered,
         reason = EXCLUDED.reason,
         attempts = EXCLUDED.attempts,
         status = EXCLUDED.status,
         updated_at = clock_timestamp()`,
    [
      input.messageId,
      input.agentId,
      input.delivered,
      input.reason,
      input.attempts,
      input.status,
    ],
  );
}

async function persist(
  pool: Pick<Pool, 'query'>,
  log: DoorbellLogger,
  input: {
    messageId: string;
    agent: Recipient;
    delivered: boolean;
    reason: WakeDeliveryReason | null;
    attempts: number;
    status: number | null;
  },
): Promise<void> {
  try {
    await recordDelivery(pool, {
      messageId: input.messageId,
      agentId: input.agent.id,
      delivered: input.delivered,
      reason: input.reason,
      attempts: input.attempts,
      status: input.status,
    });
  } catch (error) {
    // The wake already happened; losing its row is a reporting gap, not a
    // reason to fail a send whose message is committed.
    log.warn(
      {
        messageId: input.messageId,
        agent: input.agent.name,
        error: messageOf(error),
      },
      'delivery outcome not recorded',
    );
  }
}

// Rings every configured recipient of a committed message. Never throws: the
// message is already stored, so a broken doorbell must not turn a send into an
// error the sender would retry (and duplicate).
export async function notifyMessage(
  pool: Pick<Pool, 'query'>,
  config: DoorbellConfig | null,
  message: NotifyMessage,
  log: DoorbellLogger,
  now: Date = new Date(),
): Promise<WakeReport> {
  if (config === null) {
    return { enabled: false, deliveries: [] };
  }
  let recipients: Recipient[];
  try {
    const result = await pool.query<{ id: string; name: string }>(
      `SELECT a.id, a.name
       FROM message_receipts r
       JOIN agents a ON a.id = r.agent_id
       WHERE r.message_id = $1::uuid
       ORDER BY a.name ASC`,
      [message.messageId],
    );
    recipients = result.rows.map((row) => ({ id: row.id, name: row.name }));
  } catch (error) {
    log.warn(
      { messageId: message.messageId, error: messageOf(error) },
      'doorbell recipients unreadable',
    );
    // Not the same as "no recipient had a target": report the ambiguity
    // instead of letting the caller believe the doorbell found nobody.
    return { enabled: true, deliveries: [], error: 'recipients_unreadable' };
  }
  const rung = recipients.flatMap((agent) => {
    const target = config.targets.get(agent.name.toLowerCase());
    return target === undefined ? [] : [{ agent, target }];
  });
  const deliveries = await Promise.all(
    rung.map(async ({ agent, target }): Promise<WakeDelivery> => {
      // The API fans out at API speed; one sender must not be able to ring one
      // bot more often than the CLI's hourly cap allows, and must not spend
      // another sender's budget: the CLI's cap is per target bot per local user
      // (bin/wake's wake-rate-<uid>.json). Over the cap no request is made at
      // all, and the outcome is still recorded.
      if (
        !config.limit.allow(capKey(message.from, agent.name), now.getTime())
      ) {
        log.warn(
          {
            messageId: message.messageId,
            agent: agent.name,
            botId: target.botId,
            delivered: false,
            reason: 'rate_limited',
            attempts: 0,
            status: null,
          },
          'wake not rung: hourly cap reached',
        );
        await persist(pool, log, {
          messageId: message.messageId,
          agent,
          delivered: false,
          reason: 'rate_limited',
          attempts: 0,
          status: null,
        });
        return {
          agent: agent.name,
          delivered: false,
          reason: 'rate_limited',
          attempts: 0,
        };
      }
      const body = encodeDoorbellPayload(
        doorbellPayload({
          messageId: message.messageId,
          from: message.from,
          to: agent.name,
          body: message.body,
          event: message.event,
          threadId: message.threadId,
          time: now,
        }),
      );
      const outcome = await ringDoorbell(config, target, {
        // Scoped per bot on Rakazo's side, so one message id can key every
        // recipient: a retry after a timeout can never run a bot twice.
        idempotencyKey: message.messageId,
        body,
      });
      const fields = {
        messageId: message.messageId,
        agent: agent.name,
        botId: target.botId,
        delivered: outcome.delivered,
        reason: outcome.reason,
        attempts: outcome.attempts,
        status: outcome.status,
      };
      if (outcome.delivered) {
        log.info(fields, 'wake delivered');
      } else {
        log.warn(fields, 'wake undelivered');
      }
      await persist(pool, log, {
        messageId: message.messageId,
        agent,
        delivered: outcome.delivered,
        reason: outcome.reason,
        attempts: outcome.attempts,
        status: outcome.status,
      });
      return {
        agent: agent.name,
        delivered: outcome.delivered,
        reason: outcome.reason,
        attempts: outcome.attempts,
      };
    }),
  );
  return { enabled: true, deliveries };
}

export const wakeDeliveryReasonSchema = {
  type: ['string', 'null'],
  enum: [
    'no_routine',
    'unauthorized',
    'payload_too_large',
    'rate_limited',
    'rejected',
    'bad_response',
    'server_error',
    'network',
    'timeout',
    null,
  ],
} as const;

export const wakeReportSchema = {
  type: 'object',
  additionalProperties: false,
  required: ['enabled', 'deliveries'],
  properties: {
    enabled: { type: 'boolean' },
    deliveries: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['agent', 'delivered', 'reason', 'attempts'],
        properties: {
          agent: { type: 'string' },
          delivered: { type: 'boolean' },
          reason: wakeDeliveryReasonSchema,
          attempts: { type: 'integer' },
        },
      },
    },
    error: { type: 'string', enum: ['recipients_unreadable'] },
  },
} as const;
