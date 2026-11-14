export function pgField(
  error: unknown,
  field: 'code' | 'constraint',
): string | undefined {
  if (typeof error !== 'object' || error === null || !(field in error)) {
    return undefined;
  }
  const value = (error as Record<string, unknown>)[field];
  return typeof value === 'string' ? value : undefined;
}
