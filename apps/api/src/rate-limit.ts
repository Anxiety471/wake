// A sliding-window limiter: one small array of timestamps per key, pruned on
// every check. In-process state, so it resets on restart and is per instance;
// both callers put a bound on how fast one bot can be woken, not on money.
export type RateLimit = {
  allow: (key: string, now?: number) => boolean;
  reset: () => void;
};

export function slidingWindow(options: {
  limit: number;
  windowMs: number;
  now?: () => number;
}): RateLimit {
  const hits = new Map<string, number[]>();
  const clock = options.now ?? (() => Date.now());
  return {
    allow(key: string, now: number = clock()): boolean {
      if (options.limit <= 0) {
        return true;
      }
      const cutoff = now - options.windowMs;
      const recent = (hits.get(key) ?? []).filter((at) => at > cutoff);
      if (recent.length >= options.limit) {
        hits.set(key, recent);
        return false;
      }
      recent.push(now);
      hits.set(key, recent);
      return true;
    },
    reset(): void {
      hits.clear();
    },
  };
}
