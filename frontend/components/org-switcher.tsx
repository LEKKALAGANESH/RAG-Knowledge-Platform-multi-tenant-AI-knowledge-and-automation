"use client";

import { useEffect, useState } from "react";
import { Building2, Check, ChevronsUpDown, Loader2 } from "lucide-react";
import { DropdownMenu } from "radix-ui";
import { Button } from "@/components/ui/button";
import { api, auth, browserNav } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import { useResource } from "@/lib/use-resource";

/** Detail pages show one org's resource; after a switch they fall back to their list. */
const DETAIL_ROOTS = ["kbs", "agent", "chat"];

export function pathAfterSwitch(pathname: string): string {
  const [, top = "", rest] = pathname.split("/");
  if (top === "oauth") return "/dashboard";
  return DETAIL_ROOTS.includes(top) && rest ? `/${top}` : pathname;
}

/** Switches the session to `orgId`, then reloads the page so no data of the old org stays in memory. */
export async function switchToOrg(orgId: string): Promise<void> {
  await auth.switchOrg(orgId);
  browserNav.assign(pathAfterSwitch(window.location.pathname));
}

/** Fired on window when the user's org list changes (an invite was accepted), so the switcher refetches. */
export const ORGS_CHANGED = "rag:orgs-changed";

const itemClass =
  "flex cursor-default items-center gap-2 rounded-sm px-2 py-1.5 text-sm outline-none select-none data-[highlighted]:bg-muted data-[disabled]:opacity-60";

export function OrgSwitcher({ currentOrgId }: { currentOrgId: string | null }) {
  const { state, reload } = useResource(api.listOrgs);
  useEffect(() => {
    window.addEventListener(ORGS_CHANGED, reload);
    return () => window.removeEventListener(ORGS_CHANGED, reload);
  }, [reload]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // ponytail: a failed load hides the switcher; the current org still shows in the user menu.
  const orgs = state.status === "ready" ? state.data : [];
  if (orgs.length < 2) return null;

  async function pick(orgId: string) {
    if (orgId === currentOrgId) return;
    setBusy(true);
    setError(null);
    try {
      await switchToOrg(orgId);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Couldn't switch. Try again.");
      setBusy(false);
    }
  }

  return (
    <div className="flex min-w-0 items-center gap-2">
      <DropdownMenu.Root>
        <DropdownMenu.Trigger asChild>
          <Button variant="outline" disabled={busy} aria-label="Switch organization" className="pointer-coarse:h-11 pointer-coarse:min-w-11">
            {busy ? <Loader2 className="animate-spin" aria-hidden /> : <Building2 aria-hidden />}
            <ChevronsUpDown className="opacity-60" aria-hidden />
          </Button>
        </DropdownMenu.Trigger>
        <DropdownMenu.Portal>
          <DropdownMenu.Content
            align="end"
            sideOffset={6}
            collisionPadding={8}
            className="z-50 max-h-[min(24rem,var(--radix-dropdown-menu-content-available-height))] w-[min(18rem,calc(100vw-2rem))] overflow-y-auto overscroll-contain rounded-md border bg-popover p-1 text-popover-foreground shadow-md"
          >
            <DropdownMenu.Label className="px-2 py-1.5 text-xs text-muted-foreground">Organizations</DropdownMenu.Label>
            {orgs.map((o) => {
              const current = o.id === currentOrgId;
              return (
                <DropdownMenu.Item
                  key={o.id}
                  className={itemClass}
                  aria-current={current ? "true" : undefined}
                  onSelect={() => void pick(o.id)}
                >
                  <Check className={current ? "size-4 shrink-0" : "invisible size-4 shrink-0"} aria-hidden />
                  <span className="min-w-0 flex-1 truncate">{o.name}</span>
                  <span className="shrink-0 text-xs text-muted-foreground capitalize">{o.role}</span>
                </DropdownMenu.Item>
              );
            })}
          </DropdownMenu.Content>
        </DropdownMenu.Portal>
      </DropdownMenu.Root>
      {error && (
        <p role="alert" className="max-w-40 truncate text-xs text-destructive" title={error}>
          {error}
        </p>
      )}
    </div>
  );
}
