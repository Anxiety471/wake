import type { Pool } from 'pg';

export const NAME_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_-]*$/;

export type RecipientLookup =
  | { ok: true; id: string | null }
  | { ok: false; status: 400 | 404; error: string };

// A message may go to one agent by name or to every agent with '*'. Threads
// are two-party, so callers that cannot use a broadcast pass allowBroadcast
// false and get the same 400 an invalid name would produce.
export async function resolveRecipient(
  pool: Pool,
  senderId: string,
  to: string,
  options?: { allowBroadcast?: boolean },
): Promise<RecipientLookup> {
  if (to === '*') {
    if (options?.allowBroadcast === false) {
      return { ok: false, status: 400, error: 'invalid request' };
    }
    return { ok: true, id: null };
  }
  if (!NAME_PATTERN.test(to)) {
    return { ok: false, status: 400, error: 'invalid request' };
  }
  const result = await pool.query<{ id: string }>(
    'SELECT id FROM agents WHERE lower(name) = lower($1)',
    [to],
  );
  const row = result.rows[0];
  if (row === undefined) {
    return { ok: false, status: 404, error: 'recipient not found' };
  }
  if (row.id === senderId) {
    return { ok: false, status: 400, error: 'invalid request' };
  }
  return { ok: true, id: row.id };
}
