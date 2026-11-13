import { ACTIVE_AGENT_STATUSES, agentResult, type AgentResult, type AgentRunDetail, type AgentStatus, type AgentStep, type Approval, type Citation } from "@/lib/api/types";

export const isActive = (s: AgentStatus) => ACTIVE_AGENT_STATUSES.includes(s);

export const MAX_STEPS_LIMIT = 25;
export const DEFAULT_MAX_STEPS = 12;

/** `step` and `step_update` both replace by n, so replays after a reconnect never duplicate a step. */
export function mergeStep(run: AgentRunDetail, step: AgentStep): AgentRunDetail {
  const rest = run.steps.filter((s) => s.n !== step.n);
  return { ...run, steps: [...rest, step].sort((a, b) => a.n - b.n) };
}

/** Approvals are re-sent on every reconnect and again when decided: replace by id. */
export function mergeApproval(run: AgentRunDetail, approval: Approval): AgentRunDetail {
  const i = run.approvals.findIndex((a) => a.id === approval.id);
  return { ...run, approvals: i < 0 ? [...run.approvals, approval] : run.approvals.with(i, approval) };
}

export const lastStepN = (steps: AgentStep[]) => steps.reduce((m, s) => Math.max(m, s.n), 0);

export const toolUseId = (s: AgentStep): string | null => {
  const id = s.input?.tool_use_id;
  return typeof id === "string" ? id : null;
};

/** Text blocks of a model turn (thinking and tool_use blocks are shown elsewhere or not at all). */
export function turnText(step: AgentStep): string {
  const content = step.output?.content;
  if (!Array.isArray(content)) return "";
  return content
    .map((b: unknown) => (b && typeof b === "object" && "type" in b && b.type === "text" && "text" in b && typeof b.text === "string" ? b.text : ""))
    .filter(Boolean)
    .join("\n\n");
}

const CHUNK_MARKER = /\[chunk:([^\]\s]+)\]/gi;

/**
 * Rewrites `[chunk:<id>]` to the chat's `[cN]` form so the chat chips and side panel render it.
 * Numbers follow `result.citations` order; a marker with no citation is dropped (the server already strips them).
 */
export function toCited(result: AgentResult): { text: string; citations: Citation[] } {
  const citations: Citation[] = result.citations.map((c, i) => ({
    n: i + 1,
    chunk_id: c.chunk_id,
    document_id: c.document_id ?? "", // empty = no KB link (older result, or the chunk was deleted)
    kb_id: c.kb_id ?? undefined,
    document_name: c.document_name,
    loc: c.loc,
    excerpt: c.excerpt ?? "This run did not record the passage text.",
  }));
  const byChunk = new Map(citations.map((c) => [c.chunk_id.toLowerCase(), c.n]));
  const text = result.answer.replace(CHUNK_MARKER, (_, id: string) => {
    const n = byChunk.get(id.toLowerCase());
    return n ? `[c${n}]` : "";
  });
  return { text, citations };
}

export function parseResult(result: AgentRunDetail["result"]): AgentResult | null {
  const parsed = agentResult.safeParse(result);
  return parsed.success ? parsed.data : null;
}

export function errorCode(result: AgentRunDetail["result"]): string | null {
  const code = result?.error_code;
  return typeof code === "string" ? code : null;
}
