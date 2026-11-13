"use client";

import { useState, type FormEvent } from "react";
import { Loader2, MailX, UserPlus, UserMinus } from "lucide-react";
import { ConfirmDialog } from "@/components/kb-sources";
import { WithMe } from "@/components/settings-nav";
import { can } from "@/lib/roles";
import { ErrorState } from "@/components/states";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { api } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import type { Invite, Me, Member, Role } from "@/lib/api/types";
import { formatDate } from "@/lib/format";
import { useResource } from "@/lib/use-resource";

const ROLES: { value: Role; label: string }[] = [
  { value: "owner", label: "Owner" },
  { value: "admin", label: "Admin" },
  { value: "editor", label: "Editor" },
  { value: "viewer", label: "Viewer" },
];

/** Only an owner may grant or take away the owner role (the server enforces it too). */
export const roleOptions = (actor: Role) => ROLES.filter((r) => r.value !== "owner" || actor === "owner");
export const canEditMember = (actor: Role, target: Member) => can(actor, "manage") && (actor === "owner" || target.role !== "owner");

const errorText = (err: unknown) => (err instanceof ApiError ? err.message : "That didn't work. Try again.");
const selectClass =
  "h-8 rounded-lg border border-input bg-background px-2 text-sm outline-none focus-visible:ring-3 focus-visible:ring-ring/50 disabled:opacity-50";

function InviteMember({ me, onInvited }: { me: Me; onInvited: () => void }) {
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<Role>("viewer");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setPending(true);
    setError(null);
    try {
      await api.inviteMember(me.org.id, { email: email.trim(), role });
      setEmail("");
      onInvited();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setPending(false);
    }
  }

  return (
    <form onSubmit={submit} className="grid gap-3 rounded-lg border bg-card p-4 sm:grid-cols-[1fr_auto_auto] sm:items-end">
      <div className="grid gap-1.5">
        <Label htmlFor="member-email">Invite by email</Label>
        <Input id="member-email" type="email" required value={email} onChange={(e) => setEmail(e.target.value)} placeholder="name@company.com" />
      </div>
      <div className="grid gap-1.5">
        <Label htmlFor="member-role">Role</Label>
        <select id="member-role" className={selectClass} value={role} onChange={(e) => setRole(e.target.value as Role)}>
          {roleOptions(me.role).map((r) => (
            <option key={r.value} value={r.value}>{r.label}</option>
          ))}
        </select>
      </div>
      <Button type="submit" disabled={pending}>
        {pending ? <Loader2 className="animate-spin" aria-hidden /> : <UserPlus aria-hidden />}
        Invite
      </Button>
      <p className="text-xs text-muted-foreground sm:col-span-3">
        No email is sent yet. Tell them to sign in (or register) with this address: they see the invite there and join
        when they accept. Invites expire after 7 days.
      </p>
      {error && <p role="alert" className="text-sm text-destructive sm:col-span-3">{error}</p>}
    </form>
  );
}

function MemberRow({ me, m, onChanged, onRemove }: { me: Me; m: Member; onChanged: () => void; onRemove: () => void }) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const editable = canEditMember(me.role, m);
  const you = m.user_id === me.user.id;

  async function change(role: Role) {
    setPending(true);
    setError(null);
    try {
      await api.changeRole(me.org.id, m.user_id, role);
      onChanged();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setPending(false);
    }
  }

  return (
    <li className="grid gap-2 px-4 py-3 sm:grid-cols-[1fr_auto_auto] sm:items-center">
      <p className="min-w-0 truncate text-sm font-medium" title={m.email}>
        {m.email} {you && <span className="font-normal text-muted-foreground">(you)</span>}
      </p>
      {editable ? (
        <select
          aria-label={`Role for ${m.email}`}
          className={selectClass}
          value={m.role}
          disabled={pending}
          onChange={(e) => change(e.target.value as Role)}
        >
          {/* An admin looking at an owner row never gets here; an owner row seen by an owner keeps all options. */}
          {roleOptions(me.role).map((r) => (
            <option key={r.value} value={r.value}>{r.label}</option>
          ))}
        </select>
      ) : (
        <span className="text-sm text-muted-foreground capitalize">{m.role}</span>
      )}
      {editable ? (
        <Button variant="ghost" size="sm" onClick={onRemove} aria-label={`Remove ${m.email}`}>
          <UserMinus aria-hidden />
          Remove
        </Button>
      ) : (
        <span aria-hidden className="hidden sm:block" />
      )}
      {error && <p role="alert" className="text-sm text-destructive sm:col-span-3">{error}</p>}
    </li>
  );
}

function PendingInvites({ me, invites, onChanged }: { me: Me; invites: Invite[]; onChanged: () => void }) {
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  if (invites.length === 0) return null;

  async function revoke(i: Invite) {
    setBusy(i.id);
    setError(null);
    try {
      await api.revokeInvite(me.org.id, i.id);
      onChanged();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(null);
    }
  }

  return (
    <section aria-labelledby="invites-heading" className="grid gap-2">
      <h2 id="invites-heading" className="text-base font-semibold">
        Pending invites <span className="font-normal text-muted-foreground">({invites.length})</span>
      </h2>
      <ul className="divide-y rounded-lg border">
        {invites.map((i) => (
          <li key={i.id} className="grid gap-2 px-4 py-3 sm:grid-cols-[1fr_auto_auto_auto] sm:items-center">
            <p className="min-w-0 truncate text-sm font-medium" title={i.email}>{i.email}</p>
            <span className="text-sm text-muted-foreground capitalize">{i.role}</span>
            <span className="text-sm text-muted-foreground">Expires {formatDate(i.expires_at)}</span>
            {i.role !== "owner" || me.role === "owner" ? (
              <Button variant="ghost" size="sm" disabled={busy === i.id} onClick={() => revoke(i)} aria-label={`Revoke invite for ${i.email}`}>
                <MailX aria-hidden />
                Revoke
              </Button>
            ) : (
              <span aria-hidden className="hidden sm:block" />
            )}
          </li>
        ))}
      </ul>
      {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
    </section>
  );
}

export function Team({ me }: { me: Me }) {
  const { state, reload } = useResource(() => api.listMembers(me.org.id));
  const invites = useResource(() => (can(me.role, "manage") ? api.listInvites(me.org.id) : Promise.resolve([])));
  const [removing, setRemoving] = useState<Member | null>(null);

  if (!can(me.role, "manage")) {
    // ponytail: GET /members is owner/admin-only on the server, so other roles see just their own membership.
    return (
      <div className="grid gap-3">
        <ul className="divide-y rounded-lg border">
          <li className="flex items-center justify-between gap-3 px-4 py-3 text-sm">
            <span className="min-w-0 truncate font-medium">{me.user.email} <span className="font-normal text-muted-foreground">(you)</span></span>
            <span className="text-muted-foreground capitalize">{me.role}</span>
          </li>
        </ul>
        <p className="text-sm text-muted-foreground">Only owners and admins can see the full member list and change roles.</p>
      </div>
    );
  }

  return (
    <div className="grid grid-cols-1 gap-6">
      <InviteMember me={me} onInvited={invites.reload} />
      {invites.state.status === "error" && <ErrorState error={invites.state.error} onRetry={invites.reload} />}
      {invites.state.status === "ready" && <PendingInvites me={me} invites={invites.state.data} onChanged={invites.reload} />}
      {state.status === "loading" && <Skeleton aria-label="Loading members" className="h-32 w-full" />}
      {state.status === "error" && <ErrorState error={state.error} onRetry={reload} />}
      {state.status === "ready" && (
        <section aria-labelledby="members-heading" className="grid gap-2">
          <h2 id="members-heading" className="text-base font-semibold">
            Members <span className="font-normal text-muted-foreground">({state.data.length})</span>
          </h2>
          <ul className="divide-y rounded-lg border">
            {state.data.map((m) => (
              <MemberRow key={m.user_id} me={me} m={m} onChanged={reload} onRemove={() => setRemoving(m)} />
            ))}
          </ul>
        </section>
      )}
      <ConfirmDialog
        open={removing !== null}
        destructive
        title={`Remove ${removing?.email ?? "member"}?`}
        body="They lose access to this organization. Their signed-in sessions end within 15 minutes, and API keys they created stop working at once."
        action="Remove member"
        onClose={() => setRemoving(null)}
        onConfirm={async () => {
          if (removing) await api.removeMember(me.org.id, removing.user_id);
          reload();
        }}
      />
    </div>
  );
}

export function TeamScreen() {
  return <WithMe>{(me) => <Team me={me} />}</WithMe>;
}
