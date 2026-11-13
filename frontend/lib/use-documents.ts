"use client";

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import type { KbDocument } from "@/lib/api/types";

export const POLL_MIN_MS = 1_000;
export const POLL_MAX_MS = 15_000;

type State =
  | { status: "loading" }
  | { status: "error"; error: ApiError }
  // `refreshError`: a later poll failed; the last good list stays on screen.
  | { status: "ready"; docs: KbDocument[]; refreshError: ApiError | null };

const isSettled = (d: KbDocument) => d.status !== "queued" && d.status !== "processing";

function toApiError(err: unknown): ApiError {
  return err instanceof ApiError ? err : new ApiError(0, "unknown", "Something went wrong. Try again.");
}

/**
 * Lists a KB's documents and keeps polling, with exponential backoff, only while any of them is
 * queued or processing. `refresh` restarts at the fastest interval (after an upload or reprocess).
 */
export function useDocuments(kbId: string) {
  const [state, setState] = useState<State>({ status: "loading" });
  const [generation, setGeneration] = useState(0);

  useEffect(() => {
    let live = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let delay = POLL_MIN_MS;

    async function poll() {
      try {
        const { items } = await api.listDocuments(kbId);
        if (!live) return;
        setState({ status: "ready", docs: items, refreshError: null });
        if (items.every(isSettled)) return;
        timer = setTimeout(poll, delay);
        delay = Math.min(delay * 2, POLL_MAX_MS);
      } catch (err) {
        if (!live) return;
        const error = toApiError(err);
        setState((s) => (s.status === "ready" ? { ...s, refreshError: error } : { status: "error", error }));
      }
    }
    void poll();
    return () => {
      live = false;
      clearTimeout(timer);
    };
  }, [kbId, generation]);

  const refresh = useCallback(() => setGeneration((n) => n + 1), []);
  return { state, refresh };
}
