"use client";

import Link from "next/link";
import { FileText } from "lucide-react";
import { chunkLocLabel } from "@/components/document-chunks";
import { Skeleton } from "@/components/ui/skeleton";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { api } from "@/lib/api/client";
import type { Citation } from "@/lib/api/types";
import { useResource } from "@/lib/use-resource";

const linkClass =
  "inline-flex items-center gap-1.5 rounded-sm text-sm font-medium text-primary underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-ring";

function KbLink({ kbId, documentId }: { kbId: string; documentId: string }) {
  return (
    <Link href={`/kbs/${encodeURIComponent(kbId)}?doc=${encodeURIComponent(documentId)}`} className={linkClass}>
      <FileText className="size-4" aria-hidden />
      Open document in its knowledge base
    </Link>
  );
}

/** Citations stored before the server sent kb_id: the KB link waits on a document lookup. */
function LookupKbLink({ documentId }: { documentId: string }) {
  const { state } = useResource(() => api.getDocument(documentId));
  if (state.status === "loading") return <Skeleton aria-label="Loading document link" className="h-5 w-44" />;
  if (state.status === "error") {
    return (
      <p className="text-sm text-muted-foreground">
        {state.error.status === 404 ? "This document was deleted since the answer was written." : state.error.message}
      </p>
    );
  }
  return <KbLink kbId={state.data.kb_id} documentId={documentId} />;
}

export function CitationPanel({ citation, onClose }: { citation: Citation | null; onClose: () => void }) {
  return (
    <Sheet open={citation !== null} onOpenChange={(open) => !open && onClose()}>
      <SheetContent className="w-[min(100vw,28rem)] overflow-y-auto overscroll-contain data-[side=right]:sm:max-w-md">
        {citation && (
          <>
            <SheetHeader>
              <p className="flex size-6 items-center justify-center rounded-sm bg-primary font-mono text-xs font-medium text-primary-foreground">
                {citation.n}
              </p>
              <SheetTitle className="pr-8 break-words">{citation.document_name}</SheetTitle>
              <SheetDescription>{chunkLocLabel(citation.loc) ?? "Location not recorded"}</SheetDescription>
            </SheetHeader>
            <div className="grid gap-5 px-4 pb-6">
              <blockquote className="border-l-2 border-primary pl-3 text-sm leading-relaxed break-words whitespace-pre-wrap">
                {citation.excerpt}
              </blockquote>
              {/* No document id: an agent result stored before the server sent one, or the chunk was deleted. */}
              {!citation.document_id ? null : citation.kb_id ? (
                <KbLink kbId={citation.kb_id} documentId={citation.document_id} />
              ) : (
                <LookupKbLink key={citation.document_id} documentId={citation.document_id} />
              )}
            </div>
          </>
        )}
      </SheetContent>
    </Sheet>
  );
}
