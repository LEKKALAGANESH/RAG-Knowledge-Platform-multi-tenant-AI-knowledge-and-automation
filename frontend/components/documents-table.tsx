"use client";

import { useState, type ReactNode } from "react";
import { useSearchParams } from "next/navigation";
import { AlertTriangle, ChevronDown, Loader2, RotateCw, Trash2 } from "lucide-react";
import { DocumentContent } from "@/components/document-chunks";
import { StatusPill } from "@/components/status-pill";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { api } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import type { KbDocument } from "@/lib/api/types";
import { formatBytes, formatDate, formatDateTime } from "@/lib/format";

const TYPE: Record<string, string> = {
  "application/pdf": "PDF",
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "DOCX",
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "XLSX",
  "text/csv": "CSV",
  "text/plain": "TXT",
  "text/markdown": "MD",
  "text/html": "HTML",
  "application/json": "JSON",
  "application/x-ndjson": "JSONL",
};

const warningText = (w: unknown) => (typeof w === "string" ? w : JSON.stringify(w));
const errorMessage = (err: unknown, fallback: string) => (err instanceof ApiError ? err.message : fallback);

function Warnings({ doc }: { doc: KbDocument }) {
  const [open, setOpen] = useState(false);
  const n = doc.warnings.length;
  if (n === 0) return null;
  return (
    <div className="mt-1.5">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
        className="inline-flex items-center gap-1 rounded-sm text-xs text-foreground hover:underline focus-visible:outline-2 focus-visible:outline-ring"
      >
        <AlertTriangle className="size-3.5 text-warning" aria-hidden />
        {n} {n === 1 ? "warning" : "warnings"}
        <ChevronDown className={open ? "size-3.5 rotate-180" : "size-3.5"} aria-hidden />
      </button>
      {open && (
        <ul className="mt-1 list-disc pl-5 text-xs text-muted-foreground">
          {doc.warnings.map((w, i) => (
            <li key={i} className="break-words">{warningText(w)}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

function ReprocessButton({ doc, onDone }: { doc: KbDocument; onDone: () => void }) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  async function run() {
    setPending(true);
    setError(null);
    try {
      await api.reprocessDocument(doc.id);
      onDone();
    } catch (err) {
      setError(errorMessage(err, "Couldn't reprocess the document. Try again."));
    } finally {
      setPending(false);
    }
  }
  return (
    <>
      <Button size="sm" variant="outline" onClick={run} disabled={pending} aria-label={`Reprocess ${doc.name}`}>
        {pending ? <Loader2 className="animate-spin" aria-hidden /> : <RotateCw aria-hidden />}
        <span className="sr-only sm:not-sr-only">Reprocess</span>
      </Button>
      {error && <p role="alert" className="mt-1 text-xs text-destructive">{error}</p>}
    </>
  );
}

function DeleteDialog({ doc, onClose, onDeleted }: { doc: KbDocument | null; onClose: () => void; onDeleted: () => void }) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  async function confirm() {
    if (!doc) return;
    setPending(true);
    setError(null);
    try {
      await api.deleteDocument(doc.id);
      onClose();
      onDeleted();
    } catch (err) {
      setError(errorMessage(err, "Couldn't delete the document. Try again."));
    } finally {
      setPending(false);
    }
  }
  return (
    <Dialog
      open={doc !== null}
      onOpenChange={(open) => {
        if (open) return;
        setError(null);
        onClose();
      }}
    >
      <DialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Delete document?</DialogTitle>
          <DialogDescription className="break-words">
            &ldquo;{doc?.name}&rdquo; and its indexed content will be removed from this knowledge base. This can&rsquo;t be undone.
          </DialogDescription>
        </DialogHeader>
        {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>Cancel</Button>
          <Button variant="destructive" onClick={confirm} disabled={pending}>
            {pending && <Loader2 className="animate-spin" aria-hidden />}
            Delete document
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="grid grid-cols-[96px_1fr] gap-3 py-2">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="min-w-0 break-words">{children}</dd>
    </div>
  );
}

function DocumentDrawer({ doc, onClose }: { doc: KbDocument | null; onClose: () => void }) {
  return (
    <Sheet open={doc !== null} onOpenChange={(open) => !open && onClose()}>
      <SheetContent className="overflow-y-auto overscroll-contain">
        {doc && (
          <>
            <SheetHeader>
              <SheetTitle className="pr-8 break-words">{doc.name}</SheetTitle>
              <SheetDescription>Document details</SheetDescription>
            </SheetHeader>
            <dl className="divide-y px-4 text-sm">
              <Field label="Status"><StatusPill status={doc.status} /></Field>
              <Field label="Type"><span className="font-mono text-xs">{doc.mime}</span></Field>
              <Field label="Size">{formatBytes(doc.meta.size)}</Field>
              <Field label="SHA-256"><code className="font-mono text-xs" title={doc.sha256}>{doc.sha256.slice(0, 12)}…</code></Field>
              <Field label="Version">{doc.version}</Field>
              <Field label="Added">{formatDateTime(doc.created_at)}</Field>
              {doc.error && <Field label="Error"><span className="text-destructive">{doc.error}</span></Field>}
              <Field label="Warnings">
                {doc.warnings.length === 0 ? (
                  <span className="text-muted-foreground">None</span>
                ) : (
                  <ul className="list-disc pl-4">
                    {doc.warnings.map((w, i) => <li key={i}>{warningText(w)}</li>)}
                  </ul>
                )}
              </Field>
            </dl>
            <DocumentContent doc={doc} />
          </>
        )}
      </SheetContent>
    </Sheet>
  );
}

export function DocumentsTable({ docs, canEdit, onChanged }: { docs: KbDocument[]; canEdit: boolean; onChanged: () => void }) {
  // `?doc=<id>` (citation "Open in knowledge base" links) opens that document's drawer on arrival.
  const [openId, setOpenId] = useState<string | null>(useSearchParams().get("doc"));
  const [deleting, setDeleting] = useState<KbDocument | null>(null);
  // Read from the live list so the drawer follows polling and closes when the doc disappears.
  const open = docs.find((d) => d.id === openId) ?? null;

  return (
    <>
      <div className="relative overflow-x-auto rounded-lg border">
        <table className="w-full text-sm sm:min-w-[640px]">
          <thead className="bg-muted/50 text-left text-xs text-muted-foreground">
            <tr>
              <th scope="col" className="px-4 py-2.5 font-medium">Name</th>
              <th scope="col" className="hidden px-4 py-2.5 font-medium md:table-cell">Type</th>
              <th scope="col" className="hidden px-4 py-2.5 font-medium md:table-cell">Size</th>
              <th scope="col" className="px-4 py-2.5 font-medium">Status</th>
              <th scope="col" className="hidden px-4 py-2.5 font-medium sm:table-cell">Added</th>
              {canEdit && <th scope="col" className="px-4 py-2.5 font-medium"><span className="sr-only">Actions</span></th>}
            </tr>
          </thead>
          <tbody className="divide-y">
            {docs.map((doc) => (
              <tr key={doc.id} className="align-top">
                <td className="max-w-[160px] px-4 py-3 sm:max-w-[320px]">
                  <button
                    type="button"
                    onClick={() => setOpenId(doc.id)}
                    title={doc.name}
                    className="block max-w-full truncate rounded-sm text-left font-medium hover:underline focus-visible:outline-2 focus-visible:outline-ring"
                  >
                    {doc.name}
                  </button>
                </td>
                <td className="hidden px-4 py-3 text-muted-foreground md:table-cell">{TYPE[doc.mime] ?? doc.mime}</td>
                <td className="hidden px-4 py-3 whitespace-nowrap text-muted-foreground md:table-cell">{formatBytes(doc.meta.size)}</td>
                <td className="max-w-[280px] px-4 py-3">
                  <StatusPill status={doc.status} reason={doc.error} />
                  {doc.status === "failed" && (
                    <p className="mt-1.5 text-xs break-words text-destructive">{doc.error ?? "No reason was recorded."}</p>
                  )}
                  <Warnings doc={doc} />
                </td>
                <td className="hidden px-4 py-3 whitespace-nowrap text-muted-foreground sm:table-cell">{formatDate(doc.created_at)}</td>
                {canEdit && (
                  <td className="px-4 py-3">
                    <div className="flex items-start justify-end gap-2">
                      {doc.status === "failed" && (
                        <div><ReprocessButton doc={doc} onDone={onChanged} /></div>
                      )}
                      <Button size="icon-sm" variant="ghost" onClick={() => setDeleting(doc)} aria-label={`Delete ${doc.name}`}>
                        <Trash2 aria-hidden />
                      </Button>
                    </div>
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <DocumentDrawer doc={open} onClose={() => setOpenId(null)} />
      <DeleteDialog doc={deleting} onClose={() => setDeleting(null)} onDeleted={onChanged} />
    </>
  );
}
