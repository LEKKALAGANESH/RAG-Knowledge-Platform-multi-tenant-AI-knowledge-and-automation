"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { CircleAlert, TriangleAlert, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { onBudget, type BudgetSignal } from "@/lib/budget";
import { cn } from "@/lib/utils";

/** App-wide notice for 402 budget_exceeded and the warn-mode flag, whichever page triggered it. */
export function BudgetBanner({ canManage }: { canManage: boolean }) {
  const [signal, setSignal] = useState<BudgetSignal | null>(null);
  useEffect(() => onBudget(setSignal), []);
  if (!signal) return null;

  const exceeded = signal.kind === "exceeded";
  const Icon = exceeded ? CircleAlert : TriangleAlert;
  return (
    <div
      role={exceeded ? "alert" : "status"}
      className={cn(
        "flex items-start gap-3 border-b px-4 py-2.5 text-sm md:px-6",
        exceeded ? "border-destructive/30 bg-destructive/10" : "border-warning/40 bg-warning/15",
      )}
    >
      <Icon className={cn("mt-0.5 size-4 shrink-0", exceeded ? "text-destructive" : "text-warning")} aria-hidden />
      <p className="min-w-0 flex-1 break-words">
        {exceeded
          ? signal.message
          : "This organization is over its monthly AI budget. Requests still run because the budget is set to warn."}{" "}
        {canManage ? (
          <Link href="/usage" className="font-medium text-primary underline-offset-4 hover:underline">
            Review usage and budget
          </Link>
        ) : (
          exceeded && <span className="text-muted-foreground">Ask an owner or admin to raise the budget.</span>
        )}
      </p>
      <Button variant="ghost" size="icon-sm" aria-label="Dismiss budget notice" onClick={() => setSignal(null)}>
        <X aria-hidden />
      </Button>
    </div>
  );
}
