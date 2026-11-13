"use client";

import { useEffect, useState } from "react";
import { Download, Loader2 } from "lucide-react";
import { AdminOnly, WithMe } from "@/components/settings-nav";
import { can } from "@/lib/roles";
import { EmptyState, ErrorState } from "@/components/states";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { api } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import type { AuditEntry, Me } from "@/lib/api/types";
import { formatDateTime } from "@/lib/format";

// Every action the backend emits today (grep `emit(`/`record(` in backend/app). Unknown ones still list under "All".
export const AUDIT_ACTIONS: Record<string, string> = {
  "auth.register": "Account created",
  "auth.login": "Signed in",
  "auth.logout_all": "Signed out everywhere",
  "member.add": "Member added",
  "invite.create": "Member invited",
  "invite.accept": "Invite accepted",
  "invite.decline": "Invite declined",
  "invite.revoke": "Invite revoked",
  "member.role_change": "Role changed",
  "member.remove": "Member removed",
  "api_key.create": "API key created",
  "api_key.revoke": "API key revoked",
  "org.settings_update": "Settings changed",
  "kb.create": "Knowledge base created",
  "kb.delete": "Knowledge base deleted",
  "document.upload": "Document uploaded",
  "document.delete": "Document deleted",
  "source.create": "Source connected",
  "source.update": "Source changed",
  "source.sync": "Source synced",
  "source.delete": "Source removed",
  "agent_run.start": "Agent run started",
  "session.reuse_detected": "Stolen sign-in token blocked",
};

export const auditExportHref = (action: string) => `/api/v1/audit/export${action ? `?action=${encodeURIComponent(action)}` : ""}`;

type Feed = { items: AuditEntry[]; cursor: string | null; status: "loading" | "ready" | "more"; error: ApiError | null };

function actor(e: AuditEntry, emails: Map<string, string>): string {
  if (e.actor_api_key_id) return `API key ${e.actor_api_key_id.slice(0, 8)}`;
  if (e.actor_user_id) return emails.get(e.actor_user_id) ?? `User ${e.actor_user_id.slice(0, 8)}`;
  return "System";
}

const details = (meta: Record<string, unknown>) =>
  Object.entries(meta)
    .map(([k, v]) => `${k}: ${typeof v === "string" ? v : JSON.stringify(v)}`)
    .join(", ");

export function AuditLog({ me }: { me: Me }) {
  const [action, setAction] = useState("");
  const [feed, setFeed] = useState<Feed>({ items: [], cursor: null, status: "loading", error: null });
  const [attempt, setAttempt] = useState(0);
  const [emails, setEmails] = useState(new Map<string, string>());

  useEffect(() => {
    // Best effort: rows fall back to a short user id when the member list can't load (or the user left).
    api.listMembers(me.org.id).then((ms) => setEmails(new Map(ms.map((m) => [m.user_id, m.email]))), () => undefined);
  }, [me.org.id]);

  useEffect(() => {
    let live = true;
    api.listAudit(action, null).then(
      (p) => live && setFeed({ items: p.items, cursor: p.next_cursor, status: "ready", error: null }),
      (err: unknown) => live && setFeed({ items: [], cursor: null, status: "ready", error: err instanceof ApiError ? err : new ApiError(0, "unknown", "Something went wrong. Try again.") }),
    );
    return () => {
      live = false;
    };
  }, [action, attempt]);

  function reload(next = action) {
    setFeed({ items: [], cursor: null, status: "loading", error: null });
    setAction(next);
    setAttempt((n) => n + 1);
  }

  async function loadMore() {
    if (!feed.cursor) return;
    setFeed((f) => ({ ...f, status: "more", error: null }));
    try {
      const p = await api.listAudit(action, feed.cursor);
      setFeed((f) => ({ items: [...f.items, ...p.items], cursor: p.next_cursor, status: "ready", error: null }));
    } catch (err) {
      setFeed((f) => ({ ...f, status: "ready", error: err instanceof ApiError ? err : new ApiError(0, "unknown", "Something went wrong. Try again.") }));
    }
  }

  return (
    <div className="grid grid-cols-1 gap-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="grid gap-1.5">
          <Label htmlFor="audit-action">Action</Label>
          <select
            id="audit-action"
            className="h-8 rounded-lg border border-input bg-background px-2 text-sm outline-none focus-visible:ring-3 focus-visible:ring-ring/50"
            value={action}
            onChange={(e) => reload(e.target.value)}
          >
            <option value="">All actions</option>
            {Object.entries(AUDIT_ACTIONS).map(([v, label]) => (
              <option key={v} value={v}>{label}</option>
            ))}
          </select>
        </div>
        <Button variant="outline" asChild>
          <a href={auditExportHref(action)} download>
            <Download aria-hidden />
            Download CSV
          </a>
        </Button>
      </div>

      {feed.status === "loading" && <Skeleton aria-label="Loading audit log" className="h-48 w-full" />}
      {feed.error && feed.items.length === 0 && <ErrorState error={feed.error} onRetry={() => reload()} />}
      {feed.status !== "loading" && !feed.error && feed.items.length === 0 && (
        <EmptyState title="Nothing recorded yet" body={action ? "No events of this kind. Pick another action or All actions." : "Sign-ins, member changes, keys and settings changes show up here."} />
      )}
      {feed.items.length > 0 && (
        <div className="relative overflow-x-auto rounded-lg border">
          <table className="w-full min-w-[720px] text-sm">
            <caption className="sr-only">Audit log, newest first</caption>
            <thead className="bg-muted/50 text-left text-xs text-muted-foreground">
              <tr>
                {["When", "Action", "By", "Details"].map((h) => (
                  <th key={h} scope="col" className="px-3 py-2 font-medium">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {feed.items.map((e) => (
                <tr key={e.id} className="border-t align-top">
                  <td className="px-3 py-2.5 whitespace-nowrap">{formatDateTime(e.created_at)}</td>
                  <td className="px-3 py-2.5">
                    <span className="font-medium">{AUDIT_ACTIONS[e.action] ?? e.action}</span>
                    <span className="block font-mono text-xs text-muted-foreground">{e.action}</span>
                  </td>
                  <td className="max-w-56 truncate px-3 py-2.5" title={actor(e, emails)}>{actor(e, emails)}</td>
                  <td className="px-3 py-2.5 text-xs break-words text-muted-foreground">{details(e.meta) || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {feed.error && feed.items.length > 0 && <p role="alert" className="text-sm text-destructive">{feed.error.message}</p>}
      {feed.cursor && (
        <Button variant="outline" className="justify-self-center" onClick={loadMore} disabled={feed.status === "more"}>
          {feed.status === "more" && <Loader2 className="animate-spin" aria-hidden />}
          Load older events
        </Button>
      )}
    </div>
  );
}

export function AuditScreen() {
  return <WithMe>{(me) => (can(me.role, "manage") ? <AuditLog me={me} /> : <AdminOnly what="see the audit log" />)}</WithMe>;
}
