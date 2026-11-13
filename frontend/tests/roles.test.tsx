import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import gdrive from "./fixtures/connector-gdrive.json";
import DashboardPage from "@/app/(app)/dashboard/page";
import { AppShell } from "@/components/app-shell";
import { Integrations } from "@/components/integrations";
import { InvitesBanner } from "@/components/invites-banner";
import { KbList } from "@/components/kb-list";
import { OrgSwitcher } from "@/components/org-switcher";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { Role } from "@/lib/api/types";
import { can } from "@/lib/roles";

vi.mock("next/navigation", () => ({
  usePathname: () => "/dashboard",
  useSearchParams: () => new URLSearchParams(),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const meBody = (role: Role) => ({ user: { id: "u1", email: "u@example.com" }, org: { id: "o1", name: "Acme" }, role });
const kb = { id: "kb-1", name: "Finance", settings: {}, embedding_index_id: null, created_at: "2026-11-01T00:00:00Z" };

/** GET /me answers with `role`; the rest by URL suffix, defaulting to an empty page. */
function api(role: Role, routes: Record<string, unknown> = {}) {
  return vi.fn(async (url: string) => {
    if (url.endsWith("/me")) return json(200, meBody(role));
    const hit = Object.entries(routes).find(([suffix]) => url.split("?")[0].endsWith(suffix));
    return json(200, hit ? hit[1] : { items: [], next_cursor: null });
  });
}

describe("can()", () => {
  // Same groups as backend tests/test_rbac.py POLICY: EDIT = owner/admin/editor, ADMIN = owner/admin.
  it.each([
    ["owner", true, true],
    ["admin", true, true],
    ["editor", true, false],
    ["viewer", false, false],
  ] as const)("%s: edit=%s manage=%s", (role, edit, manage) => {
    expect(can(role, "edit")).toBe(edit);
    expect(can(role, "manage")).toBe(manage);
  });

  it("denies everything before the role is known", () => {
    expect(can(null, "edit")).toBe(false);
    expect(can(undefined, "manage")).toBe(false);
  });
});

const NEW_KB = { name: "New knowledge base" };

describe("New knowledge base is role-gated", () => {
  it.each(["viewer", "editor"] as const)("/kbs with items, %s", async (role) => {
    vi.stubGlobal("fetch", api(role, { "/kbs": { items: [kb], next_cursor: null } }));
    render(<TooltipProvider><KbList /></TooltipProvider>);
    await screen.findByRole("link", { name: /Finance/ });
    if (role === "editor") expect(await screen.findByRole("button", NEW_KB)).toBeInTheDocument();
    else {
      await new Promise((r) => setTimeout(r, 0));
      expect(screen.queryByRole("button", NEW_KB)).toBeNull();
    }
  });

  it("/kbs empty: a viewer gets a hint instead of the button", async () => {
    vi.stubGlobal("fetch", api("viewer"));
    render(<KbList />);
    expect(await screen.findByText(/Editors and admins create knowledge bases/)).toBeInTheDocument();
    expect(screen.queryByRole("button", NEW_KB)).toBeNull();
  });

  it("/kbs empty: an editor gets the button", async () => {
    vi.stubGlobal("fetch", api("editor"));
    render(<KbList />);
    expect(await screen.findByRole("button", NEW_KB)).toBeInTheDocument();
  });

  it("dashboard empty: viewer gets the hint, no button", async () => {
    vi.stubGlobal("fetch", api("viewer"));
    render(<DashboardPage />);
    expect(await screen.findByText(/Editors and admins create knowledge bases/)).toBeInTheDocument();
    expect(screen.queryByRole("button", NEW_KB)).toBeNull();
  });

  it("dashboard empty: editor gets the button", async () => {
    vi.stubGlobal("fetch", api("editor"));
    render(<DashboardPage />);
    expect(await screen.findByRole("button", NEW_KB)).toBeInTheDocument();
    expect(screen.queryByText(/Editors and admins create knowledge bases/)).toBeNull();
  });
});

describe("Integrations Connect is role-gated", () => {
  it.each(["viewer", "editor"] as const)("%s", async (role) => {
    vi.stubGlobal("fetch", api(role, { "/connectors": { items: [gdrive] } }));
    render(<Integrations />);
    const connect = await screen.findByRole("button", { name: "Connect" });
    const reason = screen.queryByText("Editors and admins can connect sources.");
    if (role === "viewer") {
      expect(connect).toBeDisabled();
      expect(reason).toBeInTheDocument();
    } else expect(reason).toBeNull();
  });
});

describe("Org switcher after accepting an invite", () => {
  it("appears without a reload", async () => {
    const orgA = { id: "6f1c2c6e-8a0b-4c39-9a52-0d3b5d1f2a11", name: "Org A", role: "viewer" };
    const orgB = { id: "0b7e4a52-3f1d-4e8a-b6c2-9d8e7f6a5b44", name: "Org B", role: "owner" };
    const invite = { id: "i1", org_id: orgA.id, org_name: "Org A", role: "viewer", created_at: "2026-11-01T00:00:00Z", expires_at: "2026-11-08T00:00:00Z" };
    let accepted = false;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        if (url === "/api/v1/orgs") return json(200, accepted ? [orgB, orgA] : [orgB]);
        if (url === "/api/v1/invites") return json(200, accepted ? [] : [invite]);
        accepted = true;
        return json(200, { org_id: orgA.id, role: "viewer" });
      }),
    );
    render(<><OrgSwitcher currentOrgId={orgB.id} /><InvitesBanner /></>);
    await userEvent.click(await screen.findByRole("button", { name: "Accept invite to Org A" }));
    expect(await screen.findByRole("button", { name: "Switch organization" })).toBeInTheDocument();
  });
});

describe("Header touch targets", () => {
  it("switcher, sign-out and nav toggle are 44px on coarse pointers", async () => {
    const orgs = [{ id: "a", name: "A", role: "owner" }, { id: "b", name: "B", role: "viewer" }];
    vi.stubGlobal("fetch", api("owner", { "/orgs": orgs, "/invites": [] }));
    render(<TooltipProvider><AppShell><p>page</p></AppShell></TooltipProvider>);
    const targets = [
      await screen.findByRole("button", { name: "Switch organization" }),
      screen.getByRole("button", { name: "Sign out" }),
      screen.getByRole("button", { name: "Open navigation" }),
    ];
    for (const el of targets) expect(el.className).toMatch(/pointer-coarse:(size-11|h-11 pointer-coarse:min-w-11)/);
    await waitFor(() => expect(screen.getByText("page")).toBeInTheDocument());
  });
});
