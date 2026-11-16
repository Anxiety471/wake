-- The bot a doorbell wakes holds no agent token, so an API-born thread had no
-- answer path that worked for it (#93). The API mints one reply token when it
-- opens a thread, keeps only the sha256 here, and ships the plaintext once in
-- the doorbell payload. The token answers that one thread, once, and expires
-- with the thread's TTL.
ALTER TABLE threads
  ADD COLUMN reply_token_hash bytea,
  ADD COLUMN reply_token_expires_at timestamptz;
