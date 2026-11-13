import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { chunkLocLabel, DocumentContent } from "@/components/document-chunks";
import type { Chunk, KbDocument } from "@/lib/api/types";

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

const doc = (over: Partial<KbDocument> = {}): KbDocument => ({
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
const chunk = (ord: number, text = `chunk ${ord}`, loc: Chunk["loc"] = { page: ord + 1 }): Chunk => ({
  id: `c${ord}`,
  ord,
  text,
  loc,
});

describe("chunkLocLabel", () => {
  it.each([
    [{ page: 3 }, "Page 3"],
    [{ section: "Expenses" }, "Section: Expenses"],
    [{ section: "Expenses", table: 1 }, "Section: Expenses · table 1"],
    [{ section: "Expenses", table: 1, range: "A1:C4" }, "Section: Expenses · table 1 · A1:C4"],
    [{ sheet: "Invoices", range: "A1:B3" }, "Sheet Invoices · A1:B3"],
    [{ range: "A1:B3" }, "Range A1:B3"],
    [{ record: 4 }, "Record 4"],
    [{}, null],
    [{ page: null, other: "x" }, null],
  ])("%j -> %s", (loc, label) => {
    expect(chunkLocLabel(loc)).toBe(label);
  });
});

describe("DocumentContent", () => {
  it("does not fetch until the document is ready", () => {
    const fetch = vi.fn();
    vi.stubGlobal("fetch", fetch);
    render(<DocumentContent doc={doc({ status: "processing" })} />);
    expect(fetch).not.toHaveBeenCalled();
    expect(screen.getByText(/once processing finishes/)).toBeVisible();
  });

  it("lists chunks with labels and preserved line breaks, then appends the next page", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(json(200, { items: [chunk(0, "line one\nline two")], next_cursor: "MA==" }))
      .mockResolvedValueOnce(json(200, { items: [chunk(1, "row", { sheet: "Invoices", range: "A1:B3" })], next_cursor: null }));
    vi.stubGlobal("fetch", fetch);
    render(<DocumentContent doc={doc()} />);
    expect(screen.getByLabelText("Loading content")).toBeInTheDocument();
    const first = await screen.findByText(/line one/);
    expect(first.textContent).toBe("line one\nline two");
    expect(first).toHaveClass("whitespace-pre-wrap");
    expect(screen.getByText("Page 1")).toBeVisible();
    expect(fetch).toHaveBeenLastCalledWith("/api/v1/documents/d1/chunks?limit=50", expect.anything());

    await userEvent.click(screen.getByRole("button", { name: "Load more" }));
    expect(await screen.findByText("Sheet Invoices · A1:B3")).toBeVisible();
    expect(fetch).toHaveBeenLastCalledWith("/api/v1/documents/d1/chunks?limit=50&cursor=MA%3D%3D", expect.anything());
    expect(screen.getAllByRole("listitem")).toHaveLength(2);
    expect(screen.getByText(/line one/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Load more" })).not.toBeInTheDocument();
  });

  it("clamps long text and expands on demand", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(json(200, { items: [chunk(0, "x".repeat(500))], next_cursor: null })));
    render(<DocumentContent doc={doc()} />);
    const text = await screen.findByText("x".repeat(500));
    expect(text).toHaveClass("line-clamp-6");
    await userEvent.click(screen.getByRole("button", { name: "Show more" }));
    expect(text).not.toHaveClass("line-clamp-6");
    expect(screen.getByRole("button", { name: "Show less" })).toHaveAttribute("aria-expanded", "true");
  });

  it("shows an empty state that points at warnings", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(json(200, { items: [], next_cursor: null })));
    render(<DocumentContent doc={doc({ warnings: ["page 2: no text"] })} />);
    expect(await screen.findByText("No content extracted")).toBeVisible();
    expect(screen.getByText(/warnings above/)).toBeVisible();
  });

  it("shows an error with retry that refetches", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(json(500, { error: { code: "internal", message: "boom" } }))
      .mockResolvedValueOnce(json(200, { items: [chunk(0)], next_cursor: null }));
    vi.stubGlobal("fetch", fetch);
    render(<DocumentContent doc={doc()} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Something went wrong on our side");
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("chunk 0")).toBeVisible();
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it("keeps loaded chunks when load more fails", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(json(200, { items: [chunk(0)], next_cursor: "MA==" }))
      .mockResolvedValueOnce(json(422, { error: { code: "invalid_cursor", message: "Invalid cursor." } }));
    vi.stubGlobal("fetch", fetch);
    render(<DocumentContent doc={doc()} />);
    await userEvent.click(await screen.findByRole("button", { name: "Load more" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Invalid cursor.");
    expect(screen.getByText("chunk 0")).toBeVisible();
  });
});
