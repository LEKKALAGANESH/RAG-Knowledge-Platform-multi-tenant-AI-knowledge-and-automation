"use client";

import { useState, type FormEvent } from "react";
import { Check, Copy, Loader2, Plus } from "lucide-react";
import { ConfirmDialog } from "@/components/kb-sources";
import { AdminOnly, WithMe } from "@/components/settings-nav";
import { can } from "@/lib/roles";
import { EmptyState, ErrorState } from "@/components/states";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { api } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import type { ApiKey, ApiKeyInput } from "@/lib/api/types";
import { formatDate, formatDateTime } from "@/lib/format";
import { useResource } from "@/lib/use-resource";

export const SCOPES = [
  { value: "kb:read", label: "Read knowledge bases", help: "List knowledge bases and read their documents." },
  { value: "kb:write", label: "Manage knowledge bases", help: "Create, rename and delete knowledge bases." },
  { value: "documents:write", label: "Manage documents", help: "Upload, reprocess and delete documents." },
  { value: "search", label: "Search", help: "Run searches across knowledge bases." },
  { value: "answer", label: "Ask questions", help: "Get cited answers. Uses the AI budget." },
  { value: "agent:run", label: "Run agent workflows", help: "Start and follow agent runs. Uses the AI budget." },
  { value: "sources:read", label: "Read data sources", help: "See connected sources and their sync history." },
  { value: "sources:write", label: "Manage data sources", help: "Connect, change, sync and remove sources." },
  { value: "admin", label: "Administer", help: "Manage API keys, members and settings, and read the audit log." },
] as const;

/** Form -> request body. Scopes keep the catalog order; an expiry date means the end of that day, UTC. */
export function keyPayload(name: string, picked: ReadonlySet<string>, expiry: string): ApiKeyInput {
  return {
    name: name.trim(),
    scopes: SCOPES.map((s) => s.value).filter((s) => picked.has(s)),
    expires_at: expiry ? `${expiry}T23:59:59Z` : null,
  };
}

const errorText = (err: unknown) => (err instanceof ApiError ? err.message : "That didn't work. Try again.");

function CreateKeyDialog({ open, onClose, onCreated }: { open: boolean; onClose: () => void; onCreated: (key: string) => void }) {
  const [name, setName] = useState("");
  const [picked, setPicked] = useState<Set<string>>(new Set(["kb:read", "search"]));
  const [expiry, setExpiry] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function toggle(scope: string) {
    setPicked((prev) => {
      const next = new Set(prev);
      if (!next.delete(scope)) next.add(scope);
      return next;
    });
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (picked.size === 0) return setError("Pick at least one permission.");
    setPending(true);
    setError(null);
    try {
      const created = await api.createApiKey(keyPayload(name, picked, expiry));
      setName("");
      setExpiry("");
      onCreated(created.key);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setPending(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Create an API key</DialogTitle>
          <DialogDescription>The key acts with your role, limited to the permissions you pick here.</DialogDescription>
        </DialogHeader>
        <form onSubmit={submit} className="grid gap-4">
          <div className="grid gap-1.5">
            <Label htmlFor="key-name">Name</Label>
            <Input id="key-name" required maxLength={100} value={name} onChange={(e) => setName(e.target.value)} placeholder="Support bot" />
          </div>
          <fieldset className="grid gap-2">
            <legend className="mb-1 text-sm font-medium">Permissions</legend>
            {SCOPES.map((s) => (
              <label key={s.value} className="flex cursor-pointer items-start gap-2.5 rounded-md p-1.5 hover:bg-muted/60">
                <input type="checkbox" className="mt-1 size-4 accent-primary" checked={picked.has(s.value)} onChange={() => toggle(s.value)} />
                <span className="grid">
                  <span className="text-sm font-medium">{s.label}</span>
                  <span className="text-xs text-muted-foreground">{s.help}</span>
                </span>
              </label>
            ))}
          </fieldset>
          <div className="grid gap-1.5">
            <Label htmlFor="key-expiry">Expires (optional)</Label>
            <Input id="key-expiry" type="date" value={expiry} onChange={(e) => setExpiry(e.target.value)} className="w-auto" />
            <p className="text-xs text-muted-foreground">The key stops working at the end of this day (UTC). Leave empty to keep it until revoked.</p>
          </div>
          {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose}>Cancel</Button>
            <Button type="submit" disabled={pending}>
              {pending && <Loader2 className="animate-spin" aria-hidden />}
              Create key
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

/** Shows a new key once. The parent drops the secret on close, so it never outlives this dialog. */
export function ShownOnceDialog({ secret, onDone }: { secret: string | null; onDone: () => void }) {
  const [copied, setCopied] = useState(false);
  const done = () => {
    setCopied(false);
    onDone();
  };
  return (
    <Dialog open={secret !== null} onOpenChange={(o) => !o && done()}>
      <DialogContent showCloseButton={false} onInteractOutside={(e) => e.preventDefault()} className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Copy your new API key</DialogTitle>
          <DialogDescription>This is the only time it is shown. Store it somewhere safe, like a password manager.</DialogDescription>
        </DialogHeader>
        <div className="flex items-center gap-2 rounded-lg border bg-muted/50 p-2">
          <code className="min-w-0 flex-1 font-mono text-xs break-all" data-testid="new-key">{secret}</code>
          <Button
            variant="outline"
            size="sm"
            onClick={async () => {
              if (!secret) return;
              await navigator.clipboard.writeText(secret).then(() => setCopied(true), () => setCopied(false));
            }}
          >
            {copied ? <Check aria-hidden /> : <Copy aria-hidden />}
            {copied ? "Copied" : "Copy"}
          </Button>
        </div>
        <DialogFooter>
          <Button onClick={done}>I&rsquo;ve saved it</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function keyStatus(k: ApiKey, now: number): { label: string; live: boolean } {
  if (k.revoked_at) return { label: `Revoked ${formatDate(k.revoked_at)}`, live: false };
  if (k.expires_at && Date.parse(k.expires_at) <= now) return { label: "Expired", live: false };
  return { label: "Active", live: true };
}

function KeyRow({ k, onRevoke }: { k: ApiKey; onRevoke: () => void }) {
  const [now] = useState(() => Date.now());
  const status = keyStatus(k, now);
  return (
    <tr className="border-t align-top">
      <td className="px-3 py-2.5 font-medium">{k.name}</td>
      <td className="px-3 py-2.5 font-mono text-xs">rag_{k.prefix}_&hellip;</td>
      <td className="px-3 py-2.5">
        <ul className="flex flex-wrap gap-1">
          {k.scopes.map((s) => (
            <li key={s} className="rounded-full bg-muted px-2 py-0.5 font-mono text-xs">{s}</li>
          ))}
        </ul>
      </td>
      <td className="px-3 py-2.5 whitespace-nowrap">{formatDate(k.created_at)}</td>
      <td className="px-3 py-2.5 whitespace-nowrap">{k.last_used_at ? formatDateTime(k.last_used_at) : "Never"}</td>
      <td className="px-3 py-2.5 whitespace-nowrap">{k.expires_at ? formatDate(k.expires_at) : "Never"}</td>
      <td className="px-3 py-2.5 whitespace-nowrap">
        {status.live ? (
          <Button variant="ghost" size="sm" className="text-destructive" onClick={onRevoke} aria-label={`Revoke ${k.name}`}>
            Revoke
          </Button>
        ) : (
          <span className="text-muted-foreground">{status.label}</span>
        )}
      </td>
    </tr>
  );
}

// The table wrapper is `relative`: the sr-only status header is absolutely positioned and would otherwise widen the page.
export function ApiKeys() {
  const { state, reload } = useResource(api.listApiKeys);
  const [creating, setCreating] = useState(false);
  const [secret, setSecret] = useState<string | null>(null);
  const [revoking, setRevoking] = useState<ApiKey | null>(null);

  const create = (
    <Button onClick={() => setCreating(true)}>
      <Plus aria-hidden />
      Create API key
    </Button>
  );

  return (
    <div className="grid grid-cols-1 gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="max-w-prose text-sm text-muted-foreground">Keys let scripts and other apps call the API. Each key only gets the permissions you pick.</p>
        {state.status === "ready" && state.data.length > 0 && create}
      </div>
      {state.status === "loading" && <Skeleton aria-label="Loading API keys" className="h-32 w-full" />}
      {state.status === "error" && <ErrorState error={state.error} onRetry={reload} />}
      {state.status === "ready" && state.data.length === 0 && (
        <EmptyState title="No API keys yet" body="Create one to call the API from a script or another app." action={create} />
      )}
      {state.status === "ready" && state.data.length > 0 && (
        <div className="relative overflow-x-auto rounded-lg border">
          <table className="w-full min-w-[760px] text-sm">
            <caption className="sr-only">API keys, newest first</caption>
            <thead className="bg-muted/50 text-left text-xs text-muted-foreground">
              <tr>
                {["Name", "Key", "Permissions", "Created", "Last used", "Expires", ""].map((h) => (
                  <th key={h} scope="col" className="px-3 py-2 font-medium">{h || <span className="sr-only">Status</span>}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {state.data.map((k) => (
                <KeyRow key={k.id} k={k} onRevoke={() => setRevoking(k)} />
              ))}
            </tbody>
          </table>
        </div>
      )}
      <CreateKeyDialog
        open={creating}
        onClose={() => setCreating(false)}
        onCreated={(key) => {
          setCreating(false);
          setSecret(key);
          reload();
        }}
      />
      <ShownOnceDialog secret={secret} onDone={() => setSecret(null)} />
      <ConfirmDialog
        open={revoking !== null}
        destructive
        title={`Revoke “${revoking?.name ?? "key"}”?`}
        body="Apps using this key stop working immediately. This can't be undone."
        action="Revoke key"
        onClose={() => setRevoking(null)}
        onConfirm={async () => {
          if (revoking) await api.revokeApiKey(revoking.id);
          reload();
        }}
      />
    </div>
  );
}

export function ApiKeysScreen() {
  return <WithMe>{(me) => (can(me.role, "manage") ? <ApiKeys /> : <AdminOnly what="manage API keys" />)}</WithMe>;
}
