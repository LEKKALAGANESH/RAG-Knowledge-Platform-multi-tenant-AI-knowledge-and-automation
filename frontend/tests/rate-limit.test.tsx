import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AuthForm } from "@/components/auth-form";
import { Search } from "@/components/search";
import { ErrorState } from "@/components/states";
import { ApiError, parseRetryAfter, rateLimitMessage, toApiError, wallClock } from "@/lib/api/errors";
import { mapUploadResponse } from "@/lib/upload";

const body = (code: string, message: string) => JSON.stringify({ error: { code, message, request_id: "r1" } });

describe("Retry-After parsing", () => {
  it("reads delta-seconds and HTTP dates, ignores junk", () => {
    expect(parseRetryAfter("36", 1_000)).toBe(37_000);
    expect(parseRetryAfter("Wed, 21 Oct 2026 07:28:00 GMT")).toBe(Date.parse("Wed, 21 Oct 2026 07:28:00 GMT"));
    expect(parseRetryAfter(null)).toBeUndefined();
    expect(parseRetryAfter("soon")).toBeUndefined();
  });

  it("a 429 rate_limited carries retryAt; the fixed server sentence is replaced", async () => {
    const before = wallClock();
    const err = await toApiError(new Response(body("rate_limited", "Too many requests. Retry in 36 s."), { status: 429, headers: { "Retry-After": "36" } }));
    expect(err).toMatchObject({ status: 429, code: "rate_limited", retryable: true });
    expect(err.retryAt! - before).toBeGreaterThanOrEqual(36_000);
    expect(err.retryAt! - before).toBeLessThan(37_000);
    expect(err.message).not.toMatch(/36/);
  });

  it("503 rate_limiter_unavailable gets its own readable message", async () => {
    const err = await toApiError(new Response(body("rate_limiter_unavailable", "Redis down"), { status: 503 }));
    expect(err.message).toBe("Sign-in is paused because a safety service is down. Try again in a minute.");
    expect(err.retryAt).toBeUndefined();
  });

  it("upload XHR passes Retry-After through", async () => {
    const out = await mapUploadResponse(429, body("rate_limited", "x"), "12");
    expect(out.kind === "error" && out.retryAt! - wallClock()).toBeGreaterThan(11_000);
  });
});

describe("countdown", () => {
  afterEach(() => vi.useRealTimers());

  it("ErrorState counts down and keeps Try again disabled until Retry-After elapses", async () => {
    vi.useFakeTimers({ now: 100_000 });
    const onRetry = vi.fn();
    render(<ErrorState error={new ApiError(429, "rate_limited", "Too many requests.", "r", 103_000)} onRetry={onRetry} />);
    await act(async () => vi.advanceTimersByTime(0));
    expect(screen.getByRole("alert")).toHaveTextContent("Too many requests — try again in 3 s.");
    const button = screen.getByRole("button", { name: "Try again" });
    expect(button).toBeDisabled();
    await act(async () => vi.advanceTimersByTime(1000));
    expect(screen.getByRole("alert")).toHaveTextContent("try again in 2 s.");
    await act(async () => vi.advanceTimersByTime(2000));
    expect(screen.getByRole("alert")).toHaveTextContent("You can try again now.");
    expect(button).toBeEnabled();
  });

  it("formats the message", () => {
    expect(rateLimitMessage(36)).toBe("Too many requests — try again in 36 s.");
    expect(rateLimitMessage(0)).toBe("You can try again now.");
  });
});

describe("login rate limits", () => {
  async function signIn() {
    render(<AuthForm mode="login" />);
    await userEvent.type(screen.getByLabelText("Work email"), "a@b.co");
    await userEvent.type(screen.getByLabelText("Password"), "secret-pass");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
  }

  it("429 shows the countdown and disables Sign in", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(body("rate_limited", "x"), { status: 429, headers: { "Retry-After": "36" } })));
    await signIn();
    expect(await screen.findByRole("alert")).toHaveTextContent(/Too many requests — try again in 3[56] s\./);
    expect(screen.getByRole("button", { name: "Sign in" })).toBeDisabled();
  });

  it("503 rate_limiter_unavailable explains the outage", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(body("rate_limiter_unavailable", "x"), { status: 503 })));
    await signIn();
    expect(await screen.findByRole("alert")).toHaveTextContent("Sign-in is paused because a safety service is down.");
    expect(screen.getByRole("button", { name: "Sign in" })).toBeEnabled();
  });
});

describe("search rate limit", () => {
  it("429 keeps both Search and Try again disabled while the countdown runs", async () => {
    const kb = { id: "kb1", name: "Policies", settings: {}, embedding_index_id: null, created_at: "2026-11-12T00:00:00Z" };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) =>
        url.startsWith("/api/v1/kbs")
          ? new Response(JSON.stringify({ items: [kb], next_cursor: null }), { status: 200 })
          : new Response(body("rate_limited", "x"), { status: 429, headers: { "Retry-After": "30" } }),
      ),
    );
    render(<Search />);
    await userEvent.type(await screen.findByLabelText("Search query"), "refunds{Enter}");
    expect(await screen.findByRole("alert")).toHaveTextContent(/try again in (29|30) s\./);
    expect(screen.getByRole("button", { name: "Search" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Try again" })).toBeDisabled();
  });
});
