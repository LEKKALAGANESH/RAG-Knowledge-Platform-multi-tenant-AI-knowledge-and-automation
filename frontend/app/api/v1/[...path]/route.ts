import { cookies } from "next/headers";
import { randomBytes } from "node:crypto";
import { NextResponse, type NextRequest } from "next/server";
import {
  ACCESS_COOKIE,
  API_URL,
  REFRESH_COOKIE,
  clearSessionCookies,
  crossSiteResponse,
  refreshTokens,
  setSessionCookies,
  unavailableBody,
  upstream,
  type TokenPair,
} from "@/lib/server/session";

// Backend caps uploads at 50 MB (ingestion max_upload_mb); headroom covers multipart framing.
// The body is forwarded as raw bytes with its original Content-Type, so the multipart boundary survives.
const MAX_BODY_BYTES = 51 * 2 ** 20;

const BACKSLASH = String.fromCharCode(92);

const invalidPath = () =>
  NextResponse.json(
    { error: { code: "invalid_request", message: "This API path is not allowed.", request_id: crypto.randomUUID() } },
    { status: 400 },
  );

/** A segment that could walk out of /v1 once a hop decodes it again: dot segments, empty, slashes, bad escapes. */
function unsafeSegment(seg: string): boolean {
  let decoded: string;
  try {
    decoded = decodeURIComponent(seg);
  } catch {
    return true;
  }
  return [seg, decoded].some((s) => s === "" || s === "." || s === ".." || s.includes("/") || s.includes(BACKSLASH));
}

/** Backend URL for the path, or null when it would leave the API origin or the /v1/ prefix. */
function targetUrl(path: string[], search: string): URL | null {
  if (path.some(unsafeSegment)) return null;
  const base = new URL(API_URL);
  const url = new URL(`/v1/${path.map(encodeURIComponent).join("/")}${search}`, base);
  return url.origin === base.origin && url.pathname.startsWith("/v1/") ? url : null;
}

// S2 (FX-1): one random value per browser, in an httpOnly cookie only this BFF reads. Start/reconnect send it
// to the API, which binds the OAuth state to it; finish sends it back, so a sign-in link opened in another
// browser (or by another account) can't finish the flow. Page JS never sees it and can't set the header.
const OAUTH_COOKIE = "rag_oauth";
const OAUTH_START = /^(kbs\/[^/]+\/sources\/oauth\/start|sources\/[^/]+\/oauth\/reconnect)$/;
const OAUTH_FINISH = "connectors/oauth/finish";
const NONCE = /^[A-Za-z0-9_-]{43}$/;

function oauthBinding(method: string, route: string, cookie: string | undefined): { value?: string; start: boolean } {
  if (method !== "POST") return { start: false };
  if (OAUTH_START.test(route)) {
    // Reused while valid, so two tabs connecting at once don't void each other; each state stays single-use.
    const value = cookie && NONCE.test(cookie) ? cookie : randomBytes(32).toString("base64url");
    return { value, start: true };
  }
  return { value: route === OAUTH_FINISH ? cookie : undefined, start: false };
}

const tooLarge = () =>
  NextResponse.json(
    { error: { code: "file_too_large", message: "Files must be at most 50 MB.", request_id: crypto.randomUUID() } },
    { status: 413 },
  );

/**
 * BFF proxy: forwards /api/v1/* to the backend with the access token from the httpOnly cookie.
 * On a 401 it refreshes once with the refresh cookie and retries, so the browser never holds tokens.
 */
async function forward(req: NextRequest, ctx: RouteContext<"/api/v1/[...path]">) {
  if (!["GET", "HEAD", "OPTIONS"].includes(req.method)) {
    const blocked = crossSiteResponse(req); // defense in depth on top of SameSite=Lax cookies
    if (blocked) return blocked;
  }
  const { path } = await ctx.params;
  const url = targetUrl(path, req.nextUrl.search);
  if (!url) return invalidPath();
  const target = url.pathname + url.search;
  const jar = await cookies();
  if (Number(req.headers.get("content-length")) > MAX_BODY_BYTES) return tooLarge();
  const body = req.method === "GET" || req.method === "HEAD" ? undefined : await req.arrayBuffer();
  if (body && body.byteLength > MAX_BODY_BYTES) return tooLarge();
  const contentType = req.headers.get("content-type");
  const binding = oauthBinding(req.method, path.join("/"), jar.get(OAUTH_COOKIE)?.value);

  const send = (token?: string) =>
    upstream(target, {
      method: req.method,
      body,
      signal: req.signal, // the browser aborting (chat Stop) cancels the backend stream too
      headers: {
        ...(contentType ? { "Content-Type": contentType } : {}),
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...(binding.value ? { "X-OAuth-Binding": binding.value } : {}),
      },
    }, req);

  let refreshed: TokenPair | null = null;
  let res = await send(jar.get(ACCESS_COOKIE)?.value);
  const refreshToken = jar.get(REFRESH_COOKIE)?.value;
  if (res?.status === 401 && refreshToken) {
    const outcome = await refreshTokens(refreshToken, req);
    // Backend down is not a sign-out: keep the cookies and let the UI retry.
    if (outcome === "unavailable") return NextResponse.json(unavailableBody(), { status: 503 });
    if (outcome !== "signed_out") {
      refreshed = outcome;
      res = await send(refreshed.access_token);
    }
  }
  if (!res) return NextResponse.json(unavailableBody(), { status: 503 });

  const retryAfter = res.headers.get("retry-after");
  const disposition = res.headers.get("content-disposition"); // report downloads keep their file name
  const budgetWarning = res.headers.get("x-budget-warning");
  // The body is piped, not buffered, so text/event-stream frames reach the browser as they arrive.
  const out = new NextResponse(res.status === 204 ? null : res.body, {
    status: res.status,
    headers: {
      "Content-Type": res.headers.get("content-type") ?? "application/json",
      "Cache-Control": "no-cache, no-transform",
      ...(retryAfter ? { "Retry-After": retryAfter } : {}), // 429: the UI counts down instead of guessing
      ...(disposition ? { "Content-Disposition": disposition } : {}),
      ...(budgetWarning ? { "X-Budget-Warning": budgetWarning } : {}),
    },
  });
  if (binding.start && binding.value && res.ok)
    out.cookies.set(OAUTH_COOKIE, binding.value, {
      httpOnly: true,
      sameSite: "lax", // the provider's redirect is a top-level GET, and the finish POST is same-origin
      secure: process.env.NODE_ENV === "production",
      path: "/api/v1/connectors/oauth",
      maxAge: 600, // the API's state TTL
    });
  if (refreshed) setSessionCookies(out, refreshed);
  else if (res.status === 401) clearSessionCookies(out);
  return out;
}

export { forward as GET, forward as POST, forward as PATCH, forward as PUT, forward as DELETE };
