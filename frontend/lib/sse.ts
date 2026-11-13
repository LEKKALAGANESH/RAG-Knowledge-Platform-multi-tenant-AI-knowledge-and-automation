import { z } from "zod";
import { redirectToLogin } from "@/lib/api/client";
import { ApiError, networkError, toApiError } from "@/lib/api/errors";
import { BUDGET_EXCEEDED, reportBudget } from "@/lib/budget";
import { agentRun, agentStatus, agentStep, approval, citation } from "@/lib/api/types";

export type SseFrame = { event: string; data: string };

/** Minimal text/event-stream parser: frames may be split anywhere across chunks; `data:` lines join with "\n". */
export async function* parseSse(body: ReadableStream<Uint8Array>): AsyncGenerator<SseFrame> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  let event = "message";
  let data: string[] = [];
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) return;
      buf += decoder.decode(value, { stream: true }); // stream: a UTF-8 char may straddle two chunks
      let nl: number;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, nl).replace(/\r$/, "");
        buf = buf.slice(nl + 1);
        if (line === "") {
          if (data.length) yield { event, data: data.join("\n") };
          event = "message";
          data = [];
        } else if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) data.push(line.slice(5).replace(/^ /, ""));
      }
    }
  } finally {
    reader.releaseLock();
  }
}

/**
 * Opens an SSE response through the BFF. Null when the caller aborted. Pre-stream failures throw ApiError
 * (429 carries retryAt); a 401 means the BFF's refresh already failed, so go to sign-in.
 */
async function openStream(url: string, init: RequestInit, signal: AbortSignal): Promise<ReadableStream<Uint8Array> | null> {
  let res: Response;
  try {
    res = await fetch(url, { ...init, signal });
  } catch (err) {
    if (signal.aborted) return null;
    throw err instanceof ApiError ? err : networkError();
  }
  if (!res.ok || !res.body) {
    const err = await toApiError(res);
    if (res.status === 401) redirectToLogin();
    throw err;
  }
  return res.body;
}

function parseData<T extends z.ZodType>(schema: T, data: string): z.infer<T> {
  let json: unknown;
  try {
    json = JSON.parse(data);
  } catch {
    json = null;
  }
  const parsed = schema.safeParse(json);
  if (!parsed.success) throw new ApiError(0, "invalid_response", "The server sent an unexpected response. Try again.");
  return parsed.data;
}

const events = {
  meta: z.object({ conversation_id: z.string(), message_id: z.string() }),
  token: z.object({ text: z.string() }),
  citation,
  done: z.object({ abstained: z.boolean(), budget_warning: z.boolean().optional() }),
  error: z.object({ code: z.string(), message: z.string() }),
};

export type AnswerEvent = {
  [K in keyof typeof events]: { type: K; data: z.infer<(typeof events)[K]> };
}[keyof typeof events];

export type AnswerRequest = { question: string; kb_ids?: string[]; conversation_id?: string };

/**
 * POSTs /v1/answer through the BFF and yields typed events. Pre-stream failures throw ApiError
 * (e.g. 503 llm_not_configured); unknown event names are skipped so the server can add new ones.
 * A 401 means the BFF's refresh already failed: go to sign-in instead of an inline dead end.
 */
export async function* streamAnswer(body: AnswerRequest, signal: AbortSignal): AsyncGenerator<AnswerEvent> {
  const res = await openStream(
    "/api/v1/answer",
    { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) },
    signal,
  );
  if (!res) return;
  for await (const frame of parseSse(res)) {
    if (!Object.hasOwn(events, frame.event)) continue;
    const type = frame.event as keyof typeof events;
    const ev = { type, data: parseData(events[type], frame.data) } as AnswerEvent;
    if (ev.type === "done" && ev.data.budget_warning) reportBudget({ kind: "warning" });
    if (ev.type === "error" && ev.data.code === BUDGET_EXCEEDED) reportBudget({ kind: "exceeded", message: ev.data.message });
    yield ev;
  }
}

const agentEvents = {
  step: agentStep,
  step_update: agentStep, // a step already sent changed (tool_call started/finished); replace by n
  approval, // created (pending) or decided; replace by id
  status: agentRun,
  end: z.object({ status: agentStatus }),
  timeout: z.object({ after: z.number() }),
};

export type AgentEvent = {
  [K in keyof typeof agentEvents]: { type: K; data: z.infer<(typeof agentEvents)[K]> };
}[keyof typeof agentEvents];

/**
 * GET /v1/agent/runs/{id}/events?after=n: replays steps with n > after, then live ones. On every (re)connect the
 * server re-sends tool_call steps with n <= after as `step_update` and every approval as `approval`.
 */
export async function* streamAgentEvents(runId: string, after: number, signal: AbortSignal): AsyncGenerator<AgentEvent> {
  const body = await openStream(`/api/v1/agent/runs/${encodeURIComponent(runId)}/events?after=${after}`, {}, signal);
  if (!body) return;
  for await (const frame of parseSse(body)) {
    if (!Object.hasOwn(agentEvents, frame.event)) continue;
    const type = frame.event as keyof typeof agentEvents;
    yield { type, data: parseData(agentEvents[type], frame.data) } as AgentEvent;
  }
}
