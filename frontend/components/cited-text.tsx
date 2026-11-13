import type { ReactNode } from "react";
import type { Citation } from "@/lib/api/types";

const MARKER = /\[c(\d+)\]/g;
// While streaming, a marker can arrive split across tokens ("[c" … "1]"); hold the fragment back.
const PARTIAL_TAIL = /\[(c\d*)?$/;

type Props = {
  text: string;
  citations: Citation[];
  streaming?: boolean;
  onOpen: (c: Citation) => void;
};

/** Renders answer text with each `[cN]` replaced by a numbered chip; markers with no citation are dropped. */
export function CitedText({ text, citations, streaming = false, onOpen }: Props) {
  const shown = streaming ? text.replace(PARTIAL_TAIL, "") : text;
  const byN = new Map(citations.map((c) => [c.n, c]));
  const out: ReactNode[] = [];
  let last = 0;
  for (const m of shown.matchAll(MARKER)) {
    out.push(shown.slice(last, m.index));
    last = m.index + m[0].length;
    const c = byN.get(Number(m[1]));
    if (!c) continue;
    out.push(
      <button
        key={m.index}
        type="button"
        onClick={() => onOpen(c)}
        aria-label={`Source ${c.n}: ${c.document_name}`}
        title={c.document_name}
        className="mx-0.5 inline-flex h-5 min-w-5 -translate-y-px items-center justify-center rounded-sm border border-primary/30 bg-primary/10 px-1 align-middle font-mono text-[11px] leading-none font-medium text-primary hover:bg-primary hover:text-primary-foreground focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-ring pointer-coarse:h-7 pointer-coarse:min-w-7"
      >
        {c.n}
      </button>,
    );
  }
  out.push(shown.slice(last));
  return <>{out}</>;
}
