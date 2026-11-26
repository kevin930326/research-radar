// 百分比的顯示位數（tests/golden/stats/format-percent.json）：分母 0、全部都有、全部都沒有、四捨五入的邊界。
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { formatPercent } from "./format";

const FILE = fileURLToPath(new URL("../../../tests/golden/stats/format-percent.json", import.meta.url));
const { cases } = JSON.parse(readFileSync(FILE, "utf-8")) as {
  cases: { name: string; kind: string; with_abstract: number; total: number; expected: string | null }[];
};

describe("formatPercent (tests/golden/stats/format-percent.json)", () => {
  it.each(cases)("$name", ({ with_abstract, total, expected }) => {
    expect(formatPercent(total === 0 ? null : with_abstract / total)).toBe(expected);
  });

  it("covers the four kinds the story asks for", () => {
    const kinds = new Set(cases.map((item) => item.kind));
    for (const kind of ["zero_denominator", "all", "none", "rounding"]) expect(kinds).toContain(kind);
  });

  it("refuses ratios outside 0 to 1", () => {
    expect(() => formatPercent(1.2)).toThrow(RangeError);
    expect(() => formatPercent(-0.1)).toThrow(RangeError);
    expect(() => formatPercent(Number.NaN)).toThrow(RangeError);
  });
});
