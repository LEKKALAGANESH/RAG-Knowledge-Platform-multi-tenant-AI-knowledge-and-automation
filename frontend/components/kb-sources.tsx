"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { CheckCircle2, ChevronDown, Loader2, Plug, XCircle } from "lucide-react";
import { OAuthNotice } from "@/components/oauth-notice";
import { SourceForm } from "@/components/source-form";
import { EmptyState, ErrorState } from "@/components/states";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { api, browserNav } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import type { ConnectorMeta, RunStatus, Source, SourceStatus, SyncRun, TestResult } from "@/lib/api/types";
import { formatDateTime } from "@/lib/format";
import { NO_OAUTH_RETURN, oauthStartMessage, oauthSupported, readOAuthReturn, safeAuthorizeUrl, stripOAuthParams } from "@/lib/oauth";
import { usePoll } from "@/lib/use-poll";
import { useResource } from "@/lib/use-resource";
import { cn } from "@/lib/utils";

type Tone = "muted" | "busy" | "ok" | "bad";
const TONE: Record<Tone, { className: string; dot: string }> = {
  muted: { className: "bg-muted text-muted-foreground", dot: "bg-muted-foreground" },
  busy: { className: "bg-primary/10 text-foreground", dot: "" },
  ok: { className: "bg-success/12 text-success", dot: "bg-success" },
  bad: { className: "bg-destructive/10 text-foreground", dot: "bg-destructive" }, // see status-pill.tsx for contrast
};
const SOURCE_PILL: Record<SourceStatus, [string, Tone]> = {
  idle: ["Connected", "ok"],
  syncing: ["Syncing", "busy"],
  error: ["Last sync failed", "bad"],
  disabled: ["Disabled", "muted"],
};
const RUN_PILL: Record<RunStatus, [string, Tone]> = {
  running: ["Running", "busy"],
  succeeded: ["Succeeded", "ok"],
  partial: ["Partial", "bad"],
  failed: ["Failed", "bad"],
};

function Pill({ label, tone }: { label: string; tone: Tone }) {
  const t = TONE[tone];
  return (
    <span className={cn("inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium whitespace-nowrap", t.className)}>
      {tone === "busy" ? <Loader2 className="size-3 text-primary motion-safe:animate-spin" aria-hidden /> : <span className={cn("size-1.5 rounded-full", t.dot)} aria-hidden />}
      {label}
    </span>
  );
}

const STATS = ["discovered", "added", "updated", "unchanged", "deleted", "failed"] as const;
const errorText = (err: unknown, fallback: string) => (err instanceof ApiError ? err.message : fallback);
const isRunning = (runs: SyncRun[]) => runs.some((r) => r.status === "running");

function RunHistory({ runs }: { runs: SyncRun[] }) {
  if (runs.length === 0) return <p className="text-sm text-muted-foreground">No syncs yet. Start one with Sync now.</p>;
  return (
    <div className="overflow-x-auto rounded-lg border">
      <table className="w-full min-w-[640px] text-sm">
        <caption className="sr-only">Sync history, newest first</caption>
        <thead className="bg-muted/50 text-left text-xs text-muted-foreground">
          <tr>
            <th scope="col" className="px-3 py-2 font-medium">Started</th>
            <th scope="col" className="px-3 py-2 font-medium">Status</th>
            {STATS.map((s) => (
              <th key={s} scope="col" className="px-2 py-2 text-right font-medium capitalize">{s}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {runs.map((r) => (
            <RunRow key={r.id} run={r} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

function RunRow({ run }: { run: SyncRun }) {
  const [label, tone] = RUN_PILL[run.status];
  return (
    <>
      <tr className="border-t align-top">
        <td className="px-3 py-2 whitespace-nowrap">{formatDateTime(run.started_at)}</td>
        <td className="px-3 py-2"><Pill label={label} tone={tone} /></td>
        {STATS.map((s) => (
          <td key={s} className="px-2 py-2 text-right tabular-nums">{typeof run.stats[s] === "number" ? String(run.stats[s]) : "—"}</td>
        ))}
      </tr>
      {run.error && (
        <tr>
          <td colSpan={2 + STATS.length} className="px-3 pb-2 text-xs break-words text-destructive">{run.error}</td>
        </tr>
      )}
    </>
  );
}

function TestOutcome({ result }: { result: TestResult }) {
  return (
    <div role="status" className={cn("grid gap-2 rounded-md border px-3 py-2 text-sm", result.ok ? "border-success/30 bg-success/5" : "border-destructive/30 bg-destructive/5")}>
      <p className="flex items-start gap-2 break-words">
        {result.ok ? <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-success" aria-hidden /> : <XCircle className="mt-0.5 size-4 shrink-0 text-destructive" aria-hidden />}
        <span>{result.ok ? "Connection works. " : "Connection failed. "}{result.message}</span>
      </p>
      {result.schema && (
        <div className="grid gap-1">
          <p className="text-xs text-muted-foreground">Tables this account can read ({result.schema.tables.length}):</p>
          <ul className="grid gap-1">
            {result.schema.tables.map((t) => (
              <li key={t.name}>
                <details>
                  <summary className="cursor-pointer rounded font-mono text-xs focus-visible:outline-2 focus-visible:outline-ring">{t.name}</summary>
                  <p className="mt-1 pl-4 font-mono text-xs break-words text-muted-foreground">{t.columns.join(", ")}</p>
                </details>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

export function ConfirmDialog(props: {
  open: boolean; title: string; body: ReactNode; action: string; destructive?: boolean;
  onClose: () => void; onConfirm: () => Promise<void>;
}) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  async function confirm() {
    setPending(true);
    setError(null);
    try {
      await props.onConfirm();
      props.onClose();
    } catch (err) {
      setError(errorText(err, "That didn't work. Try again."));
    } finally {
      setPending(false);
    }
  }
  return (
    <Dialog open={props.open} onOpenChange={(o) => { if (!o) { setError(null); props.onClose(); } }}>
      <DialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{props.title}</DialogTitle>
          <DialogDescription className="break-words">{props.body}</DialogDescription>
        </DialogHeader>
        {error && <p role="alert" className="text-sm break-words text-destructive">{error}</p>}
        <DialogFooter>
          <Button variant="outline" onClick={props.onClose}>Cancel</Button>
          <Button variant={props.destructive ? "destructive" : "default"} onClick={confirm} disabled={pending}>
            {pending && <Loader2 className="animate-spin" aria-hidden />}
            {props.action}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

type Busy = null | "test" | "sync" | "toggle" | "reconnect";
type RowProps = { source: Source; connector?: ConnectorMeta; canEdit: boolean; highlight?: "new" | "reconnected"; onChanged: () => void };

/** New sign-in for this source; the API callback puts the fresh tokens back on the same row.
 * Returns a readable error, or never settles because the browser leaves for the provider. */
async function reconnect(source: Source, connector: ConnectorMeta): Promise<string> {
  let authorizeUrl: string;
  try {
    authorizeUrl = (await api.reconnectOAuth(source.id)).authorize_url;
  } catch (err) {
    return err instanceof ApiError && err.code === "not_found" ? "This source no longer exists. Reload the page." : oauthStartMessage(err, connector);
  }
  const target = safeAuthorizeUrl(source.type, authorizeUrl);
  if (!target) return `The server returned an unexpected sign-in address for ${connector.title}, so it wasn't opened. Ask an operator to check the server's OAuth settings.`;
  browserNav.assign(target);
  return new Promise(() => undefined); // keeps the button busy until the page unloads
}

export function SourceRow({ source, connector, canEdit, highlight, onChanged }: RowProps) {
  const runs = usePoll(`runs:${source.id}`, () => api.listRuns(source.id).then((r) => r.items), isRunning);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState<Busy>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [test, setTest] = useState<TestResult | null>(null);
  const [dialog, setDialog] = useState<null | "full" | "disconnect" | "edit">(null);
  const rowRef = useRef<HTMLLIElement>(null);
  useEffect(() => {
    if (highlight) rowRef.current?.scrollIntoView({ block: "nearest" });
  }, [highlight]);

  // A run finishing changes the source's status and last sync time: reload the list once.
  const running = runs.state.status === "ready" && isRunning(runs.state.data);
  const wasRunning = useRef(running);
  useEffect(() => {
    if (wasRunning.current && !running) onChanged();
    wasRunning.current = running;
  }, [running, onChanged]);

  async function act(kind: Busy, fn: () => Promise<void>) {
    setBusy(kind);
    setNotice(null);
    try {
      await fn();
    } catch (err) {
      setNotice(errorText(err, "That didn't work. Try again."));
    } finally {
      setBusy(null);
    }
  }
  const sync = async (full: boolean) => {
    await api.syncSource(source.id, full);
    setOpen(true);
    runs.refresh();
  };

  const status: SourceStatus = running ? "syncing" : source.status;
  const [label, tone] = SOURCE_PILL[status];
  const disabled = source.status === "disabled";
  const canReconnect = connector?.auth === "oauth2" && oauthSupported(source.type) && source.needs_reconnect;

  return (
    <li ref={rowRef} className={cn("grid min-w-0 gap-3 rounded-lg border p-4", highlight && "border-primary ring-2 ring-primary/30")}>
      <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
        <div className="min-w-0">
          <h3 className="font-medium break-words">{source.name}</h3>
          <p className="text-sm text-muted-foreground">
            {connector?.title ?? source.type} · {source.last_sync_at ? `Last synced ${formatDateTime(source.last_sync_at)}` : "Never synced"} ·{" "}
            {source.schedule_minutes ? `Every ${source.schedule_minutes} min` : "Manual sync"}
          </p>
        </div>
        <div className="flex items-center gap-2">
          {highlight && <span className="rounded-full bg-primary/10 px-2.5 py-0.5 text-xs font-medium text-primary">{highlight === "new" ? "New" : "Reconnected"}</span>}
          <Pill label={label} tone={tone} />
        </div>
      </div>
      {source.last_error && (
        <p className="text-sm break-words text-destructive">
          {source.needs_reconnect ? "Sign-in needed: " : "Last sync: "}
          {source.last_error}
        </p>
      )}

      {canEdit && (
        <div className="flex flex-wrap gap-2">
          <Button size="sm" variant="outline" disabled={busy !== null} onClick={() => act("test", async () => setTest(await api.testSource(source.id)))}>
            {busy === "test" && <Loader2 className="animate-spin" aria-hidden />}Test connection
          </Button>
          <Button size="sm" disabled={busy !== null || running || disabled} onClick={() => act("sync", () => sync(false))}>
            {busy === "sync" && <Loader2 className="animate-spin" aria-hidden />}Sync now
          </Button>
          <Button size="sm" variant="outline" disabled={busy !== null || running || disabled} onClick={() => setDialog("full")}>Full sync</Button>
          {canReconnect && connector && (
            <Button size="sm" disabled={busy !== null} onClick={() => act("reconnect", async () => setNotice(await reconnect(source, connector)))}>
              {busy === "reconnect" && <Loader2 className="animate-spin" aria-hidden />}Reconnect {connector.title}
            </Button>
          )}
          <Button size="sm" variant="outline" disabled={!connector} onClick={() => setDialog("edit")}>Edit</Button>
          <Button size="sm" variant="outline" disabled={busy !== null || running} onClick={() => act("toggle", async () => { await api.updateSource(source.id, { enabled: disabled }); onChanged(); })}>
            {busy === "toggle" && <Loader2 className="animate-spin" aria-hidden />}{disabled ? "Enable" : "Disable"}
          </Button>
          <Button size="sm" variant="ghost" className="text-destructive hover:text-destructive" onClick={() => setDialog("disconnect")}>Disconnect</Button>
        </div>
      )}
      {notice && <p role="alert" className="text-sm break-words text-destructive">{notice}</p>}
      {test && <TestOutcome result={test} />}

      <div className="min-w-0">
        <Button size="sm" variant="ghost" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
          <ChevronDown className={cn("transition-transform", open && "rotate-180")} aria-hidden />
          Sync history
        </Button>
        {open && (
          <div className="mt-2">
            {runs.state.status === "loading" && <Skeleton aria-label="Loading sync history" className="h-20 w-full" />}
            {runs.state.status === "error" && <ErrorState error={runs.state.error} onRetry={runs.refresh} />}
            {runs.state.status === "ready" && <RunHistory runs={runs.state.data} />}
          </div>
        )}
      </div>

      <ConfirmDialog
        open={dialog === "full"}
        title="Run a full sync?"
        body={<>A full sync re-reads everything in &ldquo;{source.name}&rdquo;. Documents that no longer exist in the source are removed from this knowledge base.</>}
        action="Start full sync"
        onClose={() => setDialog(null)}
        onConfirm={() => sync(true)}
      />
      <ConfirmDialog
        open={dialog === "disconnect"}
        destructive
        title={`Disconnect “${source.name}”?`}
        body={<>The source is deleted, and every document it brought into this knowledge base is removed with its search index. This can&rsquo;t be undone.</>}
        action="Disconnect source"
        onClose={() => setDialog(null)}
        onConfirm={async () => { await api.deleteSource(source.id); onChanged(); }}
      />
      {connector && (
        <Dialog open={dialog === "edit"} onOpenChange={(o) => !o && setDialog(null)}>
          <DialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto overscroll-contain sm:max-w-2xl">
            <DialogHeader>
              <DialogTitle>Edit {source.name}</DialogTitle>
              <DialogDescription>{connector.auth === "oauth2" ? "Sign-in stays as it is; only these settings change." : "Changes to the settings or credentials are tested before they’re saved."}</DialogDescription>
            </DialogHeader>
            <SourceForm connector={connector} kbId={source.kb_id} source={source} onCancel={() => setDialog(null)} onSaved={() => { setDialog(null); onChanged(); }} />
          </DialogContent>
        </Dialog>
      )}
    </li>
  );
}

const connectedMessage = (s: Source | undefined) => `${s ? `“${s.name}”` : "The source"} is connected. Its first sync has started.`;
const reconnectedMessage = (s: Source | undefined) => `${s ? `“${s.name}”` : "The source"} is reconnected. It keeps its documents and syncs with the new sign-in.`;

export function KbSources({ kbId, canEdit }: { kbId: string; canEdit: boolean }) {
  const params = useSearchParams();
  const [oauth, setOauth] = useState(() => readOAuthReturn(params));
  useEffect(stripOAuthParams, []);
  const sources = usePoll(`sources:${kbId}`, () => api.listSources(kbId).then((r) => r.items), (items) => items.some((s) => s.status === "syncing"));
  const connectors = useResource(api.listConnectors);
  const byType = new Map(connectors.state.status === "ready" ? connectors.state.data.items.map((c) => [c.type, c]) : []);
  const add = canEdit && (
    <Button asChild size="sm">
      <Link href={`/integrations?kb=${encodeURIComponent(kbId)}`}><Plug aria-hidden />Add source</Link>
    </Button>
  );

  return (
    <div className="grid min-w-0 gap-3">
      <div className="flex items-center justify-between gap-2">
        <h2 className="text-base font-semibold">Sources</h2>
        {add}
      </div>
      {oauth.error && (
        <OAuthNotice tone="error" message={oauth.error} onDismiss={() => setOauth(NO_OAUTH_RETURN)}>
          <Button asChild size="sm" variant="outline">
            <Link href={`/integrations?kb=${encodeURIComponent(kbId)}`}>Connect again</Link>
          </Button>
        </OAuthNotice>
      )}
      {(oauth.connected || oauth.reconnected) && (
        <OAuthNotice
          tone="success"
          message={(oauth.connected ? connectedMessage : reconnectedMessage)(sources.state.status === "ready" ? sources.state.data.find((s) => s.id === (oauth.connected ?? oauth.reconnected)) : undefined)}
          onDismiss={() => setOauth(NO_OAUTH_RETURN)}
        />
      )}
      {sources.state.status === "loading" && <Skeleton aria-label="Loading sources" className="h-32 w-full" />}
      {sources.state.status === "error" && <ErrorState error={sources.state.error} onRetry={sources.refresh} />}
      {sources.state.status === "ready" && (
        <>
          {sources.state.refreshError && (
            <p role="alert" className="text-sm text-destructive">Couldn&rsquo;t refresh sources. {sources.state.refreshError.message}</p>
          )}
          {connectors.state.status === "error" && (
            <p role="alert" className="text-sm text-destructive">Couldn&rsquo;t load connector settings, so editing is unavailable. {connectors.state.error.message}</p>
          )}
          {sources.state.data.length === 0 ? (
            <EmptyState
              title="No sources connected"
              body={canEdit ? "Connect a website, storage bucket or database to keep this knowledge base in sync with it." : "Sources added by editors appear here with their sync status."}
              action={add || undefined}
            />
          ) : (
            <ul className="grid gap-3">
              {sources.state.data.map((s) => (
                <SourceRow key={s.id} source={s} connector={byType.get(s.type)} canEdit={canEdit} highlight={s.id === oauth.connected ? "new" : s.id === oauth.reconnected ? "reconnected" : undefined} onChanged={sources.refresh} />
              ))}
            </ul>
          )}
        </>
      )}
    </div>
  );
}
