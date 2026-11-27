// 「任一個／同時符合」畫面上的字（Story 3.20，TASK PR B 的「標題的字」；審查 B 的 F4）：
// 示範站標題多個主題、方法用「 或 」「 ＋ 」接、期刊維持頓號；篩選標籤「N 個」與「同時符合 N 個」（桌機標籤與手機那一列共用）。
// 用 react-dom/server 靜態渲染（同 PaperPage.test.tsx）；字一律從預期檔拿，不寫死。
import i18n from "../i18n";

import type { TFunction } from "i18next";
import { Fragment } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { methodValue, topicValue, type FilterState } from "../components/search/filters";
import type { DisplayNames } from "../generated/model";
import expected from "../i18n/expected/notice.zh-TW.json";
import { headingParts } from "./DemoPapersPage";

const TEXT: Record<string, string> = {
  ...expected.strings,
  ...Object.fromEntries(Object.entries(expected.otherSources).map(([key, entry]) => [key, entry.text])),
};
const text = (key: string): string => {
  const value = TEXT[key];
  if (value === undefined) throw new Error(`no expected text ${key}`);
  return value;
};
const fill = (template: string, values: Record<string, string | number>) =>
  template.replace(/\{\{(\w+)\}\}/g, (_, name: string) => String(values[name]));

const NAMES: DisplayNames = {
  journals: [
    { key: "misq", name: "MIS Quarterly" },
    { key: "isr", name: "Information Systems Research" },
  ],
  topics: [
    { key: "erp", name: "ERP" },
    { key: "supply_chain", name: "Supply chain" },
  ],
  methods: [
    { key: "experiment", name: "Experiment" },
    { key: "survey_sem", name: "Survey SEM" },
  ],
} as DisplayNames;
const UNCLASSIFIED = text("paper:paper.classification.unclassified");
const demoT = i18n.getFixedT("zh-TW", "demo") as TFunction<"demo">;
const searchT = i18n.getFixedT("zh-TW", "search") as TFunction<"search">;

/** 標題各段（不含篇數）渲染出來的字。 */
function headingText(params: Parameters<typeof headingParts>[0]): string[] {
  return headingParts(params, NAMES, { t: demoT, thisWeek: text("search:search.condition.thisWeek"), unclassified: UNCLASSIFIED }).map((node) =>
    renderToStaticMarkup(<Fragment>{node}</Fragment>).replace(/<[^>]+>/g, ""),
  );
}

describe("demo heading words for any and all (Story 3.20)", () => {
  it("joins topics with the any word by default and the all word for topic_match=all", () => {
    const any = text("demo:demo.papers.anyJoin");
    const all = text("demo:demo.papers.allJoin");
    expect(headingText({ topic: "erp,supply_chain" })).toEqual([`ERP${any}Supply chain`]);
    expect(headingText({ topic: "erp,supply_chain", topic_match: "all" })).toEqual([`ERP${all}Supply chain`]);
    // 一個就只有名稱，沒有接的字。
    expect(headingText({ topic: "erp" })).toEqual(["ERP"]);
  });

  it("methods have their own word; journals keep the list separator", () => {
    const any = text("demo:demo.papers.anyJoin");
    const all = text("demo:demo.papers.allJoin");
    expect(headingText({ journal: "misq,isr", topic: "erp,supply_chain", method: "experiment,survey_sem", method_match: "all" })).toEqual([
      `MIS Quarterly${text("demo:demo.papers.separator")}Information Systems Research`,
      `ERP${any}Supply chain`,
      `Experiment${all}Survey SEM`,
    ]);
  });

  it("the join word is the secondary text colour (design .cj)", () => {
    const html = renderToStaticMarkup(<Fragment>{headingParts({ topic: "erp,supply_chain" }, NAMES, { t: demoT, thisWeek: "", unclassified: UNCLASSIFIED })}</Fragment>);
    expect(html).toContain(`<span class="whitespace-normal text-text-secondary">${text("demo:demo.papers.anyJoin")}</span>`);
  });
});

describe("chip words for any and all (Story 3.20)", () => {
  const state = (params: FilterState["params"]): FilterState => ({ params, names: NAMES, currentYear: 2026, update: () => {} });

  it("topics: a name for one, N for any, all N for all", () => {
    expect(topicValue(state({ topic: "erp" }), searchT, UNCLASSIFIED)).toBe("ERP");
    expect(topicValue(state({ topic: "erp", topic_match: "all" }), searchT, UNCLASSIFIED)).toBe("ERP");
    expect(topicValue(state({ topic: "erp,supply_chain" }), searchT, UNCLASSIFIED)).toBe(fill(text("search:search.filter.topicCount"), { count: 2 }));
    expect(topicValue(state({ topic: "erp,supply_chain", topic_match: "all" }), searchT, UNCLASSIFIED)).toBe(
      fill(text("search:search.filter.matchAllCount"), { count: 2 }),
    );
  });

  it("methods use their own setting, not the topic one", () => {
    expect(methodValue(state({ method: "experiment,survey_sem", topic_match: "all" }), searchT)).toBe(fill(text("search:search.filter.topicCount"), { count: 2 }));
    expect(methodValue(state({ method: "experiment,survey_sem", method_match: "all" }), searchT)).toBe(
      fill(text("search:search.filter.matchAllCount"), { count: 2 }),
    );
  });
});
