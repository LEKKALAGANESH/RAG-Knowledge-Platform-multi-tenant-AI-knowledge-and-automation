import { describe, expect, it } from "vitest";
import { mapUploadResponse, precheck } from "@/lib/upload";

const doc = {
  id: "d1",
  kb_id: "kb-1",
  source_id: null,
  name: "report.pdf",
  mime: "application/pdf",
  sha256: "a".repeat(64),
  version: 1,
  status: "queued",
  error: null,
  warnings: [],
  meta: { size: 10 },
  created_at: "2026-11-12T00:00:00Z",
};
const err = (code: string, message: string) => JSON.stringify({ error: { code, message, request_id: "r1" } });

describe("mapUploadResponse", () => {
  it("201 is a new queued document", async () => {
    expect(await mapUploadResponse(201, JSON.stringify(doc))).toMatchObject({ kind: "queued", doc: { id: "d1" } });
  });

  it("200 is a duplicate of an existing document", async () => {
    expect(await mapUploadResponse(200, JSON.stringify({ ...doc, status: "ready" }))).toMatchObject({
      kind: "duplicate",
      doc: { status: "ready" },
    });
  });

  it.each([
    [413, err("file_too_large", "Files must be at most 50 MB."), "The file is too large. Files must be at most 50 MB."],
    [415, err("unsupported_type", "Supported: PDF, DOCX."), "This file type isn't supported. Supported: PDF, DOCX."],
    [422, err("empty_file", "The uploaded file is empty."), "The uploaded file is empty."],
    [403, err("forbidden", "Insufficient role"), "Your role can't upload documents. Ask an owner or admin for editor access."],
    [404, err("not_found", "Knowledge base not found"), "This knowledge base no longer exists."],
    [500, err("internal", "Traceback secret"), "Something went wrong on our side. Try again in a moment."],
    [0, "", "Can't reach the server. Check your connection and try again."],
  ])("%i maps to a readable error", async (status, body, message) => {
    expect(await mapUploadResponse(status, body)).toEqual({ kind: "error", message });
  });

  it("a 2xx with a malformed body is an error, not a fake success", async () => {
    expect((await mapUploadResponse(201, "<html>")).kind).toBe("error");
  });
});

describe("precheck", () => {
  const file = (name: string, size: number) => new File([new Uint8Array(size)], name);

  it("accepts supported extensions regardless of case", () => {
    expect(precheck(file("A.PDF", 3))).toBeNull();
    expect(precheck(file("rows.jsonl", 3))).toBeNull();
  });

  it("rejects unknown extensions, empty and oversized files", () => {
    expect(precheck(file("setup.exe", 3))).toMatch(/isn't supported/);
    expect(precheck(file("noext", 3))).toMatch(/isn't supported/);
    expect(precheck(file("notes.txt", 0))).toBe("The file is empty.");
    const big = file("big.pdf", 1);
    Object.defineProperty(big, "size", { value: 51 * 2 ** 20 });
    expect(precheck(big)).toMatch(/larger than 50 MB/);
  });
});
