import "server-only";
import { isIP } from "node:net";
import { z } from "zod";
import { NextResponse, type NextRequest } from "next/server";
import { UPSTREAM_UNAVAILABLE } from "@/lib/api/errors";

export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
export const ACCESS_COOKIE = "rag_at";
export const REFRESH_COOKIE = "rag_rt";

/** Must match the backend's REFRESH_TOKEN_TTL_DAYS so the cookie never outlives (or undercuts) the token. */
const REFRESH_TTL_DAYS = z.coerce.number().int().min(1).max(90).parse(process.env.REFRESH_TOKEN_TTL_DAYS || 7);
const REFRESH_MAX_AGE = 60 * 60 * 24 * REFRESH_TTL_DAYS;

/** S11: how many reverse proxies in front of Next append to X-Forwarded-For (1 = one nginx/load balancer). */
const TRUSTED_PROXY_HOPS = z.coerce.number().int().min(1).max(10).parse(process.env.TRUSTED_PROXY_HOPS || 1);

export const tokenPair = z.object({
  access_token: z.string().min(1),
  refresh_token: z.string().min(1),
  expires_in: z.number().int().positive().optional(),
});
export type TokenPair = z.infer<typeof tokenPair>;

const base = {
  httpOnly: true,
  sameSite: "lax" as const,
  secure: process.env.NODE_ENV === "production",
  path: "/",
};

export function setSessionCookies(res: NextResponse, tokens: TokenPair): void {
  res.cookies.set(ACCESS_COOKIE, tokens.access_token, { ...base, maxAge: tokens.expires_in ?? 900 });
  res.cookies.set(REFRESH_COOKIE, tokens.refresh_token, { ...base, maxAge: REFRESH_MAX_AGE });
}

export function clearSessionCookies(res: NextResponse): void {
  res.cookies.delete(ACCESS_COOKIE);
  res.cookies.delete(REFRESH_COOKIE);
}

/**
 * The browser's IP, for the API's per-IP rate limit and audit hashes. Each trusted proxy appends the address it
 * accepted the connection from, and Next fills the header with its socket address only when it is missing, so
 * the client is the entry the outermost trusted hop wrote; everything left of it is client-supplied and ignored.
 * Without a proxy in front, a client-sent header survives Next unchanged: production must run behind one.
 */
export function clientIp(req: Request): string | null {
  const hops = (req.headers.get("x-forwarded-for") ?? "").split(",").map((h) => h.trim()).filter(Boolean);
  const ip = hops.at(-Math.min(TRUSTED_PROXY_HOPS, hops.length));
  return ip && isIP(ip) ? ip : null;
}

/**
 * fetch against the backend; a connection failure becomes null instead of throwing.
 * `from` = the browser request: its client IP goes upstream as X-Forwarded-For (the API trusts it only from
 * FORWARDED_ALLOW_IPS); a browser-sent X-Forwarded-For is never passed through as is.
 * The 15 s timeout covers only the wait for response headers, so a long SSE body is not cut off;
 * `init.signal` (the browser request's) still aborts the whole exchange, body included.
 */
export async function upstream(path: string, init: RequestInit, from?: Request): Promise<Response | null> {
  const timeout = new AbortController();
  const timer = setTimeout(() => timeout.abort(), 15_000);
  const signal = init.signal ? AbortSignal.any([init.signal, timeout.signal]) : timeout.signal;
  const headers = new Headers(init.headers);
  const ip = from && clientIp(from);
  if (ip) {
    headers.set("X-Forwarded-For", ip);
    headers.set("X-Real-IP", ip);
  }
  try {
    return await fetch(`${API_URL}${path}`, { ...init, headers, cache: "no-store", signal });
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

export function unavailableBody() {
  return {
    error: {
      code: UPSTREAM_UNAVAILABLE,
      message: "Can't reach the API server.",
      request_id: crypto.randomUUID(),
    },
  };
}

/**
 * Rotates the refresh token. "signed_out" = the backend rejected it (401 session_revoked / invalid_token,
 * or any 4xx): the caller clears the cookies. "unavailable" = backend unreachable or 5xx: keep them.
 */
export async function refreshTokens(
  refreshToken: string,
  from?: Request,
): Promise<TokenPair | "signed_out" | "unavailable"> {
  const res = await upstream(
    "/v1/auth/refresh",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: refreshToken }),
    },
    from,
  );
  if (!res || res.status >= 500) return "unavailable";
  if (!res.ok) return "signed_out";
  const parsed = tokenPair.safeParse(await res.json().catch(() => null));
  return parsed.success ? parsed.data : "signed_out";
}

/**
 * CSRF guard for cookie-setting and state-changing BFF routes. Login CSRF needs no existing cookie (a
 * cross-site text/plain form POST would sign the victim into the attacker's account), and SameSite=Lax
 * doesn't stop same-site sibling hosts, so the browser's provenance headers decide.
 */
export function crossSiteResponse(req: NextRequest): NextResponse | null {
  const site = req.headers.get("sec-fetch-site");
  const origin = req.headers.get("origin");
  if ((site !== null && site !== "same-origin") || (origin !== null && origin !== req.nextUrl.origin))
    return NextResponse.json(
      { error: { code: "forbidden", message: "Cross-site request blocked.", request_id: crypto.randomUUID() } },
      { status: 403 },
    );
  return null;
}
