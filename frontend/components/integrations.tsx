"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";
import { Briefcase, Database, Globe, HardDrive, Search, Share2, Webhook, type LucideIcon } from "lucide-react";
import { AUTH_LABEL, canConnect, SourceForm } from "@/components/source-form";
import { OAuthNotice } from "@/components/oauth-notice";
import { EmptyState, ErrorState } from "@/components/states";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { api } from "@/lib/api/client";
import type { ConnectorCategory, ConnectorMeta, KnowledgeBase } from "@/lib/api/types";
import { readOAuthReturn, stripOAuthParams } from "@/lib/oauth";
import { can } from "@/lib/roles";
import { useResource } from "@/lib/use-resource";
import { cn } from "@/lib/utils";

const CATEGORY: Record<ConnectorCategory, { label: string; icon: LucideIcon }> = {
  web: { label: "Websites", icon: Globe },
  storage: { label: "Cloud storage", icon: HardDrive },
  database: { label: "Databases", icon: Database },
  business: { label: "Business apps", icon: Briefcase },
  api: { label: "APIs", icon: Webhook },
  meta: { label: "Meta", icon: Share2 },
};

export function IntegrationsSkeleton() {
  return (
    <div aria-busy="true" aria-label="Loading integrations" className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
      {[0, 1, 2].map((i) => <Skeleton key={i} className="h-44 w-full" />)}
    </div>
  );
}

function ConnectorCard({ connector, blockedReason, onConnect }: { connector: ConnectorMeta; blockedReason: string | null; onConnect: () => void }) {
  const { label, icon: Icon } = CATEGORY[connector.category];
  return (
    <li className="flex flex-col gap-3 rounded-lg border bg-card p-4">
      <div className="flex items-start gap-3">
        <span className="grid size-10 shrink-0 place-items-center rounded-md bg-primary/10 text-primary">
          <Icon className="size-5" aria-hidden />
        </span>
        <div className="min-w-0">
          <h2 className="font-semibold break-words">{connector.title}</h2>
          <p className="text-xs text-muted-foreground">{label}</p>
        </div>
      </div>
      <p className="flex-1 text-sm text-muted-foreground">{connector.description}</p>
      <dl className="text-xs">
        <dt className="sr-only">Sign-in</dt>
        <dd className="text-muted-foreground">{AUTH_LABEL[connector.auth]}</dd>
      </dl>
      <div className="grid gap-1">
        <Button className="w-full sm:w-fit" disabled={blockedReason !== null} onClick={onConnect} aria-describedby={blockedReason ? `blocked-${connector.type}` : undefined}>
          Connect
        </Button>
        {blockedReason && <p id={`blocked-${connector.type}`} className="text-xs text-muted-foreground">{blockedReason}</p>}
      </div>
    </li>
  );
}

function ConnectDialog({ connector, kbs, initialKb, onClose }: { connector: ConnectorMeta | null; kbs: KnowledgeBase[]; initialKb: string | null; onClose: () => void }) {
  const router = useRouter();
  const [kbId, setKbId] = useState(() => (kbs.some((k) => k.id === initialKb) ? (initialKb as string) : (kbs[0]?.id ?? "")));
  return (
    <Dialog open={connector !== null} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto overscroll-contain sm:max-w-2xl">
        {connector && (
          <>
            <DialogHeader>
              <DialogTitle>Connect {connector.title}</DialogTitle>
              <DialogDescription>
                {connector.auth === "oauth2" ? "You sign in with the provider, then come back here." : "The connection is tested before it’s saved."} Synced content goes into the knowledge base you pick.
              </DialogDescription>
            </DialogHeader>
            {kbs.length === 0 ? (
              <EmptyState
                title="Create a knowledge base first"
                body="Sources sync into a knowledge base. Create one, then come back to connect."
                action={<Button asChild><Link href="/kbs">Go to knowledge bases</Link></Button>}
              />
            ) : (
              <div className="grid gap-5">
                <div className="grid gap-1.5">
                  <Label htmlFor="connect-kb">Knowledge base</Label>
                  <select
                    id="connect-kb"
                    value={kbId}
                    onChange={(e) => setKbId(e.target.value)}
                    className="h-8 w-full rounded-lg border border-input bg-background px-2 text-base outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 md:text-sm"
                  >
                    {kbs.map((k) => <option key={k.id} value={k.id}>{k.name}</option>)}
                  </select>
                </div>
                <SourceForm
                  connector={connector}
                  kbId={kbId}
                  onCancel={onClose}
                  onSaved={(s) => {
                    onClose(); // the route stays mounted in the background; a still-open dialog would reappear on Back
                    router.push(`/kbs/${encodeURIComponent(s.kb_id)}?tab=sources`);
                  }}
                />
              </div>
            )}
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}

export function Integrations() {
  const params = useSearchParams();
  const { state, reload } = useResource(() => Promise.all([api.listConnectors(), api.listKbs(), api.me()]));
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState<ConnectorCategory | "all">("all");
  const [connecting, setConnecting] = useState<ConnectorMeta | null>(null);
  // Finish-step refusals land here (expired/replayed state, another account or browser). Read once, then strip.
  const [oauthError, setOauthError] = useState(() => readOAuthReturn(params).error);
  useEffect(stripOAuthParams, []);

  const header = (
    <div className="grid gap-1">
      <h1 className="text-2xl font-semibold tracking-tight">Integrations</h1>
      <p className="max-w-2xl text-sm text-muted-foreground">Connect a source once and its content stays in sync with a knowledge base.</p>
      {oauthError && (
        <OAuthNotice className="mt-3" tone="error" message={oauthError} onDismiss={() => setOauthError(null)}>
          Pick the integration below and select Connect to start again.
        </OAuthNotice>
      )}
    </div>
  );
  if (state.status === "loading") return <section className="grid gap-6">{header}<IntegrationsSkeleton /></section>;
  if (state.status === "error") return <section className="grid gap-6">{header}<ErrorState error={state.error} onRetry={reload} /></section>;

  const [{ items: connectors }, { items: kbs }, me] = state.data;
  const counts = new Map<ConnectorCategory, number>();
  for (const c of connectors) counts.set(c.category, (counts.get(c.category) ?? 0) + 1);
  const q = query.trim().toLowerCase();
  const shown = connectors.filter(
    (c) => (category === "all" || c.category === category) && (!q || `${c.title} ${c.description} ${c.type}`.toLowerCase().includes(q)),
  );
  const blocked = (c: ConnectorMeta) =>
    !can(me.role, "edit") ? "Editors and admins can connect sources." : canConnect(c) ? null : c.auth === "oauth2" ? "Sign-in with this provider isn't supported by this app yet." : "This sign-in method isn't available yet.";
  const chip = (value: ConnectorCategory | "all", text: string, n: number) => (
    <Button
      key={value}
      size="sm"
      variant={category === value ? "default" : "outline"}
      aria-pressed={category === value}
      onClick={() => setCategory(value)}
    >
      {text} <span className={cn("tabular-nums", category === value ? "opacity-80" : "text-muted-foreground")}>{n}</span>
    </Button>
  );

  return (
    <section className="grid gap-6">
      {header}
      <div className="grid gap-3">
        <div className="relative max-w-md">
          <Search className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden />
          <Input type="search" aria-label="Search integrations" placeholder="Search integrations" value={query} onChange={(e) => setQuery(e.target.value)} className="pl-8" />
        </div>
        <div role="group" aria-label="Filter by category" className="flex flex-wrap gap-2">
          {chip("all", "All", connectors.length)}
          {(Object.keys(CATEGORY) as ConnectorCategory[]).filter((k) => counts.has(k)).map((k) => chip(k, CATEGORY[k].label, counts.get(k) ?? 0))}
        </div>
      </div>
      {shown.length === 0 ? (
        <EmptyState
          title="No integrations match"
          body="Try another word or category."
          action={<Button variant="outline" onClick={() => { setQuery(""); setCategory("all"); }}>Clear filters</Button>}
        />
      ) : (
        <ul className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
          {shown.map((c) => <ConnectorCard key={c.type} connector={c} blockedReason={blocked(c)} onConnect={() => setConnecting(c)} />)}
        </ul>
      )}
      <ConnectDialog key={connecting?.type ?? "none"} connector={connecting} kbs={kbs} initialKb={params.get("kb")} onClose={() => setConnecting(null)} />
    </section>
  );
}
