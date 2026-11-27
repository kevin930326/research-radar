// 資料涵蓋範圍的說明與名詞說明照設計稿把「14 本」「2016 年」寫在 i18n（Story 3.15）：
// 這裡確認那幾句的數字和設定檔 config/journals.yaml（追蹤幾本、從哪一年起）一致，改了設定沒改字就失敗。
// 表格的期刊縮寫是前端寫死的對照表（設計稿的 I&M、I&O）：設定檔的每個代號都要有，加了期刊沒加縮寫就失敗。
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import coverage from "../i18n/zh-TW/coverage.json";
import { JOURNAL_ABBREVIATIONS, journalAbbreviation } from "./CoveragePage";

const JOURNALS = readFileSync(fileURLToPath(new URL("../../../config/journals.yaml", import.meta.url)), "utf-8");
const keys = JOURNALS.split("\n").flatMap((line) => /^\s*- key:\s*(\S+)/.exec(line)?.[1] ?? []);
const count = keys.length;
const startYear = Number(/^start_year:\s*(\d+)/m.exec(JOURNALS)?.[1]);

describe("coverage text matches config/journals.yaml", () => {
  it("reads the config", () => {
    expect(count).toBeGreaterThan(0);
    expect(startYear).toBeGreaterThan(1900);
  });

  it.each([coverage.coverage.intro.text, coverage.coverage.terms.outsideText])("%s", (text) => {
    // 句子裡的數字只有這兩個：追蹤幾本、從哪一年起。
    expect(text.match(/\d+/g)).toEqual([String(count), String(startYear)]);
  });
});

describe("journal abbreviations", () => {
  it("has one for every journal in config/journals.yaml, and nothing else", () => {
    expect(keys.length).toBeGreaterThan(0);
    expect(Object.keys(JOURNAL_ABBREVIATIONS).sort()).toEqual([...keys].sort());
  });

  it("uses the design's I&M and I&O, matching the Elsevier note", () => {
    expect(journalAbbreviation("im")).toBe("I&M");
    expect(journalAbbreviation("io")).toBe("I&O");
    // Elsevier 那段的四本（以頓號分開）和表格用同一份縮寫。
    expect(coverage.coverage.elsevier.list.split(/\u3001/)).toEqual(["jsis", "dss", "im", "io"].map(journalAbbreviation));
  });
});
