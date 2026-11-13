import { Loader2 } from "lucide-react";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { DocumentStatus } from "@/lib/api/types";
import { cn } from "@/lib/utils";

// Measured label-on-tint contrast (12px text needs 4.5:1): success 5.0 light / 6.5 dark, so "Ready" takes
// the token color. Destructive is 5.5 light but 4.2 dark, so "Failed" keeps foreground; color stays in tint + dot.
const STYLE: Record<DocumentStatus, { label: string; className: string; dot: string }> = {
  queued: { label: "Queued", className: "bg-muted text-muted-foreground", dot: "bg-muted-foreground" },
  processing: { label: "Processing", className: "bg-primary/10 text-foreground", dot: "" },
  ready: { label: "Ready", className: "bg-success/12 text-success", dot: "bg-success" },
  failed: { label: "Failed", className: "bg-destructive/10 text-foreground", dot: "bg-destructive" },
  deleted: { label: "Deleted", className: "bg-muted text-muted-foreground line-through", dot: "bg-muted-foreground" },
};

export function StatusPill({ status, reason }: { status: DocumentStatus; reason?: string | null }) {
  const { label, className, dot } = STYLE[status];
  const pill = (
    <span className={cn("inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium", className)}>
      {status === "processing" ? (
        <Loader2 className="size-3 text-primary motion-safe:animate-spin" aria-hidden />
      ) : (
        <span className={cn("size-1.5 rounded-full", dot)} aria-hidden />
      )}
      {label}
    </span>
  );
  // `reason` omitted = the caller shows the error itself, so no tooltip button.
  if (status !== "failed" || reason === undefined) return pill;
  const why = reason || "No reason was recorded.";
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button type="button" className="rounded-full focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring" aria-label={`Failed: ${why}`}>
          {pill}
        </button>
      </TooltipTrigger>
      <TooltipContent className="max-w-xs">{why}</TooltipContent>
    </Tooltip>
  );
}
