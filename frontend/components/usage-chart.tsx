"use client";

import { useState } from "react";
import type { DayPoint } from "@/lib/usage";
import { cn } from "@/lib/utils";

const shortDay = new Intl.DateTimeFormat("en", { month: "short", day: "numeric", timeZone: "UTC" });
const dayLabel = (day: string) => shortDay.format(new Date(`${day}T00:00:00Z`));

/**
 * One measure per day as columns. Single series, so no legend: the title names it. Hover/focus shows the
 * exact value; the table view below is the keyboard and screen-reader path.
 */
export function DailyColumns({
  title,
  points,
  value,
  format,
  integer = false,
  empty,
}: {
  title: string;
  points: DayPoint[];
  value: (p: DayPoint) => number;
  format: (n: number) => string;
  /** Counts: the axis top rounds up to an even whole number so the midline label is whole too. */
  integer?: boolean;
  /** Shown instead of a flat chart when every day is 0. */
  empty: string;
}) {
  const [active, setActive] = useState<number | null>(null);
  const max = Math.max(0, ...points.map(value));
  const top = integer ? Math.max(2, Math.ceil(max / 2) * 2) : max;
  const shown = active === null ? null : points[active];

  if (max === 0) {
    return (
      <figure className="grid min-w-0 content-start gap-2 rounded-lg border bg-card p-4">
        <figcaption className="text-sm font-medium">{title}</figcaption>
        <p className="py-10 text-center text-sm text-muted-foreground">{empty}</p>
      </figure>
    );
  }

  return (
    <figure className="grid min-w-0 gap-2 rounded-lg border bg-card p-4">
      <figcaption className="text-sm font-medium">{title}</figcaption>
      <div className="grid grid-cols-[auto_1fr] gap-2">
        <div aria-hidden className="flex h-36 flex-col justify-between text-right text-xs text-muted-foreground tabular-nums">
          <span>{format(top)}</span>
          <span>{format(top / 2)}</span>
          <span>{format(0)}</span>
        </div>
        <div className="relative h-36 min-w-0 border-b border-foreground/20" onMouseLeave={() => setActive(null)}>
          <div aria-hidden className="absolute inset-x-0 top-0 border-t border-dashed border-border" />
          <div aria-hidden className="absolute inset-x-0 top-1/2 border-t border-dashed border-border" />
          <div className="absolute inset-0 flex items-end gap-[2px]">
            {points.map((p, i) => (
              <div
                key={p.day}
                data-testid="day-column"
                className="flex h-full min-w-0 flex-1 items-end"
                onMouseEnter={() => setActive(i)}
              >
                <div
                  className={cn(
                    "w-full rounded-t-[4px] bg-primary transition-opacity",
                    active !== null && active !== i && "opacity-40",
                  )}
                  style={{ height: `${(value(p) / top) * 100}%` }}
                />
              </div>
            ))}
          </div>
          {shown && active !== null && (
            <div
              role="status"
              className="pointer-events-none absolute -top-2 z-10 -translate-x-1/2 -translate-y-full rounded-md border bg-popover px-2 py-1 text-xs whitespace-nowrap shadow-sm"
              style={{ left: `${Math.min(85, Math.max(15, ((active + 0.5) / points.length) * 100))}%` }}
            >
              <span className="text-muted-foreground">{dayLabel(shown.day)}</span>{" "}
              <span className="font-medium tabular-nums">{format(value(shown))}</span>
            </div>
          )}
        </div>
        <span />
        {points.length > 0 && (
          <div aria-hidden className="flex justify-between text-xs text-muted-foreground">
            <span>{dayLabel(points[0].day)}</span>
            {points.length > 1 && <span>{dayLabel(points[points.length - 1].day)}</span>}
          </div>
        )}
      </div>
      <details className="text-sm">
        <summary className="cursor-pointer text-xs text-muted-foreground hover:text-foreground">Show as table</summary>
        <div className="mt-2 max-h-56 overflow-y-auto overscroll-contain">
          <table className="w-full text-xs">
            <caption className="sr-only">{title} by day (UTC)</caption>
            <thead className="text-left text-muted-foreground">
              <tr>
                <th scope="col" className="py-1 font-medium">Day</th>
                <th scope="col" className="py-1 text-right font-medium">{title}</th>
              </tr>
            </thead>
            <tbody>
              {points.map((p) => (
                <tr key={p.day} className="border-t">
                  <td className="py-1">{p.day}</td>
                  <td className="py-1 text-right tabular-nums">{format(value(p))}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </figure>
  );
}
