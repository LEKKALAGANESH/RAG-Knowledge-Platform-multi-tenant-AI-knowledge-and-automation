import { z } from "zod";
import { BUDGET_EXCEEDED, reportBudget } from "@/lib/budget";

const errorBody = z.object({
  error: z.object({
    code: z.string(),
    message: z.string(),
    request_id: z.string().optional(),
  }),
});

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly requestId?: string,
    /** Epoch ms after which a 429 may be retried (from `Retry-After`); undefined when the server gave none. */
    readonly retryAt?: number,
  ) {
    super(message);
    this.name = "ApiError";
  }

  /** Network failures, timeouts, rate limits and server errors are worth retrying; client errors are not. */
  get retryable(): boolean {
    return this.status === 0 || this.status === 408 || this.status === 429 || this.status >= 500;
  }
}

/** 503 from login/register when Redis (the auth rate limiter) is down: the backend fails closed. */
export const RATE_LIMITER_UNAVAILABLE = "rate_limiter_unavailable";

/**
 * Epoch ms from the monotonic-backed performance clock. Next refuses Date.now() while prerendering, and the
 * countdown must read the same clock that stamped retryAt (the two drift apart by a second or more).
 */
export const wallClock = () => performance.timeOrigin + performance.now();

/** `Retry-After` is delta-seconds or an HTTP date; returns epoch ms, or undefined when absent/unparseable. */
export function parseRetryAfter(value: string | null, now = wallClock()): number | undefined {
  if (!value) return undefined;
  const secs = Number(value);
  if (Number.isFinite(secs) && secs >= 0) return now + secs * 1000;
  const at = Date.parse(value);
  return Number.isNaN(at) ? undefined : at;
}

/** Whole seconds left until `retryAt`, never negative. */
export const secondsLeft = (retryAt: number | undefined, now: number) =>
  retryAt === undefined ? 0 : Math.max(0, Math.ceil((retryAt - now) / 1000));

export const rateLimitMessage = (seconds: number) =>
  seconds > 0 ? `Too many requests — try again in ${seconds} s.` : "You can try again now.";

/** Code the BFF proxy returns when the backend can't be reached. */
export const UPSTREAM_UNAVAILABLE = "upstream_unavailable";

const GENERIC: Record<number, string> = {
  0: "Can't reach the server. Check your connection and try again.",
  401: "Your session has expired. Sign in again.",
  403: "You don't have access to this.",
  404: "This item doesn't exist or was deleted.",
  408: "The server took too long to respond. Try again.",
  422: "Some fields are invalid. Check them and try again.",
  429: "Too many requests. Wait a moment and try again.",
};

function fallbackMessage(status: number): string {
  if (status >= 500) return "Something went wrong on our side. Try again in a moment.";
  return GENERIC[status] ?? "The request failed. Try again.";
}

/**
 * Builds an ApiError from a failed response. Backend messages are shown for 4xx (they describe
 * what the user did, e.g. wrong password); 5xx use our own wording so internals never reach the screen.
 */
export async function toApiError(res: Response): Promise<ApiError> {
  let parsed: z.infer<typeof errorBody> | null = null;
  try {
    const result = errorBody.safeParse(await res.json());
    parsed = result.success ? result.data : null;
  } catch {
    // Non-JSON body (proxy page, HTML error): fall through to the status-based message.
  }
  const code = parsed?.error.code ?? `http_${res.status}`;
  const message =
    code === UPSTREAM_UNAVAILABLE
      ? fallbackMessage(0)
      : code === RATE_LIMITER_UNAVAILABLE
        ? "Sign-in is paused because a safety service is down. Try again in a minute."
        : res.status === 429
          ? fallbackMessage(429) // the server's text bakes in a fixed number of seconds; callers show a live countdown
          : parsed && res.status < 500
        ? parsed.error.message
        : fallbackMessage(res.status);
  const retryAt = res.status === 429 ? parseRetryAfter(res.headers.get("Retry-After")) : undefined;
  if (code === BUDGET_EXCEEDED) reportBudget({ kind: "exceeded", message }); // the app-wide banner links to Usage
  return new ApiError(res.status, code, message, parsed?.error.request_id, retryAt);
}

export function networkError(): ApiError {
  return new ApiError(0, "network_error", fallbackMessage(0));
}
