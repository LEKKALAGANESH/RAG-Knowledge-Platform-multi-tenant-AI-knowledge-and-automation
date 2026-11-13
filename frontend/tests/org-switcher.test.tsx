import { NextRequest } from "next/server";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { InvitesBanner } from "@/components/invites-banner";
import { OrgSwitcher, pathAfterSwitch } from "@/components/org-switcher";
import { browserNav } from "@/lib/api/client";

vi.mock("server-only", () => ({}));

const route = await import("@/app/api/session/[action]/route");

const ORIGIN = "http://localhost:3000";
const ORG_A = "6f1c2c6e-8a0b-4c39-9a52-0d3b5d1f2a11";
const ORG_B = "0b7e4a52-3f1d-4e8a-b6c2-9d8e7f6a5b44";
const json = (status: number, body: unknown) =>
  new Response(status === 204 ? null : JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const refused = (status: number) =>
  json(status, { error: { code: "forbidden", message: "You are not a member of that organization.", request_id: "r" } });

const switchOrg = (headers: Record<string, string>, body: string, cookie = "rag_rt=old-rt; rag_at=old-at") =>
  route.POST(
    new NextRequest(`${ORIGIN}/api/session/switch-org`, { method: "POST", headers: { cookie, ...headers }, body }),
    { params: Promise.resolve({ action: "switch-org" }) } as never,
  );
const JSON_SAME = { "content-type": "application/json", origin: ORIGIN, "sec-fetch-site": "same-origin" };

describe("BFF switch-org", () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  beforeEach(() => {
    fetchMock = vi.fn(async () => json(200, { access_token: "new-at", refresh_token: "new-rt", expires_in: 900 }));
    vi.stubGlobal("fetch", fetchMock);
  });

  it.each([
    ["cross-site Origin", { "content-type": "application/json", origin: "https://evil.example.com" }, 403],
    ["sec-fetch-site: cross-site", { "content-type": "application/json", "sec-fetch-site": "cross-site" }, 403],
    ["a text/plain body", { "content-type": "text/plain" }, 415],
  ])("blocks %s and calls nothing", async (_l, headers, status) => {
    const res = await switchOrg(headers, JSON.stringify({ org_id: ORG_A }));
    expect(res.status).toBe(status);
    expect(res.headers.get("set-cookie")).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it.each([["not-a-uuid"], [""], ["../../me"]])("rejects org_id %j with 400 before calling the API", async (id) => {
    const res = await switchOrg(JSON_SAME, JSON.stringify({ org_id: id }));
    expect(res.status).toBe(400);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("without a refresh cookie answers 401", async () => {
    const res = await switchOrg(JSON_SAME, JSON.stringify({ org_id: ORG_A }), "");
    expect(res.status).toBe(401);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("refreshes with the cookie's token + org_id and rotates both cookies", async () => {
    const res = await switchOrg(JSON_SAME, JSON.stringify({ org_id: ORG_A }));
    expect(res.status).toBe(204);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url.endsWith("/v1/auth/refresh")).toBe(true);
    expect(JSON.parse(String(init.body))).toEqual({ refresh_token: "old-rt", org_id: ORG_A });
    expect(res.cookies.get("rag_at")?.value).toBe("new-at");
    expect(res.cookies.get("rag_rt")?.value).toBe("new-rt");
  });

  it.each([403, 404])("an API %i passes its message through and leaves cookies alone", async (status) => {
    fetchMock.mockResolvedValueOnce(refused(status));
    const res = await switchOrg(JSON_SAME, JSON.stringify({ org_id: ORG_A }));
    expect(res.status).toBe(status);
    expect(await res.json()).toMatchObject({ error: { message: "You are not a member of that organization." } });
    expect(res.headers.get("set-cookie")).toBeNull();
  });

  it("an unreachable API is a 503 and leaves cookies alone", async () => {
    fetchMock.mockRejectedValueOnce(new TypeError("fetch failed"));
    const res = await switchOrg(JSON_SAME, JSON.stringify({ org_id: ORG_A }));
    expect(res.status).toBe(503);
    expect(res.headers.get("set-cookie")).toBeNull();
  });
});

describe("pathAfterSwitch", () => {
  it.each([
    ["/kbs/abc", "/kbs"],
    ["/agent/abc", "/agent"],
    ["/chat/abc", "/chat"],
    ["/kbs", "/kbs"],
    ["/settings/team", "/settings/team"],
    ["/oauth/finish", "/dashboard"],
  ])("%s -> %s", (from, to) => expect(pathAfterSwitch(from)).toBe(to));
});

const orgs = [
  { id: ORG_A, name: "Org A", role: "viewer", created_at: "2026-11-01T00:00:00Z" },
  { id: ORG_B, name: "Org B", role: "owner", created_at: "2026-11-02T00:00:00Z" },
];

describe("OrgSwitcher", () => {
  it("is hidden when the user has one org", async () => {
    const fetchMock = vi.fn(async () => json(200, orgs.slice(0, 1)));
    vi.stubGlobal("fetch", fetchMock);
    const { container } = render(<OrgSwitcher currentOrgId={ORG_A} />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await new Promise((r) => setTimeout(r, 0));
    expect(container).toBeEmptyDOMElement();
  });

  it("lists orgs with roles, marks the current one, and switches by keyboard", async () => {
    const nav = vi.spyOn(browserNav, "assign").mockImplementation(() => undefined);
    window.history.replaceState(null, "", "/kbs/some-kb");
    const fetchMock = vi.fn(async (url: string) => (url === "/api/v1/orgs" ? json(200, orgs) : json(204, null)));
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    render(<OrgSwitcher currentOrgId={ORG_B} />);

    const trigger = await screen.findByRole("button", { name: "Switch organization" });
    trigger.focus();
    await user.keyboard("{Enter}");
    const items = await screen.findAllByRole("menuitem");
    expect(items.map((i) => i.textContent)).toEqual(["Org Aviewer", "Org Bowner"]);
    expect(items[1]).toHaveAttribute("aria-current", "true");
    expect(items[0]).not.toHaveAttribute("aria-current");

    await waitFor(() => expect(items[0]).toHaveFocus());
    await user.keyboard("{Enter}");
    await waitFor(() => expect(nav).toHaveBeenCalledWith("/kbs"));
    const call = fetchMock.mock.calls.find(([u]) => u === "/api/session/switch-org") as unknown as [string, RequestInit];
    expect(JSON.parse(String(call[1].body))).toEqual({ org_id: ORG_A });
  });

  it("shows the API's message when the switch is refused and stays put", async () => {
    const nav = vi.spyOn(browserNav, "assign").mockImplementation(() => undefined);
    vi.stubGlobal("fetch", vi.fn(async (url: string) => (url === "/api/v1/orgs" ? json(200, orgs) : refused(403))));
    const user = userEvent.setup();
    render(<OrgSwitcher currentOrgId={ORG_B} />);
    (await screen.findByRole("button", { name: "Switch organization" })).focus();
    await user.keyboard("{Enter}");
    await user.click((await screen.findAllByRole("menuitem"))[0]);
    expect(await screen.findByRole("alert")).toHaveTextContent("You are not a member of that organization.");
    expect(nav).not.toHaveBeenCalled();
  });
});

describe("InvitesBanner switch offer", () => {
  it("after accepting, offers Switch to <org> and switches to it", async () => {
    const nav = vi.spyOn(browserNav, "assign").mockImplementation(() => undefined);
    window.history.replaceState(null, "", "/dashboard");
    const invite = { id: "i1", org_id: ORG_A, org_name: "Org A", role: "viewer", created_at: "2026-11-01T00:00:00Z", expires_at: "2026-11-08T00:00:00Z" };
    let accepted = false;
    const fetchMock = vi.fn(async (url: string) => {
      if (url === "/api/v1/invites") return json(200, accepted ? [] : [invite]);
      if (url.endsWith("/accept")) {
        accepted = true;
        return json(200, { org_id: ORG_A, role: "viewer" });
      }
      return json(204, null);
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    render(<InvitesBanner />);
    await user.click(await screen.findByRole("button", { name: "Accept invite to Org A" }));
    await user.click(await screen.findByRole("button", { name: "Switch to Org A" }));
    await waitFor(() => expect(nav).toHaveBeenCalledWith("/dashboard"));
    const call = fetchMock.mock.calls.find(([u]) => u === "/api/session/switch-org") as unknown as [string, RequestInit];
    expect(JSON.parse(String(call[1].body))).toEqual({ org_id: ORG_A });
  });
});
