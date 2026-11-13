"use client";

import Link from "next/link";
import { ChevronRight, Plus } from "lucide-react";
import { CreateKbDialog } from "@/components/create-kb-dialog";
import { EmptyState, ErrorState } from "@/components/states";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { api } from "@/lib/api/client";
import { formatDate } from "@/lib/format";
import { useCan } from "@/lib/roles";
import { useResource } from "@/lib/use-resource";

export const VIEWER_HINT = "Editors and admins create knowledge bases. Ask one of them for editor access.";

export function KbList() {
  const { state, reload } = useResource(api.listKbs);
  const canCreate = useCan("edit");
  const createButton = (
    <Button>
      <Plus aria-hidden />
      New knowledge base
    </Button>
  );

  return (
    <section className="grid gap-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Knowledge bases</h1>
          <p className="mt-1 text-sm text-muted-foreground">Collections of documents and sources your team can ask questions about.</p>
        </div>
        {canCreate && state.status === "ready" && state.data.items.length > 0 && (
          <CreateKbDialog trigger={createButton} onCreated={reload} />
        )}
      </div>

      {state.status === "loading" && (
        <ul aria-label="Loading knowledge bases" aria-busy="true" className="divide-y rounded-lg border">
          {[0, 1, 2].map((i) => (
            <li key={i} className="flex items-center gap-4 p-4">
              <div className="grid flex-1 gap-2">
                <Skeleton className="h-4 w-48" />
                <Skeleton className="h-3 w-72 max-w-full" />
              </div>
              <Skeleton className="h-3 w-20" />
            </li>
          ))}
        </ul>
      )}

      {state.status === "error" && <ErrorState error={state.error} onRetry={reload} />}

      {state.status === "ready" && state.data.items.length === 0 && (
        <EmptyState
          title="No knowledge bases yet"
          body={canCreate ? "Create one, then add documents or connect a source to start asking questions." : VIEWER_HINT}
          action={canCreate && <CreateKbDialog trigger={createButton} onCreated={reload} />}
        />
      )}

      {state.status === "ready" && state.data.items.length > 0 && (
        <ul className="divide-y rounded-lg border">
          {state.data.items.map((kb) => (
            <li key={kb.id}>
              <Link
                href={`/kbs/${kb.id}`}
                className="group flex items-center gap-4 p-4 outline-none hover:bg-muted/60 focus-visible:bg-muted/60 focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
              >
                <div className="min-w-0 flex-1">
                  <p className="truncate font-medium">{kb.name}</p>
                </div>
                <span className="hidden shrink-0 text-xs text-muted-foreground sm:block">Created {formatDate(kb.created_at)}</span>
                <ChevronRight className="size-4 shrink-0 text-muted-foreground" aria-hidden />
              </Link>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
