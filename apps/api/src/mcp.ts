import type { FastifyInstance, FastifyRequest } from 'fastify';
import type { Pool } from 'pg';
import { caller } from './auth.js';
import { ConfigError } from './options.js';
import { slidingWindow, type RateLimit } from './rate-limit.js';

// MCP 2025-06-18 over Streamable HTTP: one POST per JSON-RPC message, answered
// with application/json (or a one-event SSE stream when the client asks for
// that only). Stateless on purpose: no session id to lose, cheaper through
// Cloudflare's 100 s proxy limit, and every request is authenticated on its own.
// Same bound as MAX_READ_IDS in app.ts: the tool advertises it, so it must
// refuse it too. One list per call, so a caller cannot splice in more work than
// the route behind it accepts.
export const MAX_TOOL_IDS = 100;

export const MCP_PROTOCOL_VERSION = '2025-06-18';
export const MCP_SERVER_NAME = 'wake';
export const MCP_SERVER_VERSION = '0.2.0';
export const DEFAULT_MCP_RATE_LIMIT_PER_MINUTE = 60;
export const MAX_MCP_RATE_LIMIT_PER_MINUTE = 6000;

export type McpConfig = {
  rateLimitPerMinute: number;
  limit: RateLimit;
};

function setting(value: string | undefined): string | undefined {
  const trimmed = value?.trim();
  return trimmed === undefined || trimmed === '' ? undefined : trimmed;
}

export function parseMcpRateLimit(value: string | undefined): number {
  const raw = setting(value);
  if (raw === undefined) {
    return DEFAULT_MCP_RATE_LIMIT_PER_MINUTE;
  }
  if (!/^\d+$/.test(raw) || Number(raw) > MAX_MCP_RATE_LIMIT_PER_MINUTE) {
    throw new ConfigError(
      `WAKE_MCP_RATE_LIMIT_PER_MINUTE must be an integer from 0 to ${MAX_MCP_RATE_LIMIT_PER_MINUTE} (0 disables the limit)`,
    );
  }
  return Number(raw);
}

// Off by default: an instance that does not set WAKE_MCP_ENABLED=true has no
// /mcp route at all, so the public surface cannot be reached by accident.
export function parseMcp(env: NodeJS.ProcessEnv): McpConfig | null {
  const raw = setting(env.WAKE_MCP_ENABLED)?.toLowerCase();
  if (raw === undefined || raw === 'false' || raw === '0') {
    return null;
  }
  if (raw !== 'true' && raw !== '1') {
    throw new ConfigError('WAKE_MCP_ENABLED must be true or false');
  }
  const rateLimitPerMinute = parseMcpRateLimit(
    env.WAKE_MCP_RATE_LIMIT_PER_MINUTE,
  );
  return {
    rateLimitPerMinute,
    limit: slidingWindow({ limit: rateLimitPerMinute, windowMs: 60_000 }),
  };
}

type JsonRpcId = string | number | null;

type JsonRpcBody = {
  jsonrpc?: unknown;
  id?: unknown;
  method?: unknown;
  params?: unknown;
};

type RpcError = { code: number; message: string; data?: unknown };

function result(id: JsonRpcId, value: unknown): Record<string, unknown> {
  return { jsonrpc: '2.0', id, result: value };
}

function failure(id: JsonRpcId, error: RpcError): Record<string, unknown> {
  return { jsonrpc: '2.0', id, error };
}

function isId(value: unknown): value is JsonRpcId {
  return (
    value === null ||
    typeof value === 'string' ||
    (typeof value === 'number' && Number.isFinite(value))
  );
}

class ToolArgumentError extends Error {}

function objectArgs(value: unknown): Record<string, unknown> {
  if (value === undefined) {
    return {};
  }
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new ToolArgumentError('arguments must be an object');
  }
  return value as Record<string, unknown>;
}

function requireString(
  args: Record<string, unknown>,
  name: string,
  maxLength = 8192,
): string {
  const value = args[name];
  if (typeof value !== 'string' || value.length === 0) {
    throw new ToolArgumentError(`${name} must be a non-empty string`);
  }
  if (value.length > maxLength) {
    throw new ToolArgumentError(
      `${name} must be at most ${maxLength} characters`,
    );
  }
  return value;
}

function optionalString(
  args: Record<string, unknown>,
  name: string,
  maxLength = 8192,
): string | undefined {
  const value = args[name];
  if (value === undefined) {
    return undefined;
  }
  if (typeof value !== 'string' || value.length === 0) {
    throw new ToolArgumentError(`${name} must be a non-empty string`);
  }
  if (value.length > maxLength) {
    throw new ToolArgumentError(
      `${name} must be at most ${maxLength} characters`,
    );
  }
  return value;
}

function requireInteger(
  args: Record<string, unknown>,
  name: string,
  min: number,
  max: number,
): number {
  const value = optionalInteger(args, name, min, max);
  if (value === undefined) {
    throw new ToolArgumentError(`${name} is required`);
  }
  return value;
}

function optionalInteger(
  args: Record<string, unknown>,
  name: string,
  min: number,
  max: number,
): number | undefined {
  const value = args[name];
  if (value === undefined) {
    return undefined;
  }
  if (typeof value !== 'number' || !Number.isInteger(value)) {
    throw new ToolArgumentError(`${name} must be an integer`);
  }
  if (value < min || value > max) {
    throw new ToolArgumentError(`${name} must be from ${min} to ${max}`);
  }
  return value;
}

function optionalEnum<T extends string>(
  args: Record<string, unknown>,
  name: string,
  values: readonly T[],
): T | undefined {
  const value = args[name];
  if (value === undefined) {
    return undefined;
  }
  if (typeof value !== 'string' || !values.includes(value as T)) {
    throw new ToolArgumentError(`${name} must be one of ${values.join(', ')}`);
  }
  return value as T;
}

// The server, not the client, enforces the schema it advertises: an empty or
// oversized list is -32602 here, not an HTTP 400 from the route behind it.
function stringArray(
  args: Record<string, unknown>,
  name: string,
  max = MAX_TOOL_IDS,
): string[] {
  const value = args[name];
  if (
    !Array.isArray(value) ||
    value.length === 0 ||
    value.some((item) => typeof item !== 'string' || item.length === 0)
  ) {
    throw new ToolArgumentError(`${name} must be a non-empty array of strings`);
  }
  if (value.length > max) {
    throw new ToolArgumentError(`${name} must be at most ${max} ids`);
  }
  return value as string[];
}

type ToolCall = {
  method: 'GET' | 'POST';
  url: string;
  payload?: Record<string, unknown>;
};

type Tool = {
  name: string;
  description: string;
  inputSchema: Record<string, unknown>;
  call: (args: Record<string, unknown>) => ToolCall;
};

function query(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined) {
      search.set(key, String(value));
    }
  }
  const encoded = search.toString();
  return encoded === '' ? '' : `?${encoded}`;
}

const TOOLS: Tool[] = [
  {
    name: 'wake_ask',
    description:
      'Ask one bot a question. Opens a thread and wakes the recipient with the thread id. Use wake_reply for the answer; a thread opened with kind "ask" takes exactly one answer, while "thread" stays open.',
    inputSchema: {
      type: 'object',
      additionalProperties: false,
      required: ['to', 'body'],
      properties: {
        to: {
          type: 'string',
          maxLength: 64,
          description: 'recipient agent name',
        },
        body: { type: 'string', maxLength: 8192 },
        kind: { type: 'string', enum: ['ask', 'thread'] },
      },
    },
    call: (args) => {
      const to = requireString(args, 'to', 64);
      const body = requireString(args, 'body');
      const kind = optionalEnum(args, 'kind', ['ask', 'thread']);
      return {
        method: 'POST',
        url: '/threads',
        payload: kind === undefined ? { to, body } : { to, body, kind },
      };
    },
  },
  {
    name: 'wake_reply',
    description:
      'Answer a thread you are part of. The asker is woken; the asker cannot answer its own ask.',
    inputSchema: {
      type: 'object',
      additionalProperties: false,
      required: ['threadId', 'body'],
      properties: {
        threadId: { type: 'string' },
        body: { type: 'string', maxLength: 8192 },
      },
    },
    call: (args) => ({
      method: 'POST',
      url: `/threads/${encodeURIComponent(requireString(args, 'threadId', 64))}/reply`,
      payload: { body: requireString(args, 'body') },
    }),
  },
  {
    name: 'wake_read_thread',
    description:
      'Read one thread: status, participants, read positions and the messages with their sequence numbers.',
    inputSchema: {
      type: 'object',
      additionalProperties: false,
      required: ['threadId'],
      properties: { threadId: { type: 'string' } },
    },
    call: (args) => ({
      method: 'GET',
      url: `/threads/${encodeURIComponent(requireString(args, 'threadId', 64))}`,
    }),
  },
  {
    name: 'wake_thread_read',
    description:
      'Move your read position in a thread up to a sequence number (the API clamps it to the newest message).',
    inputSchema: {
      type: 'object',
      additionalProperties: false,
      required: ['threadId', 'seq'],
      properties: {
        threadId: { type: 'string' },
        seq: { type: 'integer', minimum: 0, maximum: 2147483647 },
      },
    },
    call: (args) => ({
      method: 'POST',
      url: `/threads/${encodeURIComponent(requireString(args, 'threadId', 64))}/read`,
      payload: { seq: requireInteger(args, 'seq', 0, 2147483647) },
    }),
  },
  {
    name: 'wake_threads',
    description:
      'List your threads, newest first, with unread counts. Page with the returned cursor.',
    inputSchema: {
      type: 'object',
      additionalProperties: false,
      properties: {
        limit: { type: 'integer', minimum: 1, maximum: 100 },
        cursor: { type: 'string', maxLength: 128 },
      },
    },
    call: (args) => ({
      method: 'GET',
      url: `/threads${query({
        limit: optionalInteger(args, 'limit', 1, 100),
        cursor: optionalString(args, 'cursor', 128),
      })}`,
    }),
  },
  {
    name: 'wake_inbox',
    description:
      'Read your inbox of direct messages, newest first. Use wake_inbox_read to mark messages read.',
    inputSchema: {
      type: 'object',
      additionalProperties: false,
      properties: {
        unread: { type: 'boolean' },
        limit: { type: 'integer', minimum: 1, maximum: 100 },
        after: { type: 'string', maxLength: 80 },
      },
    },
    call: (args) => {
      const unread = args['unread'];
      if (unread !== undefined && typeof unread !== 'boolean') {
        throw new ToolArgumentError('unread must be a boolean');
      }
      return {
        method: 'GET',
        url: `/inbox${query({
          unread: unread === undefined ? undefined : String(unread),
          limit: optionalInteger(args, 'limit', 1, 100),
          after: optionalString(args, 'after', 80),
        })}`,
      };
    },
  },
  {
    name: 'wake_inbox_read',
    description:
      'Mark inbox messages read by id. Marking a thread message also moves your thread read position.',
    inputSchema: {
      type: 'object',
      additionalProperties: false,
      required: ['ids'],
      properties: {
        ids: {
          type: 'array',
          minItems: 1,
          maxItems: MAX_TOOL_IDS,
          items: { type: 'string' },
        },
      },
    },
    call: (args) => ({
      method: 'POST',
      url: '/inbox/read',
      payload: { ids: stringArray(args, 'ids') },
    }),
  },
  {
    name: 'wake_bots',
    description:
      'List the agents you can reach, with online state. Private bots are not listed to the public token.',
    inputSchema: {
      type: 'object',
      additionalProperties: false,
      properties: {},
    },
    call: () => ({ method: 'GET', url: '/agents' }),
  },
  {
    name: 'wake_send',
    description:
      'Send a direct message that wakes the recipient. Use to "*" for every open bot (never private ones).',
    inputSchema: {
      type: 'object',
      additionalProperties: false,
      required: ['to', 'body'],
      properties: {
        to: { type: 'string', maxLength: 64 },
        body: { type: 'string', maxLength: 8192 },
      },
    },
    call: (args) => ({
      method: 'POST',
      url: '/messages',
      payload: {
        to: requireString(args, 'to', 64),
        body: requireString(args, 'body'),
      },
    }),
  },
  {
    name: 'wake_search',
    description:
      'Search your messages by status, sender, time window or text. Box is received (default) or sent.',
    inputSchema: {
      type: 'object',
      additionalProperties: false,
      properties: {
        status: { type: 'string', enum: ['unread', 'read', 'all'] },
        from: { type: 'string', maxLength: 64 },
        since: { type: 'string', maxLength: 40 },
        until: { type: 'string', maxLength: 40 },
        text: { type: 'string', maxLength: 1024 },
        box: { type: 'string', enum: ['received', 'sent'] },
        limit: { type: 'integer', minimum: 1, maximum: 100 },
        cursor: { type: 'string', maxLength: 128 },
      },
    },
    call: (args) => ({
      method: 'GET',
      url: `/messages/search${query({
        status: optionalEnum(args, 'status', ['unread', 'read', 'all']),
        from: optionalString(args, 'from', 64),
        since: optionalString(args, 'since', 40),
        until: optionalString(args, 'until', 40),
        text: optionalString(args, 'text', 1024),
        box: optionalEnum(args, 'box', ['received', 'sent']),
        limit: optionalInteger(args, 'limit', 1, 100),
        cursor: optionalString(args, 'cursor', 128),
      })}`,
    }),
  },
];

export type McpRouteOptions = {
  // The root instance, used to dispatch tool calls through the real routes
  // (app.inject), so auth, schemas and thread rules are the API's own.
  server: FastifyInstance;
  // Where the /mcp route is registered: inside the agent-authenticated plugin.
  routes: FastifyInstance;
  pool: Pool;
  config: McpConfig;
};

type DispatchOutcome = {
  status: number;
  body: string;
  // What the model sees: the API's answer, prefixed with the status when the
  // call failed, so a tool error is not mistaken for a payload.
  text: string;
  ok: boolean;
  error: string | null;
};

export function registerMcpRoutes(options: McpRouteOptions): void {
  const { server, routes, pool, config } = options;

  // `remote` is the peer of this process. Fastify is not told to trust a proxy
  // (`trustProxy` is off), so behind nginx or Cloudflare it records the proxy's
  // address, not the client's; turn it on in the host step, with a proxy list,
  // not by trusting every hop.
  async function audit(input: {
    agentId: string;
    method: string;
    tool: string | null;
    ok: boolean;
    error: string | null;
    status: number;
    durationMs: number;
    remote: string;
  }): Promise<void> {
    try {
      await pool.query(
        `INSERT INTO mcp_audit
           (agent_id, method, tool, ok, error, status, duration_ms, remote)
         VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8)`,
        [
          input.agentId,
          input.method.slice(0, 120),
          input.tool,
          input.ok,
          input.error,
          input.status,
          Math.max(0, Math.round(input.durationMs)),
          input.remote,
        ],
      );
    } catch (error) {
      routes.log.warn(
        { error: error instanceof Error ? error.message : String(error) },
        'mcp audit write failed',
      );
    }
  }

  async function dispatch(
    request: FastifyRequest,
    tool: Tool,
    args: Record<string, unknown>,
  ): Promise<DispatchOutcome> {
    let call: ToolCall;
    try {
      call = tool.call(args);
    } catch (error) {
      if (error instanceof ToolArgumentError) {
        const body = JSON.stringify({ error: error.message });
        return {
          status: 400,
          body,
          text: body,
          ok: false,
          error: 'invalid_arguments',
        };
      }
      throw error;
    }
    const authorization = request.headers.authorization;
    const response = await server.inject({
      method: call.method,
      url: call.url,
      headers: authorization === undefined ? {} : { authorization },
      ...(call.payload === undefined ? {} : { payload: call.payload }),
    });
    const ok = response.statusCode >= 200 && response.statusCode < 300;
    return {
      status: response.statusCode,
      body: response.body,
      text: ok ? response.body : `HTTP ${response.statusCode} ${response.body}`,
      ok,
      error: ok ? null : `http_${response.statusCode}`,
    };
  }

  type ToolCallOutcome = {
    payload: Record<string, unknown>;
    tool: string | null;
    ok: boolean;
    error: string | null;
    status: number;
  };

  async function toolCall(
    request: FastifyRequest,
    id: JsonRpcId,
    params: unknown,
  ): Promise<ToolCallOutcome> {
    const bag =
      typeof params === 'object' && params !== null
        ? (params as Record<string, unknown>)
        : {};
    const name = bag['name'];
    if (typeof name !== 'string') {
      return {
        payload: failure(id, { code: -32602, message: 'missing tool name' }),
        tool: null,
        ok: false,
        error: 'invalid_params',
        status: 200,
      };
    }
    const tool = TOOLS.find((candidate) => candidate.name === name);
    if (tool === undefined) {
      return {
        payload: failure(id, {
          code: -32602,
          message: `unknown tool: ${name}`,
        }),
        tool: name,
        ok: false,
        error: 'unknown_tool',
        status: 200,
      };
    }
    let args: Record<string, unknown>;
    try {
      args = objectArgs(bag['arguments']);
    } catch {
      return {
        payload: failure(id, {
          code: -32602,
          message: 'arguments must be an object',
        }),
        tool: name,
        ok: false,
        error: 'invalid_params',
        status: 200,
      };
    }
    const outcome = await dispatch(request, tool, args);
    if (outcome.error === 'invalid_arguments') {
      // The tool call never reached the API: the client sent bad arguments.
      return {
        payload: failure(id, {
          code: -32602,
          message: JSON.parse(outcome.body)['error'] as string,
        }),
        tool: name,
        ok: false,
        error: 'invalid_arguments',
        status: 200,
      };
    }
    return {
      payload: result(id, {
        content: [{ type: 'text', text: outcome.text }],
        isError: !outcome.ok,
      }),
      tool: name,
      ok: outcome.ok,
      error: outcome.error,
      status: outcome.status,
    };
  }

  routes.post('/mcp', async (request, reply) => {
    const started = performance.now();
    const agent = caller(request);
    const accept = request.headers.accept ?? '';
    const wantsJson =
      accept === '' ||
      accept.includes('*/*') ||
      accept.includes('application/json');
    const wantsSse = accept.includes('text/event-stream');
    if (!wantsJson && !wantsSse) {
      return reply
        .code(406)
        .send(failure(null, { code: -32600, message: 'not acceptable' }));
    }
    const sse = !wantsJson && wantsSse;

    // Only the public identity may use this route: a bot token has no business
    // on the public surface, and the mcp token is the single public credential.
    if (agent.kind !== 'mcp') {
      return reply.code(401).send({ error: 'unauthorized' });
    }

    const version = request.headers['mcp-protocol-version'];
    if (typeof version === 'string' && version !== MCP_PROTOCOL_VERSION) {
      await audit({
        agentId: agent.id,
        method: 'initialize',
        tool: null,
        ok: false,
        error: 'unsupported_protocol_version',
        status: 400,
        durationMs: performance.now() - started,
        remote: request.ip,
      });
      return reply.code(400).send(
        failure(null, {
          code: -32600,
          message: `unsupported protocol version; this server speaks ${MCP_PROTOCOL_VERSION}`,
        }),
      );
    }

    if (!config.limit.allow(agent.id)) {
      await audit({
        agentId: agent.id,
        method: 'rate_limited',
        tool: null,
        ok: false,
        error: 'rate_limited',
        status: 429,
        durationMs: performance.now() - started,
        remote: request.ip,
      });
      // JSON-RPC errors carry an error object even when the status is the
      // transport's own: a client parsing every body as JSON-RPC must not
      // meet a bare {"error": ...} here. -32000 is the implementation-defined
      // server-error range.
      return reply
        .code(429)
        .header('retry-after', '60')
        .send(failure(null, { code: -32000, message: 'rate_limited' }));
    }

    const raw: unknown = request.body;
    if (Array.isArray(raw)) {
      await audit({
        agentId: agent.id,
        method: 'batch',
        tool: null,
        ok: false,
        error: 'batching_unsupported',
        status: 400,
        durationMs: performance.now() - started,
        remote: request.ip,
      });
      return reply
        .code(400)
        .send(
          failure(null, { code: -32600, message: 'batching is not supported' }),
        );
    }
    if (typeof raw !== 'object' || raw === null) {
      return reply
        .code(400)
        .send(failure(null, { code: -32600, message: 'invalid request' }));
    }
    const body = raw as JsonRpcBody;
    if (body.jsonrpc !== '2.0' || typeof body.method !== 'string') {
      return reply.code(400).send(
        failure(isId(body.id) ? body.id : null, {
          code: -32600,
          message: 'invalid request',
        }),
      );
    }

    const method = body.method;
    const notification = !('id' in body) || body.id === undefined;
    const id: JsonRpcId = notification ? null : isId(body.id) ? body.id : null;

    const send = (payload: Record<string, unknown>) => {
      if (!sse) {
        return reply.code(200).send(payload);
      }
      // One-event SSE stream: allowed for a POST response, and what a client
      // that only accepts text/event-stream is asking for.
      return reply
        .code(200)
        .header('content-type', 'text/event-stream')
        .header('cache-control', 'no-cache')
        .send(`event: message\ndata: ${JSON.stringify(payload)}\n\n`);
    };

    if (notification) {
      // notifications/initialized, notifications/cancelled, and anything else a
      // client reports: nothing to answer, by design.
      await audit({
        agentId: agent.id,
        method,
        tool: null,
        ok: true,
        error: null,
        status: 202,
        durationMs: performance.now() - started,
        remote: request.ip,
      });
      return reply.code(202).send();
    }

    let outcome: ToolCallOutcome;
    if (method === 'initialize') {
      outcome = {
        payload: result(id, {
          protocolVersion: MCP_PROTOCOL_VERSION,
          capabilities: { tools: { listChanged: false } },
          serverInfo: { name: MCP_SERVER_NAME, version: MCP_SERVER_VERSION },
          instructions:
            'wake lets agents message each other: wake_ask opens a thread, wake_reply answers it, wake_inbox reads what arrived.',
        }),
        tool: null,
        ok: true,
        error: null,
        status: 200,
      };
    } else if (method === 'ping') {
      outcome = {
        payload: result(id, {}),
        tool: null,
        ok: true,
        error: null,
        status: 200,
      };
    } else if (method === 'tools/list') {
      outcome = {
        payload: result(id, {
          tools: TOOLS.map((tool) => ({
            name: tool.name,
            description: tool.description,
            inputSchema: tool.inputSchema,
          })),
        }),
        tool: null,
        ok: true,
        error: null,
        status: 200,
      };
    } else if (method === 'tools/call') {
      outcome = await toolCall(request, id, body.params);
    } else {
      outcome = {
        payload: failure(id, { code: -32601, message: 'method not found' }),
        tool: null,
        ok: false,
        error: 'method_not_found',
        status: 200,
      };
    }

    await audit({
      agentId: agent.id,
      method,
      tool: outcome.tool,
      ok: outcome.ok,
      error: outcome.error,
      status: outcome.status,
      durationMs: performance.now() - started,
      remote: request.ip,
    });
    return send(outcome.payload);
  });

  for (const method of ['GET', 'DELETE'] as const) {
    routes.route({
      method,
      url: '/mcp',
      handler: async (_request, reply) => {
        // No server-initiated stream, no session to terminate.
        return reply
          .code(405)
          .header('allow', 'POST')
          .send({ error: 'method not allowed' });
      },
    });
  }
}
