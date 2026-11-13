import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { KbDetail } from "@/components/kb-detail";
import { TooltipProvider } from "@/components/ui/tooltip";

vi.mock("next/navigation", () => ({
  useParams: () => ({ id: "kb-1" }),
  useRouter: () => ({ replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

const renderDetail = () =>
  render(
    <TooltipProvider>
      <KbDetail />
    </TooltipProvider>,
  );

describe("KbDetail", () => {
  it("shows a not-found state, without retry, for another org's or a deleted KB", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(json(404, { error: { code: "not_found", message: "Not found", request_id: "r1" } })));
    renderDetail();
    expect(await screen.findByText("Knowledge base not found")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Try again" })).not.toBeInTheDocument();
  });

  it("renders the KB name and the empty documents state, without upload for viewers", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) =>
        Promise.resolve(
          url.includes("/documents")
            ? json(200, { items: [], next_cursor: null })
            : url.endsWith("/me")
              ? json(200, { user: { id: "u1", email: "v@example.com" }, org: { id: "o1", name: "Acme" }, role: "viewer" })
              : json(200, { id: "kb-1", name: "Finance", settings: {}, embedding_index_id: null, created_at: "2026-11-01T00:00:00Z" }),
        ),
      ),
    );
    renderDetail();
    expect(await screen.findByRole("heading", { name: "Finance" })).toBeInTheDocument();
    expect(await screen.findByText("No documents yet")).toBeInTheDocument();
    expect(screen.getByText(/view-only access/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Choose files" })).not.toBeInTheDocument();
  });
});
