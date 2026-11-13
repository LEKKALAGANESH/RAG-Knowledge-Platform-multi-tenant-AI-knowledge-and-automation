import { NextRequest } from "next/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

const jar = new Map<string, string>();
vi.mock("server-only", () => ({}));
vi.mock("next/headers", () => ({
  cookies: async () => ({ get: (name: string) => (jar.has(name) ? { name, value: jar.get(name) } : undefined) }),
}));

const { GET } = await import("@/app/api/v1/[...path]/route");

const BACKSLASH = String.fromCharCode(92);
const call = (path: string[], url = "http://localhost:3000/api/v1/x") =>
  GET(new NextRequest(url), { params: Promise.resolve({ path }) } as never);

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

beforeEach(() => jar.clear());

describe("BFF proxy path guard", () => {
  it.each([
    ["..", ["kbs", ".."]],
    ["%2e%2e (decoded by Next)", ["..", "admin"]],
    ["double-encoded %2e%2e", ["%2e%2e", "admin"]],
    [".", [".", "kbs"]],
    ["empty", ["kbs", ""]],
    ["%2f (decoded by Next)", ["kbs/../../x"]],
    ["double-encoded %2f", ["kbs%2f..%2fx"]],
    ["backslash", [`kbs${BACKSLASH}..`]],
    ["malformed escape", ["%E0%A4%A"]],
  ])("rejects %s with 400 and never calls the backend", async (_label, path) => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const res = await call(path);
    expect(res.status).toBe(400);
    expect(await res.json()).toMatchObject({ error: { code: "invalid_request" } });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("forwards a normal path with its query string to /v1 on the API host", async () => {
    const fetchMock = vi.fn(async () => json(200, { ok: true }));
    vi.stubGlobal("fetch", fetchMock);
    const res = await call(["kbs", "abc"], "http://localhost:3000/api/v1/kbs/abc?limit=5");
    expect(res.status).toBe(200);
    const target = new URL(String((fetchMock.mock.calls[0] as unknown[])[0]));
    expect(target.pathname).toBe("/v1/kbs/abc");
    expect(target.search).toBe("?limit=5");
  });

  it("forwards X-Budget-Warning and Content-Disposition", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("a,b\n", {
      status: 200,
      headers: { "Content-Type": "text/csv", "Content-Disposition": 'attachment; filename="audit-log.csv"', "X-Budget-Warning": "over_budget" },
    })));
    const res = await call(["audit", "export"]);
    expect(res.headers.get("content-disposition")).toBe('attachment; filename="audit-log.csv"');
    expect(res.headers.get("x-budget-warning")).toBe("over_budget");
  });
});

describe("BFF refresh", () => {
  it.each(["session_revoked", "invalid_token"])("401 %s on refresh = signed out: 401 out, cookies cleared", async (code) => {
    jar.set("rag_at", "old-access");
    jar.set("rag_rt", "old-refresh");
    vi.stubGlobal("fetch", vi.fn(async (url: string) =>
      url.endsWith("/v1/auth/refresh") ? json(401, { error: { code, message: "Sign in again." } }) : json(401, { error: { code: "invalid_token", message: "x" } }),
    ));
    const res = await call(["me"]);
    expect(res.status).toBe(401);
    const cleared = res.headers.getSetCookie().join("\n");
    expect(cleared).toMatch(/rag_at=;/);
    expect(cleared).toMatch(/rag_rt=;/);
  });

  it("refresh endpoint unreachable = 503, cookies kept (not a sign-out)", async () => {
    jar.set("rag_at", "old-access");
    jar.set("rag_rt", "old-refresh");
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      if (url.endsWith("/v1/auth/refresh")) throw new TypeError("fetch failed");
      return json(401, { error: { code: "invalid_token", message: "x" } });
    }));
    const res = await call(["me"]);
    expect(res.status).toBe(503);
    expect(res.headers.getSetCookie()).toEqual([]);
  });

  it("refresh succeeds: retried with the new token and cookies rotated", async () => {
    jar.set("rag_rt", "old-refresh");
    const fetchMock = vi.fn(async (url: string, init: RequestInit) => {
      if (url.endsWith("/v1/auth/refresh")) return json(200, { access_token: "new-a", refresh_token: "new-r", expires_in: 900 });
      const auth = new Headers(init.headers).get("authorization");
      return auth === "Bearer new-a" ? json(200, { ok: 1 }) : json(401, { error: { code: "invalid_token", message: "x" } });
    });
    vi.stubGlobal("fetch", fetchMock);
    const res = await call(["me"]);
    expect(res.status).toBe(200);
    expect(res.headers.getSetCookie().join("\n")).toMatch(/rag_rt=new-r/);
  });
});

describe("sign out", () => {
  it("revokes the refresh token on the backend, then clears cookies", async () => {
    const { POST } = await import("@/app/api/session/[action]/route");
    const fetchMock = vi.fn(async () => new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);
    const req = new NextRequest("http://localhost:3000/api/session/logout", { method: "POST", headers: { cookie: "rag_rt=rt-1; rag_at=at-1", "content-type": "application/json" } });
    const res = await POST(req, { params: Promise.resolve({ action: "logout" }) } as never);
    expect(res.status).toBe(204);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toMatch(/\/v1\/auth\/logout$/);
    expect(JSON.parse(String(init.body))).toEqual({ refresh_token: "rt-1" });
    expect(res.headers.getSetCookie().join("\n")).toMatch(/rag_rt=;/);
  });

  it("still clears cookies when the backend is down", async () => {
    const { POST } = await import("@/app/api/session/[action]/route");
    vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("down"); }));
    const req = new NextRequest("http://localhost:3000/api/session/logout", { method: "POST", headers: { cookie: "rag_rt=rt-1", "content-type": "application/json" } });
    const res = await POST(req, { params: Promise.resolve({ action: "logout" }) } as never);
    expect(res.status).toBe(204);
    expect(res.headers.getSetCookie().join("\n")).toMatch(/rag_rt=;/);
  });
});
