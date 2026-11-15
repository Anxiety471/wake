-- The public MCP surface has exactly one identity, and some bots must never be
-- reachable from it. `kind` marks that identity; `is_private` hides a bot from
-- every non-bot caller without changing bot-to-bot traffic.
ALTER TABLE agents ADD COLUMN kind text NOT NULL DEFAULT 'bot';
ALTER TABLE agents
  ADD CONSTRAINT agents_kind_valid CHECK (kind IN ('bot', 'mcp'));
ALTER TABLE agents ADD COLUMN is_private boolean NOT NULL DEFAULT false;

-- One row per MCP request. The bearer token is the only gate on the public
-- surface, so the audit trail is how an operator answers "who asked for what".
CREATE TABLE mcp_audit (
  id bigserial PRIMARY KEY,
  agent_id uuid NOT NULL REFERENCES agents (id) ON DELETE CASCADE,
  method text NOT NULL,
  tool text,
  ok boolean NOT NULL,
  error text,
  status integer NOT NULL,
  duration_ms integer NOT NULL,
  remote text,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX mcp_audit_created_idx ON mcp_audit (created_at DESC);
CREATE INDEX mcp_audit_agent_idx ON mcp_audit (agent_id, created_at DESC);