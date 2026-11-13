/** `compact="below-lg"` hides the name while the sidebar is icon-only (md to lg). */
export function BrandMark({ compact = false }: { compact?: boolean | "below-lg" }) {
  return (
    <span className="flex items-center gap-2.5 font-semibold tracking-tight">
      <svg viewBox="0 0 24 24" className="size-7 shrink-0" aria-hidden>
        <rect width="24" height="24" rx="6" className="fill-primary" />
        <path d="M7 7h7a3 3 0 0 1 0 6H7zM7 13v4M11.5 13l4 4" className="fill-none stroke-primary-foreground" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
      <span className={compact === "below-lg" ? "sr-only lg:not-sr-only" : compact ? "sr-only" : undefined}>Knowledge Platform</span>
    </span>
  );
}
