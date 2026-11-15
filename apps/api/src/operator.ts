import type { IncomingHttpHeaders } from 'node:http';
import type { FastifyInstance } from 'fastify';
import type { Pool } from 'pg';
import { isoTime } from './time.js';
import { generateToken, hashToken, secretsEqual } from './token.js';

// The single public MCP identity. The token is an ordinary agent token (hashed,
// revocable, rotatable) of an agent whose `kind` is 'mcp'; there is no second
// token store. Waking agents never authenticate as this one, and it can never
// reach a private bot (see resolveRecipient).
export const MCP_AGENT_NAME = 'mcp';

const NAME_SCHEMA = {
  type: 'string',
  minLength: 1,
  maxLength: 64,
  pattern: '^[A-Za-z0-9][A-Za-z0-9_-]*$',
} as const;

export type OperatorOptions = {
  registrationSecret?: string;
};

type IdentityRow = {
  id: string;
  name: string;
  kind: string;
  is_private: boolean;
  token_revoked: boolean;
  created_at: Date | string;
  last_seen_at: Date | string | null;
};

const IDENTITY_SQL = `SELECT id, name, kind, is_private, token_revoked, created_at, last_seen_at
  FROM agents
  WHERE lower(name) = $1`;

const ERROR_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['error'],
  properties: { error: { type: 'string' } },
} as const;

const IDENTITY_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: [
    'id',
    'name',
    'kind',
    'private',
    'tokenRevoked',
    'createdAt',
    'lastSeenAt',
  ],
  properties: {
    id: { type: 'string' },
    name: { type: 'string' },
    kind: { type: 'string', enum: ['bot', 'mcp'] },
    private: { type: 'boolean' },
    tokenRevoked: { type: 'boolean' },
    createdAt: { type: 'string' },
    lastSeenAt: { type: ['string', 'null'] },
  },
} as const;

function identityBody(row: IdentityRow): {
  id: string;
  name: string;
  kind: 'bot' | 'mcp';
  private: boolean;
  tokenRevoked: boolean;
  createdAt: string;
  lastSeenAt: string | null;
} {
  return {
    id: row.id,
    name: row.name,
    kind: row.kind === 'mcp' ? 'mcp' : 'bot',
    private: row.is_private,
    tokenRevoked: row.token_revoked,
    createdAt: isoTime(row.created_at) ?? '',
    lastSeenAt: isoTime(row.last_seen_at),
  };
}

function operatorHeader(headers: IncomingHttpHeaders): string {
  const raw = headers['x-registration-secret'];
  if (Array.isArray(raw)) {
    return raw[0] ?? '';
  }
  return raw ?? '';
}

// Operator routes: the same `x-registration-secret` that gates POST /agents.
// With no secret configured they do not exist, so an instance that never meant
// to expose them cannot be provisioned by accident.
export function registerOperatorRoutes(
  app: FastifyInstance,
  pool: Pool,
  options: OperatorOptions,
): void {
  const secret = options.registrationSecret;

  app.register(async (operator) => {
    operator.addHook('onRequest', async (request, reply) => {
      if (secret === undefined || secret === '') {
        return reply.code(404).send({ error: 'not found' });
      }
      if (!secretsEqual(secret, operatorHeader(request.headers))) {
        return reply.code(401).send({ error: 'unauthorized' });
      }
    });

    operator.get(
      '/mcp/identity',
      { schema: { response: { 200: IDENTITY_SCHEMA, 404: ERROR_SCHEMA } } },
      async (_request, reply) => {
        const result = await pool.query<IdentityRow>(IDENTITY_SQL, [
          MCP_AGENT_NAME,
        ]);
        const row = result.rows[0];
        if (row === undefined || row.kind !== 'mcp') {
          return reply.code(404).send({ error: 'not found' });
        }
        return identityBody(row);
      },
    );

    operator.post(
      '/mcp/identity',
      {
        schema: {
          response: {
            201: {
              type: 'object',
              additionalProperties: false,
              required: ['id', 'name', 'token'],
              properties: {
                id: { type: 'string' },
                name: { type: 'string' },
                token: { type: 'string' },
              },
            },
            409: ERROR_SCHEMA,
          },
        },
      },
      async (_request, reply) => {
        const token = generateToken();
        const result = await pool.query<{ id: string; name: string }>(
          `INSERT INTO agents (name, token_hash, kind)
           VALUES ($2, $1, 'mcp')
           ON CONFLICT (lower(name)) DO NOTHING
           RETURNING id, name`,
          [hashToken(token), MCP_AGENT_NAME],
        );
        const row = result.rows[0];
        if (row === undefined) {
          // Rotate instead of silently replacing an identity that is in use.
          return reply.code(409).send({ error: 'name already registered' });
        }
        return reply.code(201).send({ id: row.id, name: row.name, token });
      },
    );

    operator.post(
      '/mcp/identity/rotate',
      {
        schema: {
          response: {
            200: {
              type: 'object',
              additionalProperties: false,
              required: ['id', 'name', 'token'],
              properties: {
                id: { type: 'string' },
                name: { type: 'string' },
                token: { type: 'string' },
              },
            },
            404: ERROR_SCHEMA,
          },
        },
      },
      async (_request, reply) => {
        const token = generateToken();
        const result = await pool.query<{ id: string; name: string }>(
          `UPDATE agents
           SET token_hash = $1,
               token_revoked = false,
               pending_token_hash = NULL,
               pending_expires_at = NULL
           WHERE lower(name) = $2
             AND kind = 'mcp'
           RETURNING id, name`,
          [hashToken(token), MCP_AGENT_NAME],
        );
        const row = result.rows[0];
        if (row === undefined) {
          return reply.code(404).send({ error: 'not found' });
        }
        return { id: row.id, name: row.name, token };
      },
    );

    operator.delete(
      '/mcp/identity',
      {
        schema: {
          response: {
            200: {
              type: 'object',
              additionalProperties: false,
              required: ['name', 'tokenRevoked'],
              properties: {
                name: { type: 'string' },
                tokenRevoked: { type: 'boolean' },
              },
            },
            404: ERROR_SCHEMA,
          },
        },
      },
      async (_request, reply) => {
        const result = await pool.query<{ name: string }>(
          `UPDATE agents
           SET token_revoked = true,
               pending_token_hash = NULL,
               pending_expires_at = NULL
           WHERE lower(name) = $1
             AND kind = 'mcp'
           RETURNING name`,
          [MCP_AGENT_NAME],
        );
        const row = result.rows[0];
        if (row === undefined) {
          return reply.code(404).send({ error: 'not found' });
        }
        return { name: row.name, tokenRevoked: true };
      },
    );

    operator.put<{ Params: { name: string }; Body: { private: boolean } }>(
      '/agents/:name/private',
      {
        schema: {
          params: {
            type: 'object',
            additionalProperties: false,
            required: ['name'],
            properties: { name: NAME_SCHEMA },
          },
          body: {
            type: 'object',
            additionalProperties: false,
            required: ['private'],
            properties: { private: { type: 'boolean' } },
          },
          response: {
            200: {
              type: 'object',
              additionalProperties: false,
              required: ['name', 'private'],
              properties: {
                name: { type: 'string' },
                private: { type: 'boolean' },
              },
            },
            404: ERROR_SCHEMA,
          },
        },
      },
      async (request, reply) => {
        const result = await pool.query<{ name: string; is_private: boolean }>(
          `UPDATE agents
           SET is_private = $2
           WHERE lower(name) = lower($1)
           RETURNING name, is_private`,
          [request.params.name, request.body.private],
        );
        const row = result.rows[0];
        if (row === undefined) {
          return reply.code(404).send({ error: 'not found' });
        }
        return { name: row.name, private: row.is_private };
      },
    );
  });
}
