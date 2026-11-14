import type { Pool } from 'pg';
import type {
  WakeDelivery,
  WakeDeliveryReason,
  WakeReport,
} from '@wake/shared';
import { ConfigError } from './options.js';
import { NAME_PATTERN } from './recipients.js';

// Rakazo's doorbell: POST /api/v1/bots/<botId>/webhook. See docs/doorbell.md for
// the contract this client is written against.
export const DEFAULT_DOORBELL_URL = 'http://127.0.0.1:3417/api/v1/bots';
export const DOORBELL_MAX_BODY_BYTES = 64 * 1024;
export const DEFAULT_DOORBELL_TIMEOUT_MS = 1500;
export const MIN_DOORBELL_TIMEOUT_MS = 100;
export const MAX_DOORBELL_TIMEOUT_MS = 10_000;
export const DOORBELL_ATTEMPTS = 2;
export const DOORBELL_BACKOFF_MS = 200;
export const DOORBELL_USER_AGENT = 'wake-api';

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
};

export type DoorbellConfig = DoorbellSettings & {
  // Keyed by lowercased agent name, because agent names are looked up
  // case-insensitively everywhere else in the API.
  targets: Map<string, DoorbellTarget>;
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
  const targets = parseDoorbellTargets(env.WAKE_DOORBELL_BOTS);
  const url = parseDoorbellUrl(env.WAKE_DOORBELL_URL);
  const timeoutMs = parseDoorbellTimeout(env.WAKE_DOORBELL_TIMEOUT_MS);
  if (targets.size === 0) {
    return null;
  }
  return {
    url,
    targets,
    timeoutMs,
    attempts: DOORBELL_ATTEMPTS,
    backoffMs: DOORBELL_BACKOFF_MS,
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
    depth: 1,
    source: `bot:${input.from}`,
    event: input.event,
    level: 'info',
    detail:
      input.threadId === null
        ? input.body
        : `${input.body}${replyHint(input.threadId)}`,
    ref: input.threadId === null ? '' : `thread:${input.threadId}`,
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
        signal: AbortSignal.timeout(config.timeoutMs),
      });
      const text = await response.text();
      last = classify(response.status, text, attempts);
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

export type NotifyMessage = {
  messageId: string;
  from: string;
  body: string;
  event: DoorbellEvent;
  threadId: string | null;
};

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
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
  let recipients: string[];
  try {
    const result = await pool.query<{ name: string }>(
      `SELECT a.name
       FROM message_receipts r
       JOIN agents a ON a.id = r.agent_id
       WHERE r.message_id = $1::uuid
       ORDER BY a.name ASC`,
      [message.messageId],
    );
    recipients = result.rows.map((row) => row.name);
  } catch (error) {
    log.warn(
      { messageId: message.messageId, error: messageOf(error) },
      'doorbell recipients unreadable',
    );
    return { enabled: true, deliveries: [] };
  }
  const rung = recipients.flatMap((agent) => {
    const target = config.targets.get(agent.toLowerCase());
    return target === undefined ? [] : [{ agent, target }];
  });
  const deliveries = await Promise.all(
    rung.map(async ({ agent, target }): Promise<WakeDelivery> => {
      const body = encodeDoorbellPayload(
        doorbellPayload({
          messageId: message.messageId,
          from: message.from,
          to: agent,
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
        agent,
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
      return {
        agent,
        delivered: outcome.delivered,
        reason: outcome.reason,
        attempts: outcome.attempts,
      };
    }),
  );
  return { enabled: true, deliveries };
}

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
          reason: { type: ['string', 'null'] },
          attempts: { type: 'integer' },
        },
      },
    },
  },
} as const;
