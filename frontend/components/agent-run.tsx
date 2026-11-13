"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useState } from "react";
import { Check, ChevronLeft, Download, Loader2, RotateCw, ShieldAlert, Square, WifiOff, X } from "lucide-react";
import { AgentStatusPill, JsonBlock, RiskBadge, Timeline } from "@/components/agent-steps";
import { NotConfigured } from "@/components/chat";
import { CitationPanel } from "@/components/citation-panel";
import { CitedText } from "@/components/cited-text";
import { ConfirmDialog } from "@/components/kb-sources";
import { EmptyState, ErrorState } from "@/components/states";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { errorCode, isActive, mergeApproval, parseResult, toCited } from "@/lib/agent";
import { api } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import type { AgentRunDetail, Approval, Citation } from "@/lib/api/types";
import { formatDateTime } from "@/lib/format";
import { useAgentRun, type Connection } from "@/lib/use-agent-run";
import { useResource } from "@/lib/use-resource";

const asApiError = (err: unknown) => (err instanceof ApiError ? err : new ApiError(0, "unknown", "Something went wrong. Try again."));

/** Short "key: value" lines so the decision can be made without opening the JSON. */
function summarize(input: Record<string, unknown>): [string, string][] {
  return Object.entries(input).map(([k, v]) => {
    const text = typeof v === "string" ? v : JSON.stringify(v);
    return [k, text.length > 140 ? `${text.slice(0, 140)}…` : text];
  });
}

/** `decided` = the approval the server returned; null after a 409 (someone else decided first). */
type ApprovalProps = { approval: Approval; risk: string | null; onDecided: (decided: Approval | null, notice: string | null) => Promise<void> };

function ApprovalCard({ approval, risk, onDecided }: ApprovalProps) {
  const [busy, setBusy] = useState<null | "approve" | "reject">(null);
  const [error, setError] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  // Unknown risk is treated like irreversible: the safer default asks twice.
  const needsConfirm = risk !== "read" && risk !== "reversible_write";

  async function decide(decision: "approve" | "reject") {
    setBusy(decision);
    setError(null);
    try {
      await onDecided(await api.decideApproval(approval.id, decision), null);
    } catch (err) {
      const e = asApiError(err);
      if (e.status === 409) await onDecided(null, "Someone already decided this request. The run shows the outcome.");
      else setError(e.message);
    } finally {
      setBusy(null);
    }
  }

  return (
    <section aria-labelledby={`approval-${approval.id}`} className="grid gap-3 rounded-lg border border-warning/50 bg-warning/5 p-4">
      <div className="flex flex-wrap items-center gap-2">
        <ShieldAlert className="size-4 text-warning" aria-hidden />
        <h2 id={`approval-${approval.id}`} className="text-sm font-semibold">
          The agent wants to run <code className="font-mono">{approval.tool}</code>
        </h2>
        <RiskBadge risk={risk} />
      </div>
      <dl className="grid gap-1 text-sm">
        {summarize(approval.input).map(([k, v]) => (
          <div key={k} className="grid gap-x-3 sm:grid-cols-[10rem_minmax(0,1fr)]">
            <dt className="font-mono text-xs text-muted-foreground">{k}</dt>
            <dd className="font-mono text-xs break-all">{v}</dd>
          </div>
        ))}
      </dl>
      <JsonBlock label="Full input" value={approval.input} />
      {error && <p role="alert" className="text-sm break-words text-destructive">{error}</p>}
      <div className="flex flex-wrap gap-2">
        <Button onClick={() => (needsConfirm ? setConfirming(true) : void decide("approve"))} disabled={busy !== null} className="pointer-coarse:h-11">
          {busy === "approve" ? <Loader2 className="animate-spin" aria-hidden /> : <Check aria-hidden />}
          Approve
        </Button>
        <Button variant="outline" onClick={() => void decide("reject")} disabled={busy !== null} className="pointer-coarse:h-11">
          {busy === "reject" ? <Loader2 className="animate-spin" aria-hidden /> : <X aria-hidden />}
          Reject
        </Button>
      </div>
      <ConfirmDialog
        open={confirming}
        title={`Approve ${approval.tool}?`}
        body="This action can't be undone. The agent runs it as soon as you approve."
        action="Approve and run"
        destructive
        onClose={() => setConfirming(false)}
        onConfirm={() => decide("approve")}
      />
    </section>
  );
}

function Outcome({ run, onRetry }: { run: AgentRunDetail; onRetry: () => void }) {
  const [open, setOpen] = useState<Citation | null>(null);
  if (run.status === "failed") {
    if (errorCode(run.result) === "llm_not_configured") {
      return <NotConfigured title="Agent runs need a language model" note="This run stopped before any step ran." onRetry={onRetry} />;
    }
    return (
      <div role="alert" className="rounded-lg border border-destructive/30 bg-destructive/5 p-4 text-sm">
        <p className="font-medium text-destructive">The run failed</p>
        <p className="mt-1 break-words">{run.error ?? "No reason was recorded."}</p>
      </div>
    );
  }
  if (run.status === "cancelled") return <p className="rounded-lg border bg-muted/40 p-4 text-sm">{run.error ?? "Cancelled."}</p>;
  const result = run.status === "succeeded" ? parseResult(run.result) : null;
  if (!result) return null;
  const { text, citations } = toCited(result);
  return (
    <section aria-labelledby="run-answer" className="grid gap-3 rounded-lg border bg-card p-4 md:p-5">
      <h2 id="run-answer" className="text-base font-semibold">Answer</h2>
      <p className="leading-relaxed break-words whitespace-pre-wrap">
        <CitedText text={text} citations={citations} onOpen={setOpen} />
      </p>
      {result.reports.length > 0 && (
        <div className="flex flex-wrap gap-2 border-t pt-3">
          {result.reports.map((id, i) => (
            <Button key={id} variant="outline" asChild className="pointer-coarse:h-11">
              <a href={`/api/v1/reports/${encodeURIComponent(id)}`} download>
                <Download aria-hidden />
                {result.reports.length > 1 ? `Download report ${i + 1} (.xlsx)` : "Download report (.xlsx)"}
              </a>
            </Button>
          ))}
        </div>
      )}
      <CitationPanel citation={open} onClose={() => setOpen(null)} />
    </section>
  );
}

function ConnectionNote({ connection, onReconnect, message }: { connection: Connection; onReconnect: () => void; message?: string }) {
  if (connection === "reconnecting") {
    return (
      <p role="status" className="flex items-center gap-2 text-sm text-muted-foreground">
        <Loader2 className="size-3.5 motion-safe:animate-spin" aria-hidden /> Connection dropped. Reconnecting…
      </p>
    );
  }
  if (connection !== "lost") return null;
  return (
    <div role="alert" className="flex flex-wrap items-center gap-3 rounded-lg border border-destructive/30 bg-destructive/5 px-4 py-3 text-sm">
      <WifiOff className="size-4 text-destructive" aria-hidden />
      <span className="min-w-0 flex-1">{message ?? "Lost the live connection to this run."} The run keeps going on the server.</span>
      <Button variant="outline" size="sm" onClick={onReconnect}>
        <RotateCw aria-hidden /> Reconnect
      </Button>
    </div>
  );
}

function useCancel(run: AgentRunDetail, setRun: (fn: (r: AgentRunDetail) => AgentRunDetail) => void, refresh: () => Promise<unknown>) {
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  async function cancel() {
    setBusy(true);
    setNotice(null);
    try {
      const out = await api.cancelAgentRun(run.id);
      setRun((r) => ({ ...r, ...out }));
    } catch (err) {
      const e = asApiError(err);
      setNotice(e.status === 409 ? "The run had already finished." : e.message);
      if (e.status === 409) await refresh().catch(() => undefined);
    } finally {
      setBusy(false);
    }
  }
  return { busy, notice, cancel };
}

function RunView({ initial }: { initial: AgentRunDetail }) {
  const router = useRouter();
  const { run, setRun, refresh, connection, lostError, reconnect } = useAgentRun(initial);
  const cancel = useCancel(run, setRun, refresh);
  const [notice, setNotice] = useState<string | null>(null);
  const active = isActive(run.status);
  const stepOf = new Map(run.steps.map((s) => [s.id, s]));
  const pending = run.status === "awaiting_approval" ? run.approvals.filter((a) => a.status === "pending") : [];

  async function startAgain() {
    const next = await api.createAgentRun({ goal: run.goal, kb_ids: run.kb_ids.length ? run.kb_ids : undefined, max_steps: run.max_steps });
    router.push(`/agent/${encodeURIComponent(next.id)}`);
  }

  return (
    <section className="mx-auto grid w-full max-w-[960px] gap-6">
      <Link href="/agent" className="inline-flex w-fit items-center gap-1 rounded-sm text-sm text-muted-foreground outline-none hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring">
        <ChevronLeft className="size-4" aria-hidden /> All runs
      </Link>
      <header className="grid gap-3">
        <h1 className="text-xl font-semibold break-words md:text-2xl">{run.goal}</h1>
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-sm text-muted-foreground">
          <AgentStatusPill status={run.status} />
          <time dateTime={run.created_at}>Started {formatDateTime(run.created_at)}</time>
          <span>Up to {run.max_steps} tool calls</span>
          {active && (
            <Button variant="outline" size="sm" onClick={() => void cancel.cancel()} disabled={cancel.busy} className="ml-auto pointer-coarse:h-11">
              {cancel.busy ? <Loader2 className="animate-spin" aria-hidden /> : <Square className="fill-current" aria-hidden />}
              Cancel run
            </Button>
          )}
        </div>
        {(cancel.notice ?? notice) && <p role="status" className="text-sm text-muted-foreground">{cancel.notice ?? notice}</p>}
      </header>
      <ConnectionNote connection={connection} onReconnect={reconnect} message={lostError?.message} />
      {pending.map((a) => (
        <ApprovalCard
          key={a.id}
          approval={a}
          risk={(a.step_id && stepOf.get(a.step_id)?.risk) || null}
          onDecided={async (decided, msg) => {
            setNotice(msg);
            if (decided) setRun((r) => mergeApproval(r, decided));
            else await refresh().catch(() => undefined);
          }}
        />
      ))}
      <Outcome run={run} onRetry={() => void startAgain().catch((e: unknown) => setNotice(asApiError(e).message))} />
      <div className="grid gap-3">
        <h2 className="text-base font-semibold">Steps</h2>
        {run.steps.length === 0 ? (
          <p className="text-sm text-muted-foreground">{active ? "Waiting for the first step…" : "No steps ran."}</p>
        ) : (
          <Timeline steps={run.steps} approvals={run.approvals} runActive={active} />
        )}
      </div>
    </section>
  );
}

export function AgentRunSkeleton() {
  return (
    <div aria-busy="true" aria-label="Loading run" className="mx-auto grid max-w-[960px] gap-4">
      <Skeleton className="h-5 w-20" />
      <Skeleton className="h-8 w-3/4" />
      <Skeleton className="h-40 w-full" />
    </div>
  );
}

export function AgentRunPage() {
  const { id } = useParams<{ id: string }>();
  const { state, reload } = useResource(() => api.getAgentRun(id));
  if (state.status === "loading") return <AgentRunSkeleton />;
  if (state.status === "error") {
    return state.error.status === 404 ? (
      <EmptyState
        title="Run not found"
        body="It may belong to another account, or the link is wrong."
        action={<Button asChild><Link href="/agent">All runs</Link></Button>}
      />
    ) : (
      <ErrorState error={state.error} onRetry={reload} />
    );
  }
  return <RunView key={state.data.id} initial={state.data} />;
}
