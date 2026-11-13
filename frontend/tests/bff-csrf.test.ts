import { NextRequest } from "next/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

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
