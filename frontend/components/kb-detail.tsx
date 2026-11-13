"use client";

import Link from "next/link";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { Tabs } from "radix-ui";
import { ChevronLeft } from "lucide-react";
import { EmptyState, ErrorState } from "@/components/states";
import { DocumentUpload } from "@/components/document-upload";
import { DocumentsTable } from "@/components/documents-table";
import { KbSources } from "@/components/kb-sources";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { api } from "@/lib/api/client";
import { can } from "@/lib/roles";
import { useDocuments } from "@/lib/use-documents";
import { useResource } from "@/lib/use-resource";

export function KbDetailSkeleton() {
  return (
    <div aria-busy="true" aria-label="Loading knowledge base" className="grid gap-6">
      <Skeleton className="h-8 w-64" />
      <Skeleton className="h-40 w-full" />
    </div>
  );
}

function Documents({ kbId, canEdit }: { kbId: string; canEdit: boolean }) {
  const { state, refresh } = useDocuments(kbId);
  return (
    <div className="grid gap-3">
      <h2 className="text-base font-semibold">Documents</h2>
      {canEdit ? (
        <DocumentUpload kbId={kbId} onUploaded={refresh} />
      ) : (
        <p className="rounded-md border bg-muted/40 px-3 py-2 text-sm text-muted-foreground">
          You have view-only access. Ask an owner or admin for editor access to upload, reprocess or delete documents.
        </p>
      )}
      {state.status === "loading" && <Skeleton aria-label="Loading documents" className="h-32 w-full" />}
      {state.status === "error" && <ErrorState error={state.error} onRetry={refresh} />}
      {state.status === "ready" && (
        <>
          {state.refreshError && (
            <p role="alert" className="flex flex-wrap items-center gap-2 text-sm text-destructive">
              Couldn&rsquo;t refresh document statuses. {state.refreshError.message}
              <Button size="sm" variant="outline" onClick={refresh}>Try again</Button>
            </p>
          )}
          {state.docs.length === 0 ? (
            <EmptyState
              title="No documents yet"
              body={canEdit ? "Upload files above. Each one is parsed, chunked and indexed, and its status shows here." : "Documents added by editors appear here with their processing status."}
            />
          ) : (
            <DocumentsTable docs={state.docs} canEdit={canEdit} onChanged={refresh} />
          )}
        </>
      )}
    </div>
  );
}

const tabClass =
  "-mb-px border-b-2 border-transparent px-3 py-2 text-sm font-medium text-muted-foreground outline-none hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring data-[state=active]:border-primary data-[state=active]:text-foreground";

function KbTabs({ kbId, canEdit }: { kbId: string; canEdit: boolean }) {
  const router = useRouter();
  const tab = useSearchParams().get("tab") === "sources" ? "sources" : "documents";
  return (
    <Tabs.Root value={tab} onValueChange={(v) => router.replace(`/kbs/${encodeURIComponent(kbId)}${v === "sources" ? "?tab=sources" : ""}`, { scroll: false })} className="grid min-w-0 gap-4">
      <Tabs.List aria-label="Knowledge base content" className="flex gap-1 border-b">
        <Tabs.Trigger value="documents" className={tabClass}>Documents</Tabs.Trigger>
        <Tabs.Trigger value="sources" className={tabClass}>Sources</Tabs.Trigger>
      </Tabs.List>
      <Tabs.Content value="documents" className="min-w-0 outline-none">
        <Documents kbId={kbId} canEdit={canEdit} />
      </Tabs.Content>
      <Tabs.Content value="sources" className="min-w-0 outline-none">
        <KbSources kbId={kbId} canEdit={canEdit} />
      </Tabs.Content>
    </Tabs.Root>
  );
}

export function KbDetail() {
  const { id } = useParams<{ id: string }>();
  const { state, reload } = useResource(() => Promise.all([api.getKb(id), api.me()]));

  return (
    <section className="grid gap-6">
      <Link
        href="/kbs"
        className="inline-flex w-fit items-center gap-1 rounded-md text-sm text-muted-foreground hover:text-foreground focus-visible:outline-2 focus-visible:outline-ring"
      >
        <ChevronLeft className="size-4" aria-hidden />
        Knowledge bases
      </Link>

      {state.status === "loading" && <KbDetailSkeleton />}

      {state.status === "error" &&
        (state.error.status === 404 ? (
          // RLS hides other orgs' rows, so "not yours" and "deleted" are both a 404 here.
          <EmptyState title="Knowledge base not found" body="It may have been deleted, or it belongs to an organization you're not signed in to." />
        ) : (
          <ErrorState error={state.error} onRetry={reload} />
        ))}

      {state.status === "ready" && (
        <>
          <h1 className="text-2xl font-semibold tracking-tight break-words">{state.data[0].name}</h1>
          <KbTabs kbId={id} canEdit={can(state.data[1].role, "edit")} />
        </>
      )}
    </section>
  );
}
