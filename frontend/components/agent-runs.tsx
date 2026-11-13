"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";
import { Bot, Loader2, Play } from "lucide-react";
import { AgentStatusPill } from "@/components/agent-steps";
import { KbPicker } from "@/components/kb-picker";
import { EmptyState, ErrorState } from "@/components/states";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { DEFAULT_MAX_STEPS, isActive, MAX_STEPS_LIMIT } from "@/lib/agent";
import { api } from "@/lib/api/client";
import { ApiError, rateLimitMessage } from "@/lib/api/errors";
import type { AgentRun, KnowledgeBase } from "@/lib/api/types";
import { formatDateTime } from "@/lib/format";
import { usePoll } from "@/lib/use-poll";
import { useResource } from "@/lib/use-resource";
import { useRetryCountdown } from "@/lib/use-retry-countdown";

const GOAL_MAX = 4000;

function NewRunForm({ kbs }: { kbs: KnowledgeBase[] }) {
  const router = useRouter();
  const [goal, setGoal] = useState("");
  const [kbIds, setKbIds] = useState<string[]>([]);
  const [maxSteps, setMaxSteps] = useState(String(DEFAULT_MAX_STEPS));
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const wait = useRetryCountdown(error?.retryAt);
  const steps = Number(maxSteps);
  const stepsValid = Number.isInteger(steps) && steps >= 1 && steps <= MAX_STEPS_LIMIT;

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!goal.trim() || !stepsValid || wait > 0) return;
    setPending(true);
    setError(null);
    try {
      const run = await api.createAgentRun({ goal: goal.trim(), kb_ids: kbIds.length ? kbIds : undefined, max_steps: steps });
      router.push(`/agent/${encodeURIComponent(run.id)}`);
    } catch (err) {
      setError(err instanceof ApiError ? err : new ApiError(0, "unknown", "Something went wrong. Try again."));
      setPending(false);
    }
  }

  return (
    <form onSubmit={submit} className="grid gap-4 rounded-lg border bg-card p-4 md:p-5" aria-busy={pending}>
      <div className="grid gap-1.5">
        <Label htmlFor="agent-goal">What should the agent do?</Label>
        <textarea
          id="agent-goal"
          required
          rows={3}
          maxLength={GOAL_MAX}
          value={goal}
          onChange={(e) => setGoal(e.target.value)}
          placeholder="Find all invoices from 2025, total them by currency, compare with 2024 and create an Excel report."
          className="min-h-20 w-full resize-y rounded-md border bg-transparent px-3 py-2 text-sm outline-none field-sizing-content placeholder:text-muted-foreground focus-visible:ring-2 focus-visible:ring-ring/50"
        />
        <p className="text-[13px] text-muted-foreground">
          The agent searches your sources, calculates and writes reports. Anything that deletes data waits for your approval.
        </p>
      </div>
      <div className="flex flex-wrap items-end gap-3">
        {kbs.length > 0 && (
          <div className="grid min-w-0 gap-1.5">
            <span className="text-sm font-medium">Sources</span>
            <KbPicker kbs={kbs} value={kbIds} onChange={setKbIds} disabled={pending} />
          </div>
        )}
        <div className="grid w-28 gap-1.5">
          <Label htmlFor="agent-max-steps">Max tool calls</Label>
          <Input
            id="agent-max-steps"
            type="number"
            inputMode="numeric"
            min={1}
            max={MAX_STEPS_LIMIT}
            value={maxSteps}
            onChange={(e) => setMaxSteps(e.target.value)}
            aria-invalid={!stepsValid}
            aria-describedby="agent-max-steps-hint"
            className="pointer-coarse:h-11"
          />
        </div>
        <Button type="submit" disabled={pending || !goal.trim() || !stepsValid || wait > 0} className="ml-auto pointer-coarse:h-11">
          {pending ? <Loader2 className="animate-spin" aria-hidden /> : <Play aria-hidden />}
          Start run
        </Button>
      </div>
      <p id="agent-max-steps-hint" className={stepsValid ? "sr-only" : "text-[13px] text-destructive"}>
        Max tool calls must be a whole number from 1 to {MAX_STEPS_LIMIT}.
      </p>
      {error && (
        <p role="alert" className="rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-sm text-destructive">
          {error.retryAt !== undefined ? rateLimitMessage(wait) : error.message}
        </p>
      )}
    </form>
  );
}

function RunList({ runs }: { runs: AgentRun[] }) {
  if (runs.length === 0) {
    return <EmptyState title="No runs yet" body="Describe a goal above and start a run. Its steps, approvals and reports appear here." />;
  }
  return (
    <ul aria-label="Agent runs" className="divide-y rounded-lg border">
      {runs.map((r) => (
        <li key={r.id}>
          <Link
            href={`/agent/${encodeURIComponent(r.id)}`}
            className="grid gap-1.5 px-4 py-3 outline-none hover:bg-muted/50 focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center sm:gap-4"
          >
            <span className="line-clamp-2 text-sm font-medium break-words">{r.goal}</span>
            <span className="flex items-center gap-3 text-xs text-muted-foreground sm:justify-end">
              <AgentStatusPill status={r.status} />
              <time dateTime={r.created_at} className="whitespace-nowrap">{formatDateTime(r.created_at)}</time>
            </span>
          </Link>
        </li>
      ))}
    </ul>
  );
}

function Runs() {
  const { state, refresh } = usePoll("agent-runs", () => api.listAgentRuns().then((r) => r.items), (items) => items.some((r) => isActive(r.status)));
  if (state.status === "loading") return <Skeleton aria-label="Loading runs" className="h-40 w-full" />;
  if (state.status === "error") return <ErrorState error={state.error} onRetry={refresh} />;
  return <RunList runs={state.data} />;
}

export function AgentRunsSkeleton() {
  return (
    <div aria-busy="true" aria-label="Loading agent workflows" className="grid gap-6">
      <Skeleton className="h-8 w-56" />
      <Skeleton className="h-44 w-full" />
      <Skeleton className="h-40 w-full" />
    </div>
  );
}

export function AgentRuns() {
  const kbs = useResource(api.listKbs);
  return (
    <section className="mx-auto grid w-full max-w-[960px] gap-6">
      <header className="flex items-center gap-3">
        <Bot className="size-6 text-primary" aria-hidden />
        <h1 className="text-2xl font-semibold tracking-tight">Agent workflows</h1>
      </header>
      {kbs.state.status === "loading" ? (
        <Skeleton aria-label="Loading form" className="h-44 w-full" />
      ) : kbs.state.status === "error" ? (
        <ErrorState error={kbs.state.error} onRetry={kbs.reload} />
      ) : (
        <NewRunForm kbs={kbs.state.data.items} />
      )}
      <div className="grid gap-3">
        <h2 className="text-base font-semibold">Recent runs</h2>
        <Runs />
      </div>
    </section>
  );
}
