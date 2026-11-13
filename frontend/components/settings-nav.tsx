"use client";

import Link from "next/link";
import type { ReactNode } from "react";
import { ErrorState } from "@/components/states";
import { Skeleton } from "@/components/ui/skeleton";
import { api } from "@/lib/api/client";
import type { Me } from "@/lib/api/types";
import { useResource } from "@/lib/use-resource";
import { cn } from "@/lib/utils";

const TABS = [
  { href: "/settings", label: "Security" },
  { href: "/settings/team", label: "Team" },
  { href: "/settings/api-keys", label: "API keys" },
  { href: "/settings/audit", label: "Audit log" },
] as const;

export type SettingsTab = (typeof TABS)[number]["href"];


/** Page title + settings tabs. `current` is passed in so the tabs prerender without reading the URL. */
export function SettingsHeader({ current, title, description }: { current: SettingsTab; title: string; description: string }) {
  return (
    <header className="mb-6 grid gap-4">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
        <p className="mt-1 text-sm text-muted-foreground">{description}</p>
      </div>
      <nav aria-label="Settings" className="-mx-1 overflow-x-auto">
        <ul className="flex min-w-max gap-1 border-b px-1">
          {TABS.map((t) => (
            <li key={t.href}>
              <Link
                href={t.href}
                aria-current={t.href === current ? "page" : undefined}
                className={cn(
                  "-mb-px inline-flex h-10 items-center border-b-2 px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring",
                  t.href === current
                    ? "border-primary font-medium text-foreground"
                    : "border-transparent text-muted-foreground hover:text-foreground",
                )}
              >
                {t.label}
              </Link>
            </li>
          ))}
        </ul>
      </nav>
    </header>
  );
}

/** Loads /me (role + org id) and renders the page with it. */
export function WithMe({ children }: { children: (me: Me) => ReactNode }) {
  const { state, reload } = useResource(api.me);
  if (state.status === "loading") return <Skeleton aria-label="Loading" className="h-40 w-full" />;
  if (state.status === "error") return <ErrorState error={state.error} onRetry={reload} />;
  return children(state.data);
}

export function AdminOnly({ what }: { what: string }) {
  return (
    <div className="rounded-lg border border-dashed px-6 py-10 text-center text-sm text-muted-foreground">
      Only owners and admins can {what}. Ask one of them if you need access.
    </div>
  );
}
