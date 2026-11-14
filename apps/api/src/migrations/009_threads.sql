-- Threads carry wake's ask/reply protocol: one question, one answer, with a
-- TTL. The messages table stays the single transcript; thread rows add the
-- ordering and read state that a conversation needs.
CREATE TABLE threads (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  kind text NOT NULL,
  status text NOT NULL DEFAULT 'open',
  created_by uuid NOT NULL REFERENCES agents (id),
  last_seq integer NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  CONSTRAINT threads_kind_valid CHECK (kind IN ('ask', 'thread')),
  CONSTRAINT threads_status_valid CHECK (status IN ('open', 'answered', 'closed')),
  CONSTRAINT threads_last_seq_nonnegative CHECK (last_seq >= 0)
);

CREATE TABLE thread_participants (
  thread_id uuid NOT NULL REFERENCES threads (id) ON DELETE CASCADE,
  agent_id uuid NOT NULL REFERENCES agents (id),
  last_read_seq integer NOT NULL DEFAULT 0,
  joined_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (thread_id, agent_id),
  CONSTRAINT thread_participants_last_read_nonnegative CHECK (last_read_seq >= 0)
);

CREATE INDEX thread_participants_agent_idx ON thread_participants (agent_id, thread_id);

ALTER TABLE messages
  ADD COLUMN thread_id uuid REFERENCES threads (id) ON DELETE CASCADE,
  ADD COLUMN thread_seq integer;

ALTER TABLE messages
  ADD CONSTRAINT messages_thread_seq_pair CHECK ((thread_id IS NULL) = (thread_seq IS NULL)),
  ADD CONSTRAINT messages_thread_seq_positive CHECK (thread_seq IS NULL OR thread_seq > 0);

CREATE UNIQUE INDEX messages_thread_seq_idx
  ON messages (thread_id, thread_seq)
  WHERE thread_id IS NOT NULL;