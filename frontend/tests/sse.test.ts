import { describe, expect, it, vi } from "vitest";
import { loginPath, redirectToLogin } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import { safeNext } from "@/lib/safe-next";
import { parseSse, streamAnswer, type AnswerEvent } from "@/lib/sse";

// jsdom cannot navigate; the redirect itself is observed as a call, its URL via loginPath.
vi.mock("@/lib/api/client", async (orig) => ({ ...(await orig<typeof import("@/lib/api/client")>()), redirectToLogin: vi.fn() }));

const bytes = (s: string) => new TextEncoder().encode(s);

/** A body that delivers exactly these byte chunks, so frame boundaries land wherever the test wants. */
function body(chunks: Uint8Array[]): ReadableStream<Uint8Array> {
  return new ReadableStream({
    start(c) {
      chunks.forEach((x) => c.enqueue(x));
      c.close();
    },
  });
}

async function collect<T>(it: AsyncIterable<T>): Promise<T[]> {
  const out: T[] = [];
  for await (const x of it) out.push(x);
  return out;
}

describe("parseSse", () => {
  it("reassembles frames split at arbitrary points, including inside a multi-byte character", async () => {
    const all = bytes('event: token\ndata: {"text":"Grüße"}\n\nevent: done\ndata: {}\n\n');
    const umlaut = all.indexOf(0xc3); // first byte of "ü"
    const chunks = [all.slice(0, 3), all.slice(3, umlaut + 1), all.slice(umlaut + 1, umlaut + 30), all.slice(umlaut + 30)];
    expect(await collect(parseSse(body(chunks)))).toEqual([
      { event: "token", data: '{"text":"Grüße"}' },
      { event: "done", data: "{}" },
    ]);
  });

  it("joins multi-line data with newlines, accepts CRLF and skips comments", async () => {
    const frames = await collect(parseSse(body([bytes(": keep-alive\r\nevent: token\r\ndata: line one\r\ndata:line two\r\n\r\n")])));
    expect(frames).toEqual([{ event: "token", data: "line one\nline two" }]);
  });
});

const sse = (status: number, text: string, type = "text/event-stream") =>
  new Response(text, { status, headers: { "Content-Type": type } });

describe("streamAnswer", () => {
  it("yields typed events and ignores unknown event names", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        sse(
          200,
          'event: meta\ndata: {"conversation_id":"c1","message_id":"m1"}\n\n' +
            'event: heartbeat\ndata: {"x":1}\n\n' +
            'event: token\ndata: {"text":"Hi"}\n\n' +
            'event: done\ndata: {"usage":null,"abstained":true}\n\n',
        ),
      ),
    );
    const events = await collect(streamAnswer({ question: "q" }, new AbortController().signal));
    expect(events.map((e: AnswerEvent) => e.type)).toEqual(["meta", "token", "done"]);
    expect(events[2]).toEqual({ type: "done", data: { abstained: true } });
  });

  it("throws the pre-stream JSON error with its code (503 llm_not_configured)", async () => {
    const err = { error: { code: "llm_not_configured", message: "Set ANTHROPIC_API_KEY", request_id: "r1" } };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(sse(503, JSON.stringify(err), "application/json")));
    const run = collect(streamAnswer({ question: "q" }, new AbortController().signal));
    await expect(run).rejects.toBeInstanceOf(ApiError);
    await expect(run).rejects.toMatchObject({ status: 503, code: "llm_not_configured" });
  });

  it("sends a 401 (refresh already failed in the BFF) to sign-in instead of an inline error", async () => {
    const err = { error: { code: "unauthorized", message: "Sign in again", request_id: "r1" } };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(sse(401, JSON.stringify(err), "application/json")));
    const run = collect(streamAnswer({ question: "q" }, new AbortController().signal));
    await expect(run).rejects.toMatchObject({ status: 401 });
    expect(redirectToLogin).toHaveBeenCalledOnce();
  });

  it("does not redirect on other pre-stream errors", async () => {
    vi.mocked(redirectToLogin).mockClear();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(sse(500, "{}", "application/json")));
    await expect(collect(streamAnswer({ question: "q" }, new AbortController().signal))).rejects.toBeInstanceOf(ApiError);
    expect(redirectToLogin).not.toHaveBeenCalled();
  });

  it("rejects a malformed known event instead of rendering garbage", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(sse(200, "event: token\ndata: not json\n\n")));
    await expect(collect(streamAnswer({ question: "q" }, new AbortController().signal))).rejects.toMatchObject({
      code: "invalid_response",
    });
  });
});

describe("loginPath", () => {
  it("returns to the current chat, query included, through safeNext", () => {
    const path = loginPath({ pathname: "/chat/conv1", search: "?x=1" });
    expect(path).toBe("/login?next=%2Fchat%2Fconv1%3Fx%3D1");
    const origin = "http://localhost:3000";
    expect(safeNext(new URL(path, origin).searchParams.get("next"), origin)).toBe("/chat/conv1?x=1");
  });
});
