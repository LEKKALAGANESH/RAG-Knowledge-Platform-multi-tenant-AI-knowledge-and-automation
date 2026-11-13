"use client";

import { useState } from "react";
import { ArrowLeftRight, Check, Loader2, MailOpen, X } from "lucide-react";
import { ORGS_CHANGED, switchToOrg } from "@/components/org-switcher";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import type { MyInvite } from "@/lib/api/types";
import { useResource } from "@/lib/use-resource";

/** Invites addressed to the signed-in user's email. Joining needs an explicit Accept (S10). */
export function InvitesBanner() {
  const { state, reload } = useResource(api.myInvites);
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ ok: boolean; text: string } | null>(null);
  // Accepting keeps the user in their current org (FX-4); this offers the jump to the one just joined.
  const [joined, setJoined] = useState<{ id: string; name: string } | null>(null);
  const [switching, setSwitching] = useState(false);

  async function answer(i: MyInvite, accept: boolean) {
    setBusy(i.id);
    setNotice(null);
    setJoined(null);
    try {
      if (accept) await api.acceptInvite(i.id);
      else await api.declineInvite(i.id);
      setNotice({
        ok: true,
        text: accept ? `You joined ${i.org_name} as ${i.role}.` : `Declined the invite to ${i.org_name}.`,
      });
      if (accept) {
        setJoined({ id: i.org_id, name: i.org_name });
        window.dispatchEvent(new Event(ORGS_CHANGED));
      }
      reload();
    } catch (err) {
      setNotice({ ok: false, text: err instanceof ApiError ? err.message : "That didn't work. Try again." });
    } finally {
      setBusy(null);
    }
  }

  async function switchToJoined(org: { id: string; name: string }) {
    setSwitching(true);
    try {
      await switchToOrg(org.id);
    } catch (err) {
      setNotice({ ok: false, text: err instanceof ApiError ? err.message : "Couldn't switch. Try again." });
      setSwitching(false);
    }
  }

  // ponytail: a failed load shows nothing; this banner is a hint on every page, not a page of its own.
  const invites = state.status === "ready" ? state.data : [];
  if (invites.length === 0 && !notice) return null;

  return (
    <section aria-label="Invitations" className="grid gap-2 border-b bg-primary/5 px-4 py-2.5 text-sm md:px-6">
      {invites.map((i) => (
        <div key={i.id} className="flex flex-wrap items-center gap-x-3 gap-y-2">
          <MailOpen className="size-4 shrink-0 text-primary" aria-hidden />
          <p className="min-w-0 flex-1 break-words">
            You&apos;re invited to <span className="font-medium">{i.org_name}</span> as{" "}
            <span className="capitalize">{i.role}</span>.
          </p>
          <div className="flex gap-2">
            <Button size="sm" disabled={busy !== null} onClick={() => answer(i, true)} aria-label={`Accept invite to ${i.org_name}`}>
              {busy === i.id ? <Loader2 className="animate-spin" aria-hidden /> : <Check aria-hidden />}
              Accept
            </Button>
            <Button size="sm" variant="ghost" disabled={busy !== null} onClick={() => answer(i, false)} aria-label={`Decline invite to ${i.org_name}`}>
              <X aria-hidden />
              Decline
            </Button>
          </div>
        </div>
      ))}
      {notice && (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
          <p role={notice.ok ? "status" : "alert"} className={notice.ok ? "text-muted-foreground" : "text-destructive"}>
            {notice.text}
          </p>
          {joined && (
            <Button size="sm" variant="outline" disabled={switching} onClick={() => switchToJoined(joined)}>
              {switching ? <Loader2 className="animate-spin" aria-hidden /> : <ArrowLeftRight aria-hidden />}
              Switch to {joined.name}
            </Button>
          )}
        </div>
      )}
    </section>
  );
}
