"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { Plus } from "lucide-react";
import { CreateKbDialog } from "@/components/create-kb-dialog";
import { VIEWER_HINT } from "@/components/kb-list";
import { EmptyState, ErrorState } from "@/components/states";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { api } from "@/lib/api/client";
import { formatDate } from "@/lib/format";
import { useCan } from "@/lib/roles";
import { useResource } from "@/lib/use-resource";

const RECENT = 5;

export default function DashboardPage() {
  const router = useRouter();
  const { state, reload } = useResource(api.listKbs);
  const canCreate = useCan("edit");
  const recent = state.status === "ready" ? [...state.data.items].sort((a, b) => b.created_at.localeCompare(a.created_at)).slice(0, RECENT) : [];

  return (
    <section className="grid gap-6">
      <h1 className="text-2xl font-semibold tracking-tight">Dashboard</h1>

      {state.status === "loading" && <Skeleton aria-label="Loading dashboard" className="h-48 w-full" />}
      {state.status === "error" && <ErrorState error={state.error} onRetry={reload} />}

      {state.status === "ready" && recent.length === 0 && (
        <EmptyState
          title="Start with a knowledge base"
          body={`A knowledge base holds the documents and sources you want answers from.${canCreate ? "" : ` ${VIEWER_HINT}`}`}
          action={
            canCreate && (
              <CreateKbDialog
                trigger={<Button><Plus aria-hidden />New knowledge base</Button>}
                onCreated={(kb) => router.push(`/kbs/${kb.id}`)}
              />
            )
          }
        />
      )}

      {state.status === "ready" && recent.length > 0 && (
        <div className="rounded-lg border">
          <div className="flex items-baseline justify-between gap-4 border-b px-4 py-3">
            <h2 className="text-base font-semibold">
              Knowledge bases <span className="font-normal text-muted-foreground">({state.data.items.length})</span>
            </h2>
            <Link href="/kbs" className="text-sm font-medium text-primary underline-offset-4 hover:underline focus-visible:underline">
              View all
            </Link>
          </div>
          <ul className="divide-y">
            {recent.map((kb) => (
              <li key={kb.id} className="flex items-center justify-between gap-4 px-4 py-3">
                <Link href={`/kbs/${kb.id}`} className="truncate font-medium hover:underline focus-visible:underline">
                  {kb.name}
                </Link>
                <span className="shrink-0 text-xs text-muted-foreground">{formatDate(kb.created_at)}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}
