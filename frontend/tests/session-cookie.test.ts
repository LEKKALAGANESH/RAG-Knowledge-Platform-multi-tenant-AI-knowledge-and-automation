import { NextResponse } from "next/server";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("server-only", () => ({}));

const tokens = { access_token: "at", refresh_token: "rt" };
async function refreshMaxAge(days?: string) {
  vi.resetModules();
  vi.stubEnv("REFRESH_TOKEN_TTL_DAYS", days);
  const { setSessionCookies } = await import("@/lib/server/session");
  const res = NextResponse.json({});
  setSessionCookies(res, tokens);
  return res.cookies.get("rag_rt")?.maxAge;
}

afterEach(() => vi.unstubAllEnvs());

describe("refresh cookie max-age", () => {
  it("defaults to the backend's 7-day refresh TTL", async () => {
    expect(await refreshMaxAge()).toBe(7 * 86400);
    expect(await refreshMaxAge("")).toBe(7 * 86400);
  });

  it("follows REFRESH_TOKEN_TTL_DAYS", async () => {
    expect(await refreshMaxAge("14")).toBe(14 * 86400);
  });

  it.each(["0", "91", "1.5", "abc"])("rejects invalid REFRESH_TOKEN_TTL_DAYS=%j", async (days) => {
    await expect(refreshMaxAge(days)).rejects.toThrow();
  });
});
