import type { UsageBucket } from "@/lib/api/types";

export type DayPoint = { day: string; cost: number; calls: number };

const DAY_MS = 86_400_000;
export const utcDay = (d: Date) => d.toISOString().slice(0, 10);

/** This UTC month up to today, as inclusive YYYY-MM-DD dates (what the date inputs show). */
export function defaultRange(now: Date): { from: string; to: string } {
  const today = utcDay(now);
  return { from: `${today.slice(0, 8)}01`, to: today };
}

/** Inclusive dates -> the API's UTC bounds; `to` is exclusive there, so it moves to the next midnight. */
export function rangeQuery(from: string, to: string): string {
  const end = utcDay(new Date(Date.parse(`${to}T00:00:00Z`) + DAY_MS));
  return `from=${encodeURIComponent(`${from}T00:00:00Z`)}&to=${encodeURIComponent(`${end}T00:00:00Z`)}`;
}

/** One point per day in the range (days without calls are 0), so quiet days show as gaps, not missing columns. */
export function dailySeries(from: string, to: string, buckets: UsageBucket[]): DayPoint[] {
  const byDay = new Map(buckets.map((b) => [b.key, b]));
  const out: DayPoint[] = [];
  const end = Date.parse(`${to}T00:00:00Z`);
  for (let t = Date.parse(`${from}T00:00:00Z`); t <= end && out.length <= 366; t += DAY_MS) {
    const day = utcDay(new Date(t));
    const b = byDay.get(day);
    out.push({ day, cost: b?.cost_usd ?? 0, calls: b?.calls ?? 0 });
  }
  return out;
}

export type Share = { key: string; label: string; cost: number; calls: number; fraction: number };

/** Breakdown rows, largest first. Bars follow cost, or calls when nothing in the range is priced. */
export function shares(buckets: UsageBucket[], fallback: string): Share[] {
  const byCost = buckets.some((b) => b.cost_usd > 0);
  const measure = (b: UsageBucket) => (byCost ? b.cost_usd : b.calls);
  const max = Math.max(0, ...buckets.map(measure));
  return [...buckets]
    .sort((a, b) => measure(b) - measure(a))
    .map((b) => ({
      key: b.key ?? "",
      label: b.label ?? b.key ?? fallback,
      cost: b.cost_usd,
      calls: b.calls,
      fraction: max > 0 ? measure(b) / max : 0,
    }));
}

export function formatUsd(n: number): string {
  if (n > 0 && n < 0.01) return `$${n.toFixed(4)}`;
  return n.toLocaleString("en", { style: "currency", currency: "USD" });
}
