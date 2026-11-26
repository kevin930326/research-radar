import { describe, expect, it } from "vitest";

import { clampReason, REASON_MAX_LENGTH } from "./ClassificationFix";

const FULL = "a".repeat(REASON_MAX_LENGTH - 1) + "z";

describe("clampReason", () => {
  it("keeps anything within the limit as typed", () => {
    expect(clampReason("short reason", "short")).toBe("short reason");
  });

  it("typing in the middle of a full reason does not eat the original ending", () => {
    const typed = FULL.slice(0, 10) + "x" + FULL.slice(10);
    expect(clampReason(typed, FULL)).toBe(FULL);
  });

  it("a paste keeps only what fits and leaves the text around it alone", () => {
    const previous = "b".repeat(40) + "e".repeat(40);
    const pasted = "p".repeat(50);
    const result = clampReason("b".repeat(40) + pasted + "e".repeat(40), previous);
    expect([...result]).toHaveLength(REASON_MAX_LENGTH);
    expect(result).toBe("b".repeat(40) + "p".repeat(20) + "e".repeat(40));
  });

  it("counts an emoji as one character", () => {
    const previous = "w".repeat(REASON_MAX_LENGTH - 1);
    expect(clampReason(previous + "😀", previous)).toBe(previous + "😀");
    expect(clampReason(previous + "😀😀", previous)).toBe(previous + "😀");
  });
});
