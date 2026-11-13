import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ApiKeys, keyPayload } from "@/components/api-keys";
import { AuditLog, auditExportHref } from "@/components/audit-log";
import { BudgetBanner } from "@/components/budget-banner";
import { InvitesBanner } from "@/components/invites-banner";
import { canEditMember, roleOptions, Team } from "@/components/team";
import { api, browserNav } from "@/lib/api/client";
import { toApiError } from "@/lib/api/errors";
import type { Me, UsageBucket } from "@/lib/api/types";
import { parseBudget } from "@/lib/budget";
import { dailySeries, defaultRange, rangeQuery, shares } from "@/lib/usage";

const json = (status: number, body: unknown, headers: Record<string, string> = {}) =>
  new Response(status === 204 ? null : JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...headers } });
const apiErr = (code: string, message: string) => ({ error: { code, message, request_id: "r1" } });

const me = (role: Me["role"]): Me => ({ user: { id: "u1", email: "owner@example.com" }, org: { id: "o1", name: "Acme" }, role });
const bodyOf = (call: unknown[]) => JSON.parse(String((call[1] as RequestInit).body)) as unknown;

describe("API keys", () => {
  it("scope picker -> payload: catalog order, end-of-day UTC expiry, empty expiry = null", () => {
    expect(keyPayload(" Bot ", new Set(["search", "kb:read", "admin"]), "2027-01-31")).toEqual({
      name: "Bot",
      scopes: ["kb:read", "search", "admin"],
      expires_at: "2027-01-31T23:59:59Z",
    });
    expect(keyPayload("x", new Set(["answer"]), "").expires_at).toBeNull();
  });

  it("creates a key, shows it once, and drops it when closed", async () => {
    const created = { id: "k1", name: "CI", prefix: "abc123def456", scopes: ["kb:read", "search", "answer"], user_id: "u1", created_at: "2026-11-12T00:00:00Z", expires_at: null, last_used_at: null, revoked_at: null };
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === "POST") return json(201, { ...created, key: "rag_abc123def456_SECRETSECRET" });
      return json(200, fetchMock.mock.calls.length > 1 ? [created] : []);
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    render(<ApiKeys />);

    await user.click(await screen.findByRole("button", { name: "Create API key" }));
    await user.type(screen.getByLabelText("Name"), "CI");
    await user.click(screen.getByRole("checkbox", { name: /Ask questions/ }));
    await user.click(screen.getByRole("button", { name: "Create key" }));

    const post = fetchMock.mock.calls.find((c) => (c[1] as RequestInit | undefined)?.method === "POST")!;
    expect(bodyOf(post)).toEqual({ name: "CI", scopes: ["kb:read", "search", "answer"], expires_at: null });

    expect(await screen.findByTestId("new-key")).toHaveTextContent("rag_abc123def456_SECRETSECRET");
    await user.click(screen.getByRole("button", { name: "I’ve saved it" }));
    await waitFor(() => expect(screen.queryByText(/SECRETSECRET/)).not.toBeInTheDocument());
    expect(await screen.findByText("CI")).toBeInTheDocument();
    expect(screen.getByText(/rag_abc123def456_/)).toBeInTheDocument(); // list shows only the prefix
  });

  it("shows the server message when no scope is accepted", async () => {
    vi.stubGlobal("fetch", vi.fn(async (_u: string, init?: RequestInit) =>
      init?.method === "POST" ? json(403, apiErr("insufficient_scope", "A key cannot grant scopes it does not have.")) : json(200, []),
    ));
    const user = userEvent.setup();
    render(<ApiKeys />);
    await user.click(await screen.findByRole("button", { name: "Create API key" }));
    await user.type(screen.getByLabelText("Name"), "x");
    await user.click(screen.getByRole("button", { name: "Create key" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("A key cannot grant scopes it does not have.");
  });
});

describe("Team", () => {
  const members = [
    { user_id: "u1", email: "owner@example.com", role: "owner" },
    { user_id: "u2", email: "ed@example.com", role: "editor" },
  ];
  // GET /members -> members, GET /invites -> none pending.
  const list = (u: string) => json(200, u.includes("/invites") ? [] : members);

  it("owner-only options are gated client-side", () => {
    expect(roleOptions("admin").map((r) => r.value)).toEqual(["admin", "editor", "viewer"]);
    expect(roleOptions("owner").map((r) => r.value)).toContain("owner");
    expect(canEditMember("admin", { user_id: "x", email: "o", role: "owner" })).toBe(false);
    expect(canEditMember("owner", { user_id: "x", email: "o", role: "owner" })).toBe(true);
    expect(canEditMember("editor", { user_id: "x", email: "v", role: "viewer" })).toBe(false);
  });

  it("demoting the last owner shows the 409 last_owner message and keeps the role", async () => {
    vi.stubGlobal("fetch", vi.fn(async (u: string, init?: RequestInit) =>
      init?.method === "PATCH" ? json(409, apiErr("last_owner", "An organization needs an owner. Add another first.")) : list(u),
    ));
    const user = userEvent.setup();
    render(<Team me={me("owner")} />);
    const select = await screen.findByLabelText("Role for owner@example.com");
    await user.selectOptions(select, "admin");
    expect(await screen.findByRole("alert")).toHaveTextContent("An organization needs an owner. Add another first.");
    expect(select).toHaveValue("owner");
  });

  it("a 403 from the server is shown on the row", async () => {
    vi.stubGlobal("fetch", vi.fn(async (u: string, init?: RequestInit) =>
      init?.method === "PATCH" ? json(403, apiErr("forbidden", "Only an owner can grant or remove the owner role.")) : list(u),
    ));
    const user = userEvent.setup();
    render(<Team me={{ ...me("admin"), user: { id: "u9", email: "admin@example.com" } }} />);
    expect(screen.queryByLabelText("Role for owner@example.com")).not.toBeInTheDocument(); // owner row is read-only for admins
    await user.selectOptions(await screen.findByLabelText("Role for ed@example.com"), "viewer");
    expect(await screen.findByRole("alert")).toHaveTextContent("Only an owner can grant or remove the owner role.");
  });

  it("removes a member after confirming", async () => {
    const fetchMock = vi.fn(async (u: string, init?: RequestInit) => (init?.method === "DELETE" ? json(204, null) : list(u)));
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    render(<Team me={me("owner")} />);
    await user.click(await screen.findByRole("button", { name: "Remove ed@example.com" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Remove member" }));
    await waitFor(() => expect(fetchMock.mock.calls.some((c) => (c[1] as RequestInit | undefined)?.method === "DELETE" && String(c[0]).endsWith("/orgs/o1/members/u2"))).toBe(true));
  });

  it("invites by email, lists the pending invite, and revokes it", async () => {
    const pending = { id: "i1", email: "new@example.com", role: "editor", created_at: "2026-11-12T00:00:00Z", expires_at: "2026-11-19T00:00:00Z" };
    let invites: unknown[] = [];
    const fetchMock = vi.fn(async (u: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        invites = [pending];
        return json(201, pending);
      }
      if (init?.method === "DELETE") {
        invites = [];
        return json(204, null);
      }
      return json(200, u.includes("/invites") ? invites : members);
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    render(<Team me={me("owner")} />);
    expect(screen.getByText(/No email is sent yet/)).toBeInTheDocument();
    await user.type(await screen.findByLabelText("Invite by email"), " new@example.com ");
    await user.selectOptions(screen.getByLabelText("Role"), "editor");
    await user.click(screen.getByRole("button", { name: "Invite" }));
    const post = fetchMock.mock.calls.find((c) => (c[1] as RequestInit | undefined)?.method === "POST")!;
    expect(String(post[0])).toBe("/api/v1/orgs/o1/members");
    expect(bodyOf(post)).toEqual({ email: "new@example.com", role: "editor" });
    // Pending, not a member: it shows under invites and the member count is unchanged.
    expect(await screen.findByRole("heading", { name: /Pending invites/ })).toHaveTextContent("(1)");
    expect(screen.getByRole("heading", { name: /Members/ })).toHaveTextContent("(2)");
    await user.click(screen.getByRole("button", { name: "Revoke invite for new@example.com" }));
    await waitFor(() => expect(fetchMock.mock.calls.some((c) => (c[1] as RequestInit | undefined)?.method === "DELETE" && String(c[0]).endsWith("/orgs/o1/invites/i1"))).toBe(true));
    await waitFor(() => expect(screen.queryByRole("heading", { name: /Pending invites/ })).not.toBeInTheDocument());
  });

  it("an admin cannot revoke an owner invite", async () => {
    const ownerInvite = { id: "i2", email: "boss@example.com", role: "owner", created_at: "2026-11-12T00:00:00Z", expires_at: "2026-11-19T00:00:00Z" };
    vi.stubGlobal("fetch", vi.fn(async (u: string) => json(200, u.includes("/invites") ? [ownerInvite] : members)));
    render(<Team me={{ ...me("admin"), user: { id: "u9", email: "admin@example.com" } }} />);
    expect(await screen.findByText("boss@example.com")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Revoke invite/ })).not.toBeInTheDocument();
  });

  it("viewers see a read-only view without calling the admin-only list", () => {
    const fetchMock = vi.fn(async () => json(403, apiErr("forbidden", "no")));
    vi.stubGlobal("fetch", fetchMock);
    render(<Team me={me("viewer")} />);
    expect(screen.getByText(/Only owners and admins can see the full member list/)).toBeInTheDocument();
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
  });
});

describe("Audit log", () => {
  const row = (id: string, action = "member.add") => ({
    id, action, actor_user_id: "u1", actor_api_key_id: null, target_type: "user", target_id: null,
    request_id: null, ip_hash: null, meta: { role: "editor" }, created_at: "2026-11-12T10:00:00Z",
  });

  it("pages with the cursor and links the CSV export with the filter", async () => {
    const fetchMock = vi.fn(async (url: string) => {
      if (url.includes("/members")) return json(200, [{ user_id: "u1", email: "owner@example.com", role: "owner" }]);
      if (url.includes("cursor=c1")) return json(200, { items: [row("b")], next_cursor: null });
      return json(200, { items: [row("a")], next_cursor: "c1" });
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    render(<AuditLog me={me("owner")} />);
    expect(await screen.findAllByText("Member added")).not.toHaveLength(0);
    await user.click(screen.getByRole("button", { name: "Load older events" }));
    await waitFor(() => expect(screen.getAllByRole("row")).toHaveLength(3)); // header + 2
    expect(screen.queryByRole("button", { name: "Load older events" })).not.toBeInTheDocument();
    expect(await screen.findAllByText("owner@example.com")).toHaveLength(2);

    await user.selectOptions(screen.getByLabelText("Action"), "api_key.revoke");
    expect(screen.getByRole("link", { name: "Download CSV" })).toHaveAttribute("href", "/api/v1/audit/export?action=api_key.revoke");
    await waitFor(() => expect(fetchMock.mock.calls.some((c) => String(c[0]).includes("action=api_key.revoke"))).toBe(true));
    expect(auditExportHref("")).toBe("/api/v1/audit/export");
  });
});

describe("usage mapping", () => {
  const b = (key: string | null, cost: number, calls: number, label?: string): UsageBucket => ({
    key, label, cost_usd: cost, calls, in_tok: 0, out_tok: 0, unpriced_calls: 0,
  });

  it("empty range: one zero point per day", () => {
    const pts = dailySeries("2026-11-01", "2026-11-03", []);
    expect(pts).toEqual([
      { day: "2026-11-01", cost: 0, calls: 0 },
      { day: "2026-11-02", cost: 0, calls: 0 },
      { day: "2026-11-03", cost: 0, calls: 0 },
    ]);
  });

  it("populated: buckets land on their day, gaps stay zero", () => {
    const pts = dailySeries("2026-10-31", "2026-11-02", [b("2026-11-02", 0.5, 4), b("2026-10-31", 0.01, 1)]);
    expect(pts.map((p) => [p.day, p.cost, p.calls])).toEqual([["2026-10-31", 0.01, 1], ["2026-11-01", 0, 0], ["2026-11-02", 0.5, 4]]);
  });

  it("breakdown sorts by cost, falls back to calls when nothing is priced, labels nulls", () => {
    expect(shares([b("a", 1, 10), b("b", 3, 1)], "x").map((s) => [s.key, s.fraction])).toEqual([["b", 1], ["a", 1 / 3]]);
    const unpriced = shares([b(null, 0, 2), b("u2", 0, 8, "ed@example.com")], "Background jobs");
    expect(unpriced.map((s) => [s.label, s.fraction])).toEqual([["ed@example.com", 1], ["Background jobs", 0.25]]);
  });

  it("range: this UTC month by default; `to` becomes the next midnight (exclusive)", () => {
    expect(defaultRange(new Date("2026-11-12T23:30:00Z"))).toEqual({ from: "2026-11-01", to: "2026-11-12" });
    expect(decodeURIComponent(rangeQuery("2026-11-01", "2026-11-30"))).toBe("from=2026-11-01T00:00:00Z&to=2026-12-01T00:00:00Z");
  });
});

describe("budget form validation", () => {
  it.each([
    ["", { ok: true, value: null }],
    [" 250 ", { ok: true, value: "250" }],
    ["99.5", { ok: true, value: "99.5" }],
    ["0", { ok: true, value: "0" }],
    ["10000000", { ok: true, value: "10000000" }],
  ])("accepts %j", (input, out) => expect(parseBudget(input)).toEqual(out));

  it.each(["-1", "1.234", "1e3", "$5", "abc", "1,000", "10000000.01"])("rejects %j", (input) => {
    expect(parseBudget(input).ok).toBe(false);
  });
});

describe("budget signals", () => {
  it("402 budget_exceeded anywhere raises the banner with a usage link for admins", async () => {
    render(<BudgetBanner canManage />);
    const msg = "This organization has used $1.00 of its $1.00 monthly AI budget.";
    const err = await act(() => toApiError(json(402, apiErr("budget_exceeded", msg))));
    expect(err.message).toBe(msg);
    expect(screen.getByRole("alert")).toHaveTextContent(msg);
    expect(screen.getByRole("link", { name: "Review usage and budget" })).toHaveAttribute("href", "/usage");
  });

  it("non-admins get no usage link", async () => {
    render(<BudgetBanner canManage={false} />);
    await act(() => toApiError(json(402, apiErr("budget_exceeded", "Over budget."))));
    expect(screen.getByRole("alert")).toHaveTextContent("Ask an owner or admin");
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it("X-Budget-Warning shows a dismissible warning", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json(201, { id: "r", goal: "g", kb_ids: [], status: "planning", max_steps: 5, result: null, error: null, created_at: "2026-11-12T00:00:00Z", finished_at: null }, { "X-Budget-Warning": "over_budget" })));
    const user = userEvent.setup();
    render(<BudgetBanner canManage />);
    await act(() => api.createAgentRun({ goal: "g", max_steps: 5 }));
    expect(screen.getByRole("status")).toHaveTextContent("over its monthly AI budget");
    await user.click(screen.getByRole("button", { name: "Dismiss budget notice" }));
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
});

describe("signed-out session", () => {
  it("a 401 session_revoked from the BFF sends the browser to sign-in with next=", async () => {
    const assign = vi.spyOn(browserNav, "assign").mockImplementation(() => undefined);
    window.history.replaceState(null, "", "/settings/team?x=1");
    vi.stubGlobal("fetch", vi.fn(async () => json(401, apiErr("session_revoked", "Your session has ended. Sign in again."))));
    await expect(api.me()).rejects.toMatchObject({ status: 401, code: "session_revoked" });
    expect(assign).toHaveBeenCalledWith(`/login?next=${encodeURIComponent("/settings/team?x=1")}`);
  });
});

describe("daily chart", () => {
  it("all-zero series shows the empty note instead of a flat chart; counts get a whole-number axis", async () => {
    const { DailyColumns } = await import("@/components/usage-chart");
    const pts = dailySeries("2026-11-01", "2026-11-03", []);
    const { rerender } = render(<DailyColumns title="Cost" points={pts} value={(p) => p.cost} format={String} empty="Nothing priced." />);
    expect(screen.getByText("Nothing priced.")).toBeInTheDocument();
    expect(screen.queryAllByTestId("day-column")).toHaveLength(0);
    const one = dailySeries("2026-11-01", "2026-11-03", [{ key: "2026-11-02", cost_usd: 0, calls: 1, in_tok: 0, out_tok: 0, unpriced_calls: 1 }]);
    rerender(<DailyColumns title="Calls" points={one} value={(p) => p.calls} format={String} integer empty="x" />);
    expect(screen.getAllByTestId("day-column")).toHaveLength(3);
    expect(screen.getAllByText("2").length).toBeGreaterThan(0); // axis top
    expect(screen.queryByText("0.5")).not.toBeInTheDocument();
  });
});

describe("Invites banner", () => {
  const inv = { id: "i1", org_id: "o2", org_name: "Victimcorp", role: "admin", created_at: "2026-11-12T00:00:00Z", expires_at: "2026-11-19T00:00:00Z" };

  it("shows nothing without invites", async () => {
    const fetchMock = vi.fn(async () => json(200, []));
    vi.stubGlobal("fetch", fetchMock);
    const { container } = render(<InvitesBanner />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/v1/invites", expect.anything()));
    expect(container).toBeEmptyDOMElement();
  });

  it("joins only on Accept", async () => {
    let open = [inv];
    const fetchMock = vi.fn(async (_u: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        open = [];
        return json(200, { org_id: "o2", role: "admin" });
      }
      return json(200, open);
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    render(<InvitesBanner />);
    expect(await screen.findByText(/You.re invited to/)).toHaveTextContent("Victimcorp");
    expect(fetchMock.mock.calls.some((c) => (c[1] as RequestInit | undefined)?.method === "POST")).toBe(false);
    await user.click(screen.getByRole("button", { name: "Accept invite to Victimcorp" }));
    expect(fetchMock.mock.calls.find((c) => (c[1] as RequestInit | undefined)?.method === "POST")![0]).toBe("/api/v1/invites/i1/accept");
    expect(await screen.findByRole("status")).toHaveTextContent("You joined Victimcorp as admin.");
  });

  it("declines, and shows the server message when the invite is gone", async () => {
    const fetchMock = vi.fn(async (u: string, init?: RequestInit) => {
      if (init?.method === "POST" && u.endsWith("/decline")) return json(204, null);
      if (init?.method === "POST") return json(404, apiErr("not_found", "Invite not found."));
      return json(200, [inv, { ...inv, id: "i2", org_name: "Other" }]);
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    render(<InvitesBanner />);
    await user.click(await screen.findByRole("button", { name: "Accept invite to Other" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Invite not found.");
    await user.click(screen.getByRole("button", { name: "Decline invite to Victimcorp" }));
    expect(await screen.findByRole("status")).toHaveTextContent("Declined the invite to Victimcorp.");
    expect(fetchMock.mock.calls.some((c) => String(c[0]) === "/api/v1/invites/i1/decline")).toBe(true);
  });
});
