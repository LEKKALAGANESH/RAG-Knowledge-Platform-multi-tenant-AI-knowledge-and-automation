"use client";

import type { ReactNode } from "react";
import { AlertTriangle, RotateCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { rateLimitMessage, type ApiError } from "@/lib/api/errors";
import { useRetryCountdown } from "@/lib/use-retry-countdown";

/** Retry button that stays disabled while a 429's Retry-After runs; shows the live countdown above it. */
export function ErrorState({ error, onRetry }: { error: ApiError; onRetry: () => void }) {
  const wait = useRetryCountdown(error.retryAt);
  const limited = error.retryAt !== undefined;
  return (
    <div role="alert" className="flex flex-col items-start gap-3 rounded-lg border border-destructive/30 bg-destructive/5 p-6">
      <div className="flex items-center gap-2 font-medium text-destructive">
        <AlertTriangle className="size-4" aria-hidden />
        <span aria-live={limited ? "polite" : undefined}>{limited ? rateLimitMessage(wait) : error.message}</span>
      </div>
      {error.requestId && (
        <p className="text-xs text-muted-foreground">
          Request ID <code className="font-mono">{error.requestId}</code>
        </p>
      )}
      <Button variant="outline" onClick={onRetry} disabled={wait > 0}>
        <RotateCw aria-hidden />
        Try again
      </Button>
    </div>
  );
}

export function EmptyState({ title, body, action }: { title: string; body: string; action?: ReactNode }) {
  return (
    <div className="flex flex-col items-center rounded-lg border border-dashed px-6 py-14 text-center">
      <h2 className="text-base font-semibold">{title}</h2>
      <p className="mt-1 max-w-sm text-sm text-muted-foreground">{body}</p>
      {action && <div className="mt-5">{action}</div>}
    </div>
  );
}
