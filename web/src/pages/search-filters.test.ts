// 找論文的網址參數與年份檢查（Story 3.14）：網址只留看得懂的值、條件轉成 API 參數、自訂年份的四種錯誤。
import { describe, expect, it } from "vitest";

import {
  AIS_CORE_JOURNALS,
  TRACKING_START_YEAR,
  anyMatch,
  appliedFilterCount,
  checkYears,
  clearConditions,
  currentTaipeiYear,
  hasAllMatch,
  isStart,
  knownOnly,
  tidyMatch,
  toApiParams,
  validateSearchParams,
  yearPreset,
} from "./search-filters";

describe("search params from the URL", () => {
  it("keeps only values it understands", () => {
    expect(
      validateSearchParams({
        scope: "everyone",
        journal: "misq,BAD KEY,misq,isr",
        topic: "<script>",
        from: "2020",
        to: 2022,
        this_week: "yes",
        shown: 9999,
        extra: "x",
      }, 2026),
    ).toEqual({ journal: "misq,isr", from: 2020, to: 2022, shown: 9999 });
  });

  it("drops a year range that is incomplete or reversed", () => {
    expect(validateSearchParams({ from: 2020 }, 2026)).toEqual({});
    expect(validateSearchParams({ from: 2023, to: 2020 }, 2026)).toEqual({});
  });

  it("drops a year range outside the tracking start year and this year", () => {
    // 後端對這些年份一定回 422（ADR-0050 第 10 點）：整組不套用，不送出去。
    expect(validateSearchParams({ from: 2010, to: 2020 }, 2026)).toEqual({});
    expect(validateSearchParams({ from: 2016, to: 2070 }, 2026)).toEqual({});
    expect(validateSearchParams({ from: 1, to: 9999 }, 2026)).toEqual({});
    expect(validateSearchParams({ from: 2016, to: 2027 }, 2026)).toEqual({});
    expect(validateSearchParams({ from: 2016, to: 2026 }, 2026)).toEqual({ from: 2016, to: 2026 });
    expect(validateSearchParams({ from: 2016, to: 2027 }, 2027)).toEqual({ from: 2016, to: 2027 });
  });

  it("keeps method keys brought in from the paper page", () => {
    expect(validateSearchParams({ method: "experiment,BAD KEY" }, 2026)).toEqual({ method: "experiment" });
  });

  it("keeps lab and personal scopes and the this-week condition", () => {
    expect(validateSearchParams({ scope: "lab", this_week: true })).toEqual({ scope: "lab", this_week: true });
    expect(validateSearchParams({ scope: "tracked" })).toEqual({});
  });

  it("caps each list at 50 keys", () => {
    const keys = Array.from({ length: 60 }, (_, index) => `k${index}`).join(",");
    expect(validateSearchParams({ journal: keys }).journal!.split(",")).toHaveLength(50);
  });
});

describe("search conditions", () => {
  it("is the start state only with tracked journals and no conditions", () => {
    expect(isStart({})).toBe(true);
    expect(isStart({ shown: 40 })).toBe(true);
    expect(isStart({ scope: "lab" })).toBe(false);
    expect(isStart({ this_week: true })).toBe(false);
    expect(isStart({ topic: "erp" })).toBe(false);
  });

  it("counts the filter menus that have a selection", () => {
    expect(appliedFilterCount({ journal: "misq", from: 2020, to: 2022, topic: "erp", this_week: true })).toBe(3);
    expect(appliedFilterCount({ scope: "personal" })).toBe(0);
  });

  it("clearing keeps the scope only", () => {
    expect(clearConditions({ scope: "lab", journal: "misq", from: 2020, to: 2021, this_week: true, shown: 40 })).toEqual({
      scope: "lab",
    });
  });

  it("turns conditions into API parameters, years expanded", () => {
    expect(toApiParams({ scope: "lab", journal: "misq,isr", from: 2021, to: 2023, topic: "erp,unclassified", this_week: true, shown: 40 })).toEqual({
      scope: "lab",
      journal: ["misq", "isr"],
      year: [2021, 2022, 2023],
      topic: ["erp", "unclassified"],
      this_week: true,
    });
    expect(toApiParams({})).toEqual({});
  });

  it("the API parameters ignore how many rows are shown", () => {
    // 原本測 filterKey（只有測試在用，審查 H-L1 刪掉）；同一個性質由 toApiParams 保證。
    expect(toApiParams({ topic: "erp", shown: 60 })).toEqual({ topic: ["erp"] });
  });

  it("a method is a condition sent to the API and cleared with the rest", () => {
    expect(isStart({ method: "experiment" })).toBe(false);
    expect(appliedFilterCount({ method: "experiment" })).toBe(0);
    expect(toApiParams({ method: "experiment,survey" })).toEqual({ method: ["experiment", "survey"] });
    expect(clearConditions({ method: "experiment", scope: "personal" })).toEqual({ scope: "personal" });
  });

  it("drops keys the display names do not know before sending", () => {
    const names = {
      journals: [{ key: "misq", name: "MIS Quarterly" }],
      topics: [{ key: "erp", name: "ERP" }],
      methods: [{ key: "experiment", name: "Experiment" }],
    };
    expect(
      knownOnly({ scope: "lab", journal: "misq,old_journal", topic: "erp,unclassified,not_a_topic", method: "gone", from: 2020, to: 2021 }, names),
    ).toEqual({ scope: "lab", journal: "misq", topic: "erp,unclassified", method: undefined, from: 2020, to: 2021 });
    expect(knownOnly({ topic: "not_a_topic" }, names)).toEqual({ journal: undefined, topic: undefined, method: undefined });
  });
});

describe("topic and method match mode (Story 3.20)", () => {
  it("reads topic_match / method_match from the address only as all, with two keys or more", () => {
    expect(validateSearchParams({ topic: "erp,supply_chain", topic_match: "all" }, 2026)).toEqual({ topic: "erp,supply_chain", topic_match: "all" });
    expect(validateSearchParams({ method: "experiment,survey_sem", method_match: "all" }, 2026)).toEqual({
      method: "experiment,survey_sem",
      method_match: "all",
    });
    // 一個 key、沒有 key、不認得的值：不留（網址上不殘留）。
    expect(validateSearchParams({ topic: "erp", topic_match: "all" }, 2026)).toEqual({ topic: "erp" });
    expect(validateSearchParams({ topic_match: "all", method_match: "all" }, 2026)).toEqual({});
    expect(validateSearchParams({ topic: "erp,supply_chain", topic_match: "any" }, 2026)).toEqual({ topic: "erp,supply_chain" });
    expect(validateSearchParams({ topic: "erp,supply_chain", topic_match: "both" }, 2026)).toEqual({ topic: "erp,supply_chain" });
    // 重複的 key 去掉之後只剩一個：也不留。
    expect(validateSearchParams({ topic: "erp,erp", topic_match: "all" }, 2026)).toEqual({ topic: "erp" });
  });

  it("drops the setting for a field with fewer than two keys, keeps the other field's", () => {
    expect(tidyMatch({ topic: "erp", topic_match: "all", method: "a,b", method_match: "all" })).toEqual({ topic: "erp", method: "a,b", method_match: "all" });
    expect(tidyMatch({ topic: undefined, topic_match: "all" })).toEqual({});
    expect(hasAllMatch({ topic: "erp,supply_chain", topic_match: "all" })).toBe(true);
    expect(hasAllMatch({ topic: "erp", topic_match: "all" })).toBe(false);
    expect(hasAllMatch({ topic: "erp,supply_chain" })).toBe(false);
  });

  it("sends *_match=all to the API only for all; any (the default) is not sent", () => {
    expect(toApiParams({ topic: "erp,supply_chain", topic_match: "all" })).toEqual({ topic: ["erp", "supply_chain"], topic_match: "all" });
    expect(toApiParams({ topic: "erp,supply_chain" })).toEqual({ topic: ["erp", "supply_chain"] });
    expect(toApiParams({ topic: "erp", topic_match: "all" })).toEqual({ topic: ["erp"] });
    // 找論文沒有方法選單，但從網址帶進來的方法與 method_match=all 照樣送出去。
    expect(toApiParams({ method: "experiment,survey_sem", method_match: "all" })).toEqual({
      method: ["experiment", "survey_sem"],
      method_match: "all",
    });
  });

  it("back to any keeps every other condition", () => {
    const params = { scope: "lab" as const, journal: "misq", topic: "erp,supply_chain", topic_match: "all" as const, method: "a,b", method_match: "all" as const };
    expect(anyMatch(params)).toEqual({ scope: "lab", journal: "misq", topic: "erp,supply_chain", method: "a,b" });
  });

  it("unknown keys leaving fewer than two drop the setting before sending", () => {
    const names = { journals: [], topics: [{ key: "erp", name: "ERP" }], methods: [{ key: "experiment", name: "Experiment" }] };
    expect(knownOnly({ topic: "erp,not_a_topic", topic_match: "all" }, names).topic_match).toBeUndefined();
    expect(knownOnly({ topic: "erp,unclassified", topic_match: "all" }, names).topic_match).toBe("all");
    expect(knownOnly({ method: "experiment,gone", method_match: "all" }, names).method_match).toBeUndefined();
  });

  it("clearing the filters drops the setting", () => {
    expect(clearConditions({ topic: "erp,supply_chain", topic_match: "all", scope: "lab" })).toEqual({ scope: "lab" });
  });
});

describe("years", () => {
  it("starts tracking in 2016", () => {
    expect(TRACKING_START_YEAR).toBe(2016);
  });

  it("recognises the quick options", () => {
    expect(yearPreset({}, 2026)).toBe("none");
    expect(yearPreset({ from: 2026, to: 2026 }, 2026)).toBe("thisYear");
    expect(yearPreset({ from: 2024, to: 2026 }, 2026)).toBe("last3");
    expect(yearPreset({ from: 2022, to: 2026 }, 2026)).toBe("last5");
    expect(yearPreset({ from: 2019, to: 2021 }, 2026)).toBe("custom");
  });

  it("reports each custom year problem", () => {
    expect(checkYears("", "2020", 2026)).toEqual({ ok: false, problem: "missing" });
    expect(checkYears("20x0", "2020", 2026)).toEqual({ ok: false, problem: "missing" });
    expect(checkYears("2015", "2020", 2026)).toEqual({ ok: false, problem: "tooEarly" });
    expect(checkYears("2018", "2027", 2026)).toEqual({ ok: false, problem: "tooLate" });
    expect(checkYears("2022", "2019", 2026)).toEqual({ ok: false, problem: "reversed" });
    expect(checkYears(" 2016 ", "2026", 2026)).toEqual({ ok: true, from: 2016, to: 2026 });
  });

  it("the current year is the Taipei calendar year", () => {
    // UTC 12 月 31 日 17:00 已經是台北的隔年 1 月 1 日。
    expect(currentTaipeiYear(new Date("2026-12-31T17:00:00Z"))).toBe(2027);
    expect(currentTaipeiYear(new Date("2026-12-31T15:00:00Z"))).toBe(2026);
  });
});

describe("AIS core journals", () => {
  it("are the 11 keys Kevin confirmed, in config order", () => {
    expect(AIS_CORE_JOURNALS).toEqual(["misq", "isr", "jmis", "jais", "ejis", "isj", "jit", "jsis", "dss", "im", "io"]);
  });
});
