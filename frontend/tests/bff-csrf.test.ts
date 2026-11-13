import { NextRequest } from "next/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("server-only", () => ({}));
vi.mock("next/headers", () => ({ cookies: async () => ({ get: () => undefined }) }));

const session = await import("@/app/api/session/[action]/route");
const proxy = await import("@/app/api/v1/[...path]/route");

const ORIGIN = "http://localhost:3000";
const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const creds = JSON.stringify({ email: "a@example.com", password: "pw" });
const login = (headers: Record<string, string>, body = creds) =>
  session.POST(new NextRequest(`${ORIGIN}/api/session/login`, { method: "POST", headers, body }), {
    params: Promise.resolve({ action: "login" }),
  } as never);
const proxied = (method: string, headers: Record<string, string>) =>
  proxy[method as "POST"](new NextRequest(`${ORIGIN}/api/v1/kbs`, { method, headers, body: method === "GET" ? undefined : "{}" }), {
    params: Promise.resolve({ path: ["kbs"] }),
  } as never);

let fetchMock: ReturnType<typeof vi.fn>;
beforeEach(() => {
  fetchMock = vi.fn(() => Promise.resolve(json(200, { access_token: "at", refresh_token: "rt", items: [] })));
  vi.stubGlobal("fetch", fetchMock);
});

describe("session route CSRF guard", () => {
  it.each([
    ["cross-site Origin", { "content-type": "application/json", origin: "https://evil.example.com" }],
    ["sec-fetch-site: cross-site", { "content-type": "application/json", "sec-fetch-site": "cross-site" }],
    ["sec-fetch-site: same-site", { "content-type": "application/json", "sec-fetch-site": "same-site" }],
  ])("blocks %s with 403 and sets no cookie", async (_l, headers) => {
    const res = await login(headers);
    expect(res.status).toBe(403);
    expect(await res.json()).toMatchObject({ error: { code: "forbidden", message: "Cross-site request blocked." } });
    expect(res.headers.get("set-cookie")).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("rejects a text/plain body (what a cross-site form can send) even without provenance headers", async () => {
    const res = await login({ "content-type": "text/plain" });
    expect(res.status).toBe(415);
    expect(res.headers.get("set-cookie")).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("a same-origin JSON login still signs in", async () => {
    const res = await login({ "content-type": "application/json", origin: ORIGIN, "sec-fetch-site": "same-origin" });
    expect(res.status).toBe(204);
    expect(res.headers.get("set-cookie")).toContain("rag_at=at");
  });
});

describe("expected origin behind a container or proxy (server URL is http://0.0.0.0:3000)", () => {
  afterEach(() => vi.unstubAllEnvs());
  async function containerLogin(headers: Record<string, string>, env: Record<string, string> = {}) {
    vi.resetModules();
    for (const [k, v] of Object.entries(env)) vi.stubEnv(k, v);
    const route = await import("@/app/api/session/[action]/route");
    const req = new NextRequest("http://0.0.0.0:3000/api/session/login", {
      method: "POST",
      headers: { "content-type": "application/json", "sec-fetch-site": "same-origin", ...headers },
      body: creds,
    });
    return route.POST(req, { params: Promise.resolve({ action: "login" }) } as never);
  }
  const host = { host: "localhost:13062" };

  it("allows an Origin matching the Host the browser sent", async () => {
    expect((await containerLogin({ ...host, origin: "http://localhost:13062" })).status).toBe(204);
  });

  it("uses the scheme the trusted proxy reports", async () => {
    const res = await containerLogin({ host: "app.example.com", "x-forwarded-proto": "https", origin: "https://app.example.com" });
    expect(res.status).toBe(204);
  });

  it("allows PUBLIC_ORIGIN regardless of Host", async () => {
    const env = { PUBLIC_ORIGIN: "https://app.example.com" };
    expect((await containerLogin({ ...host, origin: "https://app.example.com" }, env)).status).toBe(204);
    expect((await containerLogin({ host: "app.example.com", origin: "http://app.example.com" }, env)).status).toBe(403);
  });

  it("blocks a foreign Origin", async () => {
    expect((await containerLogin({ ...host, origin: "https://evil.example" })).status).toBe(403);
    const env = { PUBLIC_ORIGIN: "https://app.example.com" };
    expect((await containerLogin({ ...host, origin: "https://evil.example" }, env)).status).toBe(403);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("ignores a spoofed X-Forwarded-Host", async () => {
    const res = await containerLogin({ ...host, "x-forwarded-host": "evil.example", origin: "http://evil.example" });
    expect(res.status).toBe(403);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("refuses a PUBLIC_ORIGIN with a path", async () => {
    await expect(containerLogin(host, { PUBLIC_ORIGIN: "https://app.example.com/app" })).rejects.toThrow(/PUBLIC_ORIGIN/);
  });
});

describe("BFF proxy CSRF guard", () => {
  it.each(["POST", "PUT", "PATCH", "DELETE"])("blocks a cross-site %s", async (method) => {
    for (const headers of [{ origin: "https://evil.example.com" }, { "sec-fetch-site": "cross-site" }] as Record<string, string>[]) {
      const res = await proxied(method, { "content-type": "application/json", ...headers });
      expect(res.status).toBe(403);
    }
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("forwards same-origin writes and leaves GET alone", async () => {
    expect((await proxied("POST", { "content-type": "application/json", origin: ORIGIN, "sec-fetch-site": "same-origin" })).status).toBe(200);
    expect((await proxied("GET", { "sec-fetch-site": "cross-site" })).status).toBe(200);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});
