import { render, waitFor } from "@testing-library/react";
import { NextRequest } from "next/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { finishErrorPath, OAUTH_RETURN_ERRORS, safeFinishRedirect } from "@/lib/oauth";

// FX-1 / S2: the provider redirect lands on /oauth/finish; the BFF binds the flow to this browser.
const jar = new Map<string, string>();
let search = "";
const replace = vi.fn();
vi.mock("server-only", () => ({}));
vi.mock("next/headers", () => ({
  cookies: async () => ({ get: (name: string) => (jar.has(name) ? { name, value: jar.get(name) } : undefined) }),
}));
vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(search),
  useRouter: () => ({ push: vi.fn(), replace }),
}));

const { POST } = await import("@/app/api/v1/[...path]/route");
const { FinishOAuth } = await import("@/app/(app)/oauth/finish/finish");

const KB = "0b6c1d2e-3f40-4a5b-8c6d-7e8f90a1b2c3";
const SID = "11111111-1111-4111-8111-111111111111";
const NONCE = "n".repeat(43);
const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

function post(path: string[], headers: Record<string, string> = {}) {
  const req = new NextRequest(`http://localhost:3000/api/v1/${path.join("/")}`, {
    method: "POST",
    body: "{}",
    headers: { "content-type": "application/json", origin: "http://localhost:3000", "sec-fetch-site": "same-origin", ...headers },
  });
  return POST(req, { params: Promise.resolve({ path }) } as never);
}
const sentBinding = (fetchMock: ReturnType<typeof vi.fn>) =>
  new Headers((fetchMock.mock.calls[0] as unknown as [string, RequestInit])[1].headers).get("x-oauth-binding");

beforeEach(() => {
  jar.clear();
  replace.mockReset();
});

describe("BFF browser binding", () => {
  it.each([
    ["start", ["kbs", KB, "sources", "oauth", "start"]],
    ["reconnect", ["sources", SID, "oauth", "reconnect"]],
  ])("%s mints an httpOnly per-browser cookie and sends its value to the API", async (_l, path) => {
    const fetchMock = vi.fn(async () => json(200, { authorize_url: "https://accounts.google.com/x" }));
    vi.stubGlobal("fetch", fetchMock);
    const res = await post(path);
    const value = sentBinding(fetchMock);
    expect(value).toMatch(/^[A-Za-z0-9_-]{43}$/);
    const cookie = res.headers.getSetCookie().join("\n");
    expect(cookie).toContain(`rag_oauth=${value}`);
    expect(cookie).toMatch(/HttpOnly/i);
    expect(cookie).toMatch(/SameSite=lax/i);
    expect(cookie).toMatch(/Path=\/api\/v1\/connectors\/oauth/);
    expect(await res.json()).not.toHaveProperty("binding"); // never in a body page JS can read
  });

  it("reuses this browser's cookie (two tabs) and mints none when start fails", async () => {
    jar.set("rag_oauth", NONCE);
    const ok = vi.fn(async () => json(200, { authorize_url: "https://accounts.google.com/x" }));
    vi.stubGlobal("fetch", ok);
    await post(["kbs", KB, "sources", "oauth", "start"]);
    expect(sentBinding(ok)).toBe(NONCE);
    jar.clear();
    vi.stubGlobal("fetch", vi.fn(async () => json(422, { error: { code: "invalid_config", message: "x" } })));
    const res = await post(["kbs", KB, "sources", "oauth", "start"]);
    expect(res.headers.getSetCookie().join("\n")).not.toContain("rag_oauth");
  });

  it("finish forwards the cookie value; a header the page sets itself is never forwarded", async () => {
    jar.set("rag_oauth", NONCE);
    const fetchMock = vi.fn(async () => json(200, { redirect: "/x" }));
    vi.stubGlobal("fetch", fetchMock);
    await post(["connectors", "oauth", "finish"], { "x-oauth-binding": "attacker-chosen-".padEnd(43, "a") });
    expect(sentBinding(fetchMock)).toBe(NONCE);
    jar.clear();
    const noCookie = vi.fn(async () => json(403, { error: { code: "oauth_not_yours", message: "x" } }));
    vi.stubGlobal("fetch", noCookie);
    await post(["connectors", "oauth", "finish"], { "x-oauth-binding": "attacker-chosen-".padEnd(43, "a") });
    expect(sentBinding(noCookie)).toBeNull();
  });

  it("other routes never carry the binding", async () => {
    jar.set("rag_oauth", NONCE);
    const fetchMock = vi.fn(async () => json(200, {}));
    vi.stubGlobal("fetch", fetchMock);
    await post(["kbs", KB, "sources"]);
    expect(sentBinding(fetchMock)).toBeNull();
  });
});

describe("finish redirect guard", () => {
  it("accepts only the API's own return paths", () => {
    for (const ok of [`/kbs/${KB}?tab=sources&connected=${SID}`, `/kbs/${KB}?tab=sources&reconnected=${SID}`, `/kbs/${KB}?tab=sources&oauth_error=denied`])
      expect(safeFinishRedirect(ok)).toBe(ok);
    for (const bad of ["//evil.example.com", "https://evil.example.com/kbs", `/kbs/${KB}?tab=sources&next=//evil`, `/kbs/../admin?tab=sources&connected=1`, `/kbs/${KB}?tab=sources&connected=${SID}#x`])
      expect(safeFinishRedirect(bad)).toBeNull();
  });
});

describe("/oauth/finish page", () => {
  async function finish(query: string, response: Response) {
    window.history.replaceState(null, "", `/oauth/finish?${query}`);
    search = window.location.search;
    const fetchMock = vi.fn(async () => response);
    vi.stubGlobal("fetch", fetchMock);
    render(<FinishOAuth />);
    await waitFor(() => expect(replace).toHaveBeenCalled());
    return fetchMock;
  }

  it("posts code+state once as the signed-in user, drops them from the URL, then goes to the KB", async () => {
    const fetchMock = await finish("code=4%2F0abc&state=st1&scope=x", json(200, { redirect: `/kbs/${KB}?tab=sources&connected=${SID}` }));
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/v1/connectors/oauth/finish");
    expect(JSON.parse(String(init.body))).toEqual({ state: "st1", code: "4/0abc", error: "" });
    expect(window.location.search).toBe("");
    expect(replace).toHaveBeenCalledWith(`/kbs/${KB}?tab=sources&connected=${SID}`);
  });

  it("a flow from another account or browser ends on a readable refusal", async () => {
    await finish("code=c&state=s", json(403, { error: { code: "oauth_not_yours", message: "x", request_id: "r" } }));
    expect(replace).toHaveBeenCalledWith("/integrations?oauth_error=oauth_not_yours");
    expect(OAUTH_RETURN_ERRORS.oauth_not_yours).toMatch(/another account or browser/);
  });

  it("an unexpected redirect or error falls back to the generic text", async () => {
    await finish("code=c&state=s", json(200, { redirect: "https://evil.example.com" }));
    expect(replace).toHaveBeenCalledWith("/integrations?oauth_error=failed");
    expect(finishErrorPath(new Error("x"))).toBe("/integrations?oauth_error=failed");
  });
});
