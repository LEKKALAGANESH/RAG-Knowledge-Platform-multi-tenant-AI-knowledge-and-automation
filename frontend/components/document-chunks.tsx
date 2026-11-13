"use client";

import { useEffect, useState } from "react";
import { Loader2 } from "lucide-react";
import { ErrorState } from "@/components/states";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { api } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import type { Chunk, ChunkLoc, KbDocument } from "@/lib/api/types";

const text = (v: unknown) => (typeof v === "string" || typeof v === "number" ? String(v) : null);

/** "Page 3", "Section: Expenses · table 1", "Sheet Invoices · A1:B3", "Record 4"; null when loc is empty. */
export function chunkLocLabel(loc: ChunkLoc): string | null {
  const parts: string[] = [];
  const [page, section, table, sheet, range, record] = ["page", "section", "table", "sheet", "range", "record"].map(
    (k) => text(loc[k]),
  );
  if (page) parts.push(`Page ${page}`);
  if (section) parts.push(`Section: ${section}`);
  if (table) parts.push(`table ${table}`);
  if (sheet) parts.push(`Sheet ${sheet}`);
  if (range) parts.push(parts.length ? range : `Range ${range}`);
  if (record) parts.push(`Record ${record}`);
  return parts.length ? parts.join(" · ") : null;
}

const toApiError = (err: unknown) =>
  err instanceof ApiError ? err : new ApiError(0, "unknown", "Something went wrong. Try again.");

const isLong = (t: string) => t.length > 400 || t.split("\n").length > 6;

function ChunkItem({ chunk }: { chunk: Chunk }) {
  const [expanded, setExpanded] = useState(false);
  const label = chunkLocLabel(chunk.loc);
  const long = isLong(chunk.text);
  return (
    <li className="py-3">
      <div className="mb-1 flex items-baseline gap-2 text-xs text-muted-foreground">
        <span className="font-mono">#{chunk.ord}</span>
        {label && <span className="min-w-0 break-words">{label}</span>}
      </div>
      <p className={`text-sm break-words whitespace-pre-wrap ${long && !expanded ? "line-clamp-6" : ""}`}>{chunk.text}</p>
      {long && (
        <button
          type="button"
          aria-expanded={expanded}
          onClick={() => setExpanded(!expanded)}
          className="mt-1 rounded-sm text-xs text-foreground hover:underline focus-visible:outline-2 focus-visible:outline-ring"
        >
          {expanded ? "Show less" : "Show more"}
        </button>
      )}
    </li>
  );
}

type State =
  | { status: "loading" }
  | { status: "error"; error: ApiError }
  | { status: "ready"; items: Chunk[]; next: string | null };

function useChunks(docId: string) {
  const [state, setState] = useState<State>({ status: "loading" });
  const [attempt, setAttempt] = useState(0);
  const [more, setMore] = useState<{ pending: boolean; error: ApiError | null }>({ pending: false, error: null });

  useEffect(() => {
    let live = true;
    api.listChunks(docId, null).then(
      (p) => live && setState({ status: "ready", items: p.items, next: p.next_cursor }),
      (err: unknown) => live && setState({ status: "error", error: toApiError(err) }),
    );
    return () => {
      live = false;
    };
  }, [docId, attempt]);

  const retry = () => {
    setState({ status: "loading" });
    setAttempt((n) => n + 1);
  };

  async function loadMore() {
    if (state.status !== "ready" || !state.next) return;
    setMore({ pending: true, error: null });
    try {
      const p = await api.listChunks(docId, state.next);
      setState({ status: "ready", items: [...state.items, ...p.items], next: p.next_cursor });
      setMore({ pending: false, error: null });
    } catch (err) {
      setMore({ pending: false, error: toApiError(err) });
    }
  }

  return { state, retry, more, loadMore };
}

function Chunks({ doc }: { doc: KbDocument }) {
  const { state, retry, more, loadMore } = useChunks(doc.id);
  if (state.status === "loading") {
    return (
      <div aria-label="Loading content" className="space-y-2">
        <Skeleton className="h-4 w-24" />
        <Skeleton className="h-16 w-full" />
        <Skeleton className="h-16 w-full" />
      </div>
    );
  }
  if (state.status === "error") return <ErrorState error={state.error} onRetry={retry} />;
  if (state.items.length === 0) {
    return (
      <div className="text-sm">
        <p className="font-medium">No content extracted</p>
        <p className="mt-1 text-muted-foreground">
          {doc.warnings.length > 0
            ? "See the warnings above for what was skipped."
            : "The file has no readable text (for example, a scanned PDF without OCR)."}
        </p>
      </div>
    );
  }
  return (
    <>
      <ol className="divide-y">
        {state.items.map((c) => <ChunkItem key={c.id} chunk={c} />)}
      </ol>
      {more.error && <p role="alert" className="mt-2 text-xs text-destructive">{more.error.message}</p>}
      {state.next && (
        <Button size="sm" variant="outline" className="mt-2" onClick={loadMore} disabled={more.pending}>
          {more.pending && <Loader2 className="animate-spin" aria-hidden />}
          Load more
        </Button>
      )}
    </>
  );
}

/** Drawer section; fetches only for ready documents. */
export function DocumentContent({ doc }: { doc: KbDocument }) {
  return (
    <section aria-labelledby="doc-content-heading" className="px-4 pb-6">
      <h3 id="doc-content-heading" className="mb-2 text-sm font-semibold">Content</h3>
      {doc.status === "ready" ? (
        <Chunks key={doc.id} doc={doc} />
      ) : (
        <p className="text-sm text-muted-foreground">
          {doc.status === "failed"
            ? "No content: processing failed. See the error above."
            : "Content appears here once processing finishes."}
        </p>
      )}
    </section>
  );
}
