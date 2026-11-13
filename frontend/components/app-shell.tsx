"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { Suspense, useState, type ReactNode } from "react";
import {
  Bot,
  Code2,
  FileBarChart,
  Gauge,
  LayoutDashboard,
  Library,
  LogOut,
  Menu,
  MessageSquare,
  PanelLeftClose,
  PanelLeftOpen,
  Plug,
  ScrollText,
  Search,
  Settings,
  Users,
  type LucideIcon,
} from "lucide-react";
import { BrandMark } from "@/components/brand-mark";
import { BudgetBanner } from "@/components/budget-banner";
import { InvitesBanner } from "@/components/invites-banner";
import { OrgSwitcher } from "@/components/org-switcher";
import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetTitle, SheetTrigger } from "@/components/ui/sheet";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { api, auth } from "@/lib/api/client";
import type { Me } from "@/lib/api/types";
import { useResource } from "@/lib/use-resource";
import { can } from "@/lib/roles";
import { cn } from "@/lib/utils";

type NavItem = { label: string; icon: LucideIcon; href?: string };

// Order follows the PRD sections. Items without href are not built yet.
const NAV: NavItem[] = [
  { label: "Dashboard", icon: LayoutDashboard, href: "/dashboard" },
  { label: "Chat", icon: MessageSquare, href: "/chat" },
  { label: "Search", icon: Search, href: "/search" },
  { label: "Knowledge bases", icon: Library, href: "/kbs" },
  { label: "Integrations", icon: Plug, href: "/integrations" },
  { label: "Agent workflows", icon: Bot, href: "/agent" },
  { label: "Reports", icon: FileBarChart },
  { label: "Usage", icon: Gauge, href: "/usage" },
  { label: "Audit", icon: ScrollText, href: "/settings/audit" },
  { label: "Team", icon: Users, href: "/settings/team" },
  { label: "Settings", icon: Settings, href: "/settings" },
  { label: "Developer", icon: Code2 },
];

const itemClass =
  "flex h-9 w-full items-center gap-3 rounded-md px-2.5 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring";

type NavListProps = { iconOnly: "never" | "below-lg" | "always"; onNavigate?: () => void };

function NavList({ iconOnly, onNavigate, pathname }: NavListProps & { pathname: string }) {
  const labelClass = { never: "", "below-lg": "sr-only lg:not-sr-only", always: "sr-only" }[iconOnly];
  const tipClass = { never: "hidden", "below-lg": "lg:hidden", always: "" }[iconOnly];

  // Longest matching href wins, so /settings/team lights Team, not Settings too.
  const current = NAV.filter((n) => n.href && (pathname === n.href || pathname.startsWith(`${n.href}/`)))
    .sort((a, b) => (b.href?.length ?? 0) - (a.href?.length ?? 0))[0]?.href;

  return (
    <ul className="grid gap-0.5">
      {NAV.map(({ label, icon: Icon, href }) => {
        const active = href !== undefined && href === current;
        const body = (
          <>
            <Icon className="size-4 shrink-0" aria-hidden />
            <span className={cn("truncate", labelClass)}>{label}</span>
          </>
        );
        return (
          <li key={label}>
            <Tooltip>
              <TooltipTrigger asChild>
                {href ? (
                  <Link
                    href={href}
                    onClick={onNavigate}
                    data-slot="nav-item"
                    aria-current={active ? "page" : undefined}
                    className={cn(
                      itemClass,
                      active ? "bg-primary/10 font-medium text-primary" : "text-foreground/80 hover:bg-muted hover:text-foreground",
                    )}
                  >
                    {body}
                  </Link>
                ) : (
                  <button
                    type="button"
                    aria-disabled="true"
                    data-slot="nav-item"
                    className={cn(itemClass, "cursor-not-allowed text-muted-foreground")}
                  >
                    {body}
                  </button>
                )}
              </TooltipTrigger>
              <TooltipContent side="right" className={href ? tipClass : undefined}>
                {href ? label : `${label}: coming soon`}
              </TooltipContent>
            </Tooltip>
          </li>
        );
      })}
    </ul>
  );
}

function CurrentNavList(props: NavListProps) {
  return <NavList {...props} pathname={usePathname()} />;
}

/** The pathname is request-time data; the nav itself prerenders and the active item streams in. */
function Nav(props: NavListProps) {
  return (
    <Suspense fallback={<NavList {...props} pathname="" />}>
      <CurrentNavList {...props} />
    </Suspense>
  );
}

function UserMenu({ me }: { me: Me | null }) {
  const router = useRouter();
  const [signingOut, setSigningOut] = useState(false);

  async function signOut() {
    setSigningOut(true);
    // Leave regardless: the BFF revokes the refresh token and clears cookies; a failed call still ends the session locally.
    await auth.logout().catch(() => undefined);
    router.replace("/login");
  }

  return (
    <div className="flex min-w-0 items-center gap-2">
      {me && (
        <div className="hidden min-w-0 text-right text-sm leading-tight sm:block" title={me.user.email}>
          <p className="truncate font-medium">
            {me.org.name} <span className="font-normal text-muted-foreground capitalize">· {me.role}</span>
          </p>
          <p className="truncate text-xs text-muted-foreground">{me.user.email}</p>
        </div>
      )}
      <Button variant="ghost" onClick={signOut} disabled={signingOut} aria-label="Sign out" className="pointer-coarse:h-11 pointer-coarse:min-w-11">
        <LogOut aria-hidden />
        <span className="hidden sm:inline">Sign out</span>
      </Button>
    </div>
  );
}

export function AppShell({ children }: { children: ReactNode }) {
  const { state } = useResource(api.me);
  const me = state.status === "ready" ? state.data : null;
  const [collapsed, setCollapsed] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);

  return (
    <div className="flex min-h-dvh">
      <aside
        className={cn(
          "sticky top-0 hidden h-dvh shrink-0 flex-col border-r bg-sidebar md:flex",
          collapsed ? "w-16" : "w-16 lg:w-60",
        )}
      >
        <div className="flex h-14 items-center border-b px-4">
          <Link href="/dashboard" className="rounded-md outline-none focus-visible:ring-2 focus-visible:ring-ring">
            <BrandMark compact={collapsed || "below-lg"} />
          </Link>
        </div>
        <nav aria-label="Main" className="flex-1 overflow-y-auto overscroll-contain p-3">
          <Nav iconOnly={collapsed ? "always" : "below-lg"} />
        </nav>
        <div className="hidden border-t p-3 lg:block">
          <Button
            variant="ghost"
            className="w-full justify-start"
            onClick={() => setCollapsed((c) => !c)}
            aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          >
            {collapsed ? <PanelLeftOpen aria-hidden /> : <PanelLeftClose aria-hidden />}
            {!collapsed && "Collapse"}
          </Button>
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-10 flex h-14 items-center justify-between gap-3 border-b bg-background/95 px-4 backdrop-blur md:px-6">
          <div className="flex items-center gap-2 md:hidden">
            <Sheet open={drawerOpen} onOpenChange={setDrawerOpen}>
              <SheetTrigger asChild>
                <Button variant="ghost" size="icon" aria-label="Open navigation" className="pointer-coarse:size-11">
                  <Menu aria-hidden />
                </Button>
              </SheetTrigger>
              <SheetContent side="left" className="w-72 overflow-y-auto overscroll-contain p-0">
                <SheetTitle className="flex h-14 items-center border-b px-4">
                  <BrandMark />
                </SheetTitle>
                <nav aria-label="Main" className="p-3">
                  <Nav iconOnly="never" onNavigate={() => setDrawerOpen(false)} />
                </nav>
              </SheetContent>
            </Sheet>
            <BrandMark compact />
          </div>
          <span className="hidden md:block" />
          <div className="flex min-w-0 items-center gap-2">
            <OrgSwitcher currentOrgId={me?.org.id ?? null} />
            <UserMenu me={me} />
          </div>
        </header>
        <BudgetBanner canManage={can(me?.role, "manage")} />
        <InvitesBanner />
        <main className="mx-auto w-full max-w-[1280px] flex-1 px-4 py-6 md:px-6 md:py-8">{children}</main>
      </div>
    </div>
  );
}
