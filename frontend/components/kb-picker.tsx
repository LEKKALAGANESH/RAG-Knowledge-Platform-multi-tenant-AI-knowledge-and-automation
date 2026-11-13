"use client";

import { ChevronDown, Library } from "lucide-react";
import { Popover } from "radix-ui";
import { Button } from "@/components/ui/button";
import type { KnowledgeBase } from "@/lib/api/types";

type Props = {
  kbs: KnowledgeBase[];
  /** Selected KB ids; an empty array means "all". */
  value: string[];
  onChange: (ids: string[]) => void;
  disabled?: boolean;
};

export function kbSummary(kbs: KnowledgeBase[], value: string[]): string {
  if (value.length === 0 || value.length === kbs.length) return "All knowledge bases";
  if (value.length === 1) return kbs.find((k) => k.id === value[0])?.name ?? "1 knowledge base";
  return `${value.length} knowledge bases`;
}

/** Multi-select over the user's KBs. Unchecking the last one is blocked: a search over nothing is never useful. */
export function KbPicker({ kbs, value, onChange, disabled }: Props) {
  const all = value.length === 0;
  const checked = (id: string) => all || value.includes(id);
  const toggle = (id: string) => {
    const current = all ? kbs.map((k) => k.id) : value;
    const next = current.includes(id) ? current.filter((x) => x !== id) : [...current, id];
    if (next.length === 0) return;
    onChange(next.length === kbs.length ? [] : next);
  };

  return (
    <Popover.Root>
      <Popover.Trigger asChild>
        <Button variant="outline" disabled={disabled} className="max-w-full min-w-0 justify-start pointer-coarse:h-11">
          <Library aria-hidden />
          <span className="truncate">{kbSummary(kbs, value)}</span>
          <ChevronDown aria-hidden className="ml-auto opacity-60" />
        </Button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content
          align="start"
          sideOffset={6}
          collisionPadding={16}
          className="z-50 grid max-h-[min(20rem,var(--radix-popover-content-available-height))] w-72 max-w-[calc(100vw-2rem)] gap-0.5 overflow-y-auto overscroll-contain rounded-lg border bg-popover p-1.5 text-sm shadow-md"
        >
          <p className="px-2 py-1.5 text-xs text-muted-foreground">Use these knowledge bases</p>
          {kbs.map((kb) => (
            <label
              key={kb.id}
              className="flex min-h-9 cursor-pointer items-center gap-2.5 rounded-md px-2 hover:bg-muted has-focus-visible:ring-2 has-focus-visible:ring-ring pointer-coarse:min-h-11"
            >
              <input
                type="checkbox"
                checked={checked(kb.id)}
                onChange={() => toggle(kb.id)}
                className="size-4 accent-primary outline-none"
              />
              <span className="min-w-0 truncate">{kb.name}</span>
            </label>
          ))}
          {!all && (
            <Button variant="ghost" size="sm" className="mt-1 justify-start" onClick={() => onChange([])}>
              Select all
            </Button>
          )}
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}
