"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { isActive, lastStepN, mergeApproval, mergeStep } from "@/lib/agent";
import { api } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import type { AgentRunDetail } from "@/lib/api/types";
import { streamAgentEvents } from "@/lib/sse";

export type Connection = "live" | "reconnecting" | "ended" | "lost";

export const MAX_RECONNECTS = 8;
const backoff = (failures: number) => Math.min(1000 * 2 ** (failures - 1), 10_000);

const sleep = (ms: number, signal: AbortSignal) =>
  new Promise<void>((resolve) => {
    const t = setTimeout(resolve, Math.max(0, ms));
    signal.addEventListener("abort", () => (clearTimeout(t), resolve()), { once: true });
  });

const asApiError = (err: unknown) =>
  err instanceof ApiError ? err : new ApiError(0, "unknown", "Something went wrong. Try again.");

/**
 * Follows a run over SSE. Reconnects with `?after=<last step n>` on the server's 15 min `timeout`
 * (immediately) and on network drops (backoff, Retry-After on 429); stops on `end`.
 * Steps, step updates, approvals and the final result all arrive as events, so nothing is re-read on `end`.
 * Only a server that predates the `approval` event pauses with no pending approval known: then the detail is re-read.
 */
export function useAgentRun(initial: AgentRunDetail) {
  const [run, setRun] = useState(initial);
  const [connection, setConnection] = useState<Connection>(isActive(initial.status) ? "live" : "ended");
  const [lostError, setLostError] = useState<ApiError | null>(null);
  const [attempt, setAttempt] = useState(0);
  const runRef = useRef(run);
  useEffect(() => {
    runRef.current = run;
  });
  const id = initial.id;

  const refresh = useCallback(async () => {
    const detail = await api.getAgentRun(id);
    setRun(detail);
    return detail;
  }, [id]);

  useEffect(() => {
    if (!isActive(runRef.current.status)) return;
    const ctrl = new AbortController();
    const { signal } = ctrl;
    async function follow() {
      let after = lastStepN(runRef.current.steps);
      let failures = 0;
      // Mirrors run.approvals for the legacy check below (state updates land after this loop reads them).
      const pending = new Set(runRef.current.approvals.filter((a) => a.status === "pending").map((a) => a.id));
      while (!signal.aborted) {
        let outcome: "end" | "timeout" | "dropped" = "dropped";
        try {
          for await (const ev of streamAgentEvents(id, after, signal)) {
            failures = 0;
            setConnection("live");
            if (ev.type === "step" || ev.type === "step_update") {
              const step = ev.data;
              if (ev.type === "step") after = Math.max(after, step.n);
              setRun((r) => mergeStep(r, step));
            } else if (ev.type === "approval") {
              const approval = ev.data;
              if (approval.status === "pending") pending.add(approval.id);
              else pending.delete(approval.id);
              setRun((r) => mergeApproval(r, approval));
            } else if (ev.type === "status") {
              setRun((r) => ({ ...r, ...ev.data }));
              if (ev.data.status === "awaiting_approval" && pending.size === 0) void refresh().catch(() => undefined);
            } else {
              if (ev.type === "timeout") after = Math.max(after, ev.data.after);
              outcome = ev.type;
              break;
            }
          }
        } catch (err) {
          if (signal.aborted) return;
          const e = asApiError(err);
          if (!e.retryable || ++failures > MAX_RECONNECTS) return lose(e);
          setConnection("reconnecting");
          await sleep(e.retryAt !== undefined ? e.retryAt - Date.now() : backoff(failures), signal);
          continue;
        }
        if (signal.aborted) return;
        if (outcome === "end") return setConnection("ended");
        if (outcome === "dropped") {
          if (++failures > MAX_RECONNECTS) return lose(new ApiError(0, "stream_lost", "Lost the live connection to this run."));
          setConnection("reconnecting");
          await sleep(backoff(failures), signal);
        }
      }
    }
    function lose(e: ApiError) {
      setLostError(e);
      setConnection("lost");
    }
    void follow();
    return () => ctrl.abort();
  }, [id, attempt, refresh]);

  /** Manual "Reconnect" after the stream was given up on. */
  const reconnect = useCallback(() => {
    setLostError(null);
    setConnection("live");
    setAttempt((n) => n + 1);
  }, []);

  return { run, setRun, refresh, connection, lostError, reconnect };
}
