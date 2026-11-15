-- The HTTP response to a send carries the doorbell outcomes, but a sender that
-- lost that response had no way to ask what happened. One row per recipient
-- that was rung, written as each delivery finishes (so a crash keeps the
-- outcomes already known), upserted because a recipient can only be rung once
-- per message.
CREATE TABLE wake_deliveries (
  message_id uuid NOT NULL REFERENCES messages (id) ON DELETE CASCADE,
  agent_id uuid NOT NULL REFERENCES agents (id) ON DELETE CASCADE,
  delivered boolean NOT NULL,
  reason text,
  attempts integer NOT NULL,
  status integer,
  updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (message_id, agent_id),
  CONSTRAINT wake_deliveries_attempts_nonnegative CHECK (attempts >= 0)
);

CREATE INDEX wake_deliveries_agent_idx
  ON wake_deliveries (agent_id, updated_at DESC);