import { createHash, randomBytes, timingSafeEqual } from 'node:crypto';

const HASH_BYTES = 32;

export function generateToken(): string {
  return randomBytes(HASH_BYTES).toString('base64url');
}

// A thread's reply token is compared by its sha256, never by stored text: the
// API keeps only the digest, so a database dump cannot answer the thread.
export function hashMatches(stored: Buffer | null, token: string): boolean {
  if (stored === null || stored.length !== HASH_BYTES) {
    return false;
  }
  return timingSafeEqual(stored, hashToken(token));
}

export function hashToken(token: string): Buffer {
  return createHash('sha256').update(token).digest();
}

export function secretsEqual(expected: string, provided: string): boolean {
  const left = createHash('sha256').update(expected).digest();
  const right = createHash('sha256').update(provided).digest();
  return timingSafeEqual(left, right);
}
