// 摘要覆蓋率的驗收情境（tests/golden/stats/coverage-*.json，預期值手算寫死）。
import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import type { CubeRow, TrendParams } from "../generated/model";
import { coverageTable, type CoverageTable } from "./coverage";

const GOLDEN = fileURLToPath(new URL("../../../tests/golden/stats/", import.meta.url));

interface Scenario {
  description: string;
  params: TrendParams;
  journals: string[];
  rows: CubeRow[];
  cases: { name: string; as_of: string; expected: CoverageTable }[];
}

const files = readdirSync(GOLDEN).filter((name) => name.startsWith("coverage-") && name.endsWith(".json"));

describe("coverage table (tests/golden/stats)", () => {
  it("has scenarios to run", () => {
    expect(files.length).toBeGreaterThanOrEqual(4);
  });

  for (const file of files) {
    const scenario = JSON.parse(readFileSync(`${GOLDEN}${file}`, "utf-8")) as Scenario;
    for (const item of scenario.cases) {
      it(`${file}: ${item.name}`, () => {
        expect(coverageTable(scenario.rows, scenario.params, scenario.journals, item.as_of)).toEqual(item.expected);
      });
    }
  }

  it("never rounds the ratio it returns", () => {
    const params: TrendParams = {
      timezone: "Asia/Taipei",
      tracking_start_year: 2025,
      current_year_cutoff: "07-01",
      growth_window_years: 3,
      baseline_years: 3,
      low_coverage_threshold: 0.5,
    };
    const rows: CubeRow[] = [
      { journal: "misq", year: 2025, dim: "total", key_a: null, key_b: null, n: 3 },
      { journal: "misq", year: 2025, dim: "with_abstract", key_a: null, key_b: null, n: 2 },
    ];
    const table = coverageTable(rows, params, ["misq"], "2025-09-01T00:00:00Z");
    expect(table.rows[0]?.cells[0]?.ratio).toBe(2 / 3);
    expect(table.ratio).toBe(2 / 3);
  });
});
