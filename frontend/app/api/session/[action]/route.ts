import { NextResponse, type NextRequest } from "next/server";
import { z } from "zod";
import {
  REFRESH_COOKIE,
  clearSessionCookies,
  crossSiteResponse,
  setSessionCookies,
  tokenPair,
  unavailableBody,
  upstream,
} from "@/lib/server/session";

const credentials = z.object({
  email: z.string().trim().min(3).max(320),
  password: z.string().min(1).max(256),
});
const switchOrg = z.object({ org_id: z.uuid() });

function badRequest(message: string) {
  return NextResponse.json(
    { error: { code: "invalid_request", message, request_id: crypto.randomUUID() } },
    { status: 400 },
  );
}

export async function POST(req: NextRequest, ctx: RouteContext<"/api/session/[action]">) {
  const blocked = crossSiteResponse(req);
  if (blocked) return blocked;
  // A cross-site form can only send text/plain, urlencoded or multipart; JSON needs a same-origin fetch.
  if (!req.headers.get("content-type")?.toLowerCase().startsWith("application/json"))
    return NextResponse.json(
      { error: { code: "unsupported_media_type", message: "Send JSON.", request_id: crypto.randomUUID() } },
      { status: 415 },
    );
  const { action } = await ctx.params;

  if (action === "logout") {
    const refreshToken = req.cookies.get(REFRESH_COOKIE)?.value;
    // Ends this login on the server too; cookies are cleared even if that call fails (already revoked, backend down).
    if (refreshToken) {
      await upstream("/v1/auth/logout", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ refresh_token: refreshToken }),
      }, req);
    }
    const res = new NextResponse(null, { status: 204 });
    clearSessionCookies(res);
    return res;
  }
  if (action === "switch-org") return switchOrgAction(req);
  if (action !== "login" && action !== "register") return badRequest("Unknown session action.");

  const body = credentials.safeParse(await req.json().catch(() => null));
  if (!body.success) return badRequest("Enter a valid email and password.");

  const up = await upstream(`/v1/auth/${action}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body.data),
  }, req);
  if (!up) return NextResponse.json(unavailableBody(), { status: 503 });

  const payload: unknown = await up.json().catch(() => null);
  if (!up.ok) {
    const retryAfter = up.headers.get("retry-after"); // 429 on the auth rate limit: the form counts it down
    return NextResponse.json(payload, { status: up.status, headers: retryAfter ? { "Retry-After": retryAfter } : {} });
  }

  const tokens = tokenPair.safeParse(payload);
  if (!tokens.success) {
    return NextResponse.json(
      { error: { code: "invalid_response", message: "Sign-in failed. Try again.", request_id: crypto.randomUUID() } },
      { status: 502 },
    );
  }
  const res = new NextResponse(null, { status: 204 });
  setSessionCookies(res, tokens.data);
  return res;
}

/** Re-issues the session for another org the user belongs to; any API refusal leaves the current cookies as they are. */
async function switchOrgAction(req: NextRequest) {
  const body = switchOrg.safeParse(await req.json().catch(() => null));
  if (!body.success) return badRequest("Pick a valid organization.");
  const refreshToken = req.cookies.get(REFRESH_COOKIE)?.value;
  if (!refreshToken)
    return NextResponse.json(
      { error: { code: "unauthorized", message: "Sign in again.", request_id: crypto.randomUUID() } },
      { status: 401 },
    );
  const up = await upstream("/v1/auth/refresh", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ refresh_token: refreshToken, org_id: body.data.org_id }),
  }, req);
  if (!up) return NextResponse.json(unavailableBody(), { status: 503 });
  const payload: unknown = await up.json().catch(() => null);
  if (!up.ok) return NextResponse.json(payload, { status: up.status });
  const tokens = tokenPair.safeParse(payload);
  if (!tokens.success)
    return NextResponse.json(
      { error: { code: "invalid_response", message: "Couldn't switch. Try again.", request_id: crypto.randomUUID() } },
      { status: 502 },
    );
  const res = new NextResponse(null, { status: 204 });
  setSessionCookies(res, tokens.data);
  return res;
}
