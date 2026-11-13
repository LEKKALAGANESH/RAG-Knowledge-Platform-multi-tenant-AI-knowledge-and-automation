"use client";

import { useCallback, useEffect, useState } from "react";
import { ApiError } from "@/lib/api/errors";

type State<T> =
  | { status: "loading" }
  | { status: "error"; error: ApiError }
  | { status: "ready"; data: T };

/** Loads once on mount; `reload` backs the Retry button and refreshes after mutations. */
export function useResource<T>(load: () => Promise<T>) {
  const [state, setState] = useState<State<T>>({ status: "loading" });
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let live = true;
    load().then(
      (data) => live && setState({ status: "ready", data }),
      (err: unknown) =>
        live &&
        setState({
          status: "error",
          error: err instanceof ApiError ? err : new ApiError(0, "unknown", "Something went wrong. Try again."),
        }),
    );
    return () => {
      live = false;
    };
    // `load` is recreated each render by callers; `attempt` is the explicit trigger.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [attempt]);

  const reload = useCallback(() => {
    setState({ status: "loading" });
    setAttempt((n) => n + 1);
  }, []);

  return { state, reload };
}
