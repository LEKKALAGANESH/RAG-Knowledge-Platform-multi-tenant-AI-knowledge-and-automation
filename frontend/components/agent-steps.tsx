import { CheckCircle2, CircleSlash, Eye, Hourglass, Loader2, ShieldAlert, TriangleAlert, Undo2, Wrench, type LucideIcon } from "lucide-react";
import { toolUseId, turnText } from "@/lib/agent";
import type { AgentStatus, AgentStep, Approval } from "@/lib/api/types";
import { cn } from "@/lib/utils";

const RISK: Record<string, { label: string; icon: LucideIcon; className: string }> = {
  read: { label: "Read only", icon: Eye, className: "bg-muted text-muted-foreground" },
  reversible_write: { label: "Reversible write", icon: Undo2, className: "bg-warning/12 text-foreground" },
  irreversible_write: { label: "Irreversible", icon: ShieldAlert, className: "bg-destructive/10 text-destructive" },
};

export function RiskBadge({ risk }: { risk: string | null }) {
  const r = RISK[risk ?? ""] ?? { label: risk ?? "Unknown risk", icon: ShieldAlert, className: "bg-muted text-muted-foreground" };
  const Icon = r.icon;
  return (
    <span className={cn("inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium", r.className)}>
      <Icon className="size-3" aria-hidden />
      {r.label}
    </span>
  );
}

const STATUS: Record<AgentStatus, { label: string; className: string }> = {
  planning: { label: "Planning", className: "bg-primary/10 text-foreground" },
  running: { label: "Running", className: "bg-primary/10 text-foreground" },
  awaiting_approval: { label: "Needs approval", className: "bg-warning/15 text-foreground" },
  succeeded: { label: "Succeeded", className: "bg-success/12 text-success" },
  failed: { label: "Failed", className: "bg-destructive/10 text-foreground" },
  cancelled: { label: "Cancelled", className: "bg-muted text-muted-foreground" },
};

export function AgentStatusPill({ status }: { status: AgentStatus }) {
  const { label, className } = STATUS[status];
  const busy = status === "planning" || status === "running";
  return (
    <span className={cn("inline-flex shrink-0 items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium whitespace-nowrap", className)}>
      {busy ? (
        <Loader2 className="size-3 text-primary motion-safe:animate-spin" aria-hidden />
      ) : (
        <span
          aria-hidden
          className={cn("size-1.5 rounded-full", {
            "bg-warning": status === "awaiting_approval",
            "bg-success": status === "succeeded",
            "bg-destructive": status === "failed",
            "bg-muted-foreground": status === "cancelled",
          })}
        />
      )}
      {label}
    </span>
  );
}

/** Tool inputs/outputs can hold whole chunk texts, so they start collapsed. */
export function JsonBlock({ label, value }: { label: string; value: unknown }) {
  return (
    <details className="group rounded-md border bg-muted/30 text-xs">
      <summary className="cursor-pointer rounded-md px-2.5 py-1.5 font-medium text-muted-foreground outline-none select-none hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring pointer-coarse:py-3">
        {label}
      </summary>
      <pre className="max-h-80 overflow-auto overscroll-contain border-t px-2.5 py-2 font-mono leading-relaxed break-words whitespace-pre-wrap">
        {JSON.stringify(value, null, 2)}
      </pre>
    </details>
  );
}

type CallState = { label: string; icon: LucideIcon; className: string; spin?: boolean };

function callState(result: AgentStep | undefined, approval: Approval | undefined, runActive: boolean): CallState {
  if (approval?.status === "rejected") return { label: "Rejected", icon: CircleSlash, className: "text-muted-foreground" };
  if (result?.error) return { label: "Failed", icon: TriangleAlert, className: "text-destructive" };
  if (result) return { label: approval ? "Approved, done" : "Done", icon: CheckCircle2, className: "text-success" };
  if (approval?.status === "pending") return { label: "Waiting for approval", icon: Hourglass, className: "text-foreground" };
  if (!runActive) return { label: "Not run", icon: CircleSlash, className: "text-muted-foreground" };
  return { label: "Running", icon: Loader2, className: "text-primary", spin: true };
}

function ToolCall({ call, result, approval, runActive }: { call: AgentStep; result?: AgentStep; approval?: Approval; runActive: boolean }) {
  const state = callState(result, approval, runActive);
  const Icon = state.icon;
  const error = result?.error ?? call.error;
  return (
    <div className="grid gap-2">
      <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1.5">
        <Wrench className="size-3.5 text-muted-foreground" aria-hidden />
        <code className="font-mono text-sm font-medium break-all">{call.tool ?? "unknown tool"}</code>
        <RiskBadge risk={call.risk} />
        <span className={cn("ml-auto inline-flex items-center gap-1 text-xs font-medium", state.className)}>
          <Icon className={cn("size-3.5", state.spin && "motion-safe:animate-spin")} aria-hidden />
          {state.label}
        </span>
      </div>
      {error && (
        <p role="note" className="flex items-start gap-1.5 text-sm break-words text-destructive">
          <TriangleAlert className="mt-0.5 size-3.5 shrink-0" aria-hidden />
          {error}
        </p>
      )}
      <JsonBlock label="Input" value={call.input?.args ?? call.input} />
      {result?.output && <JsonBlock label="Output" value={result.output.data ?? result.output} />}
    </div>
  );
}

function Turn({ step }: { step: AgentStep }) {
  const text = turnText(step);
  return (
    <div className="grid gap-1">
      <p className="text-xs font-medium text-muted-foreground">{step.kind === "plan" ? "Plan" : "Model update"}</p>
      {text ? (
        <p className="text-sm leading-relaxed break-words whitespace-pre-wrap">{text}</p>
      ) : (
        <p className="text-sm text-muted-foreground">Chose the next tool calls.</p>
      )}
      {step.error && <p className="text-sm break-words text-destructive">{step.error}</p>}
    </div>
  );
}

type TimelineProps = { steps: AgentStep[]; approvals: Approval[]; runActive: boolean };

/**
 * One row per model turn and tool call. A call's result is shown inside its call (matched by tool_use_id),
 * so the risk, state, input and output of one action read together. `step_update` keeps the call's own
 * started_at/finished_at current, but the outcome (output or error) is only written to the tool_result step.
 */
export function Timeline({ steps, approvals, runActive }: TimelineProps) {
  const results = new Map(steps.filter((s) => s.kind === "tool_result").map((s) => [toolUseId(s), s]));
  const callIds = new Set(steps.filter((s) => s.kind === "tool_call").map(toolUseId));
  const byStep = new Map(approvals.map((a) => [a.step_id, a]));
  const rows = steps.filter((s) => s.kind !== "tool_result" || !callIds.has(toolUseId(s)));
  return (
    <ol aria-label="Run steps" className="relative grid gap-0">
      {rows.map((s, i) => (
        <li key={s.id} className="relative grid grid-cols-[2rem_minmax(0,1fr)] gap-3 pb-5">
          {i < rows.length - 1 && <span aria-hidden className="absolute top-8 bottom-0 left-4 w-px -translate-x-1/2 bg-border" />}
          <span
            aria-hidden
            className={cn(
              "z-0 flex size-8 items-center justify-center rounded-full border bg-background font-mono text-xs tabular-nums",
              s.kind === "tool_call" && s.risk === "irreversible_write" && "border-destructive/50 text-destructive",
              s.kind === "tool_call" && s.risk === "reversible_write" && "border-warning/60",
            )}
          >
            {s.n}
          </span>
          <div className="min-w-0 pt-1">
            <span className="sr-only">Step {s.n}: </span>
            {s.kind === "tool_call" ? (
              <ToolCall call={s} result={results.get(toolUseId(s))} approval={byStep.get(s.id)} runActive={runActive} />
            ) : s.kind === "tool_result" ? (
              <div className="grid gap-2">
                <p className="text-xs font-medium text-muted-foreground">Result of {s.tool ?? "a tool"}</p>
                {s.error ? <p className="text-sm text-destructive">{s.error}</p> : <JsonBlock label="Output" value={s.output?.data ?? s.output} />}
              </div>
            ) : (
              <Turn step={s} />
            )}
          </div>
        </li>
      ))}
    </ol>
  );
}
