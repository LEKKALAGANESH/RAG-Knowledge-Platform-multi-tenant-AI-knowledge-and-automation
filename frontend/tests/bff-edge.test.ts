import { NextRequest } from "next/server";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("server-only", () => ({}));
vi.mock("next/headers", () => ({ cookies: async () => ({ get: () => undefined }) }));

const ORIGIN = "http://localhost:3000";
const ok = () =>
  new Response(JSON.stringify({ access_token: "at", refresh_token: "rt" }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });

/** Upstream headers the BFF sent for one login (or proxied GET) carrying this X-Forwarded-For. */
async function forwarded(xff: string | null, via: "login" | "proxy" = "login", hops?: string) {
  vi.resetModules();
  if (hops) vi.stubEnv("TRUSTED_PROXY_HOPS", hops);
  const fetchMock = vi.fn(async () => ok());
  vi.stubGlobal("fetch", fetchMock);
  const headers: Record<string, string> = { "Content-Type": "application/json", "Sec-Fetch-Site": "same-origin" };
  if (xff !== null) headers["X-Forwarded-For"] = xff;
  if (via === "login") {
    const { POST } = await import("@/app/api/session/[action]/route");
    const body = JSON.stringify({ email: "a@example.com", password: "pw" });
    await POST(new NextRequest(`${ORIGIN}/api/session/login`, { method: "POST", headers, body }), {
      params: Promise.resolve({ action: "login" }),
    } as never);
  } else {
    const { GET } = await import("@/app/api/v1/[...path]/route");
    await GET(new NextRequest(`${ORIGIN}/api/v1/kbs`, { headers }), { params: Promise.resolve({ path: ["kbs"] }) } as never);
  }
  const init = (fetchMock.mock.calls[0] as unknown as [string, RequestInit])[1];
  const sent = new Headers(init.headers);
  return { xff: sent.get("x-forwarded-for"), realIp: sent.get("x-real-ip") };
}

afterEach(() => vi.unstubAllEnvs());

describe("S11: BFF forwards the real client IP", () => {
  it("gives two browsers behind the proxy their own address upstream", async () => {
    expect(await forwarded("203.0.113.7")).toEqual({ xff: "203.0.113.7", realIp: "203.0.113.7" });
    expect(await forwarded("198.51.100.9")).toEqual({ xff: "198.51.100.9", realIp: "198.51.100.9" });
  });

  it("drops client-supplied entries left of the trusted hop instead of passing them through", async () => {
    expect((await forwarded("6.6.6.6, 203.0.113.7")).xff).toBe("203.0.113.7");
    expect((await forwarded("6.6.6.6, 203.0.113.7, 10.0.0.2", "login", "2")).xff).toBe("203.0.113.7");
  });

  it("forwards on the API proxy too", async () => {
    expect((await forwarded("6.6.6.6, 2001:db8::1", "proxy")).xff).toBe("2001:db8::1");
  });

  it.each([null, "", "not-an-ip", "203.0.113.7, <script>"])("sends no XFF for %j", async (xff) => {
    expect(await forwarded(xff)).toEqual({ xff: null, realIp: null });
  });
});

describe("S12: security headers", () => {
  it("applies CSP, frame and sniffing protection to every path", async () => {
    vi.resetModules();
    const config = (await import("@/next.config")).default;
    const rules = await config.headers!();
    expect(rules).toHaveLength(1);
    expect(rules[0].source).toBe("/:path*");
    const h = Object.fromEntries(rules[0].headers.map(({ key, value }) => [key, value]));
    expect(h["X-Frame-Options"]).toBe("DENY");
    expect(h["X-Content-Type-Options"]).toBe("nosniff");
    expect(h["Referrer-Policy"]).toBe("strict-origin-when-cross-origin");
    for (const directive of ["default-src 'self'", "frame-ancestors 'none'", "object-src 'none'", "connect-src 'self'", "base-uri 'self'", "form-action 'self'"])
      expect(h["Content-Security-Policy"]).toContain(directive);
    expect(h["Content-Security-Policy"]).not.toContain("unsafe-eval");
    expect(h["Strict-Transport-Security"]).toBeUndefined(); // test env is not production
  });

  it("adds HSTS only in production", async () => {
    vi.resetModules();
    vi.stubEnv("NODE_ENV", "production");
    const { securityHeaders } = await import("@/next.config");
    expect(securityHeaders.find((x) => x.key === "Strict-Transport-Security")?.value).toContain("max-age=");
  });
});
