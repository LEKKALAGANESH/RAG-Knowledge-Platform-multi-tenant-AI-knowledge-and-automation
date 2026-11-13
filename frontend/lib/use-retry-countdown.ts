"use client";

import { useEffect, useState } from "react";
import { secondsLeft, wallClock } from "@/lib/api/errors";

/**
 * Seconds until `retryAt` (epoch ms from a 429's Retry-After), re-rendering once a second; 0 when none or elapsed.
 * Callers disable their retry action while it is > 0.
 */
export function useRetryCountdown(retryAt: number | undefined): number {
  const [, setTick] = useState(0);
  useEffect(() => {
    if (retryAt === undefined) return;
    const id = setInterval(() => {
      setTick((n) => n + 1);
      if (wallClock() >= retryAt) clearInterval(id);
    }, 1000);
    return () => clearInterval(id);
  }, [retryAt]);
  return retryAt === undefined ? 0 : secondsLeft(retryAt, wallClock());
}
