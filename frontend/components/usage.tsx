"use client";

import { useEffect, useState, type FormEvent } from "react";
import { Download, Loader2 } from "lucide-react";
import { WithMe } from "@/components/settings-nav";
import { can } from "@/lib/roles";
import { ErrorState } from "@/components/states";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { DailyColumns } from "@/components/usage-chart";
import { api } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import type { BudgetAction, Me, OrgSettings, UsageReport } from "@/lib/api/types";
import { parseBudget } from "@/lib/budget";
import { dailySeries, defaultRange, formatUsd, rangeQuery, shares } from "@/lib/usage";
import { useResource } from "@/lib/use-resource";
import { cn } from "@/lib/utils";

type Range = { from: string; to: string };
type Reports = { day: UsageReport; kind: UsageReport; model: UsageReport; user: UsageReport | null };

const asApiError = (err: unknown) => (err instanceof ApiError ? err : new ApiError(0, "unknown", "Something went wrong. Try again."));
const count = (n: number) => n.toLocaleString("en");
const KIND_LABELS: Record<string, string> = { chat: "Answers and agent turns", embed: "Embeddings", rewrite: "Query rewrites" };

function Tile({ label, value, note }: { label: string; value: string; note?: string }) {
  return (
    <div className="grid gap-1 rounded-lg border bg-card p-4">
      <p className="text-sm text-muted-foreground">{label}</p>
      <p className="text-2xl font-semibold tracking-tight tabular-nums">{value}</p>
      {note && <p className="text-xs text-muted-foreground">{note}</p>}
    </div>
  );
}

function Breakdown({ title, report, fallback, labels }: { title: string; report: UsageReport; fallback: string; labels?: Record<string, string> }) {
  const rows = shares(report.series, fallback);
  return (
    <section className="grid min-w-0 grid-cols-1 content-start gap-2 rounded-lg border bg-card p-4">
      <h3 className="text-sm font-medium">{title}</h3>
      {rows.length === 0 ? (
        <p className="text-sm text-muted-foreground">No calls in this range.</p>
      ) : (
        <ul className="grid grid-cols-1 gap-2.5">
          {rows.map((r) => (
            <li key={r.key || r.label} className="grid min-w-0 grid-cols-1 gap-1">
              <div className="flex items-baseline justify-between gap-3 text-sm">
                <span className="min-w-0 truncate" title={r.label}>{labels?.[r.label] ?? r.label}</span>
                <span className="shrink-0 text-xs text-muted-foreground tabular-nums">
                  {formatUsd(r.cost)} · {count(r.calls)} {r.calls === 1 ? "call" : "calls"}
                </span>
              </div>
              <div className="h-1.5 rounded-full bg-muted" aria-hidden>
                <div className="h-full rounded-full bg-primary" style={{ width: `${r.fraction * 100}%` }} />
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function meterTone(ratio: number) {
  if (ratio >= 1) return { bar: "bg-destructive", label: "Over budget" };
  if (ratio >= 0.8) return { bar: "bg-warning", label: "Close to the limit" };
  return { bar: "bg-primary", label: "Within budget" };
}

export function BudgetCard({ orgId }: { orgId: string }) {
  const { state, reload } = useResource(() => api.getSettings(orgId));
  if (state.status === "loading") return <Skeleton aria-label="Loading budget" className="h-48 w-full" />;
  if (state.status === "error") return <ErrorState error={state.error} onRetry={reload} />;
  return <BudgetForm orgId={orgId} initial={state.data} />;
}

function BudgetForm({ orgId, initial }: { orgId: string; initial: OrgSettings }) {
  const [settings, setSettings] = useState(initial);
  const [amount, setAmount] = useState(initial.monthly_budget_usd ?? "");
  const [action, setAction] = useState<BudgetAction>(initial.budget_action);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const parsed = parseBudget(amount);
  const limit = settings.monthly_budget_usd === null ? null : Number(settings.monthly_budget_usd);
  const ratio = limit === null ? 0 : limit === 0 ? 1 : settings.month_spend_usd / limit;
  const tone = meterTone(ratio);

  async function save(e: FormEvent) {
    e.preventDefault();
    if (!parsed.ok) return;
    setPending(true);
    setError(null);
    setSaved(false);
    try {
      const next = await api.updateSettings(orgId, { monthly_budget_usd: parsed.value, budget_action: action });
      setSettings(next);
      setAmount(next.monthly_budget_usd ?? "");
      setSaved(true);
    } catch (err) {
      setError(asApiError(err).message);
    } finally {
      setPending(false);
    }
  }

  return (
    <section aria-labelledby="budget-heading" className="grid gap-4 rounded-lg border bg-card p-4 md:p-5">
      <div>
        <h2 id="budget-heading" className="text-base font-semibold">Monthly AI budget</h2>
        <p className="text-sm text-muted-foreground">Covers answers, agent runs and embeddings. Resets on the 1st of each month (UTC).</p>
      </div>
      <div className="grid gap-1.5">
        <div className="flex flex-wrap items-baseline justify-between gap-2 text-sm">
          <span>
            <span className="font-semibold tabular-nums">{formatUsd(settings.month_spend_usd)}</span>{" "}
            <span className="text-muted-foreground">spent this month{limit !== null && ` of ${formatUsd(limit)}`}</span>
          </span>
          <span className="text-xs text-muted-foreground">{limit === null ? "No limit set" : tone.label}</span>
        </div>
        {limit !== null && (
          <div
            role="meter"
            aria-label="Spend against budget"
            aria-valuemin={0}
            aria-valuemax={limit}
            aria-valuenow={Math.min(settings.month_spend_usd, limit)}
            aria-valuetext={`${formatUsd(settings.month_spend_usd)} of ${formatUsd(limit)}`}
            className="h-2 rounded-full bg-muted"
          >
            <div className={cn("h-full rounded-full", tone.bar)} style={{ width: `${Math.min(1, ratio) * 100}%` }} />
          </div>
        )}
      </div>
      <form onSubmit={save} className="grid gap-4 sm:grid-cols-2">
        <div className="grid content-start gap-1.5">
          <Label htmlFor="budget-amount">Budget in US dollars</Label>
          <Input
            id="budget-amount"
            inputMode="decimal"
            autoComplete="off"
            placeholder="Unlimited"
            value={amount}
            aria-invalid={!parsed.ok}
            aria-describedby="budget-help"
            onChange={(e) => {
              setAmount(e.target.value);
              setSaved(false);
            }}
          />
          <p id="budget-help" className={cn("text-xs", parsed.ok ? "text-muted-foreground" : "text-destructive")}>
            {parsed.ok ? "Leave empty for no limit." : parsed.error}
          </p>
        </div>
        <fieldset className="grid content-start gap-1.5">
          <legend className="mb-1.5 text-sm font-medium">When the budget is used up</legend>
          {(
            [
              ["block", "Stop AI requests", "Answers, agent runs and new uploads fail with a clear message until next month or a higher budget."],
              ["warn", "Keep going and warn", "Requests still run; everyone sees a warning banner."],
            ] as const
          ).map(([value, label, help]) => (
            <label key={value} className="flex cursor-pointer items-start gap-2.5">
              <input type="radio" name="budget-action" className="mt-1 size-4 accent-primary" checked={action === value} onChange={() => { setAction(value); setSaved(false); }} />
              <span className="grid">
                <span className="text-sm">{label}</span>
                <span className="text-xs text-muted-foreground">{help}</span>
              </span>
            </label>
          ))}
        </fieldset>
        <div className="flex flex-wrap items-center gap-3 sm:col-span-2">
          <Button type="submit" disabled={pending || !parsed.ok}>
            {pending && <Loader2 className="animate-spin" aria-hidden />}
            Save budget
          </Button>
          {saved && <p role="status" className="text-sm text-muted-foreground">Budget saved.</p>}
          {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
        </div>
      </form>
    </section>
  );
}

function RangeForm({ range, onApply }: { range: Range; onApply: (r: Range) => void }) {
  const [from, setFrom] = useState(range.from);
  const [to, setTo] = useState(range.to);
  const valid = from !== "" && to !== "" && from <= to;
  return (
    <form
      className="flex flex-wrap items-end gap-3"
      onSubmit={(e) => {
        e.preventDefault();
        if (valid) onApply({ from, to });
      }}
    >
      <div className="grid gap-1.5">
        <Label htmlFor="usage-from">From</Label>
        <Input id="usage-from" type="date" value={from} max={to} onChange={(e) => setFrom(e.target.value)} className="w-auto" />
      </div>
      <div className="grid gap-1.5">
        <Label htmlFor="usage-to">To</Label>
        <Input id="usage-to" type="date" value={to} min={from} onChange={(e) => setTo(e.target.value)} className="w-auto" />
      </div>
      <Button type="submit" variant="outline" disabled={!valid}>Show</Button>
      <p className="basis-full text-xs text-muted-foreground">Days are in UTC. Both dates are included; at most 366 days.</p>
    </form>
  );
}

export function Usage({ me }: { me: Me }) {
  const admin = can(me.role, "manage");
  // Usage renders only after /me loads in the browser, so reading the clock here never runs during prerender.
  const [range, setRange] = useState<Range>(() => defaultRange(new Date()));
  const [reports, setReports] = useState<Reports | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let live = true;
    const q = rangeQuery(range.from, range.to);
    Promise.all([api.usage(q, "day"), api.usage(q, "kind"), api.usage(q, "model"), admin ? api.usage(q, "user") : null]).then(
      ([day, kind, model, user]) => live && setReports({ day, kind, model, user }),
      (err: unknown) => live && setError(asApiError(err)),
    );
    return () => {
      live = false;
    };
  }, [range, admin, attempt]);

  const apply = (r: Range) => {
    setReports(null);
    setError(null);
    setRange(r);
  };

  const self = reports?.day.scope === "self" || (!admin && !reports);
  const points = reports ? dailySeries(range.from, range.to, reports.day.series) : [];
  const totals = reports?.day.totals;

  return (
    <div className="grid grid-cols-1 gap-6">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Usage</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            {self ? "Your usage: AI calls you made yourself." : `Everyone in ${me.org.name}, including background jobs.`}
          </p>
        </div>
        <Button variant="outline" asChild>
          <a href={`/api/v1/usage/export?${rangeQuery(range.from, range.to)}`} download>
            <Download aria-hidden />
            Export CSV
          </a>
        </Button>
      </header>

      <RangeForm key={`${range.from}-${range.to}`} range={range} onApply={apply} />

      {error && <ErrorState error={error} onRetry={() => { setError(null); setAttempt((n) => n + 1); }} />}
      {!error && !reports && <Skeleton aria-label="Loading usage" className="h-72 w-full" />}
      {reports && totals && (
        <>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <Tile label="Cost" value={formatUsd(totals.cost_usd)} note={totals.unpriced_calls > 0 ? `${count(totals.unpriced_calls)} calls have no known price` : undefined} />
            <Tile label="Calls" value={count(totals.calls)} />
            <Tile label="Input tokens" value={count(totals.in_tok)} />
            <Tile label="Output tokens" value={count(totals.out_tok)} />
          </div>
          {totals.calls === 0 ? (
            <p className="rounded-lg border border-dashed px-6 py-10 text-center text-sm text-muted-foreground">
              No AI calls in this range. Upload a document or ask a question and it shows up here.
            </p>
          ) : (
            <>
              <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
                <DailyColumns title="Cost per day" points={points} value={(p) => p.cost} format={formatUsd} empty="No priced calls in this range. Local models and models without a known price cost $0 here." />
                <DailyColumns title="Calls per day" points={points} value={(p) => p.calls} format={count} integer empty="No calls in this range." />
              </div>
              <div className={cn("grid grid-cols-1 gap-3", reports.user ? "lg:grid-cols-3" : "lg:grid-cols-2")}>
                <Breakdown title="By kind" report={reports.kind} fallback="Other" labels={KIND_LABELS} />
                <Breakdown title="By model" report={reports.model} fallback="Unknown model" />
                {reports.user && <Breakdown title="By person" report={reports.user} fallback="Background jobs" />}
              </div>
            </>
          )}
        </>
      )}

      {admin && <BudgetCard orgId={me.org.id} />}
    </div>
  );
}

export function UsageScreen() {
  return <WithMe>{(me) => <Usage me={me} />}</WithMe>;
}
