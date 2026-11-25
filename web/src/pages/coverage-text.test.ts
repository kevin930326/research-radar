// 資料涵蓋範圍的說明與名詞說明照設計稿把「14 本」「2016 年」寫在 i18n（Story 3.15）：
// 這裡確認那幾句的數字和設定檔 config/journals.yaml（追蹤幾本、從哪一年起）一致，改了設定沒改字就失敗。
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import coverage from "../i18n/zh-TW/coverage.json";

const JOURNALS = readFileSync(fileURLToPath(new URL("../../../config/journals.yaml", import.meta.url)), "utf-8");
const count = JOURNALS.split("\n").filter((line) => /^\s*- key:/.test(line)).length;
const startYear = Number(/^start_year:\s*(\d+)/m.exec(JOURNALS)?.[1]);

describe("coverage text matches config/journals.yaml", () => {
  it("reads the config", () => {
    expect(count).toBeGreaterThan(0);
    expect(startYear).toBeGreaterThan(1900);
  });

  it.each([coverage.coverage.intro.text, coverage.coverage.terms.scopeText, coverage.coverage.terms.outsideText])("%s", (text) => {
    // 句子裡的數字只有這兩個：追蹤幾本、從哪一年起。
    expect(text.match(/\d+/g)).toEqual([String(count), String(startYear)]);
  });
});
