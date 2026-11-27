// 示範站論文清單的篩選（Story 3.14 示範站）：網址參數的檢查、條件怎麼合（AD-19／ADR-0050 第 2 點：
// 不同欄位「且」、期刊與年份多值「或」；主題與方法多值預設「或」、*_match=all 時「且」，Story 3.20）、排序、
// 清除篩選與手機的「篩選 · N」。
import { describe, expect, it } from "vitest";

import type { DemoPaperRow } from "../datasource/types";
import {
  anyMatchCount,
  clearDemoConditions,
  demoFilterCount,
  filterDemoRows,
  hasDemoConditions,
  isAllPapers,
  fromSearchParams,
  knownDemoOnly,
  MAX_TITLE_QUERY,
  toSearchParams,
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

  it("compares ids without case, like Postgres uuid order", () => {
    // 讀檔時 UUID 不分大小寫；大寫的 id 不能因為字碼比較排到小寫前面。
    const mixed = [row("0000000a-0000-4000-8000-000000000000"), row("0000000B-0000-4000-8000-000000000000")];
    expect(ids(filterDemoRows(mixed, {}))).toEqual(["0000000B-0000-4000-8000-000000000000", "0000000a-0000-4000-8000-000000000000"]);
    expect(ids(filterDemoRows([...mixed].reverse(), {}))).toEqual(["0000000B-0000-4000-8000-000000000000", "0000000a-0000-4000-8000-000000000000"]);
  });

  it("ORs journals and years, ANDs topics and methods when *_match=all, ANDs different fields", () => {
    expect(ids(filterDemoRows(rows, { journal: "misq,io" }))).toEqual(["e", "c", "a"]);
    expect(ids(filterDemoRows(rows, { from: 2024, to: 2025 }))).toEqual(["e", "b", "c"]);
    expect(ids(filterDemoRows(rows, { topic: "erp" }))).toEqual(["b", "a"]);
    // Story 3.20：原本沒帶 *_match 就是「且」；預設改成「或」之後，這兩條明確帶 all（保住原本在測的「且」）。
    expect(ids(filterDemoRows(rows, { topic: "erp,e_commerce", topic_match: "all" }))).toEqual(["a"]);
    expect(ids(filterDemoRows(rows, { method: "experiment,survey_sem", method_match: "all" }))).toEqual(["b"]);
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

  it("ORs topics and methods by default (Story 3.20)", () => {
    expect(ids(filterDemoRows(rows, { topic: "erp,e_commerce" }))).toEqual(["e", "b", "a"]);
    expect(ids(filterDemoRows(rows, { method: "experiment,survey_sem" }))).toEqual(["e", "b", "a"]);
  });

  it("one key gives the same rows either way", () => {
    expect(ids(filterDemoRows(rows, { topic: "erp", topic_match: "all" }))).toEqual(ids(filterDemoRows(rows, { topic: "erp" })));
    expect(ids(filterDemoRows(rows, { method: "experiment", method_match: "all" }))).toEqual(ids(filterDemoRows(rows, { method: "experiment" })));
  });

  it("topic and method each have their own setting; different fields are still ANDed", () => {
    // 主題「或」（erp 或 e_commerce：a、b、e）、方法「且」（experiment 且 survey_sem：b）→ b。
    expect(ids(filterDemoRows(rows, { topic: "erp,e_commerce", method: "experiment,survey_sem", method_match: "all" }))).toEqual(["b"]);
    // 主題「且」（a）、方法「或」（a、b、e）→ a。
    expect(ids(filterDemoRows(rows, { topic: "erp,e_commerce", topic_match: "all", method: "experiment,survey_sem" }))).toEqual(["a"]);
    // 期刊、年份照舊：主題「或」再接期刊 misq → a、e。
    expect(ids(filterDemoRows(rows, { topic: "erp,e_commerce", journal: "misq" }))).toEqual(["e", "a"]);
  });

  it("unclassified with another topic: any lists both, all is zero (Kevin 2026-11-27)", () => {
    expect(ids(filterDemoRows(rows, { topic: "erp,unclassified" }))).toEqual(["b", "c", "a"]);
    expect(filterDemoRows(rows, { topic: "erp,unclassified", topic_match: "all" })).toEqual([]);
  });

  it("counts the rows with topics and methods back to any, other conditions kept", () => {
    expect(anyMatchCount(rows, { topic: "erp,unclassified", topic_match: "all" })).toBe(3);
    expect(anyMatchCount(rows, { topic: "erp,unclassified", topic_match: "all", journal: "misq" })).toBe(1);
    expect(anyMatchCount(rows, { topic: "erp,e_commerce", topic_match: "all", method: "experiment,survey_sem", method_match: "all", full: true })).toBe(1);
    expect(anyMatchCount(rows, { journal: "pmj", topic: "erp,e_commerce", topic_match: "all" })).toBe(0);
  });

  it("returns nothing when no paper has every condition", () => {
    expect(filterDemoRows(rows, { journal: "isr", full: true })).toEqual([]);
  });
});

describe("demo paper list match mode in the address (Story 3.20)", () => {
  it("keeps topic_match / method_match only as all and only with two keys or more", () => {
    expect(validateDemoPapersParams({ topic: "erp,e_commerce", topic_match: "all", method: "experiment,survey_sem", method_match: "all" }, 2026)).toMatchObject({
      topic_match: "all",
      method_match: "all",
    });
    const single = validateDemoPapersParams({ topic: "erp", topic_match: "all", method: "experiment", method_match: "all" }, 2026);
    expect(single.topic_match).toBeUndefined();
    expect(single.method_match).toBeUndefined();
    expect(validateDemoPapersParams({ topic: "erp,e_commerce", topic_match: "any" }, 2026).topic_match).toBeUndefined();
    expect(validateDemoPapersParams({ topic: "erp,e_commerce", topic_match: "ALL" }, 2026).topic_match).toBeUndefined();
  });

  it("round-trips through the shared filter shape, dropping the setting below two keys", () => {
    const params = { topic: "erp,e_commerce", topic_match: "all" as const, full: true as const, title: "x" };
    expect(fromSearchParams(params, toSearchParams(params))).toEqual(params);
    const unticked = fromSearchParams(params, { ...toSearchParams(params), topic: "erp" });
    expect(unticked.topic_match).toBeUndefined();
    expect(unticked).toMatchObject({ topic: "erp", full: true, title: "x" });
  });

  it("drops the setting when unknown keys leave fewer than two", () => {
    const names = { journals: [], topics: [{ key: "erp", name: "ERP" }], methods: [] };
    expect(knownDemoOnly({ topic: "erp,gone", topic_match: "all" }, names).topic_match).toBeUndefined();
    expect(knownDemoOnly({ topic: "erp,unclassified", topic_match: "all" }, names).topic_match).toBe("all");
  });

  it("clearing every condition also drops the setting", () => {
    expect(clearDemoConditions({ topic: "erp,e_commerce", topic_match: "all", title: "x" })).toEqual({ title: "x" });
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
