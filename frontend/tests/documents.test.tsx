import { act, render, renderHook, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { DocumentsTable } from "@/components/documents-table";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { KbDocument } from "@/lib/api/types";
import { useDocuments } from "@/lib/use-documents";

let search = "";
vi.mock("next/navigation", () => ({ useSearchParams: () => new URLSearchParams(search) }));

const json = (status: number, body: unknown) =>
  new Response(status === 204 ? null : JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

const doc = (over: Partial<KbDocument>): KbDocument => ({
  id: "d1",
  kb_id: "kb-1",
  source_id: null,
  name: "report.pdf",
  mime: "application/pdf",
  sha256: "a".repeat(64),
  version: 1,
  status: "ready",
  error: null,
  warnings: [],
  meta: { size: 2048 },
  created_at: "2026-11-12T00:00:00Z",
  ...over,
});
const page = (...items: KbDocument[]) => json(200, { items, next_cursor: null });

afterEach(() => vi.useRealTimers());

describe("useDocuments polling", () => {
  it("polls with backoff while a document is pending and stops once all are settled", async () => {
    vi.useFakeTimers();
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(page(doc({ status: "queued" })))
      .mockResolvedValueOnce(page(doc({ status: "processing" })))
      .mockResolvedValue(page(doc({ status: "ready" })));
    vi.stubGlobal("fetch", fetch);
    const { result } = renderHook(() => useDocuments("kb-1"));
    await act(() => vi.advanceTimersByTimeAsync(0));
    expect(fetch).toHaveBeenCalledTimes(1);
    await act(() => vi.advanceTimersByTimeAsync(1_000));
    expect(fetch).toHaveBeenCalledTimes(2);
    await act(() => vi.advanceTimersByTimeAsync(1_999)); // backoff doubled to 2 s
    expect(fetch).toHaveBeenCalledTimes(2);
    await act(() => vi.advanceTimersByTimeAsync(1));
    expect(fetch).toHaveBeenCalledTimes(3);
    expect(result.current.state).toMatchObject({ status: "ready", docs: [{ status: "ready" }] });
    await act(() => vi.advanceTimersByTimeAsync(60_000));
    expect(fetch).toHaveBeenCalledTimes(3);
  });

  it("stops polling on unmount", async () => {
    vi.useFakeTimers();
    const fetch = vi.fn().mockResolvedValue(page(doc({ status: "processing" })));
    vi.stubGlobal("fetch", fetch);
    const { unmount } = renderHook(() => useDocuments("kb-1"));
    await act(() => vi.advanceTimersByTimeAsync(0));
    unmount();
    await act(() => vi.advanceTimersByTimeAsync(60_000));
    expect(fetch).toHaveBeenCalledTimes(1);
  });
});

const renderTable = (docs: KbDocument[], canEdit = true) => {
  const onChanged = vi.fn();
  render(
    <TooltipProvider>
      <DocumentsTable docs={docs} canEdit={canEdit} onChanged={onChanged} />
    </TooltipProvider>,
  );
  return onChanged;
};

describe("DocumentsTable", () => {
  it("shows a failed document's error inline and reprocesses it", async () => {
    const fetch = vi.fn().mockResolvedValue(json(202, doc({ status: "queued" })));
    vi.stubGlobal("fetch", fetch);
    const onChanged = renderTable([doc({ status: "failed", error: "PDF is encrypted" })]);
    expect(screen.getByText("PDF is encrypted")).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "Reprocess report.pdf" }));
    expect(fetch).toHaveBeenCalledWith("/api/v1/documents/d1/reprocess", expect.objectContaining({ method: "POST" }));
    expect(onChanged).toHaveBeenCalledOnce();
  });

  it("offers no reprocess for ready documents and lists warnings on demand", async () => {
    renderTable([doc({ warnings: ["page 2: no text, OCR not enabled"] })]);
    expect(screen.queryByRole("button", { name: /Reprocess/ })).not.toBeInTheDocument();
    expect(screen.queryByText("page 2: no text, OCR not enabled")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "1 warning" }));
    expect(screen.getByText("page 2: no text, OCR not enabled")).toBeVisible();
  });

  it("deletes only after a confirm dialog that names the document", async () => {
    const fetch = vi.fn().mockResolvedValue(json(204, null));
    vi.stubGlobal("fetch", fetch);
    const onChanged = renderTable([doc({})]);
    await userEvent.click(screen.getByRole("button", { name: "Delete report.pdf" }));
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText(/report\.pdf/)).toBeVisible();
    expect(fetch).not.toHaveBeenCalled();
    await userEvent.click(within(dialog).getByRole("button", { name: "Delete document" }));
    expect(fetch).toHaveBeenCalledWith("/api/v1/documents/d1", expect.objectContaining({ method: "DELETE" }));
    expect(onChanged).toHaveBeenCalledOnce();
  });

  it("cancel closes the dialog without deleting", async () => {
    const fetch = vi.fn();
    vi.stubGlobal("fetch", fetch);
    renderTable([doc({})]);
    await userEvent.click(screen.getByRole("button", { name: "Delete report.pdf" }));
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(fetch).not.toHaveBeenCalled();
  });

  it("hides mutating controls for viewers", () => {
    renderTable([doc({ status: "failed", error: "bad" })], false);
    expect(screen.queryByRole("button", { name: /Reprocess|Delete/ })).not.toBeInTheDocument();
  });

  it("opens a drawer with document metadata", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(json(200, { items: [], next_cursor: null })));
    renderTable([doc({ version: 3 })]);
    await userEvent.click(screen.getByRole("button", { name: "report.pdf" }));
    const sheet = screen.getByRole("dialog");
    expect(within(sheet).getByText("application/pdf")).toBeVisible();
    expect(within(sheet).getByText("aaaaaaaaaaaa…")).toBeVisible();
    expect(within(sheet).getByText("2.0 KB")).toBeVisible();
    expect(within(sheet).getByText("3")).toBeVisible();
  });

  it("opens the drawer for ?doc=<id> on arrival (citation deep link)", () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(json(200, { items: [], next_cursor: null })));
    search = "doc=d1";
    try {
      renderTable([doc({})]);
      expect(within(screen.getByRole("dialog")).getByText("Document details")).toBeVisible();
    } finally {
      search = "";
    }
  });
});
