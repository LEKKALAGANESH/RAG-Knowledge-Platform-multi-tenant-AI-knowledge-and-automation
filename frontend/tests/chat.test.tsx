import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { Chat } from "@/components/chat";
import { CitedText } from "@/components/cited-text";
import type { Citation } from "@/lib/api/types";

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

const cite = (n: number, over: Partial<Citation> = {}): Citation => ({
  n,
  chunk_id: `ch${n}`,
  document_id: `doc${n}`,
  document_name: `policy-${n}.pdf`,
  kb_id: "kb1",
  loc: { page: 3 },
  excerpt: `Refunds are issued within 30 days (${n}).`,
  ...over,
});

const frame = (event: string, data: unknown) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;

/** A live SSE body the test feeds frame by frame; aborting the request errors it like a real fetch. */
function liveStream() {
  let ctrl!: ReadableStreamDefaultController<Uint8Array>;
  const stream = new ReadableStream<Uint8Array>({ start: (c) => void (ctrl = c) });
  return {
    respond: (init?: RequestInit) => {
      init?.signal?.addEventListener("abort", () => ctrl.error(new DOMException("Aborted", "AbortError")));
      return new Response(stream, { status: 200, headers: { "Content-Type": "text/event-stream" } });
    },
    push: (s: string) => act(async () => ctrl.enqueue(new TextEncoder().encode(s))),
    close: () => act(async () => ctrl.close()),
  };
}

type Handler = (init?: RequestInit) => Response | Promise<Response>;
const kb = (id: string, name: string) => ({ id, name, settings: {}, embedding_index_id: null, created_at: "2026-11-12T00:00:00Z" });
const KBS = { items: [kb("kb1", "Policies"), kb("kb2", "Contracts")], next_cursor: null };

function stubApi(answer: Handler, extra: Record<string, Handler> = {}) {
  const routes: Record<string, Handler> = {
    "/api/v1/kbs": () => json(200, KBS),
    "/api/v1/conversations?": () => json(200, { items: [] }),
    "/api/v1/documents/": () => json(200, { id: "doc1", kb_id: "kb1", source_id: null, name: "policy-1.pdf", mime: "application/pdf", sha256: "a", version: 1, status: "ready", error: null, warnings: [], meta: {}, created_at: "2026-11-12T00:00:00Z" }),
    ...extra,
    "/api/v1/answer": answer,
  };
  const fetch = vi.fn(async (url: string, init?: RequestInit) => {
    const key = Object.keys(routes).find((k) => url.startsWith(k));
    if (!key) throw new Error(`unexpected fetch ${url}`);
    return routes[key](init);
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

async function ask(question: string) {
  const box = await screen.findByLabelText("Question");
  await userEvent.type(box, `${question}{Enter}`);
}

beforeEach(() => {
  Element.prototype.scrollIntoView = vi.fn();
});

describe("CitedText", () => {
  it("turns [cN] into numbered chips, drops unknown markers and hides a split marker while streaming", async () => {
    const onOpen = vi.fn();
    const { container, rerender } = render(<CitedText text="Refunds take 30 days [c" citations={[cite(1)]} streaming onOpen={onOpen} />);
    expect(container.textContent).toBe("Refunds take 30 days ");
    rerender(<CitedText text="Refunds take 30 days [c1]. [c9]" citations={[cite(1)]} onOpen={onOpen} />);
    expect(container.textContent).toBe("Refunds take 30 days 1. ");
    await userEvent.click(screen.getByRole("button", { name: "Source 1: policy-1.pdf" }));
    expect(onOpen).toHaveBeenCalledWith(cite(1));
  });
});

describe("Chat", () => {
  it("streams tokens, renders a chip from a marker split across tokens, and opens the source panel", async () => {
    const s = liveStream();
    const fetch = stubApi(s.respond);
    render(<Chat conversationId={null} />);
    await ask("How long do refunds take?");
    expect(JSON.parse(String(fetch.mock.calls.find(([u]) => u === "/api/v1/answer")?.[1]?.body))).toEqual({ question: "How long do refunds take?" });
    expect(screen.getByLabelText("Question")).toBeDisabled();

    await s.push(frame("meta", { conversation_id: "conv1", message_id: "m1" }));
    await s.push(frame("token", { text: "Refunds take 30 days [c" }));
    expect(screen.getByText(/Refunds take 30 days/).textContent).not.toContain("[c");
    await s.push(frame("citation", cite(1)) + frame("token", { text: "1]." }));
    await s.push(frame("done", { usage: null, abstained: false }));
    await s.close();

    expect(window.location.pathname).toBe("/chat/conv1");
    expect(screen.getByLabelText("Question")).toBeEnabled();
    await userEvent.click(screen.getByRole("button", { name: "Source 1: policy-1.pdf" }));
    const panel = screen.getByRole("dialog");
    expect(within(panel).getByText("Page 3")).toBeVisible();
    expect(within(panel).getByText("Refunds are issued within 30 days (1).")).toBeVisible();
    expect(within(panel).getByRole("link", { name: /Open document/ })).toHaveAttribute("href", "/kbs/kb1?doc=doc1");
    expect(fetch.mock.calls.filter(([u]) => String(u).startsWith("/api/v1/documents/"))).toHaveLength(0); // kb_id came with the citation
  });

  it("falls back to a document lookup for an old stored citation without kb_id", async () => {
    const old = { ...cite(1), kb_id: undefined };
    const fetch = stubApi(() => json(500, {}), {
      "/api/v1/conversations/conv1": () =>
        json(200, {
          id: "conv1", title: "t", kb_ids: [], created_at: "2026-11-12T00:00:00Z",
          messages: [{ id: "a1", role: "assistant", content: "Thirty days [c1].", citations: [old], usage: null, created_at: "2026-11-12T00:00:01Z" }],
        }),
    });
    render(<Chat conversationId="conv1" />);
    await userEvent.click(await screen.findByRole("button", { name: "Source 1: policy-1.pdf" }));
    expect(await within(screen.getByRole("dialog")).findByRole("link", { name: /Open document/ })).toHaveAttribute("href", "/kbs/kb1?doc=doc1");
    expect(fetch.mock.calls.filter(([u]) => String(u).startsWith("/api/v1/documents/doc1"))).toHaveLength(1);
  });

  it("styles an abstained answer as not found", async () => {
    const s = liveStream();
    stubApi(s.respond);
    render(<Chat conversationId={null} />);
    await ask("What is the moon made of?");
    await s.push(frame("meta", { conversation_id: "c2", message_id: "m2" }) + frame("token", { text: "I couldn't find this in your sources." }));
    await s.push(frame("done", { usage: null, abstained: true }));
    expect(screen.getByText("Not found in your sources")).toBeVisible();
  });

  it("Stop aborts the request and re-enables the composer", async () => {
    const s = liveStream();
    stubApi(s.respond);
    render(<Chat conversationId={null} />);
    await ask("Long question");
    await s.push(frame("token", { text: "Partial" }));
    await userEvent.click(screen.getByRole("button", { name: "Stop" }));
    expect(await screen.findByText(/Stopped/)).toBeVisible();
    expect(screen.getByText("Partial")).toBeVisible();
    expect(screen.getByLabelText("Question")).toBeEnabled();
  });

  it("shows a mid-stream error event inline and retries the same question", async () => {
    const first = liveStream();
    const second = liveStream();
    const answers = [first, second];
    const fetch = stubApi((init) => answers.shift()!.respond(init));
    render(<Chat conversationId={null} />);
    await ask("Retry me");
    await first.push(frame("error", { code: "provider_error", message: "The model provider is overloaded. Retry shortly." }));
    await first.close();
    expect(screen.getByRole("alert")).toHaveTextContent("The model provider is overloaded. Retry shortly.");
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(fetch.mock.calls.filter(([u]) => u === "/api/v1/answer")).toHaveLength(2);
    expect(screen.getAllByText("Retry me")).toHaveLength(1);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("explains a 503 llm_not_configured to the admin instead of a generic error", async () => {
    stubApi(() => json(503, { error: { code: "llm_not_configured", message: "Set ANTHROPIC_API_KEY", request_id: "r" } }));
    render(<Chat conversationId={null} />);
    await ask("Anything");
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Chat needs a language model");
    expect(alert).toHaveTextContent("ANTHROPIC_API_KEY");
    expect(within(alert).getByRole("link", { name: "Search your sources instead" })).toHaveAttribute("href", "/search");
  });

  it("loads a stored conversation with its citations and lists history", async () => {
    stubApi(() => json(500, {}), {
      "/api/v1/conversations?": () => json(200, { items: [{ id: "conv1", title: "Refund window", kb_ids: [], created_at: "2026-11-12T00:00:00Z" }] }),
      "/api/v1/conversations/conv1": () =>
        json(200, {
          id: "conv1",
          title: "Refund window",
          kb_ids: ["kb1"],
          created_at: "2026-11-12T00:00:00Z",
          messages: [
            { id: "u1", role: "user", content: "Refund window?", citations: [], usage: null, created_at: "2026-11-12T00:00:00Z" },
            { id: "a1", role: "assistant", content: "Thirty days [c1].", citations: [cite(1)], usage: null, created_at: "2026-11-12T00:00:01Z" },
            { id: "u2", role: "user", content: "Moon?", citations: [], usage: null, created_at: "2026-11-12T00:00:02Z" },
            { id: "a2", role: "assistant", content: "I couldn't find this in your sources.", citations: [], usage: null, created_at: "2026-11-12T00:00:03Z" }, // old row: inferred
            { id: "u3", role: "user", content: "Hi?", citations: [], usage: null, created_at: "2026-11-12T00:00:04Z" },
            // Stored flag wins over "no citations": a plain answer is not a not-found.
            { id: "a3", role: "assistant", content: "Hello there.", citations: [], usage: { abstained: false }, abstained: false, created_at: "2026-11-12T00:00:05Z" },
            { id: "u4", role: "user", content: "Moon again?", citations: [], usage: null, created_at: "2026-11-12T00:00:06Z" },
            { id: "a4", role: "assistant", content: "Nothing on that.", citations: [], usage: { abstained: true }, abstained: true, created_at: "2026-11-12T00:00:07Z" },
          ],
        }),
    });
    render(<Chat conversationId="conv1" />);
    expect(await screen.findByText("Refund window?")).toBeVisible();
    expect(screen.getByRole("button", { name: "Source 1: policy-1.pdf" })).toBeVisible();
    expect(screen.getAllByText("Not found in your sources")).toHaveLength(2); // a2 (inferred) + a4 (stored)
    expect(screen.getByText("Hello there.")).toBeVisible();
    expect(screen.getByText("Nothing on that.")).toBeVisible();
    expect(screen.getByRole("button", { name: "Policies" })).toBeVisible(); // KB selection restored from the conversation
    const history = screen.getByRole("complementary", { name: "Conversation history" });
    expect(await within(history).findByRole("link", { name: "Refund window" })).toHaveAttribute("aria-current", "page");
  });

  it("shows not-found for an unknown conversation", async () => {
    stubApi(() => json(500, {}), {
      "/api/v1/conversations/missing": () => json(404, { error: { code: "not_found", message: "Conversation not found" } }),
    });
    render(<Chat conversationId="missing" />);
    expect(await screen.findByText("Conversation not found")).toBeVisible();
  });
});
