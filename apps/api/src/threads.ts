import type { FastifyInstance } from 'fastify';
import type { Pool, PoolClient } from 'pg';
import {
  encodeSearchCursor,
  parseSearchCursor,
  type CreatedThread,
  type ThreadDetail,
  type ThreadKind,
  type ThreadMessage,
  type ThreadPage,
  type ThreadReadReceipt,
  type ThreadReply,
  type ThreadStatus,
  type ThreadSummary,
} from '@wake/shared';
import { caller, type AuthenticatedAgent } from './auth.js';
import {
  notifyMessage,
  wakeReportSchema,
  type DoorbellConfig,
} from './doorbell.js';
import { ConfigError } from './options.js';
import { inboxLimit, UUID_PATTERN } from './paging.js';
import { resolveRecipient } from './recipients.js';
import { isoTime } from './time.js';
import { generateToken, hashToken } from './token.js';

const MAX_BODY_BYTES = 8192;
export const DEFAULT_THREAD_TTL_HOURS = 24;
export const MAX_THREAD_TTL_HOURS = 720;

export type ThreadOptions = {
  // null disables the TTL, the same as WAKE_TTL_HOURS=0 for the CLI.
  ttlHours?: number | null;
  doorbell?: DoorbellConfig | null;
};

export function parseThreadTtlHours(value: string | undefined): number | null {
  if (value === undefined || value === '') {
    return DEFAULT_THREAD_TTL_HOURS;
  }
  if (value === '0') {
    return null;
  }
  if (!/^[1-9]\d{0,3}$/.test(value)) {
    throw new ConfigError(
      `WAKE_TTL_HOURS must be 0 (no expiry) or an integer from 1 to ${MAX_THREAD_TTL_HOURS}`,
    );
  }
  const hours = Number(value);
  if (hours > MAX_THREAD_TTL_HOURS) {
    throw new ConfigError(
      `WAKE_TTL_HOURS must be 0 (no expiry) or an integer from 1 to ${MAX_THREAD_TTL_HOURS}`,
    );
  }
  return hours;
}

type ThreadStateRow = {
  id: string;
  kind: ThreadKind;
  status: ThreadStatus;
  created_by: string;
  last_seq: number;
  expired: boolean;
};

type PostedRow = {
  id: string;
  thread_seq: number;
  created_at: Date | string;
  status: ThreadStatus;
};

type DetailRow = {
  id: string;
  kind: ThreadKind;
  status: ThreadStatus;
  created_by: string;
  last_seq: number;
  last_read_seq: number;
  created_at: Date | string;
  updated_at: Date | string;
  expires_at: Date | string | null;
  expired: boolean;
};

type SummaryRow = {
  id: string;
  kind: ThreadKind;
  status: ThreadStatus;
  created_by: string;
  last_seq: number;
  last_read_seq: number;
  created_at: Date | string;
  updated_at: Date | string;
  expired: boolean;
  unread: number;
};

type ParticipantRow = {
  thread_id: string;
  name: string;
};

function threadColumns(ttlParam: string): string {
  return `
  t.id,
  t.kind,
  t.status,
  creator.name AS created_by,
  t.last_seq,
  tp.last_read_seq,
  date_trunc('milliseconds', t.created_at) AS created_at,
  date_trunc('milliseconds', t.updated_at) AS updated_at,
  (
    ${ttlParam}::integer IS NOT NULL
    AND t.updated_at < clock_timestamp() - make_interval(hours => ${ttlParam})
  ) AS expired`;
}

const POST_MESSAGE_SQL = `
WITH bumped AS (
  UPDATE threads
  SET last_seq = last_seq + 1,
      updated_at = clock_timestamp(),
      status = CASE
        WHEN kind = 'ask' AND last_seq = 1 THEN 'answered'
        ELSE status
      END
  WHERE id = $1::uuid
    AND status = 'open'
    AND (
      $5::integer IS NULL
      OR updated_at >= clock_timestamp() - make_interval(hours => $5)
    )
    AND (kind = 'thread' OR last_seq <= 1)
  RETURNING id, status, last_seq
), inserted AS (
  INSERT INTO messages (from_agent, to_agent, body, thread_id, thread_seq)
  SELECT $2::uuid, $3::uuid, $4, bumped.id, bumped.last_seq
  FROM bumped
  RETURNING id, thread_seq, created_at
), receipt AS (
  INSERT INTO message_receipts (message_id, agent_id)
  SELECT inserted.id, $3::uuid
  FROM inserted
  WHERE $3::uuid IS NOT NULL
  RETURNING message_id
), seen AS (
  UPDATE thread_participants
  SET last_read_seq = COALESCE((SELECT thread_seq FROM inserted), last_read_seq)
  WHERE thread_id = $1::uuid AND agent_id = $2::uuid
  RETURNING agent_id
)
SELECT inserted.id, inserted.thread_seq, inserted.created_at, bumped.status
FROM inserted, bumped`;

const messageSchema = {
  type: 'object',
  additionalProperties: false,
  required: ['id', 'seq', 'from', 'body', 'createdAt'],
  properties: {
    id: { type: 'string' },
    seq: { type: 'integer' },
    from: { type: 'string' },
    body: { type: 'string' },
    createdAt: { type: 'string' },
  },
} as const;

const participantsSchema = {
  type: 'array',
  items: {
    type: 'object',
    additionalProperties: false,
    required: ['name'],
    properties: { name: { type: 'string' } },
  },
} as const;

const threadProperties = {
  id: { type: 'string' },
  kind: { type: 'string', enum: ['ask', 'thread'] },
  status: { type: 'string', enum: ['open', 'answered', 'closed'] },
  createdBy: { type: 'string' },
  createdAt: { type: 'string' },
  updatedAt: { type: 'string' },
  expired: { type: 'boolean' },
  lastSeq: { type: 'integer' },
  myReadSeq: { type: 'integer' },
  participants: participantsSchema,
} as const;

const postedSchema = {
  type: 'object',
  additionalProperties: false,
  required: ['threadId', 'messageId', 'seq', 'createdAt', 'wake'],
  properties: {
    threadId: { type: 'string' },
    messageId: { type: 'string' },
    seq: { type: 'integer' },
    createdAt: { type: 'string' },
    wake: wakeReportSchema,
  },
} as const;

const replySchema = {
  type: 'object',
  additionalProperties: false,
  required: ['threadId', 'messageId', 'seq', 'createdAt', 'status'],
  properties: {
    ...postedSchema.properties,
    status: { type: 'string', enum: ['open', 'answered', 'closed'] },
  },
} as const;

const idParamsSchema = {
  type: 'object',
  additionalProperties: false,
  required: ['id'],
  properties: {
    id: { type: 'string', pattern: UUID_PATTERN },
  },
} as const;

type ReplyRefusal = { status: number; error: string };

function refused(
  row: Pick<ThreadStateRow, 'kind' | 'status' | 'created_by' | 'last_seq'> & {
    expired: boolean;
  },
  senderId: string,
): ReplyRefusal | null {
  if (row.status === 'closed') {
    return { status: 409, error: 'thread closed' };
  }
  if (row.expired) {
    return { status: 410, error: 'thread expired' };
  }
  if (row.kind !== 'ask') {
    return null;
  }
  if (row.created_by === senderId) {
    return { status: 409, error: 'the asker cannot answer its own ask' };
  }
  if (row.status === 'answered' || row.last_seq >= 2) {
    return { status: 409, error: 'thread already answered' };
  }
  return null;
}

function replyTokenHeader(
  value: string | string[] | undefined,
): string | undefined {
  const token = Array.isArray(value) ? value[0] : value;
  return token === undefined || token === '' ? undefined : token;
}

type TokenThreadRow = ThreadStateRow & {
  reply_token_hash: Buffer | null;
  token_expired: boolean;
  recipient_id: string | null;
  recipient_name: string | null;
  recipient_kind: string | null;
};

async function replyTokenThread(
  pool: Pool,
  threadId: string,
  ttlHours: number | null,
): Promise<TokenThreadRow | null> {
  const result = await pool.query<TokenThreadRow>(
    `SELECT t.id,
            t.kind,
            t.status,
            t.created_by,
            t.last_seq,
            t.reply_token_hash,
            (
              t.reply_token_expires_at IS NOT NULL
              AND t.reply_token_expires_at <= clock_timestamp()
            ) AS token_expired,
            (
              $2::integer IS NOT NULL
              AND t.updated_at < clock_timestamp() - make_interval(hours => $2)
            ) AS expired,
            r.id AS recipient_id,
            r.name AS recipient_name,
            r.kind AS recipient_kind
       FROM threads t
       LEFT JOIN LATERAL (
         SELECT a.id, a.name, a.kind
           FROM thread_participants tp
           JOIN agents a ON a.id = tp.agent_id
          WHERE tp.thread_id = t.id
            AND tp.agent_id <> t.created_by
          ORDER BY tp.joined_at ASC
          LIMIT 1
       ) r ON true
      WHERE t.id = $1::uuid`,
    [threadId, ttlHours],
  );
  return result.rows[0] ?? null;
}

// Spend the token first, then explain a refusal from the thread it names: a
// replay after the answer was stored reaches the same 409 the bearer path gives,
// a token minted for another thread is simply invalid, and an expired one says
// so. Burning before the insert is what makes it single use, races included.
async function consumeReplyToken(
  pool: Pool,
  threadId: string,
  token: string,
  ttlHours: number | null,
): Promise<
  | { ok: true; agent: AuthenticatedAgent }
  | { ok: false; status: number; error: string }
> {
  const burned = await pool.query(
    `UPDATE threads
        SET reply_token_hash = NULL,
            reply_token_expires_at = NULL
      WHERE id = $1::uuid
        AND reply_token_hash = $2
        AND (
          reply_token_expires_at IS NULL
          OR reply_token_expires_at > clock_timestamp()
        )
      RETURNING id`,
    [threadId, hashToken(token)],
  );
  const row = await replyTokenThread(pool, threadId, ttlHours);
  if (row === null) {
    return { ok: false, status: 404, error: 'not found' };
  }
  if (burned.rowCount !== 1) {
    if (row.token_expired) {
      return { ok: false, status: 401, error: 'reply token expired' };
    }
    if (row.recipient_id !== null) {
      const denial = refused(row, row.recipient_id);
      if (denial !== null) {
        return { ok: false, status: denial.status, error: denial.error };
      }
    }
    return { ok: false, status: 401, error: 'invalid reply token' };
  }
  if (row.recipient_id === null) {
    return { ok: false, status: 404, error: 'not found' };
  }
  return {
    ok: true,
    agent: {
      id: row.recipient_id,
      name: row.recipient_name ?? '',
      kind: row.recipient_kind === 'mcp' ? 'mcp' : 'bot',
    },
  };
}

async function postMessage(
  query: Pick<Pool, 'query'> | Pick<PoolClient, 'query'>,
  input: {
    threadId: string;
    fromId: string;
    toId: string;
    body: string;
    ttlHours: number | null;
  },
): Promise<PostedRow | null> {
  const result = await query.query<PostedRow>(POST_MESSAGE_SQL, [
    input.threadId,
    input.fromId,
    input.toId,
    input.body,
    input.ttlHours,
  ]);
  return result.rows[0] ?? null;
}

async function loadThreadState(
  pool: Pool,
  threadId: string,
  agentId: string,
  ttlHours: number | null,
): Promise<ThreadStateRow | null> {
  const result = await pool.query<ThreadStateRow>(
    `SELECT t.id,
            t.kind,
            t.status,
            t.created_by,
            t.last_seq,
            (
              $3::integer IS NOT NULL
              AND t.updated_at < clock_timestamp() - make_interval(hours => $3)
            ) AS expired
     FROM threads t
     JOIN thread_participants tp
       ON tp.thread_id = t.id AND tp.agent_id = $2::uuid
     WHERE t.id = $1::uuid`,
    [threadId, agentId, ttlHours],
  );
  return result.rows[0] ?? null;
}

async function participantsFor(
  pool: Pool,
  threadIds: string[],
): Promise<Map<string, string[]>> {
  const byThread = new Map<string, string[]>();
  if (threadIds.length === 0) {
    return byThread;
  }
  const result = await pool.query<ParticipantRow>(
    `SELECT tp.thread_id, a.name
     FROM thread_participants tp
     JOIN agents a ON a.id = tp.agent_id
     WHERE tp.thread_id = ANY($1::uuid[])
     ORDER BY a.name ASC`,
    [threadIds],
  );
  for (const row of result.rows) {
    const names = byThread.get(row.thread_id) ?? [];
    names.push(row.name);
    byThread.set(row.thread_id, names);
  }
  return byThread;
}

export function registerThreadRoutes(
  app: FastifyInstance,
  pool: Pool,
  options?: ThreadOptions,
): void {
  const ttlHours =
    options?.ttlHours === undefined
      ? DEFAULT_THREAD_TTL_HOURS
      : options.ttlHours;
  const doorbell = options?.doorbell ?? null;

  app.post<{
    Body: { to: string; body: string; kind?: ThreadKind };
  }>(
    '/threads',
    {
      schema: {
        body: {
          type: 'object',
          additionalProperties: false,
          required: ['to', 'body'],
          properties: {
            to: { type: 'string', minLength: 1, maxLength: 64 },
            body: { type: 'string', minLength: 1, maxLength: MAX_BODY_BYTES },
            kind: { type: 'string', enum: ['ask', 'thread'] },
          },
        },
        response: { 201: postedSchema },
      },
    },
    async (request, reply) => {
      const sender = caller(request);
      const { to, body } = request.body;
      const kind = request.body.kind ?? 'ask';
      if (
        body.includes('\0') ||
        Buffer.byteLength(body, 'utf8') > MAX_BODY_BYTES
      ) {
        return reply.code(400).send({ error: 'invalid request' });
      }
      const recipient = await resolveRecipient(pool, sender, to, {
        allowBroadcast: false,
      });
      if (!recipient.ok) {
        return reply.code(recipient.status).send({ error: recipient.error });
      }
      if (recipient.id === null) {
        return reply.code(400).send({ error: 'invalid request' });
      }
      const client = await pool.connect();
      let released = false;
      const releaseClient = (): void => {
        if (!released) {
          released = true;
          client.release();
        }
      };
      try {
        await client.query('BEGIN');
        // The bot this asks may hold no agent token (#93), so the thread carries
        // one reply token instead: only its sha256 is stored, and the plaintext
        // travels once, inside this recipient's doorbell payload.
        const replyToken = generateToken();
        const created = await client.query<{ id: string }>(
          `INSERT INTO threads (kind, created_by, reply_token_hash, reply_token_expires_at)
           VALUES ($1, $2, $3,
                   CASE
                     WHEN $4::integer IS NULL THEN NULL
                     ELSE clock_timestamp() + make_interval(hours => $4)
                   END)
           RETURNING id`,
          [kind, sender.id, hashToken(replyToken), ttlHours],
        );
        const threadId = created.rows[0]?.id;
        if (threadId === undefined) {
          throw new Error('expected a thread id');
        }
        await client.query(
          `INSERT INTO thread_participants (thread_id, agent_id)
           VALUES ($1::uuid, $2::uuid), ($1::uuid, $3::uuid)`,
          [threadId, sender.id, recipient.id],
        );
        const posted = await postMessage(client, {
          threadId,
          fromId: sender.id,
          toId: recipient.id,
          body,
          ttlHours,
        });
        if (posted === null) {
          throw new Error('thread message was not stored');
        }
        await client.query('COMMIT');
        // Release before ringing. notifyMessage reads the receipts over the pool,
        // so holding this client makes every ask in a full pool wait out the
        // connect timeout: the ring is skipped and the report says nobody had a
        // target while the wake is gone.
        releaseClient();
        const wake = await notifyMessage(
          pool,
          doorbell,
          {
            messageId: posted.id,
            from: sender.name,
            body,
            event: 'request',
            threadId,
            replyToken,
          },
          request.log,
        );
        const payload: CreatedThread = {
          threadId,
          messageId: posted.id,
          seq: posted.thread_seq,
          createdAt: isoTime(posted.created_at) ?? '',
          wake,
        };
        return reply.code(201).send(payload);
      } catch (error) {
        // A failure after COMMIT has nothing to roll back, and the client is
        // already back in the pool.
        if (!released) {
          await client.query('ROLLBACK').catch(() => undefined);
        }
        throw error;
      } finally {
        releaseClient();
      }
    },
  );

  app.get<{ Querystring: { limit?: string; cursor?: string } }>(
    '/threads',
    {
      schema: {
        querystring: {
          type: 'object',
          additionalProperties: false,
          properties: {
            limit: { type: 'string', pattern: '^[1-9][0-9]{0,2}$' },
            cursor: { type: 'string', minLength: 1, maxLength: 128 },
          },
        },
        response: {
          200: {
            type: 'object',
            additionalProperties: false,
            required: ['threads', 'next'],
            properties: {
              threads: {
                type: 'array',
                items: {
                  type: 'object',
                  additionalProperties: false,
                  required: [
                    'id',
                    'kind',
                    'status',
                    'createdBy',
                    'createdAt',
                    'updatedAt',
                    'expired',
                    'lastSeq',
                    'myReadSeq',
                    'unread',
                    'participants',
                  ],
                  properties: {
                    ...threadProperties,
                    unread: { type: 'integer' },
                  },
                },
              },
              next: { type: ['string', 'null'] },
            },
          },
        },
      },
    },
    async (request, reply) => {
      const agent = caller(request);
      const limit = inboxLimit(request.query.limit);
      const cursor =
        request.query.cursor === undefined
          ? null
          : parseSearchCursor(request.query.cursor);
      if (limit === undefined || cursor === undefined) {
        return reply.code(400).send({ error: 'invalid request' });
      }
      const result = await pool.query<SummaryRow>(
        `SELECT ${threadColumns('$2')},
                (
                  SELECT count(*)::integer
                  FROM messages m
                  WHERE m.thread_id = t.id
                    AND m.thread_seq > tp.last_read_seq
                ) AS unread
         FROM threads t
         JOIN thread_participants tp
           ON tp.thread_id = t.id AND tp.agent_id = $1::uuid
         JOIN agents creator ON creator.id = t.created_by
         WHERE (
           $3::timestamptz IS NULL
           OR (date_trunc('milliseconds', t.created_at), t.id)
              < ($3::timestamptz, $4::uuid)
         )
         ORDER BY date_trunc('milliseconds', t.created_at) DESC, t.id DESC
         LIMIT $5`,
        [
          agent.id,
          ttlHours,
          cursor?.createdAt ?? null,
          cursor?.id ?? null,
          limit + 1,
        ],
      );
      const hasMore = result.rows.length > limit;
      const rows = hasMore ? result.rows.slice(0, limit) : result.rows;
      const names = await participantsFor(
        pool,
        rows.map((row) => row.id),
      );
      const threads: ThreadSummary[] = rows.map((row) => ({
        id: row.id,
        kind: row.kind,
        status: row.status,
        createdBy: row.created_by,
        createdAt: isoTime(row.created_at) ?? '',
        updatedAt: isoTime(row.updated_at) ?? '',
        expired: row.expired,
        lastSeq: row.last_seq,
        myReadSeq: row.last_read_seq,
        unread: row.unread,
        participants: (names.get(row.id) ?? []).map((name) => ({ name })),
      }));
      const last = threads[threads.length - 1];
      const page: ThreadPage = {
        threads,
        next:
          hasMore && last !== undefined
            ? encodeSearchCursor(last.createdAt, last.id)
            : null,
      };
      return page;
    },
  );

  app.get<{ Params: { id: string } }>(
    '/threads/:id',
    {
      schema: {
        params: idParamsSchema,
        response: {
          200: {
            type: 'object',
            additionalProperties: false,
            required: [
              'id',
              'kind',
              'status',
              'createdBy',
              'createdAt',
              'updatedAt',
              'expiresAt',
              'expired',
              'lastSeq',
              'myReadSeq',
              'participants',
              'messages',
            ],
            properties: {
              ...threadProperties,
              expiresAt: { type: ['string', 'null'] },
              messages: { type: 'array', items: messageSchema },
            },
          },
        },
      },
    },
    async (request, reply) => {
      const agent = caller(request);
      const result = await pool.query<DetailRow>(
        `SELECT ${threadColumns('$3')},
                CASE
                  WHEN $3::integer IS NULL THEN NULL
                  ELSE date_trunc(
                    'milliseconds',
                    t.updated_at + make_interval(hours => $3)
                  )
                END AS expires_at
         FROM threads t
         JOIN thread_participants tp
           ON tp.thread_id = t.id AND tp.agent_id = $2::uuid
         JOIN agents creator ON creator.id = t.created_by
         WHERE t.id = $1::uuid`,
        [request.params.id, agent.id, ttlHours],
      );
      const row = result.rows[0];
      if (row === undefined) {
        return reply.code(404).send({ error: 'not found' });
      }
      const messages = await pool.query<{
        id: string;
        thread_seq: number;
        from_name: string;
        body: string;
        created_at: Date | string;
      }>(
        `SELECT m.id,
                m.thread_seq,
                sender.name AS from_name,
                m.body,
                date_trunc('milliseconds', m.created_at) AS created_at
         FROM messages m
         JOIN agents sender ON sender.id = m.from_agent
         WHERE m.thread_id = $1::uuid
         ORDER BY m.thread_seq ASC`,
        [row.id],
      );
      const names = await participantsFor(pool, [row.id]);
      const detailed: ThreadMessage[] = messages.rows.map((message) => ({
        id: message.id,
        seq: message.thread_seq,
        from: message.from_name,
        body: message.body,
        createdAt: isoTime(message.created_at) ?? '',
      }));
      const body: ThreadDetail = {
        id: row.id,
        kind: row.kind,
        status: row.status,
        createdBy: row.created_by,
        createdAt: isoTime(row.created_at) ?? '',
        updatedAt: isoTime(row.updated_at) ?? '',
        expiresAt: isoTime(row.expires_at),
        expired: row.expired,
        lastSeq: row.last_seq,
        myReadSeq: row.last_read_seq,
        participants: (names.get(row.id) ?? []).map((name) => ({ name })),
        messages: detailed,
      };
      return body;
    },
  );

  app.post<{ Params: { id: string }; Body: { body: string } }>(
    '/threads/:id/reply',
    {
      schema: {
        params: idParamsSchema,
        body: {
          type: 'object',
          additionalProperties: false,
          required: ['body'],
          properties: {
            body: { type: 'string', minLength: 1, maxLength: MAX_BODY_BYTES },
          },
        },
        response: { 201: replySchema },
      },
    },
    async (request, reply) => {
      const body = request.body.body;
      if (
        body.includes('\0') ||
        Buffer.byteLength(body, 'utf8') > MAX_BODY_BYTES
      ) {
        return reply.code(400).send({ error: 'invalid request' });
      }
      // A bot woken by a doorbell holds no agent token; its payload carries a
      // reply token for this one thread (#93). The rest of the route is the same
      // for both callers, and the token is spent before the message is stored.
      const replyToken = replyTokenHeader(
        request.headers['x-wake-reply-token'],
      );
      let agent: AuthenticatedAgent;
      if (request.agent !== null) {
        agent = caller(request);
      } else {
        if (replyToken === undefined) {
          return reply.code(401).send({ error: 'unauthorized' });
        }
        const exchanged = await consumeReplyToken(
          pool,
          request.params.id,
          replyToken,
          ttlHours,
        );
        if (!exchanged.ok) {
          return reply.code(exchanged.status).send({ error: exchanged.error });
        }
        agent = exchanged.agent;
      }
      const state = await loadThreadState(
        pool,
        request.params.id,
        agent.id,
        ttlHours,
      );
      if (state === null) {
        return reply.code(404).send({ error: 'not found' });
      }
      const denial = refused(state, agent.id);
      if (denial !== null) {
        return reply.code(denial.status).send({ error: denial.error });
      }
      const others = await pool.query<{ agent_id: string }>(
        `SELECT agent_id
         FROM thread_participants
         WHERE thread_id = $1::uuid AND agent_id <> $2::uuid
         ORDER BY joined_at ASC
         LIMIT 1`,
        [state.id, agent.id],
      );
      const toId = others.rows[0]?.agent_id;
      if (toId === undefined) {
        return reply.code(400).send({ error: 'thread has no recipient' });
      }
      const posted = await postMessage(pool, {
        threadId: state.id,
        fromId: agent.id,
        toId,
        body,
        ttlHours,
      });
      if (posted === null) {
        const again = await loadThreadState(pool, state.id, agent.id, ttlHours);
        if (again === null) {
          return reply.code(404).send({ error: 'not found' });
        }
        const raced = refused(again, agent.id);
        if (raced !== null) {
          return reply.code(raced.status).send({ error: raced.error });
        }
        return reply.code(409).send({ error: 'thread already answered' });
      }
      const wake = await notifyMessage(
        pool,
        doorbell,
        {
          messageId: posted.id,
          from: agent.name,
          body,
          event: 'reply',
          threadId: state.id,
        },
        request.log,
      );
      const payload: ThreadReply = {
        threadId: state.id,
        messageId: posted.id,
        seq: posted.thread_seq,
        createdAt: isoTime(posted.created_at) ?? '',
        status: posted.status,
        wake,
      };
      return reply.code(201).send(payload);
    },
  );

  app.post<{ Params: { id: string }; Body: { seq: number } }>(
    '/threads/:id/read',
    {
      schema: {
        params: idParamsSchema,
        body: {
          type: 'object',
          additionalProperties: false,
          required: ['seq'],
          properties: {
            seq: { type: 'integer', minimum: 0, maximum: 2147483647 },
          },
        },
        response: {
          200: {
            type: 'object',
            additionalProperties: false,
            required: ['threadId', 'readSeq'],
            properties: {
              threadId: { type: 'string' },
              readSeq: { type: 'integer' },
            },
          },
        },
      },
    },
    async (request, reply) => {
      const agent = caller(request);
      const result = await pool.query<{ last_read_seq: number }>(
        `UPDATE thread_participants tp
         SET last_read_seq = LEAST($3::integer, t.last_seq)
         FROM threads t
         WHERE tp.thread_id = t.id
           AND tp.thread_id = $1::uuid
           AND tp.agent_id = $2::uuid
         RETURNING tp.last_read_seq`,
        [request.params.id, agent.id, request.body.seq],
      );
      const row = result.rows[0];
      if (row === undefined) {
        return reply.code(404).send({ error: 'not found' });
      }
      const payload: ThreadReadReceipt = {
        threadId: request.params.id,
        readSeq: row.last_read_seq,
      };
      return payload;
    },
  );
}
