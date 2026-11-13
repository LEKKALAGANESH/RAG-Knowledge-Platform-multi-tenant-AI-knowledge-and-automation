"use client";

import Link from "next/link";
import { useState } from "react";
import { Loader2, Search as SearchIcon } from "lucide-react";
import { chunkLocLabel } from "@/components/document-chunks";
import { KbPicker } from "@/components/kb-picker";
import { EmptyState, ErrorState } from "@/components/states";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { api } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import { useRetryCountdown } from "@/lib/use-retry-countdown";
import type { KnowledgeBase, SearchHit, SearchMode } from "@/lib/api/types";
import { useResource } from "@/lib/use-resource";
import { cn } from "@/lib/utils";

const MODES: { value: SearchMode; label: string; hint: string }[] = [
  { value: "hybrid", label: "Hybrid", hint: "Meaning and exact words combined" },
  { value: "vector", label: "Meaning", hint: "Finds passages that say the same thing in other words" },
  { value: "keyword", label: "Keyword", hint: "Exact words only" },
];

type Run =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "error"; error: ApiError }
  | { status: "ready"; results: SearchHit[] };

function Results({ run, onRetry }: { run: Run; onRetry: () => void }) {
  if (run.status === "idle") {
    return <p className="py-10 text-center text-sm text-muted-foreground">Search returns the passages chat would cite, ranked by relevance.</p>;
  }
  if (run.status === "loading") {
    return (
      <div aria-busy="true" aria-label="Searching" className="grid gap-3">
        {[0, 1, 2].map((i) => <Skeleton key={i} className="h-24 w-full" />)}
      </div>
    );
  }
  if (run.status === "error") return <ErrorState error={run.error} onRetry={onRetry} />;
  if (run.results.length === 0) {
    return <EmptyState title="No matching passages" body="Try other words, switch to Meaning mode, or include more knowledge bases." />;
  }
  return (
    <ol aria-label="Results" className="grid gap-3">
      {run.results.map((r) => (
        <li key={r.chunk_id} className="rounded-lg border p-4">
          <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
            <Link
              href={`/kbs/${encodeURIComponent(r.kb_id)}?doc=${encodeURIComponent(r.document_id)}`}
              className="min-w-0 font-medium break-words text-primary underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-ring"
            >
              {r.document_name}
            </Link>
            {chunkLocLabel(r.loc) && <span className="text-xs text-muted-foreground">{chunkLocLabel(r.loc)}</span>}
            <span className="ml-auto font-mono text-xs text-muted-foreground" title="Relevance score">{r.score.toFixed(3)}</span>
          </div>
          <p className="mt-2 line-clamp-4 text-sm break-words whitespace-pre-wrap text-foreground/90">{r.text}</p>
        </li>
      ))}
    </ol>
  );
}

function SearchForm({ kbs }: { kbs: KnowledgeBase[] }) {
  const [query, setQuery] = useState("");
  const [mode, setMode] = useState<SearchMode>("hybrid");
  const [kbIds, setKbIds] = useState<string[]>([]);
  const [run, setRun] = useState<Run>({ status: "idle" });
  const wait = useRetryCountdown(run.status === "error" ? run.error.retryAt : undefined);

  async function go() {
    const q = query.trim();
    if (!q || wait > 0) return;
    setRun({ status: "loading" });
    try {
      const res = await api.search({ query: q, mode, kb_ids: kbIds.length ? kbIds : undefined });
      setRun({ status: "ready", results: res.results });
    } catch (err) {
      setRun({ status: "error", error: err instanceof ApiError ? err : new ApiError(0, "unknown", "Something went wrong. Try again.") });
    }
  }

  return (
    <div className="grid gap-5">
      <form
        role="search"
        onSubmit={(e) => {
          e.preventDefault();
          void go();
        }}
        className="grid gap-3"
      >
        <div className="flex gap-2">
          <label htmlFor="search-query" className="sr-only">Search query</label>
          <Input
            id="search-query"
            type="search"
            value={query}
            maxLength={2000}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search your documents"
            className="h-9 pointer-coarse:h-11"
          />
          <Button type="submit" disabled={!query.trim() || run.status === "loading" || wait > 0} className="h-9 pointer-coarse:h-11">
            {run.status === "loading" ? <Loader2 className="animate-spin" aria-hidden /> : <SearchIcon aria-hidden />}
            Search
          </Button>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <div role="radiogroup" aria-label="Search mode" className="inline-flex rounded-lg border p-0.5">
            {MODES.map((m) => (
              <button
                key={m.value}
                type="button"
                role="radio"
                aria-checked={mode === m.value}
                title={m.hint}
                onClick={() => setMode(m.value)}
                className={cn(
                  "h-7 rounded-md px-2.5 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring pointer-coarse:h-10",
                  mode === m.value ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:text-foreground",
                )}
              >
                {m.label}
              </button>
            ))}
          </div>
          <div className="min-w-0">
            <KbPicker kbs={kbs} value={kbIds} onChange={setKbIds} />
          </div>
        </div>
      </form>
      <Results run={run} onRetry={() => void go()} />
    </div>
  );
}

export function Search() {
  const { state, reload } = useResource(api.listKbs);
  return (
    <div className="mx-auto grid max-w-[768px] gap-5">
      <h1 className="text-xl font-semibold">Search</h1>
      {state.status === "loading" && <Skeleton aria-label="Loading knowledge bases" className="h-20 w-full" />}
      {state.status === "error" && <ErrorState error={state.error} onRetry={reload} />}
      {state.status === "ready" &&
        (state.data.items.length === 0 ? (
          <EmptyState
            title="Nothing to search yet"
            body="Create a knowledge base and upload documents first."
            action={<Button asChild><Link href="/kbs">Go to knowledge bases</Link></Button>}
          />
        ) : (
          <SearchForm kbs={state.data.items} />
        ))}
    </div>
  );
}
