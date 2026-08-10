/**
 * The engine reports why it refused in FastAPI's `detail` field. Every surface
 * that can fail should show that sentence rather than a generic apology — the
 * messages name the missing environment variable or the deleted source, which
 * is the whole of the fix.
 */
export function describeError(error: unknown, fallback = "The engine is unreachable."): string {
  if (typeof error !== "object" || error === null) return fallback;
  const { data, error: message } = error as { data?: unknown; error?: unknown };
  if (typeof data === "object" && data !== null) {
    const { detail } = data as { detail?: unknown };
    if (typeof detail === "string" && detail) return detail;
  }
  if (typeof data === "string" && data) return data;
  if (typeof message === "string" && message) return message;
  return fallback;
}
