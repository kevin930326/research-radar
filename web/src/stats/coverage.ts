// 摘要覆蓋率（AD-19：比例只在 web/src/stats/ 算；ADR-0011；EXPERIENCE.md 畫面 9、State Patterns「9. 資料涵蓋範圍」）。
// 吃計數方塊的列（只用 total 與 with_abstract）、規則的設定值（params）與快照的截止時間，回每本期刊每一年的格子。
// 回傳的比例不四捨五入（顯示位數由 format.ts 決定）；算不出來的是 null 加原因代碼，不回 0。
// 驗收情境在 tests/golden/stats/coverage-*.json（coverage.test.ts 讀）。

import type { CubeRow, TrendParams } from "../generated/model";

/** 算不出來的原因（ADR-0011 規則 8 的代碼）：這一年這本期刊沒有論文。 */
export type CoverageReason = "empty_year";

/** 一格：這本期刊這一年。`ratio` 是有摘要的比例（0 到 1，不四捨五入）；`total` 是 0 時是 null、`reason` 是 empty_year。 */
export interface CoverageCell {
  year: number;
  total: number;
  withAbstract: number;
  ratio: number | null;
  reason: CoverageReason | null;
  /** 比例低於 `params.low_coverage_threshold`；算不出來的格子不套（ADR-0011 情境 13）。 */
  low: boolean;
}

export interface CoverageRow {
  journal: string;
  cells: CoverageCell[];
}

export interface CoverageTable {
  /** 表格的年份欄：追蹤起始年到當年度（資料截止日在 params.timezone 的那一年），遞增。比當年度晚的出版年不列欄。 */
  years: number[];
  /** 當年度（最後一欄，欄名加註「未滿一年」）。 */
  currentYear: number;
  /** 照 `journals` 的順序，每本一列。 */
  rows: CoverageRow[];
  /** 全部列加總（含比當年度晚、或不在 `journals` 裡的期刊；收錄的篇數照實算）。 */
  total: number;
  withAbstract: number;
  /** 全部的有摘要比例；一篇都沒有時是 null。 */
  ratio: number | null;
}

/** 截止時間（ISO 8601、UTC）在 `timezone` 的年份。 */
export function yearIn(asOf: string, timezone: string): number {
  const instant = new Date(asOf);
  if (Number.isNaN(instant.getTime())) throw new RangeError(`bad as_of: ${asOf}`);
  const year = new Intl.DateTimeFormat("en-US", { timeZone: timezone, year: "numeric" }).format(instant);
  return Number(year);
}

function ratioOf(withAbstract: number, total: number): number | null {
  return total === 0 ? null : withAbstract / total;
}

/**
 * 覆蓋率表。`journals` 是表格要列的期刊代號（照設定的順序）；`asOf` 是快照的截止時間。
 * 同一個（期刊, 年份）的列加總（計數方塊不會重複，保險起見）。
 * 計數壞掉（篇數是負的、或某一格有摘要的比總數多）時丟 RangeError，不回大於 1 的比例（畫面當成「沒有載入成功」）。
 */
export function coverageTable(rows: readonly CubeRow[], params: TrendParams, journals: readonly string[], asOf: string): CoverageTable {
  const currentYear = yearIn(asOf, params.timezone);
  const years: number[] = [];
  for (let year = params.tracking_start_year; year <= currentYear; year += 1) years.push(year);
  const counts = new Map<string, { total: number; withAbstract: number }>();
  let total = 0;
  let withAbstract = 0;
  for (const row of rows) {
    if (row.dim !== "total" && row.dim !== "with_abstract") continue;
    const key = `${row.journal}\u0000${row.year}`;
    const cell = counts.get(key) ?? { total: 0, withAbstract: 0 };
    if (row.dim === "total") {
      cell.total += row.n;
      total += row.n;
    } else {
      cell.withAbstract += row.n;
      withAbstract += row.n;
    }
    counts.set(key, cell);
  }
  for (const [key, cell] of counts) {
    if (cell.total < 0 || cell.withAbstract < 0 || cell.withAbstract > cell.total) {
      throw new RangeError(`bad counts for ${key.replace("\u0000", " ")}: ${cell.withAbstract}/${cell.total}`);
    }
  }
  return {
    years,
    currentYear,
    rows: journals.map((journal) => ({
      journal,
      cells: years.map((year) => {
        const found = counts.get(`${journal}\u0000${year}`) ?? { total: 0, withAbstract: 0 };
        const ratio = ratioOf(found.withAbstract, found.total);
        return {
          year,
          total: found.total,
          withAbstract: found.withAbstract,
          ratio,
          reason: ratio === null ? "empty_year" : null,
          low: ratio !== null && ratio < params.low_coverage_threshold,
        };
      }),
    })),
    total,
    withAbstract,
    ratio: ratioOf(withAbstract, total),
  };
}
