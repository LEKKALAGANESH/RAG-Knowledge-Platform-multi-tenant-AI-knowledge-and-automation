/** Budget signals travel on a window event so every caller (JSON, SSE, upload) reports them in one place. */
export type BudgetSignal = { kind: "exceeded"; message: string } | { kind: "warning" };

export const BUDGET_EXCEEDED = "budget_exceeded";
const EVENT = "rag:budget";

export function reportBudget(signal: BudgetSignal): void {
  if (typeof window !== "undefined") window.dispatchEvent(new CustomEvent<BudgetSignal>(EVENT, { detail: signal }));
}

export function onBudget(fn: (signal: BudgetSignal) => void): () => void {
  const listener = (e: Event) => fn((e as CustomEvent<BudgetSignal>).detail);
  window.addEventListener(EVENT, listener);
  return () => window.removeEventListener(EVENT, listener);
}

// Mirrors the backend: Decimal >= 0, <= 10,000,000, at most 2 decimal places.
const DECIMAL = /^\d+(\.\d{1,2})?$/;
export const MAX_BUDGET = 10_000_000;

/** Monthly budget field: empty = unlimited (null); otherwise a plain decimal string the API accepts. */
export function parseBudget(input: string): { ok: true; value: string | null } | { ok: false; error: string } {
  const v = input.trim();
  if (v === "") return { ok: true, value: null };
  if (!DECIMAL.test(v)) return { ok: false, error: "Enter an amount in dollars, like 250 or 99.50." };
  if (Number(v) > MAX_BUDGET) return { ok: false, error: "The budget can be at most $10,000,000." };
  return { ok: true, value: v };
}
