import { describe, expect, it } from "vitest";
import { safeNext } from "@/lib/safe-next";

const ORIGIN = "http://localhost:3000";
const BS = String.fromCharCode(92); // backslash, spelled out so no tool or editor can collapse it

describe("safeNext", () => {
  it.each([`/${BS}evil.com`, "//evil.com", "https://evil.com", "javascript:alert(1)", `/${BS}/evil.com`, `${BS}${BS}evil.com`, "/.//evil.com", "/%2e//evil.com", "http://localhost:3000//evil.com", `/.${BS}${BS}evil.com`])(
    "rejects off-site target %s",
    (raw) => expect(safeNext(raw, ORIGIN)).toBe("/dashboard"),
  );

  it("keeps same-origin paths with query and hash", () => {
    expect(safeNext("/kbs?x=1", ORIGIN)).toBe("/kbs?x=1");
    expect(safeNext("/kbs/k1#docs", ORIGIN)).toBe("/kbs/k1#docs");
  });

  it("defaults when missing or unparsable", () => {
    expect(safeNext(null, ORIGIN)).toBe("/dashboard");
    expect(safeNext("http://[bad", ORIGIN)).toBe("/dashboard");
  });
});
