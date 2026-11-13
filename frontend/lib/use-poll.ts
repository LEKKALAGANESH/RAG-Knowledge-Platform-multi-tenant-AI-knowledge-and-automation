"use client";

import { useCallback, useEffect, useState } from "react";
import { ApiError } from "@/lib/api/errors";

export const POLL_MIN_MS = 1_000;
export const POLL_MAX_MS = 10_000;

type State<T> =
  | { status: "loading" }
  | { status: "error"; error: ApiError }
  // `refreshError`: a later poll failed; the last good data stays on screen.
  | { status: "ready"; data: T; refreshError: ApiError | null };

/**
 * Loads `load()` and polls it with exponential backoff while `busy(data)` is true.
 * `refresh` reloads now and restarts at the fastest interval (after the user starts a sync).
 */
export function usePoll<T>(key: string, load: () => Promise<T>, busy: (data: T) => boolean) {
  const [state, setState] = useState<State<T>>({ status: "loading" });
  const [generation, setGeneration] = useState(0);

  useEffect(() => {
    let live = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let delay = POLL_MIN_MS;
    async function poll() {
      try {
        const data = await load();
        if (!live) return;
        setState({ status: "ready", data, refreshError: null });
        if (!busy(data)) return;
        timer = setTimeout(poll, delay);
        delay = Math.min(delay * 2, POLL_MAX_MS);
      } catch (err) {
        if (!live) return;
        const error = err instanceof ApiError ? err : new ApiError(0, "unknown", "Something went wrong. Try again.");
        setState((s) => (s.status === "ready" ? { ...s, refreshError: error } : { status: "error", error }));
      }
    }
    void poll();
    return () => {
      live = false;
      clearTimeout(timer);
    };
    // `load`/`busy` are recreated by callers each render; `key` and `generation` are the triggers.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, generation]);

  const refresh = useCallback(() => setGeneration((n) => n + 1), []);
  return { state, refresh };
}
