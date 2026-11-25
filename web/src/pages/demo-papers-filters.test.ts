// 示範站論文清單的篩選（Story 3.14 示範站）：網址參數的檢查、條件怎麼合（AD-19／ADR-0050 第 2 點：
// 不同欄位「且」、期刊與年份多值「或」、主題與方法多值「且」）、排序、清除篩選與手機的「篩選 · N」。
import { describe, expect, it } from "vitest";

import type { DemoPaperRow } from "../datasource/types";
import {
  clearDemoConditions,
  demoFilterCount,
  filterDemoRows,
  hasDemoConditions,
  isAllPapers,
  knownDemoOnly,
  MAX_TITLE_QUERY,
  validateDemoPapersParams,
} from "./demo-papers-filters";

function row(id: string, overrides: Partial<DemoPaperRow> = {}): DemoPaperRow {
  return {
    id,
    title: `Fake paper ${id}`,
    doi: null,
    doiUrl: null,
    journal: "misq",
    year: 2024,
    classificationState: "ready",
    classificationProvenance: null,
    topics: [],
    methods: [],
    hasAbstract: true,
    hasFullContent: false,
    isNewThisWeek: false,
    ...overrides,
  };
}

const ids = (rows: DemoPaperRow[]) => rows.map((item) => item.id);

describe("demo paper list address", () => {
  it("keeps only values it understands", () => {
    expect(
      validateDemoPapersParams(
        { journal: "misq,isr", from: "2022", to: "2024", topic: "erp", method: "experiment", abstract: "true", full: true, this_week: "true", title: " chain ", shown: 100 },
        2026,
      ),
    ).toEqual({ journal: "misq,isr", from: 2022, to: 2024, topic: "erp", method: "experiment", abstract: true, full: true, this_week: true, title: " chain ", shown: 100 });
    expect(validateDemoPapersParams({ abstract: "yes", full: "1", title: "   ", scope: "lab" }, 2026)).toEqual({
      journal: undefined,
      from: undefined,
      to: undefined,
      topic: undefined,
      method: undefined,
      abstract: undefined,
      full: undefined,
      this_week: undefined,
      title: undefined,
      shown: undefined,
    });
  });

  it("cuts a very long title query", () => {
    expect(validateDemoPapersParams({ title: "a".repeat(500) }, 2026).title).toHaveLength(MAX_TITLE_QUERY);
  });

  it("drops journal, topic and method keys the taxonomy does not have", () => {
    const names = { journals: [{ key: "misq", name: "MIS Quarterly" }], topics: [{ key: "erp", name: "ERP" }], methods: [{ key: "experiment", name: "Experiment" }] };
    expect(knownDemoOnly({ journal: "misq,old", topic: "erp,unclassified,gone", method: "experiment,nope", title: "x" }, names)).toMatchObject({
      journal: "misq",
      topic: "erp,unclassified",
      method: "experiment",
      title: "x",
    });
  });
});

describe("demo paper list filtering", () => {
  const rows = [
    row("a", { journal: "misq", year: 2022, topics: ["erp", "e_commerce"], methods: ["experiment"], hasFullContent: true }),
    row("b", { journal: "isr", year: 2025, topics: ["erp"], methods: ["experiment", "survey_sem"], hasAbstract: false }),
    row("c", { journal: "io", year: 2024, topics: ["unclassified"], methods: null, isNewThisWeek: true, title: "Blockchain Trust" }),
    row("d", { journal: null, year: null, topics: null, methods: null, classificationState: null }),
    row("e", { journal: "misq", year: 2025, topics: ["e_commerce"], methods: ["survey_sem"], title: "BLOCKCHAIN and Platforms" }),
  ];

  it("sorts like the member list: year newest first, then id descending, papers without a year last", () => {
    expect(ids(filterDemoRows(rows, {}))).toEqual(["e", "b", "c", "a", "d"]);
    // 不看匯出的順序：倒過來給，結果一樣。
    expect(ids(filterDemoRows([...rows].reverse(), {}))).toEqual(["e", "b", "c", "a", "d"]);
  });

  it("ORs journals and years, ANDs topics and methods, ANDs different fields", () => {
    expect(ids(filterDemoRows(rows, { journal: "misq,io" }))).toEqual(["e", "c", "a"]);
    expect(ids(filterDemoRows(rows, { from: 2024, to: 2025 }))).toEqual(["e", "b", "c"]);
    expect(ids(filterDemoRows(rows, { topic: "erp" }))).toEqual(["b", "a"]);
    expect(ids(filterDemoRows(rows, { topic: "erp,e_commerce" }))).toEqual(["a"]);
    expect(ids(filterDemoRows(rows, { method: "experiment,survey_sem" }))).toEqual(["b"]);
    expect(ids(filterDemoRows(rows, { topic: "unclassified" }))).toEqual(["c"]);
    expect(ids(filterDemoRows(rows, { journal: "misq", topic: "e_commerce", from: 2025, to: 2025 }))).toEqual(["e"]);
  });

  it("applies the two 'only' switches, this week, and the title words", () => {
    expect(ids(filterDemoRows(rows, { abstract: true }))).toEqual(["e", "c", "a", "d"]);
    expect(ids(filterDemoRows(rows, { full: true }))).toEqual(["a"]);
    expect(ids(filterDemoRows(rows, { this_week: true }))).toEqual(["c"]);
    expect(ids(filterDemoRows(rows, { title: "blockchain" }))).toEqual(["e", "c"]);
    expect(ids(filterDemoRows(rows, { title: "blockchain", journal: "misq" }))).toEqual(["e"]);
  });

  it("returns nothing when no paper has every condition", () => {
    expect(filterDemoRows(rows, { journal: "isr", full: true })).toEqual([]);
  });
});

describe("demo paper list conditions", () => {
  it("counts what the mobile filter sheet holds (not this week, not the title words)", () => {
    expect(demoFilterCount({ journal: "misq", from: 2020, to: 2026, topic: "erp", abstract: true, this_week: true, title: "x" })).toBe(4);
    expect(demoFilterCount({ this_week: true })).toBe(0);
  });

  it("clears every condition except the title words", () => {
    const params = { journal: "misq", method: "experiment", full: true as const, this_week: true as const, title: "chain", shown: 100 };
    expect(hasDemoConditions(params)).toBe(true);
    expect(clearDemoConditions(params)).toEqual({ title: "chain" });
    expect(hasDemoConditions({ title: "chain" })).toBe(false);
    expect(isAllPapers({ title: "chain" })).toBe(false);
    expect(isAllPapers({ shown: 100 })).toBe(true);
  });
});
