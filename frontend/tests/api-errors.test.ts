import { describe, expect, it, vi } from "vitest";
import { api } from "@/lib/api/client";
import { ApiError, toApiError, UPSTREAM_UNAVAILABLE } from "@/lib/api/errors";

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

describe("toApiError", () => {
  it("uses the backend message and request id for 4xx", async () => {
    const err = await toApiError(json(409, { error: { code: "kb_exists", message: "A knowledge base with this name already exists.", request_id: "req-1" } }));
    expect(err).toMatchObject({ status: 409, code: "kb_exists", requestId: "req-1" });
    expect(err.message).toBe("A knowledge base with this name already exists.");
    expect(err.retryable).toBe(false);
  });

  it("hides backend internals on 5xx and marks it retryable", async () => {
    const err = await toApiError(json(500, { error: { code: "internal", message: "psycopg.OperationalError: boom", request_id: "req-2" } }));
    expect(err.message).toBe("Something went wrong on our side. Try again in a moment.");
    expect(err.requestId).toBe("req-2");
    expect(err.retryable).toBe(true);
  });

  it("maps the BFF upstream_unavailable code to a connection message", async () => {
    const err = await toApiError(json(503, { error: { code: UPSTREAM_UNAVAILABLE, message: "x", request_id: "r" } }));
    expect(err.message).toMatch(/can't reach the server/i);
  });

  it("falls back to a status message for non-JSON or malformed bodies", async () => {
    const html = await toApiError(new Response("<html>bad gateway</html>", { status: 429 }));
    expect(html).toMatchObject({ code: "http_429", message: "Too many requests. Wait a moment and try again." });
    const odd = await toApiError(json(404, { detail: "Not Found" }));
    expect(odd.message).toBe("This item doesn't exist or was deleted.");
  });
});

describe("api client", () => {
  it("turns a network failure into a retryable ApiError", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));
    const err = await api.listKbs().catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err).toMatchObject({ status: 0, retryable: true });
  });

  it("rejects responses that don't match the contract", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(json(200, { items: [{ id: 1 }] })));
    await expect(api.listKbs()).rejects.toMatchObject({ code: "invalid_response" });
  });

  it("parses a valid page", async () => {
    const kb = { id: "k1", name: "Finance", settings: {}, embedding_index_id: null, created_at: "2026-11-01T00:00:00Z" };
    const fetchMock = vi.fn().mockResolvedValue(json(200, { items: [kb], next_cursor: null }));
    vi.stubGlobal("fetch", fetchMock);
    await expect(api.listKbs()).resolves.toEqual({ items: [kb], next_cursor: null });
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/kbs?limit=100", expect.anything());
  });

  it("parses the real /me shape", async () => {
    const body = { user: { id: "u1", email: "a@b.co" }, org: { id: "o1", name: "a's workspace" }, role: "owner" };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(json(200, body)));
    await expect(api.me()).resolves.toEqual(body);
  });
});
