// Minimal typed fetch client for the RAG platform API. Copy it into your app; there is no npm package.
// Auth: an API key (`rag_<prefix>_<secret>`) from Settings -> API keys, sent as a Bearer token.

export type Scope =
  | "kb:read" | "kb:write" | "documents:write" | "search" | "answer"
  | "agent:run" | "sources:read" | "sources:write" | "admin";

export interface KB { id: string; name: string; settings: Record<string, unknown>; created_at: string }
export interface Document {
  id: string; kb_id: string; name: string; mime: string;
  status: "queued" | "processing" | "ready" | "failed" | "deleted"; error: string | null;
}
export interface SearchResult {
  chunk_id: string; document_id: string; document_name: string; kb_id: string;
  text: string; loc: Record<string, unknown> | null; score: number;
}
export interface Citation {
  n: number; chunk_id: string; document_id: string; document_name: string; kb_id: string;
  loc: Record<string, unknown> | null; excerpt: string;
}
export type RunStatus = "planning" | "running" | "awaiting_approval" | "succeeded" | "failed" | "cancelled";
export interface Run {
  id: string; goal: string; status: RunStatus; error: string | null;
  result: { answer?: string; citations?: Citation[]; reports?: string[]; error_code?: string } | null;
}
export interface Step { n: number; kind: "plan" | "message" | "tool_call" | "tool_result"; tool: string | null; error: string | null }
export interface Approval { id: string; run_id: string; tool: string; input: Record<string, unknown>; status: "pending" | "approved" | "rejected" }

/** `/v1/answer` SSE events. */
export type AnswerEvent =
  | { event: "meta"; data: { conversation_id: string; message_id: string } }
  | { event: "token"; data: { text: string } }
  | { event: "citation"; data: Citation }
  | { event: "done"; data: { usage: Record<string, unknown> | null; abstained: boolean; budget_warning?: true } }
  | { event: "error"; data: { code: string; message: string } };

/** `/v1/agent/runs/{id}/events` SSE events. */
export type AgentEvent =
  | { event: "step" | "step_update"; data: Step }
  | { event: "approval"; data: Approval }
  | { event: "status"; data: Run }
  | { event: "end"; data: { status: RunStatus } }
  | { event: "timeout"; data: { after: number } };

/** Every non-2xx answer: `{"error": {code, message, request_id}}`. 429 carries Retry-After seconds. */
export class RagError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId: string | null;
  readonly retryAfter: number | null;
  constructor(status: number, code: string, message: string, requestId: string | null, retryAfter: number | null) {
    super(`${status} ${code}: ${message}`);
    this.status = status;
    this.code = code;
    this.requestId = requestId;
    this.retryAfter = retryAfter;
  }
}

export class Rag {
  readonly baseUrl: string;
  readonly #key: string;

  constructor(baseUrl: string, apiKey: string) {
    this.baseUrl = baseUrl.replace(/\/+$/, "");
    this.#key = apiKey;
  }

  /** Low-level call. Throws RagError on non-2xx. */
  async raw(path: string, init: RequestInit = {}): Promise<Response> {
    const headers = new Headers(init.headers);
    headers.set("Authorization", `Bearer ${this.#key}`);
    if (typeof init.body === "string") headers.set("Content-Type", "application/json");
    const res = await fetch(this.baseUrl + path, { ...init, headers });
    if (!res.ok) throw await toError(res);
    return res;
  }

  async json<T>(path: string, method = "GET", body?: unknown): Promise<T> {
    const init: RequestInit = { method };
    if (body !== undefined) init.body = JSON.stringify(body);
    const res = await this.raw(path, init);
    return (res.status === 204 ? undefined : await res.json()) as T;
  }

  search(query: string, kbIds?: string[], mode: "hybrid" | "vector" | "keyword" = "hybrid") {
    return this.json<{ results: SearchResult[] }>("/v1/search", "POST", { query, kb_ids: kbIds, mode });
  }

  async upload(kbId: string, file: Blob, filename: string): Promise<Document> {
    const form = new FormData();
    form.append("file", file, filename);
    return (await this.raw(`/v1/kbs/${kbId}/documents`, { method: "POST", body: form })).json() as Promise<Document>;
  }

  async waitUntilReady(docId: string, timeoutMs = 300_000): Promise<Document> {
    for (const end = Date.now() + timeoutMs; ; ) {
      const doc = await this.json<Document>(`/v1/documents/${docId}`);
      if (doc.status === "ready") return doc;
      if (doc.status === "failed" || doc.status === "deleted") throw new Error(`document ${doc.status}: ${doc.error ?? ""}`);
      if (Date.now() > end) throw new Error(`document still ${doc.status}`);
      await new Promise((r) => setTimeout(r, 1000));
    }
  }

  async *answer(question: string, kbIds?: string[], conversationId?: string): AsyncGenerator<AnswerEvent> {
    const body = JSON.stringify({ question, kb_ids: kbIds, conversation_id: conversationId });
    const res = await this.raw("/v1/answer", { method: "POST", body });
    yield* sse(res) as AsyncGenerator<AnswerEvent>;
  }

  /** Agent events; pass the last seen step `n` as `after` to resume after a drop or `timeout`. */
  async *agentEvents(runId: string, after = 0): AsyncGenerator<AgentEvent> {
    const res = await this.raw(`/v1/agent/runs/${runId}/events?after=${after}`);
    yield* sse(res) as AsyncGenerator<AgentEvent>;
  }
}

async function toError(res: Response): Promise<RagError> {
  let code = `http_${res.status}`;
  let message = res.statusText;
  let requestId = res.headers.get("X-Request-ID");
  try {
    const body = (await res.json()) as { error?: { code?: string; message?: string; request_id?: string } };
    code = body.error?.code ?? code;
    message = body.error?.message ?? message;
    requestId = body.error?.request_id ?? requestId;
  } catch {
    // not JSON (e.g. a proxy error page): keep the status-based code
  }
  const ra = Number(res.headers.get("Retry-After"));
  return new RagError(res.status, code, message, requestId, res.headers.has("Retry-After") && !Number.isNaN(ra) ? ra : null);
}

/** Parse `event:`/`data:` frames from a fetch body. Chunks may split lines and frames anywhere. */
export async function* sse(res: Response): AsyncGenerator<{ event: string; data: unknown }> {
  if (!res.body) return;
  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
  let buf = "";
  let event = "message";
  let data: string[] = [];
  for (;;) {
    const { value, done } = await reader.read();
    if (done) return; // a frame without its closing blank line is incomplete: dropped
    buf += value;
    const lines = buf.split(/\r?\n/);
    buf = lines.pop() ?? "";
    for (const line of lines) {
      if (line === "") {
        if (data.length) yield { event, data: JSON.parse(data.join("\n")) };
        event = "message";
        data = [];
      } else if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) data.push(line.slice(5).replace(/^ /, ""));
    }
  }
}

/** Reads RAG_BASE_URL (default http://localhost:8000) and RAG_API_KEY. */
export function fromEnv(): Rag {
  const key = process.env.RAG_API_KEY;
  if (!key) throw new Error("Set RAG_API_KEY (create one in the app: Settings -> API keys)");
  return new Rag(process.env.RAG_BASE_URL ?? "http://localhost:8000", key);
}
