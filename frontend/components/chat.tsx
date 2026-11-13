"use client";

import Link from "next/link";
import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { History, Plus, RotateCw, SearchX, Send, Settings2, Square, TriangleAlert } from "lucide-react";
import { CitationPanel } from "@/components/citation-panel";
import { CitedText } from "@/components/cited-text";
import { KbPicker } from "@/components/kb-picker";
import { EmptyState, ErrorState } from "@/components/states";
import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetTitle, SheetTrigger } from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import { api } from "@/lib/api/client";
import { ApiError, rateLimitMessage } from "@/lib/api/errors";
import type { Citation, Conversation, KnowledgeBase } from "@/lib/api/types";
import { streamAnswer } from "@/lib/sse";
import { useResource } from "@/lib/use-resource";
import { useRetryCountdown } from "@/lib/use-retry-countdown";
import { cn } from "@/lib/utils";

type StreamError = { code: string; message: string; retryAt?: number };
type Turn =
  | { id: string; role: "user"; content: string }
  | {
      id: string;
      role: "assistant";
      content: string;
      citations: Citation[];
      status: "streaming" | "done" | "stopped" | "error";
      abstained: boolean;
      error: StreamError | null;
      question: string;
    };
type Answer = Extract<Turn, { role: "assistant" }>;

type Loaded = { id: string; kbIds: string[]; turns: Turn[] };

const LLM_NOT_CONFIGURED = "llm_not_configured";

function toStreamError(err: unknown): StreamError {
  return err instanceof ApiError
    ? { code: err.code, message: err.message, retryAt: err.retryAt }
    : { code: "unknown", message: "Something went wrong. Try again." };
}

function fromStored(conv: Awaited<ReturnType<typeof api.getConversation>>): Loaded {
  let question = "";
  const turns = conv.messages.map((m): Turn => {
    if (m.role !== "assistant") {
      question = m.content;
      return { id: m.id, role: "user", content: m.content };
    }
    // Rows stored before the server recorded the flag: infer it the way the server set it.
    const abstained = m.abstained ?? m.citations.length === 0;
    return { id: m.id, role: "assistant", content: m.content, citations: m.citations, status: "done", abstained, error: null, question };
  });
  return { id: conv.id, kbIds: conv.kb_ids, turns };
}

type NotConfiguredProps = { title?: string; note?: string; onRetry?: () => void };

/** Shown when the server has no LLM key (`llm_not_configured`); also used by agent runs. */
export function NotConfigured({ title = "Chat needs a language model", note = "Nothing was saved from this question.", onRetry }: NotConfiguredProps) {
  return (
    <div role="alert" className="grid gap-3 rounded-lg border border-warning/40 bg-warning/5 p-4 text-sm">
      <p className="flex items-center gap-2 font-medium">
        <Settings2 className="size-4 text-warning" aria-hidden />
        {title}
      </p>
      <p className="text-muted-foreground">
        No AI model is set up for this workspace yet, so answers can&apos;t be written. An admin must add an LLM provider
        key on the API server (for example <code className="font-mono text-xs">ANTHROPIC_API_KEY</code>, or an
        OpenAI-compatible endpoint) and restart it. {note}
      </p>
      <div className="flex flex-wrap gap-2">
        {onRetry && (
          <Button variant="outline" size="sm" onClick={onRetry}>
            <RotateCw aria-hidden />
            Try again
          </Button>
        )}
        <Button variant="ghost" size="sm" asChild>
          <Link href="/search">Search your sources instead</Link>
        </Button>
      </div>
    </div>
  );
}

function TurnError({ error, onRetry }: { error: StreamError; onRetry: () => void }) {
  const wait = useRetryCountdown(error.retryAt);
  return (
    <div role="alert" className="flex flex-wrap items-center gap-x-3 gap-y-2 text-sm text-destructive">
      <TriangleAlert className="size-4" aria-hidden />
      <span className="min-w-0 flex-1">{error.retryAt !== undefined ? rateLimitMessage(wait) : error.message}</span>
      <Button variant="outline" size="sm" onClick={onRetry} disabled={wait > 0}>
        <RotateCw aria-hidden />
        Retry
      </Button>
    </div>
  );
}

function AnswerBody({ turn, onOpen, onRetry }: { turn: Answer; onOpen: (c: Citation) => void; onRetry: () => void }) {
  if (turn.error?.code === LLM_NOT_CONFIGURED) return <NotConfigured onRetry={onRetry} />;
  const streaming = turn.status === "streaming";
  return (
    <div className="grid gap-2">
      {turn.abstained ? (
        <div className="flex gap-3 rounded-lg border border-dashed bg-muted/40 p-3 text-sm">
          <SearchX className="mt-0.5 size-4 shrink-0 text-muted-foreground" aria-hidden />
          <div>
            <p className="font-medium">Not found in your sources</p>
            <p className="text-muted-foreground">{turn.content}</p>
          </div>
        </div>
      ) : (
        (turn.content || streaming) && (
          <p className="leading-relaxed break-words whitespace-pre-wrap">
            <CitedText text={turn.content} citations={turn.citations} streaming={streaming} onOpen={onOpen} />
            {streaming && <span aria-hidden className="ml-0.5 inline-block h-4 w-1.5 animate-pulse bg-primary/60 align-text-bottom motion-reduce:animate-none" />}
          </p>
        )
      )}
      {turn.status === "stopped" && <p className="text-xs text-muted-foreground">Stopped. The partial answer was not saved.</p>}
      {turn.status === "error" && turn.error && <TurnError error={turn.error} onRetry={onRetry} />}
    </div>
  );
}

function Messages({ turns, onOpen, onRetry }: { turns: Turn[]; onOpen: (c: Citation) => void; onRetry: (t: Answer) => void }) {
  return (
    <ol aria-label="Conversation" className="grid gap-6">
      {turns.map((t) => (
        <li key={t.id} className={t.role === "user" ? "flex justify-end" : undefined}>
          {t.role === "user" ? (
            <p className="max-w-[85%] rounded-lg bg-muted px-3.5 py-2 break-words whitespace-pre-wrap">{t.content}</p>
          ) : (
            <div aria-live={t.status === "streaming" ? "polite" : undefined} aria-busy={t.status === "streaming"}>
              <AnswerBody turn={t} onOpen={onOpen} onRetry={() => onRetry(t)} />
            </div>
          )}
        </li>
      ))}
    </ol>
  );
}

function Composer({ busy, onSend, onStop }: { busy: boolean; onSend: (q: string) => void; onStop: () => void }) {
  const [text, setText] = useState("");
  const submit = () => {
    const q = text.trim();
    if (!q || busy) return;
    onSend(q);
    setText("");
  };
  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      submit();
    }
  };
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
      className="flex items-end gap-2 rounded-xl border bg-card p-2 shadow-sm focus-within:ring-2 focus-within:ring-ring/40"
    >
      <label htmlFor="chat-question" className="sr-only">Question</label>
      <textarea
        id="chat-question"
        rows={1}
        value={text}
        maxLength={4000}
        disabled={busy}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={onKeyDown}
        placeholder={busy ? "Writing the answer…" : "Ask about your documents"}
        className="max-h-48 min-h-9 flex-1 resize-none bg-transparent px-2 py-1.5 field-sizing-content outline-none placeholder:text-muted-foreground disabled:opacity-60"
      />
      {busy ? (
        <Button type="button" variant="outline" onClick={onStop} className="pointer-coarse:h-11">
          <Square aria-hidden className="fill-current" />
          Stop
        </Button>
      ) : (
        <Button type="submit" disabled={!text.trim()} aria-label="Send" className="pointer-coarse:size-11 size-9 p-0">
          <Send aria-hidden />
        </Button>
      )}
    </form>
  );
}

function HistoryList({ activeId, onNavigate, version }: { activeId: string | null; onNavigate?: () => void; version: number }) {
  const { state, reload } = useResource(api.listConversations);
  const seen = useRef(version);
  useEffect(() => {
    if (seen.current !== version) {
      seen.current = version;
      reload();
    }
  }, [version, reload]);

  if (state.status === "loading") return <Skeleton aria-label="Loading conversations" className="h-24 w-full" />;
  if (state.status === "error") return <ErrorState error={state.error} onRetry={reload} />;
  if (state.data.items.length === 0) return <p className="px-2 text-sm text-muted-foreground">Your conversations appear here.</p>;
  return (
    <ul className="grid gap-0.5">
      {state.data.items.map((c: Conversation) => (
        <li key={c.id}>
          <Link
            href={`/chat/${encodeURIComponent(c.id)}`}
            onClick={onNavigate}
            aria-current={c.id === activeId ? "page" : undefined}
            title={c.title}
            className={cn(
              "block truncate rounded-md px-2.5 py-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring pointer-coarse:py-3",
              c.id === activeId ? "bg-primary/10 font-medium text-primary" : "text-foreground/80 hover:bg-muted",
            )}
          >
            {c.title}
          </Link>
        </li>
      ))}
    </ul>
  );
}

function useAnswerStream(initial: Loaded | null) {
  const [turns, setTurns] = useState<Turn[]>(initial?.turns ?? []);
  const [busy, setBusy] = useState(false);
  const [conversationId, setConversationId] = useState<string | null>(initial?.id ?? null);
  const [historyVersion, setHistoryVersion] = useState(0);
  const abortRef = useRef<AbortController | null>(null);
  const convRef = useRef(conversationId);

  useEffect(() => () => abortRef.current?.abort(), []);

  async function run(question: string, kbIds: string[], answerId: string) {
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    const patch = (fn: (a: Answer) => Answer) =>
      setTurns((ts) => ts.map((t) => (t.id === answerId && t.role === "assistant" ? fn(t) : t)));
    try {
      const body = { question, kb_ids: kbIds.length ? kbIds : undefined, conversation_id: convRef.current ?? undefined };
      for await (const ev of streamAnswer(body, ctrl.signal)) {
        if (ev.type === "meta" && !convRef.current) {
          convRef.current = ev.data.conversation_id;
          setConversationId(ev.data.conversation_id);
          // Not router.replace: that would remount the page mid-stream.
          window.history.replaceState(null, "", `/chat/${ev.data.conversation_id}`);
        } else if (ev.type === "token") patch((a) => ({ ...a, content: a.content + ev.data.text }));
        else if (ev.type === "citation") patch((a) => ({ ...a, citations: [...a.citations, ev.data] }));
        else if (ev.type === "done") patch((a) => ({ ...a, status: "done", abstained: ev.data.abstained }));
        else if (ev.type === "error") patch((a) => ({ ...a, status: "error", error: ev.data }));
      }
      patch((a) => (a.status !== "streaming" ? a : ctrl.signal.aborted ? { ...a, status: "stopped" } : { ...a, status: "error", error: { code: "cut_off", message: "The answer was cut off. Try again." } }));
    } catch (err) {
      patch((a) => (ctrl.signal.aborted ? { ...a, status: "stopped" } : { ...a, status: "error", error: toStreamError(err) }));
    } finally {
      setBusy(false);
      setHistoryVersion((v) => v + 1);
    }
  }

  function send(question: string, kbIds: string[]) {
    const answerId = crypto.randomUUID();
    setBusy(true);
    setTurns((ts) => [
      ...ts,
      { id: crypto.randomUUID(), role: "user", content: question },
      { id: answerId, role: "assistant", content: "", citations: [], status: "streaming", abstained: false, error: null, question },
    ]);
    void run(question, kbIds, answerId);
  }

  /** Drops the failed exchange and asks again. */
  function retry(failed: Answer, kbIds: string[]) {
    setTurns((ts) => {
      const i = ts.findIndex((t) => t.id === failed.id);
      return i > 0 ? ts.slice(0, i - 1).concat(ts.slice(i + 1)) : ts;
    });
    send(failed.question, kbIds);
  }

  return { turns, busy, conversationId, historyVersion, send, retry, stop: () => abortRef.current?.abort() };
}

function ChatView({ kbs, initial }: { kbs: KnowledgeBase[]; initial: Loaded | null }) {
  const chat = useAnswerStream(initial);
  const [kbIds, setKbIds] = useState<string[]>(initial?.kbIds ?? []);
  const [open, setOpen] = useState<Citation | null>(null);
  const [historyOpen, setHistoryOpen] = useState(false);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: "end" });
  }, [chat.turns]);

  const noKbs = kbs.length === 0;
  return (
    <div className="grid gap-6 lg:grid-cols-[15rem_minmax(0,1fr)]">
      <aside aria-label="Conversation history" className="sticky top-20 hidden max-h-[calc(100dvh-7rem)] self-start overflow-y-auto overscroll-contain lg:block">
        <h2 className="mb-2 px-2.5 text-sm font-medium text-muted-foreground">Conversations</h2>
        <HistoryList activeId={chat.conversationId} version={chat.historyVersion} />
      </aside>
      <section className="mx-auto flex w-full max-w-[768px] min-w-0 flex-col gap-4">
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="mr-auto text-xl font-semibold">Chat</h1>
          <Sheet open={historyOpen} onOpenChange={setHistoryOpen}>
            <SheetTrigger asChild>
              <Button variant="ghost" size="icon" aria-label="Conversation history" className="lg:hidden pointer-coarse:size-11">
                <History aria-hidden />
              </Button>
            </SheetTrigger>
            <SheetContent side="left" className="w-72 overflow-y-auto overscroll-contain p-3">
              <SheetTitle className="px-2 pt-1">Conversations</SheetTitle>
              <HistoryList activeId={chat.conversationId} version={chat.historyVersion} onNavigate={() => setHistoryOpen(false)} />
            </SheetContent>
          </Sheet>
          <Button
            variant="outline"
            className="pointer-coarse:h-11"
            // A full load, not <Link>: after history.replaceState the router may reuse this mounted page.
            // eslint-disable-next-line @next/next/no-location-assign-relative-destination
            onClick={() => window.location.assign("/chat")}
          >
            <Plus aria-hidden />
            New chat
          </Button>
        </div>
        {noKbs ? (
          <EmptyState
            title="No knowledge bases yet"
            body="Chat answers only from your documents. Create a knowledge base and upload files first."
            action={<Button asChild><Link href="/kbs">Go to knowledge bases</Link></Button>}
          />
        ) : (
          <>
            <div className="min-w-0">
              <KbPicker kbs={kbs} value={kbIds} onChange={setKbIds} disabled={chat.busy} />
            </div>
            {chat.turns.length === 0 ? (
              <div className="py-12 text-center">
                <p className="text-lg font-medium">Ask a question about your documents</p>
                <p className="mx-auto mt-1 max-w-md text-sm text-muted-foreground">
                  Answers come only from the selected knowledge bases. Each claim links to the passage it came from.
                </p>
              </div>
            ) : (
              <Messages turns={chat.turns} onOpen={setOpen} onRetry={(t) => chat.retry(t, kbIds)} />
            )}
            <div ref={endRef} className="sticky bottom-0 -mx-1 bg-background/95 px-1 pt-2 pb-[max(1rem,env(safe-area-inset-bottom))] backdrop-blur">
              <Composer busy={chat.busy} onSend={(q) => chat.send(q, kbIds)} onStop={chat.stop} />
              <p className="mt-1.5 hidden text-center text-xs text-muted-foreground sm:block">Enter to send · Shift+Enter for a new line</p>
            </div>
          </>
        )}
      </section>
      <CitationPanel citation={open} onClose={() => setOpen(null)} />
    </div>
  );
}

export function ChatSkeleton() {
  return (
    <div aria-busy="true" aria-label="Loading chat" className="mx-auto grid max-w-[768px] gap-4">
      <Skeleton className="h-8 w-40" />
      <Skeleton className="h-24 w-full" />
      <Skeleton className="h-12 w-full" />
    </div>
  );
}

/** `/chat` (new) or `/chat/<id>` (stored conversation with its citations). */
export function Chat({ conversationId }: { conversationId: string | null }) {
  const { state, reload } = useResource(async () => {
    const [kbs, conv] = await Promise.all([api.listKbs(), conversationId ? api.getConversation(conversationId) : null]);
    return { kbs: kbs.items, initial: conv && fromStored(conv) };
  });
  if (state.status === "loading") return <ChatSkeleton />;
  if (state.status === "error") {
    return state.error.status === 404 ? (
      <EmptyState
        title="Conversation not found"
        body="It may have been deleted, or it belongs to another account."
        action={<Button asChild><Link href="/chat">Start a new chat</Link></Button>}
      />
    ) : (
      <ErrorState error={state.error} onRetry={reload} />
    );
  }
  return <ChatView kbs={state.data.kbs} initial={state.data.initial} />;
}
