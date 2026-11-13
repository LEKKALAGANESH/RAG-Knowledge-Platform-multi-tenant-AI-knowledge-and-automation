"use client";

import type { ReactNode } from "react";
import { CheckCircle2, X, XCircle } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

/** Result of an OAuth return: success is a polite status, a failure an alert. Text only, never HTML. */
export function OAuthNotice({ tone, message, children, onDismiss, className }: {
  tone: "success" | "error"; message: string; children?: ReactNode; onDismiss: () => void; className?: string;
}) {
  const ok = tone === "success";
  const Icon = ok ? CheckCircle2 : XCircle;
  return (
    <div
      role={ok ? "status" : "alert"}
      className={cn("flex items-start gap-2 rounded-md border px-3 py-2 text-sm", ok ? "border-success/30 bg-success/5" : "border-destructive/30 bg-destructive/5", className)}
    >
      <Icon className={cn("mt-0.5 size-4 shrink-0", ok ? "text-success" : "text-destructive")} aria-hidden />
      <div className="grid min-w-0 flex-1 gap-1 break-words">
        <p>{message}</p>
        {children && <div className="flex flex-wrap items-center gap-2 text-muted-foreground">{children}</div>}
      </div>
      <Button size="icon-sm" variant="ghost" className="-my-1 shrink-0" aria-label="Dismiss" onClick={onDismiss}>
        <X aria-hidden />
      </Button>
    </div>
  );
}
