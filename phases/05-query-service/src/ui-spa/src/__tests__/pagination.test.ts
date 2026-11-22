import { describe, expect, it } from "vitest";
import { boundedContinuationToken } from "../lib/pagination";

describe("boundedContinuationToken", () => {
  it("accepts a new opaque token within the page budget", () => {
    const seen = new Set<string>();
    expect(boundedContinuationToken(null, "next-1", seen, 1, 2)).toBe("next-1");
    expect(seen).toEqual(new Set(["next-1"]));
  });

  it("stops when a backend repeats the current or a previously seen token", () => {
    const seen = new Set(["old"]);
    expect(boundedContinuationToken("same", "same", seen, 1, 10)).toBeNull();
    expect(boundedContinuationToken("current", "old", seen, 2, 10)).toBeNull();
  });

  it("stops at the page budget even if every empty page has a new token", () => {
    expect(boundedContinuationToken("a", "b", new Set(), 2, 2)).toBeNull();
  });

  it("stops when the response has no continuation token", () => {
    expect(boundedContinuationToken("a", null, new Set(), 1, 2)).toBeNull();
  });
});
