// 找論文的網址參數與年份檢查（Story 3.14）：網址只留看得懂的值、條件轉成 API 參數、自訂年份的四種錯誤。
import { describe, expect, it } from "vitest";

import {
  AIS_CORE_JOURNALS,
  TRACKING_START_YEAR,
  appliedFilterCount,
  checkYears,
  clearConditions,
  currentTaipeiYear,
  filterKey,
  isStart,
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
      }),
    ).toEqual({ journal: "misq,isr", from: 2020, to: 2022, shown: 200 });
  });

  it("drops a year range that is incomplete or reversed", () => {
    expect(validateSearchParams({ from: 2020 })).toEqual({});
    expect(validateSearchParams({ from: 2023, to: 2020 })).toEqual({});
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

  it("the filter key ignores how many rows are shown", () => {
    expect(filterKey({ topic: "erp", shown: 60 })).toEqual({ topic: "erp" });
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
