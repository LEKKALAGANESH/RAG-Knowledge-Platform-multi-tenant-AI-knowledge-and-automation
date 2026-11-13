import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { KbList } from "@/components/kb-list";
import { TooltipProvider } from "@/components/ui/tooltip";

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

const kb = (id: string, name: string, created_at = "2026-11-01T00:00:00Z") => ({
  id,
  name,
  settings: {},
  embedding_index_id: null,
  created_at,
});

const me = (role: string) => json(200, { user: { id: "u1", email: "u@example.com" }, org: { id: "o1", name: "Acme" }, role });

/** Answers GET /me with `role`; every other call takes the next queued response (the last one repeats). */
function routed(role: string, ...responses: Response[]) {
  return vi.fn(async (url: string) => (url.endsWith("/me") ? me(role) : (responses.length > 1 ? responses.shift() : responses[0])!.clone()));
}
const kbCalls = (fetchMock: ReturnType<typeof routed>) => fetchMock.mock.calls.filter(([u]) => !String(u).endsWith("/me"));

const renderList = () =>
  render(
    <TooltipProvider>
      <KbList />
    </TooltipProvider>,
  );

describe("KbList", () => {
  it("shows a skeleton while loading", () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(() => {})));
    renderList();
    expect(screen.getByLabelText("Loading knowledge bases")).toHaveAttribute("aria-busy", "true");
  });

  it("shows the empty state with a create action", async () => {
    vi.stubGlobal("fetch", routed("editor", json(200, { items: [], next_cursor: null })));
    renderList();
    expect(await screen.findByText("No knowledge bases yet")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "New knowledge base" })).toBeInTheDocument();
  });

  it("renders items as links to their detail page", async () => {
    vi.stubGlobal(
      "fetch",
      routed("editor", json(200, { items: [kb("kb-1", "Finance")], next_cursor: null })),
    );
    renderList();
    expect(await screen.findByRole("link", { name: /Finance/ })).toHaveAttribute("href", "/kbs/kb-1");
  });

  it("shows an error with retry that refetches", async () => {
    const fetchMock = routed(
      "editor",
      json(503, { error: { code: "upstream_unavailable", message: "x", request_id: "req-9" } }),
      json(200, { items: [], next_cursor: null }),
    );
    vi.stubGlobal("fetch", fetchMock);
    renderList();
    expect(await screen.findByRole("alert")).toHaveTextContent(/can't reach the server/i);
    expect(screen.getByText("req-9")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("No knowledge bases yet")).toBeInTheDocument();
    expect(kbCalls(fetchMock)).toHaveLength(2);
  });

  it("creates a knowledge base and refreshes the list", async () => {
    const created = kb("kb-2", "Policies", "2026-11-02T00:00:00Z");
    const fetchMock = routed(
      "editor",
      json(200, { items: [], next_cursor: null }),
      json(201, created),
      json(200, { items: [created], next_cursor: null }),
    );
    vi.stubGlobal("fetch", fetchMock);
    renderList();
    await userEvent.click(await screen.findByRole("button", { name: "New knowledge base" }));
    await userEvent.type(screen.getByLabelText("Name"), "Policies");
    await userEvent.click(screen.getByRole("button", { name: "Create knowledge base" }));
    expect(await screen.findByRole("link", { name: /Policies/ })).toBeInTheDocument();
    expect(kbCalls(fetchMock)[1]).toEqual(["/api/v1/kbs", expect.objectContaining({ method: "POST", body: JSON.stringify({ name: "Policies" }) })]);
  });

  it("explains a 403 when the role changed since the page loaded", async () => {
    vi.stubGlobal(
      "fetch",
      routed(
        "editor",
        json(200, { items: [], next_cursor: null }),
        json(403, { error: { code: "forbidden", message: "forbidden", request_id: "r1" } }),
      ),
    );
    renderList();
    await userEvent.click(await screen.findByRole("button", { name: "New knowledge base" }));
    await userEvent.type(screen.getByLabelText("Name"), "Policies");
    await userEvent.click(screen.getByRole("button", { name: "Create knowledge base" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/can't create knowledge bases/i);
  });

  it("treats a list item missing the real KB fields as an invalid response", async () => {
    vi.stubGlobal("fetch", routed("editor", json(200, { items: [{ id: "x", name: "Old", created_at: "2026-11-01T00:00:00Z" }], next_cursor: null })));
    renderList();
    expect(await screen.findByRole("alert")).toHaveTextContent(/unexpected response/i);
  });
});
